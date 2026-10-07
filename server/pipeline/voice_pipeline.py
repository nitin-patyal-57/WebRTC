import asyncio
import re
import time
from typing import Optional

from audio.processor import AudioProcessor
from audio.vad import EnergyVAD, VADEvent
from config import settings
from services.answer_cache import answer_cache
from services.fixed_answers import fixed_answers
from services.knowledge import KBMatch, knowledge
from services.llm import GroqLLM
from services.stt import GroqSTT
from services.tts import create_tts
from utils.logger import get_logger
from webrtc.session import Session

log = get_logger("PIPELINE")

SENTENCE_RE = re.compile(r'[.!?]["\')\]]?(\s+|$)')
FORCE_SPLIT_CHARS = 80
MIN_SPLIT_CHARS = 20


def _now_ms() -> float:
    return time.time() * 1000.0


class VoicePipeline:
    """Per-session concurrent pipeline: audio -> STT -> LLM -> TTS -> WebRTC out."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.processor = AudioProcessor()
        self.vad = EnergyVAD(
            silence_ms=settings.vad_silence_ms,
            min_speech_ms=settings.vad_min_speech_ms,
            threshold=settings.vad_threshold,
        )
        self.stt: Optional[GroqSTT] = None
        self.llm: Optional[GroqLLM] = None
        self.tts = None
        self._tasks: list[asyncio.Task] = []
        self._user_speaking = False
        self._stopping = False
        self._generation = 0
        self._tts_buffer = ""
        self._llm_active = False
        self._last_audio_time = time.monotonic()

    def _ai_in_progress(self) -> bool:
        session = self.session
        track = session.ai_track
        return bool(
            self._llm_active
            or not session.llm_text_queue.empty()
            or not session.tts_audio_queue.empty()
            or (track is not None and (track.has_pending or session.ai_playing))
        )

    async def start(self) -> None:
        session = self.session
        session.pipeline = self
        self.stt = GroqSTT()
        try:
            await self.stt.start()
        except Exception as exc:
            log.error(f"[STT] Failed to start: {exc!r}")
            session.send_event({"type": "error", "message": f"STT unavailable: {exc}"})
            self.stt = None

        self.llm = GroqLLM()
        try:
            await self.llm.start()
        except Exception as exc:
            log.error(f"[LLM] Failed to start: {exc!r}")
            session.send_event({"type": "error", "message": f"LLM unavailable: {exc}"})
            self.llm = None

        self.tts = create_tts()
        try:
            await self.tts.start()
        except Exception as exc:
            log.error(f"[TTS] Failed to start: {exc!r}")
            session.send_event({"type": "error", "message": f"TTS unavailable: {exc}"})
            self.tts = None

        self._tasks = [
            asyncio.create_task(self._audio_loop(), name=f"audio:{session.session_id[:8]}"),
            asyncio.create_task(self._stt_event_loop(), name=f"stt-evt:{session.session_id[:8]}"),
            asyncio.create_task(self._llm_loop(), name=f"llm:{session.session_id[:8]}"),
            asyncio.create_task(self._tts_loop(), name=f"tts:{session.session_id[:8]}"),
            asyncio.create_task(self._stall_watchdog(), name=f"stall:{session.session_id[:8]}"),
        ]
        log.info(f"[PIPELINE] Started for session {session.session_id}")

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._generation += 1
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        if self.stt is not None:
            await self.stt.close()
            self.stt = None
        if self.llm is not None:
            await self.llm.close()
            self.llm = None
        if self.tts is not None:
            await self.tts.close()
            self.tts = None
        self.session.pipeline = None
        log.info(f"[PIPELINE] Stopped for session {self.session.session_id}")

    async def interrupt(self) -> None:
        """Barge-in: user started speaking while AI audio was pending."""
        session = self.session
        self._generation += 1
        cleared = 0
        for queue in (session.tts_audio_queue, session.llm_text_queue):
            while not queue.empty():
                try:
                    queue.get_nowait()
                    cleared += 1
                except asyncio.QueueEmpty:
                    break
        self._tts_buffer = ""
        self.vad.reset()
        session.ai_playing = False
        if session.ai_track is not None:
            session.ai_track.clear()
        session.send_event({"type": "interrupted"})
        log.info(f"[PIPELINE] Interrupted, cleared {cleared} pending items")

    async def _audio_loop(self) -> None:
        # Persistent: an error must never kill input processing (that would
        # stop all further replies). Only cancellation ends the loop.
        while True:
            try:
                await self._audio_loop_body()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"[PIPELINE] Audio loop error (continuing): {exc!r}")

    async def _audio_loop_body(self) -> None:
        session = self.session
        while True:
            frame = await session.audio_queue.get()
            self._last_audio_time = time.monotonic()
            for pcm in self.processor.process(frame):
                events = self.vad.process(pcm)
                for event in events:
                    await self._handle_vad(event)
                if self.stt is not None:
                    await self.stt.send_audio(pcm)
                    if settings.stt_partials:
                        await self.stt.maybe_partial()

    async def _stall_watchdog(self) -> None:
        """Force the VAD endpoint when audio stops arriving mid-speech.

        EnergyVAD only advances its silence timer as PCM chunks arrive; if the
        remote track stalls (screen off, RTP gap) SPEECH_STOPPED never fires and
        the pipeline hangs. Feed synthetic silence to complete the utterance.
        """
        try:
            while True:
                await asyncio.sleep(0.5)
                if not self.vad.is_speech_active:
                    continue
                stalled_ms = (time.monotonic() - self._last_audio_time) * 1000.0
                if stalled_ms < settings.vad_stall_ms:
                    continue
                silence_ms = max(settings.vad_silence_ms + 100, 800)
                pcm = b"\x00" * (16000 * 2 * silence_ms // 1000)
                log.warning(
                    f"[VAD] Audio stalled for {stalled_ms:.0f}ms during speech — forcing endpoint"
                )
                for event in self.vad.process(pcm):
                    await self._handle_vad(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"[PIPELINE] Stall watchdog error: {exc!r}")

    async def _handle_vad(self, event: VADEvent) -> None:
        session = self.session
        if event == VADEvent.SPEECH_STARTED:
            self._user_speaking = True
            session.latency = {}
            from dashboard_store import dashboard

            dashboard.turn_started(session.session_id)
            session.mark_stage("speech_start")
            if self.stt is not None:
                self.stt.mark_speech_start()
            session.send_event({"type": "vad", "event": "speech_started"})
            log.info("[STT] Speech started")
            if settings.barge_in and self._ai_in_progress():
                await self.interrupt()
        elif event == VADEvent.SPEECH_STOPPED:
            self._user_speaking = False
            session.mark_stage("speech_end")
            session.send_event({"type": "vad", "event": "speech_stopped"})
            log.info("[STT] Speech stopped, transcribing utterance")
            if self.stt is not None:
                await self.stt.finalize()
        elif event == VADEvent.SPEECH:
            session.touch()

    async def _stt_event_loop(self) -> None:
        session = self.session
        stt = self.stt
        if stt is None:
            return
        try:
            while True:
                evt = await stt.queue.get()
                etype = evt.get("type")

                if etype == "partial_transcript":
                    session.send_event({"type": "partial_transcript", "text": evt["text"]})
                    log.info(f"[STT] Partial: {evt['text']}")

                elif etype == "final_transcript":
                    text = evt.get("text", "").strip()
                    if not text:
                        continue
                    session.mark_stage("stt_final")
                    from dashboard_store import dashboard

                    dashboard.turn_content(session.session_id, question=text)
                    session.add_history(
                        "user", text, max_messages=settings.max_history_messages
                    )
                    session.send_event({"type": "final_transcript", "text": text})
                    log.info(f"[STT] Final transcript received: {text}")
                    await session.transcript_queue.put(text)
                    session.emit_latency()

                elif etype == "error":
                    session.send_event({"type": "error", "message": evt.get("message", "STT error")})

                elif etype in ("stt_speech_started", "stt_speech_stopped", "stt_connected", "stt_disconnected"):
                    log.info(f"[STT] Event: {etype}")

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"[PIPELINE] STT event loop error: {exc!r}")

    async def _llm_loop(self) -> None:
        session = self.session
        try:
            while True:
                user_text = await session.transcript_queue.get()
                if not user_text:
                    continue

                generation = self._generation
                self._llm_active = True
                session.mark_stage("llm_start")
                from dashboard_store import dashboard

                dashboard.turn_content(session.session_id, question=user_text)

                if settings.fixed_answers_enabled:
                    fixed = fixed_answers.match(user_text)
                    if fixed is not None:
                        log.info(f"[FIXED] Instant answer for: {user_text}")
                        try:
                            await self._answer_fixed(session, fixed, generation, dashboard)
                        except Exception as exc:
                            log.error(f"[FIXED] Failed to answer: {exc!r}")
                        finally:
                            self._llm_active = False
                            session.mark_stage("llm_end")
                            await session.llm_text_queue.put((generation, None))
                        continue

                remembered = answer_cache.match(user_text) if settings.answer_cache_enabled else None
                if remembered is not None:
                    log.info(f"[CACHE] Instant remembered answer for: {user_text}")
                    try:
                        await self._answer_fixed(session, remembered, generation, dashboard, intent="remembered_answer")
                    except Exception as exc:
                        log.error(f"[CACHE] Failed to answer: {exc!r}")
                    finally:
                        self._llm_active = False
                        session.mark_stage("llm_end")
                        await session.llm_text_queue.put((generation, None))
                    continue

                match = knowledge.match(user_text) if settings.kb_enabled else None
                if match is not None:
                    log.info(
                        f"[KB] Canned answer intent={match.intent} "
                        f"score={match.score:.2f} for: {user_text}"
                    )
                    try:
                        await self._answer_from_knowledge(session, match, generation, dashboard)
                        if settings.answer_cache_enabled:
                            answer_cache.store(user_text, match.response)
                    except Exception as exc:
                        log.error(f"[KB] Failed to answer from knowledge: {exc!r}")
                    finally:
                        self._llm_active = False
                        session.mark_stage("llm_end")
                        await session.llm_text_queue.put((generation, None))
                    continue

                if self.llm is None:
                    continue

                log.info(f"[LLM] Generation started for: {user_text}")

                parts: list[str] = []
                first_token = True
                completed = False
                try:
                    history = list(session.conversation_history)
                    async for delta in self.llm.stream(history):
                        if generation != self._generation:
                            log.info("[LLM] Generation cancelled (interrupted)")
                            parts.clear()
                            break
                        if first_token:
                            session.mark_stage("llm_first_token")
                            log.info("[LLM] First token received")
                            first_token = False
                        parts.append(delta)
                        session.send_event({"type": "assistant_delta", "text": delta})
                        await session.llm_text_queue.put((generation, delta))
                    else:
                        completed = True
                except Exception as exc:
                    log.error(f"[LLM] Generation error: {exc!r}")
                    session.send_event({"type": "error", "message": f"LLM failed: {exc}"})
                finally:
                    self._llm_active = False
                    session.mark_stage("llm_end")
                    await session.llm_text_queue.put((generation, None))

                if completed and parts:
                    answer = "".join(parts)
                    dashboard.turn_content(session.session_id, answer=answer)
                    session.add_history(
                        "assistant", answer, max_messages=settings.max_history_messages
                    )
                    session.send_event({"type": "assistant_message", "text": answer})
                    if settings.answer_cache_enabled:
                        answer_cache.store(user_text, answer)
                    log.info(f"[LLM] Generation completed: {answer}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"[PIPELINE] LLM loop error: {exc!r}")

    async def _answer_fixed(
        self, session, answer: str, generation: int, dashboard, intent: str = "fixed_answer"
    ) -> None:
        """Serve a permanent fixed answer without KB scoring or an LLM call."""
        session.mark_stage("llm_first_token")
        session.send_event({"type": "assistant_delta", "text": answer})
        await session.llm_text_queue.put((generation, answer))
        dashboard.turn_content(session.session_id, answer=answer)
        session.add_history(
            "assistant", answer, max_messages=settings.max_history_messages
        )
        session.send_event({"type": "assistant_message", "text": answer})
        session.send_event({"type": "knowledge", "intent": intent, "led_state": None, "score": 1.0})
        log.info(f"[FIXED] Answer sent ({intent}): {answer}")

    async def _answer_from_knowledge(self, session, match: KBMatch, generation: int, dashboard) -> None:
        """Serve a curated Soundbox answer without calling the LLM."""
        answer = match.response
        session.mark_stage("llm_first_token")
        session.send_event({"type": "assistant_delta", "text": answer})
        await session.llm_text_queue.put((generation, answer))
        dashboard.turn_content(session.session_id, answer=answer)
        session.add_history(
            "assistant", answer, max_messages=settings.max_history_messages
        )
        session.send_event({"type": "assistant_message", "text": answer})
        session.send_event(
            {
                "type": "knowledge",
                "intent": match.intent,
                "led_state": match.led_state,
                "score": round(match.score, 3),
            }
        )
        log.info(f"[KB] Answer sent: {answer}")

    async def _tts_loop(self) -> None:
        session = self.session
        try:
            while True:
                item = await session.llm_text_queue.get()
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                generation, payload = item
                if generation != self._generation:
                    continue

                if payload is None:
                    chunk = self._tts_buffer.strip()
                    self._tts_buffer = ""
                    if chunk:
                        await self._synthesize_to_queue(chunk, generation)
                    continue

                self._tts_buffer += payload
                while True:
                    chunk, self._tts_buffer = self._extract_sentence(self._tts_buffer)
                    if chunk is None:
                        break
                    await self._synthesize_to_queue(chunk, generation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"[PIPELINE] TTS loop error: {exc!r}")

    @staticmethod
    def _extract_sentence(buffer: str) -> tuple:
        match = SENTENCE_RE.search(buffer)
        if match:
            chunk = buffer[: match.end()].strip()
            rest = buffer[match.end() :]
            return (chunk or None), rest
        if len(buffer) >= FORCE_SPLIT_CHARS:
            cut = buffer.rfind(" ", 0, FORCE_SPLIT_CHARS)
            if cut >= MIN_SPLIT_CHARS:
                return buffer[:cut].strip(), buffer[cut:]
        return None, buffer

    async def _synthesize_to_queue(self, text: str, generation: int) -> None:
        session = self.session
        if self.tts is None or generation != self._generation:
            return
        if "tts_start" not in session.latency:
            session.mark_stage("tts_start")
            log.info(f"[TTS] Started for chunk: {text!r}")
        try:
            pcm = await self.tts.synthesize(text)
        except Exception as exc:
            log.error(f"[TTS] Synthesis failed: {exc!r}")
            session.send_event({"type": "error", "message": f"TTS failed: {exc}"})
            return
        if generation != self._generation or not pcm:
            return
        if "tts_first_audio" not in session.latency:
            session.mark_stage("tts_first_audio")
            log.info("[TTS] First audio received")
            session.emit_latency()
        await session.tts_audio_queue.put(pcm)
        log.info(f"[TTS] Queued {len(pcm)} bytes ({len(pcm)/2/8000:.2f}s)")

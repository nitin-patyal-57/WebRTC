import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from audio.processor import AudioProcessor
from audio.vad import EnergyVAD, VADEvent
from config import settings
from pipeline.reply_audio import FRAME_BYTES, FRAME_MS, ReplyAudioShaper, split_tts_chunk
from services.answer_cache import answer_cache
from services.fixed_answers import fixed_answers
from services.knowledge import KBMatch, knowledge
from services.live_answers import live_answers
from services.llm import GroqLLM
from services.stt import GroqSTT
from services.tts import create_tts
from utils.logger import get_logger
from webrtc.session import END_OF_SPEECH, REPLY_END, Session

log = get_logger("PIPELINE")

NO_AUDIO_POLL_S = 0.05


def _now_ms() -> float:
    return time.time() * 1000.0


@dataclass
class _ChunkJob:
    """One TTS chunk of a reply; ``audio`` yields PCM pieces then None."""

    text: str
    generation: int
    index: int
    audio: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: Optional[asyncio.Task] = None


@dataclass
class _ReplyEndJob:
    generation: int


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
        # Streaming TTS: chunk jobs in reply order, consumed by _reply_writer.
        self._chunk_jobs: asyncio.Queue = asyncio.Queue()
        self._active_jobs: list[_ChunkJob] = []
        self._chunk_index = 0
        self._tts_slots = asyncio.Semaphore(max(1, settings.tts_parallel))
        self._shaper: Optional[ReplyAudioShaper] = None
        self._writer_busy = False

    def _ai_in_progress(self) -> bool:
        session = self.session
        track = session.ai_track
        return bool(
            self._llm_active
            or self._writer_busy
            or not self._chunk_jobs.empty()
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
            asyncio.create_task(self._reply_writer(), name=f"reply:{session.session_id[:8]}"),
            asyncio.create_task(self._no_audio_watchdog(), name=f"noaudio:{session.session_id[:8]}"),
        ]
        log.info(f"[PIPELINE] Started for session {session.session_id}")

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._generation += 1
        self._cancel_chunk_jobs()
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
        session.end_response_send("interrupted")
        cleared = 0
        for queue in (session.tts_audio_queue, session.llm_text_queue):
            while not queue.empty():
                try:
                    queue.get_nowait()
                    cleared += 1
                except asyncio.QueueEmpty:
                    break
        self._tts_buffer = ""
        cleared += self._cancel_chunk_jobs()
        self._shaper = None
        session.ai_playing = False
        if session.ai_track is not None:
            # Stop the reply mid-stream; the track sends the 00 FF marker.
            session.ai_track.cancel_reply()
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
            if frame is END_OF_SPEECH:
                await self._force_endpoint("marker", "end-of-speech packet 00 FE")
                continue
            self._last_audio_time = time.monotonic()
            for pcm in self.processor.process(frame):
                events = self.vad.process(pcm)
                for event in events:
                    await self._handle_vad(event)
                if self.vad.is_speech_active and self.vad.last_rms >= self.vad.threshold:
                    session.mark_stage("last_voiced", record_dashboard=False)
                if self.stt is not None:
                    await self.stt.send_audio(pcm)
                    if settings.stt_partials:
                        await self.stt.maybe_partial()

    async def _force_endpoint(self, reason: str, detail: str) -> None:
        """End the current utterance now and start STT (no silence wait)."""
        if not self.vad.is_speech_active:
            return
        self.vad.reset()
        log.info(f"[VAD] Speech stopped ({detail})")
        await self._handle_vad(VADEvent.SPEECH_STOPPED, reason=reason)

    async def _no_audio_watchdog(self) -> None:
        """End the utterance when inbound audio stops arriving mid-speech.

        EnergyVAD only advances its silence timer as PCM arrives. The Soundbox
        stops sending RTP after its silence tail (and other clients can stall:
        screen off, RTP gap), so without this SPEECH_STOPPED would never fire.
        """
        try:
            while True:
                await asyncio.sleep(NO_AUDIO_POLL_S)
                if not self.vad.is_speech_active:
                    continue
                idle_ms = (time.monotonic() - self._last_audio_time) * 1000.0
                if idle_ms >= settings.vad_no_audio_ms:
                    await self._force_endpoint("no-audio", f"no audio for {idle_ms:.0f}ms")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"[PIPELINE] No-audio watchdog error: {exc!r}")

    async def _handle_vad(self, event: VADEvent, reason: str = "silence") -> None:
        session = self.session
        if event == VADEvent.SPEECH_STARTED:
            self._user_speaking = True
            session.finish_turn()  # previous turn, if its reply never completed
            session.begin_turn()
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
            session.speech_end_reason = reason
            session.send_event({"type": "vad", "event": "speech_stopped"})
            log.info(f"[STT] Speech stopped ({reason}), transcribing utterance")
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

                live = await live_answers.answer(user_text) if settings.live_answers_enabled else None
                if live is not None:
                    intent, answer = live
                    log.info(f"[LIVE] {intent} answer for: {user_text}")
                    try:
                        await self._answer_fixed(session, answer, generation, dashboard, intent=intent)
                    except Exception as exc:
                        log.error(f"[LIVE] Failed to answer: {exc!r}")
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
        """Split the streamed answer into chunks and start TTS for each at once."""
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
                        self._start_chunk(chunk, generation)
                    self._chunk_jobs.put_nowait(_ReplyEndJob(generation))
                    self._chunk_index = 0
                    continue

                self._tts_buffer += payload
                while True:
                    chunk, self._tts_buffer = split_tts_chunk(self._tts_buffer)
                    if chunk is None:
                        break
                    self._start_chunk(chunk, generation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"[PIPELINE] TTS loop error: {exc!r}")

    # Kept for tests/phase4_tts.py.
    _extract_sentence = staticmethod(split_tts_chunk)

    def _start_chunk(self, text: str, generation: int) -> None:
        if self.tts is None:
            return
        job = _ChunkJob(text=text, generation=generation, index=self._chunk_index)
        self._chunk_index += 1
        job.task = asyncio.create_task(self._synthesize_chunk(job))
        self._active_jobs.append(job)
        self._chunk_jobs.put_nowait(job)

    async def _synthesize_chunk(self, job: _ChunkJob) -> None:
        """Stream one chunk's TTS audio into job.audio (runs ahead of playback)."""
        session = self.session
        try:
            async with self._tts_slots:
                if job.generation != self._generation or self.tts is None:
                    return
                if "tts_start" not in session.latency:
                    session.mark_stage("tts_start")
                    log.info(f"[TTS] Started for chunk: {job.text!r}")
                else:
                    log.info(f"[TTS] Started chunk {job.index}: {job.text!r}")
                async for pcm in self.tts.synthesize_stream(job.text):
                    if job.generation != self._generation:
                        return
                    if "tts_first_audio" not in session.latency:
                        session.mark_stage("tts_first_audio")
                        log.info("[TTS] First audio received")
                        session.emit_latency()
                    job.audio.put_nowait(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"[TTS] Synthesis failed for chunk {job.index}: {exc!r}")
            session.send_event({"type": "error", "message": f"TTS failed: {exc}"})
        finally:
            job.audio.put_nowait(None)

    def _cancel_chunk_jobs(self) -> int:
        cleared = 0
        while not self._chunk_jobs.empty():
            try:
                self._chunk_jobs.get_nowait()
                cleared += 1
            except asyncio.QueueEmpty:
                break
        for job in self._active_jobs:
            if job.task is not None and not job.task.done():
                job.task.cancel()
            # A task cancelled before it started never runs its finally.
            job.audio.put_nowait(None)
        self._active_jobs.clear()
        self._chunk_index = 0
        return cleared

    def _outbound_starved(self) -> bool:
        """True when no reply audio is waiting to be sent."""
        session = self.session
        if not session.tts_audio_queue.empty():
            return False
        track = session.ai_track
        return track is None or not track.has_pending

    async def _reply_writer(self) -> None:
        """Play chunk jobs strictly in order into the outbound audio queue."""
        while True:
            job = await self._chunk_jobs.get()
            if job.generation != self._generation:
                continue
            self._writer_busy = True
            try:
                if isinstance(job, _ReplyEndJob):
                    self._end_reply()
                else:
                    await self._play_chunk(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"[PIPELINE] Reply writer error (continuing): {exc!r}")
            finally:
                self._writer_busy = False

    async def _play_chunk(self, job: _ChunkJob) -> None:
        session = self.session
        if self._shaper is None:
            self._shaper = ReplyAudioShaper(
                threshold=settings.tts_silence_threshold,
                max_pause_ms=settings.tts_max_pause_ms,
                is_starved=self._outbound_starved,
            )
        shaper = self._shaper
        prebuffer_frames = max(1, settings.tts_prebuffer_ms // FRAME_MS)
        held: list[bytes] = []
        streaming = False
        queued = 0
        try:
            while True:
                pcm = await job.audio.get()
                if job.generation != self._generation:
                    return
                frames = shaper.end_chunk() if pcm is None else shaper.feed(pcm)
                held.extend(frames)
                # Start this chunk once ~200 ms is ready, or at once if the
                # previous chunk's audio is still playing (no gap either way).
                if not streaming and held and (
                    len(held) >= prebuffer_frames or pcm is None or not self._outbound_starved()
                ):
                    streaming = True
                if streaming and held:
                    session.tts_audio_queue.put_nowait(b"".join(held))
                    queued += len(held)
                    held = []
                if pcm is None:
                    break
            log.info(f"[TTS] Chunk {job.index} queued {queued * FRAME_BYTES} bytes ({queued * FRAME_MS / 1000:.2f}s)")
        finally:
            if job in self._active_jobs:
                self._active_jobs.remove(job)

    def _end_reply(self) -> None:
        """All chunks played: drop trailing silence, then the end marker."""
        shaper, self._shaper = self._shaper, None
        if shaper is not None:
            shaper.finish()
            if shaper.trimmed_frames:
                log.info(f"[TTS] Trimmed {shaper.trimmed_frames * FRAME_MS}ms of TTS silence from reply")
        self.session.tts_audio_queue.put_nowait(REPLY_END)

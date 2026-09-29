# WebRTC AI Voice Assistant (Payment Soundbox)

Real-time, low-latency voice AI over WebRTC:

**Mic → WebRTC → FastAPI → VAD → STT → LLM → TTS → WebRTC → Speaker**

Designed for a smart payment soundbox today; ESP32 + SIM7600 (cellular) later.

## Stack

| Stage | Implementation |
|---|---|
| Transport | WebRTC (aiortc server), STUN, optional TURN |
| VAD | Server-side energy/RMS gate (`audio/vad.py`), 300ms min speech / 700ms hangover |
| STT | Groq `whisper-large-v3-turbo`, VAD-segmented with partials (re-transcribe ≥900ms) |
| LLM | Groq `openai/gpt-oss-20b`, streaming, short spoken-style system prompt |
| Knowledge | Curated Soundbox Q&A (`server/data/soundbox_kb.json`); questions similar to the training data are answered with the canned response, otherwise the LLM answers using the same facts |
| TTS | Sentence-buffered. `groq` = Orpheus; `sapi` = local Windows fallback (active) |
| Events | WebRTC DataChannel `events` (JSON): transcripts, deltas, latency, vad, interrupted |
| Latency | Per-turn timers (speech_start → tts_first_audio), sent as `latency` event |

## Quick start (Windows dev)

```powershell
cd server
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env   # then fill GROQ_API_KEY
.\.venv\Scripts\uvicorn main:app --host 127.0.0.1 --port 3000
```

Open `client/test-client.html` (or serve it), click **Connect**, allow mic, speak.

- Health: `GET http://127.0.0.1:3000/health`
- Offer: `POST /webrtc/offer` `{device_id, sdp, type}`
- Sessions: `GET/DELETE /sessions/{id}`

## Mobile (Android APK)

Phone client is a WebView shell around the same web UI (`android/`).

**1. Run the PC server on LAN + HTTPS** (required so the phone can use the mic):

```powershell
cd server
.\run_mobile.ps1          # prints https://<your-LAN-IP>:8443
```

Self-signed cert is generated into `server/certs/` on first run. Allow inbound TCP 8443 in Windows Firewall if the phone cannot reach the URL.

**2. Build the APK** (JDK 17 + Android SDK required):

```powershell
cd android
.\gradlew.bat assembleDebug
# output: android\app\build\outputs\apk\debug\app-debug.apk
```

**3. Install on the phone:**

```powershell
adb install -r android\app\build\outputs\apk\debug\app-debug.apk
```

Open **AI Voice Client**, enter the `https://<PC-IP>:8443` URL, tap **Load**, allow the mic, tap **Connect**, speak.

### Response-time metrics (web UI, same page in the APK)

| Metric | Meaning |
|---|---|
| **Ping** | HTTP RTT to `/health` (avg of last 10) |
| **Connect (offer)** | POST `/webrtc/offer` → SDP answer |
| **Reply text** | Client clock: speech stopped → first AI text delta |
| **Reply audio** | Client clock: speech stopped → first non-silent inbound audio |
| **Server total / breakdown** | PC-side: speech end → first audio sent; STT / LLM / TTS split |
| **Turn history** | Every Q→A turn with response, text, audio, server times |

## Configuration (`.env`)

| Var | Notes |
|---|---|
| `GROQ_API_KEY` | Required for STT/LLM (and TTS when `TTS_PROVIDER=groq`) |
| `GROQ_MODEL` / `GROQ_STT_MODEL` / `GROQ_TTS_MODEL` | Defaults in `.env.example` |
| `TTS_PROVIDER` | `groq` (Orpheus) or `sapi` (Windows dev fallback) |
| `STUN_URLS` | Default Google STUN |
| `TURN_URL`, `TURN_USERNAME`, `TURN_CREDENTIAL` | Required for production / cellular NAT |
| `VAD_SILENCE_MS`, `VAD_MIN_SPEECH_MS` | Endpointing tunables |

## Status

Verified by automated tests in `server/tests/` (all passing):

| Phase | Test | Result |
|---|---|---|
| 1. WebRTC audio in | `phase1_client.py` | PASS — 296 frames received |
| 2. STT + VAD | `phase2_stt.py` | PASS — live Groq Whisper transcript |
| 3. LLM streaming | `phase3_llm.py` | PASS — first token ~690ms |
| 4. TTS sentence buffer | `phase4_tts.py` | PASS — synthesis + WAV parsing |
| 5. AI audio out | `phase5_audio.py` | PASS — non-silent AI frames over WebRTC |
| 6. Full E2E + barge-in | `phase6_e2e.py` | PASS — speech→reply, interrupt mid-reply, ~1.4s turn latency |
| 7. Knowledge base | `kb_match.py` | PASS — 103/103 training instructions, 18/18 paraphrases, 0 false matches |

- **Browser mic test: not yet done** (manual step — see above).
- **TURN: not configured** (STUN only). Fine on LAN; add TURN for real networks/cellular.

## Known limitations / TODO

1. **Groq Orpheus TTS terms** — org-level terms acceptance not registering for the API key; `TTS_PROVIDER=sapi` is the local workaround. Accept terms in the Groq console, then set `TTS_PROVIDER=groq`.
2. **No streaming STT/TTS on Groq** — latency is mitigated with sentence-level TTS buffering and partial transcripts; first audio ≈ VAD hangover + STT + LLM + one sentence of TTS.
3. **TURN** — set before any deployment beyond this machine/LAN.
4. **`.env` contains a live API key** — do not commit; rotate if shared.

## ESP32 + SIM7600 gaps (future hardware)

- WebRTC on ESP32 needs a hardware-friendly stack (e.g. aiortc-compatible peer or a gateway that terminates WebRTC and bridges PCM/I2S over serial/AT socket).
- Cellular NAT is restrictive → TURN (UDP/TCP 443) is mandatory; consider ICE TCP and keepalive tuning for metered links.
- Audio: I2S mic/speaker at 48kHz to match the pipeline; Opus decode/encode load must be budgeted on-device or offloaded to a gateway.
- Power/thermal: continuous WebRTC + cellular radio duty-cycling strategy needed.
- Device provisioning: per-device `device_id`, credentials, and OTA update path not yet designed.

## Project layout

```
server/
  main.py               FastAPI app entry
  config.py             Settings + system prompt (env-driven)
  api/                  /health, /webrtc/offer, /sessions
  webrtc/               Peer connection, session state, AI audio track
  audio/                Resampler (48k→16k), energy VAD
  services/             Groq STT / LLM / TTS (+ SAPI fallback), knowledge matcher
  data/                 Soundbox Q&A training data (knowledge base)
  pipeline/             Voice pipeline: loops, barge-in, sentence buffer, latency
  tests/                Phase 1–6 verification scripts
client/
  test-client.html      Browser + WebView test UI (transcripts, response-time metrics, turn history)
android/
  app/                  WebView Android client (AI Voice Client)
server/
  run_mobile.ps1        LAN HTTPS server for the phone
  scripts/              Dev cert generator
  certs/                Self-signed key/cert (generated, do not commit)
```

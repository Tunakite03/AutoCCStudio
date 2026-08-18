# AutoCC

[Tiếng Việt](README.md) | **English**

AutoCC is a local-first application for generating, editing, translating, and dubbing video subtitles:

- **Subtitle Import:** Supports `.srt` and `.vtt` formats.
- **Speech-to-Text (STT):** Local offline transcription via `faster-whisper` or cloud-based via Deepgram Nova-3.
- **Multi-Speaker Diarization:** Supports acoustic audio diarization from Deepgram; contextual AI analyzes dialogue flow to insert newline breaks for speaker turns within cues without inserting artificial `[S1]`/`[S2]` tags.
- **AI Translation:** Batch translation via OpenAI-compatible endpoints (`/chat/completions`), allowing seamless connection to Ollama, LM Studio, or hosted cloud APIs.
- **Translation Styles & Custom Glossaries:** Built-in presets (Sino-Vietnamese / Hán Việt, Korean Drama, Japanese Anime, GenZ / Slang, Formal), pinned glossary terms, and full support for creating and managing custom saved translation styles.
- **Automated TTS Dubbing:** Synthesize translations using Microsoft Edge Neural TTS or mock voices; 3-tier duration fitting (`retime_pcm` speedup, silence `spill`, and LLM-assisted line shortening); mix voiceovers onto the original video with automated Audio Ducking.
- **Real-Time Progress Tracking:** Monitor transcription, translation, diarization, and dubbing live via Server-Sent Events (SSE) with automatic reconnection.
- **Professional NLE Video Editor UI:** Responsive video player with subtitle overlay, smooth interactive draggable timeline with zoomable audio waveforms.
- **Project Dashboard:** Overview of all projects in your workspace with video thumbnails, duration, translation progress bars, disk usage, search, filtering, and deletion.
- **Instant Re-transcription:** Reopen past projects and re-run transcription directly on cached server video files without re-uploading.
- **Flexible Timeline Editing:** Drag clip body to shift cue timing, drag edges to trim/extend, split/merge cues at playhead, and snap to cue boundaries and playhead.
- **Smooth Playback Scrubbing:** Dragging or scrubbing the timeline during playback smoothly pauses the video and resumes upon release.
- **Multi-Level Undo / Redo:** Snapshot-based history manager for all editing operations; rapid typing bursts are automatically grouped into single undo steps.
- **Reading Speed Warning (CPS):** Characters-Per-Second visual alerts directly on timeline clips and inspector based on 17/21 cps thresholds.
- **Customizable & Multilingual UI:** Sidebar widths and timeline height are persisted across sessions, with dark/light themes and full English / Vietnamese localization.
- **Export & Muxing:** Export clean SRT/VTT subtitle files or mux soft subtitles and dubbed audio tracks into MP4 video containers using FFmpeg.

---

## Running on Windows

**Prerequisites:** Python 3.10+ (Python 3.12 recommended). The application automatically finds `ffmpeg` in your system `PATH`; otherwise, it falls back to the bundled binary from `imageio-ffmpeg`.

```powershell
cd E:\Project2025\AutoCC
Copy-Item .env.example .env
.\run.ps1
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

> **Note:** On the first run with `faster-whisper`, the model will be downloaded to your local cache. You can choose between `tiny`, `base`, `small`, `medium`, or `large-v3`; `small` is the recommended balance for CPU execution.

---

## Multi-Speaker Diarization with Deepgram

Create an API key in Deepgram, then configure your `.env`:

```dotenv
TRANSCRIPTION_PROVIDER=deepgram
DEEPGRAM_API_KEY=your-deepgram-api-key
DEEPGRAM_MODEL=nova-3
DEEPGRAM_DIARIZE_MODEL=latest
```

Restart AutoCC. The backend sends video audio to `/v1/listen` with `utterances=true`, `smart_format=true`, and the latest diarizer model. Setting language to "Auto Detect" activates automatic language identification; selecting a specific language locks the model to that language code.

When selecting Deepgram in the UI, the model selector provides `nova-3`, `nova-2`, `nova-2-meeting`, and `nova-2-video`. `nova-3` is best suited for general video and multi-speaker content.

### Two-Tier AI Speaker Turn Analysis

1. **Tier 1 (Acoustic Diarization):** The backend uses per-word `speaker` and `speaker_confidence` data from Deepgram to insert newline breaks based on acoustic voice transitions.
2. **Tier 2 (Contextual LLM):** An LLM evaluates conversational context (e.g., question-and-answer flow, pronouns) to resolve ambiguous boundaries, even when Deepgram merges multiple speakers into a single utterance.

The model is only permitted to insert newline characters; the backend validates all text and punctuation against the original source to prevent hallucinated changes. The **Re-analyze Speaker Turns** button re-runs only this step on current cues without incurring extra STT costs.

---

## AI Translation (LLM)

By default, AutoCC connects to a local Ollama instance:

```powershell
ollama pull qwen2.5:7b
ollama serve
```

If connecting to another LLM endpoint (LM Studio, vLLM, or hosted APIs such as OpenAI, DeepSeek, Mistral), configure your `.env`:

```dotenv
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=[key-1, key-2]
LLM_MODEL=gpt-4o-mini
# Defaults to LLM_MODEL if left blank
SPEAKER_ANALYSIS_MODEL=
```

> **API Key Rotation:** You can supply multiple keys as an array `[key1, key2]`. The system rotates keys in Round-Robin order and automatically applies a 60-second cooldown when a key encounters HTTP 429 (Rate Limit).

You can also run offline translation using dedicated HuggingFace Transformers pipelines (e.g. En $\rightarrow$ Vi):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-translation-local.txt
```

```dotenv
TRANSLATION_PROVIDER=transformers
TRANSLATION_MODEL=Helsinki-NLP/opus-mt-en-vi
TRANSFORMERS_TARGET_LANGUAGE=Tiếng Việt
TRANSFORMERS_DEVICE=auto
```

### Contextual Batching & JSON Error Recovery

- **Context Continuity:** Each batch includes 4 preceding lines (including recently generated translations) and 2 upcoming lines as read-only context.
- **Continuous Glossary:** A character and terminology glossary is continuously accumulated and passed to subsequent batches (capped at 40 entries).
- **JSON Repair Engine:** Responses from the LLM are mapped strictly by line ID. If the model formats markdown incorrectly or merges lines, the backend isolates and retries the missing line without failing the entire batch.

### Translation Styles & Custom Styles

- **Presets:** `Auto by Source Language` (Chinese $\rightarrow$ Sino-Vietnamese / Hán Việt, Korean $\rightarrow$ Oppa/Unnie, Japanese $\rightarrow$ Senpai/-san), `Neutral`, `GenZ / Slang`, `Formal`.
- **Custom Rules & Pinned Glossary:** Input rules like `大哥 → đại ca` or `陛下 = bệ hạ` to override specific terms.
- **Custom Style Manager:** Save your custom prompt instructions and glossaries (persisted in `runtime/styles.json`) for reuse in other projects.

Default presets are defined in [backend/domain/translation/style.py](backend/domain/translation/style.py).

---

## Automated TTS Dubbing

After translating subtitles, AutoCC synthesizes speech for each cue, fits the duration, and mixes the audio track over the original video.

By default, it uses `edge-tts` (Microsoft Neural TTS). Configure in `.env`:

```dotenv
TTS_PROVIDER=edge
TTS_VOICE=vi-VN-HoaiMyNeural
DUB_ORIGINAL_GAIN=0.25
```

High-quality Vietnamese voices: `vi-VN-HoaiMyNeural` (Female) and `vi-VN-NamMinhNeural` (Male).

### 3-Tier Duration Fitting Algorithm

1. **Speedup (`retime_pcm`):** Increases speech rate up to `DUB_MAX_SPEEDUP` (default `1.25x`) while preserving pitch.
2. **Spill into Silence (`spill`):** Allows speech to extend into subsequent silence up to `DUB_MAX_SPILL_SECONDS` (default `1.2s`).
3. **LLM Shortening (`shorten_with_llm`):** Prompts the LLM to rewrite lengthy lines concisely within the allowed character budget (can be disabled via `DUB_SHORTEN_WITH_LLM=false`).

### Stale Dub Detection (`dub_stale`)

During dubbing, a SHA-256 fingerprint (`dubbing_fingerprint`) of all cue texts and timings is saved. If you edit subtitles later, the UI displays a warning that the existing voiceover is stale.

---

## Testing & Quality Checks

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 -m pytest
py -3.12 -m compileall backend
```

---

## Frontend Development (Tailwind CSS v4)

The frontend uses standalone Tailwind CSS v4 binary (no Node.js/npm required):

- **Watch mode (Development):**
  ```powershell
  .\build-css.ps1 -Watch
  ```
- **Production build:**
  ```powershell
  .\build-css.ps1
  ```
- Source file: `frontend/styles/input.css` (contains `@import "tailwindcss";` and `@import "./custom.css";`).
- Output file: `frontend/styles.css` (loaded directly by `frontend/index.html`).

---

## System Architecture

For a deep dive into backend architecture, see [BACKEND_ARCHITECTURE.md](BACKEND_ARCHITECTURE.md).

```text
frontend/
├── index.html                     # 3-pane shell: Inspector · Stage + Timeline · Cue list
├── app.js                         # Entry point: Initializes i18n & mounts feature modules
├── core/                          # Frontend core foundation
│   ├── api.js                     # Centralized HTTP/SSE calls to backend
│   ├── confirm.js                 # Interactive confirmation modal
│   ├── dom.js                     # $, element(), pointer capture utilities
│   ├── feedback.js                # Toasts, status bar, save state indicator
│   ├── format.js                  # Timecode, reading speed CPS, text formatters
│   ├── i18n.js                    # UI language manager (vi/en)
│   ├── icons.js                   # SVG icons template registry
│   ├── router.js                  # Screen navigation (data-screen + URL hash)
│   └── store.js                   # Central document state + Event Bus
├── features/                      # Independent feature modules
│   ├── cuelist.js                 # Cue list view + selected cue state ownership
│   ├── dashboard.js               # Projects screen: project grid, search, filter, delete, metrics
│   ├── editing.js                 # Add / split / merge / delete / shift cue timings
│   ├── history.js                 # Snapshot-based undo / redo manager
│   ├── inspector.js               # Timecode inputs, source text, translation editor, CPS meter
│   ├── jobs.js                    # Job lifecycle, SSE listeners, auto-save mechanism
│   ├── keymap.js                  # Central keyboard shortcuts handler
│   ├── shell.js                   # Layout splitters, light/dark theme, full-window file drop
│   ├── timeline-view.js           # Connects Canvas Timeline Engine to Application State
│   ├── transport.js               # Video player, playhead, subtitle overlay, playback controls
│   └── pipeline/                  # AI Pipeline sidebar
│       ├── index.js               # Tab dispatcher and container
│       ├── transcribe.js          # Speech-to-Text (Whisper/Deepgram) & Diarization
│       ├── translate.js           # AI Translation, style selector & glossary pinning
│       ├── dubbing.js             # TTS Dubbing, audio ducking & preview player
│       ├── presets.js             # Custom Translation Styles CRUD manager
│       └── export.js              # Export SRT/VTT and MP4 video muxing
├── i18n/                          # Localization dictionaries (vi.js, en.js)
├── lib/
│   └── timeline-engine.js         # Canvas Timeline Engine: ruler, waveform, draggable clips, playhead, zoom
└── styles/
    ├── input.css                  # Tailwind CSS v4 source entry
    └── custom.css                 # Custom scrollbar, animations, and CSS design tokens

backend/
├── app.py                         # FastAPI setup: SelectiveGZipMiddleware, CORS, exception mapping, static files
├── core/                          # Shared infrastructure (apikeys, cancellation, config, httpclient, messages)
├── domain/                        # Pure Python business algorithms
│   ├── subtitles/                 # parser.py (SRT/VTT/CJK), layout.py, styles.py (StyleStore JSON)
│   ├── dubbing/                   # aligner.py (3-tier fit_segment, fingerprint), audio_dsp.py (pure PCM)
│   └── translation/               # style.py (Style presets, glossary parser, StyleBrief)
├── infrastructure/                # External tools & provider adapters
│   ├── media/ffmpeg.py            # Robust wrapper for FFmpeg / FFprobe subprocesses
│   └── providers/                 # Transcription, Translation, and TTS Provider Protocols & Registry
├── ai/                            # AI Pipeline engines
│   ├── transcription.py           # Faster-Whisper local (CUDA/CPU) + Deepgram cloud
│   ├── translation.py             # Batching, Context injection, JSON repair, Shortening for dubbing
│   ├── diarization.py             # Contextual dialogue speaker turn analysis
│   ├── llm.py                     # OpenAI-compatible chat completions client + Transformers local
│   ├── tts.py                     # EdgeTTS synthesis orchestrator
│   └── shared.py                  # AI custom errors & progress types
├── api/                           # HTTP Routers & Endpoints
│   ├── jobs.py                    # Main facade router /api/jobs
│   ├── job_lifecycle.py           # Upload, Create, List, Delete, Cues Edit, Download
│   ├── job_operations.py          # Translate, Dub, Analyze Speakers
│   ├── job_events.py              # Server-Sent Events stream endpoint (/api/jobs/{id}/events)
│   ├── job_schemas.py             # Pydantic schemas (CueModel, CuesPayload, DubPayload, TranslatePayload)
│   ├── job_shared.py              # Helpers: claim lock context, save upload, engine resolvers
│   ├── media.py                   # Stream video (HTTP 206 Partial Content), Waveform, Thumbnail, Muxing
│   ├── styles.py                  # CRUD Custom Translation Styles (/api/styles)
│   └── system.py                  # /api/health, /api/capabilities
├── jobs/                          # State Management & Background Workers
│   ├── model.py                   # Job structure, Status, Phase, make_progress, clean_cues, public_job
│   ├── store.py                   # JobStore: Thread-safe repository, RLock per-job, atomic JSON persistence
│   ├── runner.py                  # JobRunner: Dedicated ThreadPoolExecutor pool, JobContext, cancellation
│   └── tasks.py                   # 4 Background workflows: transcription, speaker analysis, translation, dubbing
├── runtime/
│   ├── <job_id>/                  # Isolated project data: job.json, video, audio, waveform, dub cache
│   └── styles.json                # User Custom Translation Styles database
```

---

## Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| `Space` | Play / Pause video |
| `←` `→` | Step backward / forward 1 frame (hold `Shift` for 1-second jump) |
| `Ctrl ←` `Ctrl →` | Jump to previous / next cue |
| `Ctrl Z` · `Ctrl Y` | Undo · Redo |
| `A` · `S` · `G` · `Delete` | Add · Split · Merge · Delete cue |
| `I` · `O` | Set In / Out point at current playhead position |
| `N` · `F` · `+` `−` | Toggle Snapping · Fit timeline to view · Zoom in / out |
| `Ctrl ↵` | Trigger AI transcription |
| `F1` | Show keyboard shortcuts cheat sheet |

---

## Deployment

The project supports automated CI/CD deployment on pushes to `main`: running test suites, building Docker images, pushing to GitHub Packages (GHCR), and deploying to a virtual machine (VM). See [DEPLOY.md](DEPLOY.md) for details.

---

## License

This project is licensed under the terms of the open-source [MIT License](LICENSE).

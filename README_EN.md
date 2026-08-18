# AutoCC

[Tiếng Việt](README.md) | **English**

AutoCC is a local-first MVP application for generating, editing, and translating video subtitles:

- **Import subtitles:** Supports `.srt` and `.vtt` formats.
- **Speech-to-Text:** Local transcription with `faster-whisper` or cloud-based with Deepgram Nova-3.
- **Multi-speaker Diarization:** Deepgram audio diarization support; contextual AI intelligently splits speaker turns within cues via newlines without inserting artificial `[S1]`/`[S2]` tags.
- **LLM Translation:** Cue-by-cue translation via OpenAI-compatible endpoints (`/chat/completions`), allowing seamless integration with Ollama, LM Studio, or cloud AI providers.
- **Real-time Progress Monitoring:** Track transcription and translation status live via Server-Sent Events (SSE) with automatic heartbeats and reconnection.
- **NLE-style Video Editor UI:** Video preview with responsive subtitle overlay, interactive draggable timeline with zoomable audio waveforms.
- **Project Dashboard:** Manage all projects in your workspace with video thumbnails, translation progress bars, disk space usage, search, filtering, and deletion.
- **Instant Re-transcription:** Reopen past projects and re-run transcription directly on cached server video files without needing to re-upload.
- **Timeline Editing:** Drag clip body to shift cue timing, drag edges to trim/extend, split/merge cues at playhead, and snap to cue boundaries and playhead.
- **Smooth Scrubbing:** Dragging or scrubbing the timeline during playback smoothly pauses the video and resumes upon release.
- **Multi-level History:** Undo / Redo for all cue editing operations; rapid typing bursts are automatically grouped into single history entries.
- **Reading Speed Warning (CPS):** Visual Characters-Per-Second warnings directly on timeline clips and inspector lists based on 17/21 cps thresholds.
- **Customizable Layout:** Resizable left/right sidebars and timeline height persisted across sessions, with dark and light theme support.
- **Dubbing:** Read the translation aloud with TTS, fit every line to its own cue automatically, preview it against the video in the app, and export an MP4 with or without the original audio alongside it.
- **Export & Muxing:** Export clean SRT/VTT files or mux soft subtitle tracks into MP4 videos using ffmpeg.

## Running on Windows

**Prerequisites:** Python 3.12. The application uses `ffmpeg` from your system `PATH` if available; otherwise, it falls back to the bundled binary from `imageio-ffmpeg` for subtitle muxing.

```powershell
cd E:\Project2025\AutoCC
Copy-Item .env.example .env
.\run.ps1
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

*Note: On the first run with `faster-whisper`, the model will be downloaded to your local cache. You can choose between `tiny`, `base`, `small`, `medium`, or `large-v3`; `small` is the recommended balance for CPU execution.*

## Multi-Speaker Diarization with Deepgram

Create an API key in Deepgram, then configure your `.env`:

```dotenv
TRANSCRIPTION_PROVIDER=deepgram
DEEPGRAM_API_KEY=your-deepgram-api-key
DEEPGRAM_MODEL=nova-3
DEEPGRAM_DIARIZE_MODEL=latest
```

Restart AutoCC. The backend sends video audio to `/v1/listen` with `utterances=true`, `smart_format=true`, and the latest diarizer model. Setting language to "Auto Detect" activates automatic language identification; selecting a specific language locks the model to that language code.

When selecting Deepgram in the UI, the model selector provides `nova-3`, `nova-2`, `nova-2-meeting`, and `nova-2-video`. `nova-3` is best suited for general video and multi-speaker content; Meeting and Video models are specialized for English audio. The selected model applies per-job and is stored in the job's metadata.

The **AI Speaker Turn Analysis** option runs a two-tier process:
1. First, the backend uses per-word `speaker` and `speaker_confidence` data from Deepgram to insert newline breaks based on acoustic speaker transitions.
2. Second, an LLM evaluates conversational context (e.g., question-and-answer flow) to resolve ambiguous boundaries, even when Deepgram merges multiple speakers into a single utterance.

The model is only permitted to insert newline characters; the backend validates all text and punctuation against the original source and prevents the LLM from removing boundaries established by acoustic diarization. LLM results are indexed by `cue_id`, processed in small batches, and retried individually if missing or invalid. Cues that still fail after retries are kept as-is rather than failing the entire batch, with a partial status shown in the UI.

The **Re-analyze Speaker Turns** button re-runs only the LLM analysis phase on the current cues without re-uploading the video or re-calling Deepgram, allowing instant experimentation with different models or prompts without extra transcription cost.

## LLM Translation

By default, the app is configured for Ollama:

```powershell
ollama pull qwen2.5:7b
ollama serve
```

To use a custom endpoint or cloud provider, adjust `.env`:

```dotenv
LLM_BASE_URL=https://your-endpoint.example/v1
LLM_API_KEY=your-key
LLM_MODEL=your-model
# Defaults to LLM_MODEL if left blank
SPEAKER_ANALYSIS_MODEL=
```

You can also use local translation via Transformers for specific language pairs (e.g., English → Vietnamese):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-translation-local.txt
```

```dotenv
TRANSLATION_PROVIDER=transformers
TRANSLATION_MODEL=Helsinki-NLP/opus-mt-en-vi
TRANSFORMERS_TARGET_LANGUAGE=Tiếng Việt
TRANSFORMERS_DEVICE=auto
```

*Local models are tailored for specific language pairs; for multilingual workflows or expressive tone preservation, an LLM-compatible provider is recommended.*

Each translation batch sends a JSON object indexed by line numbers and receives matching keys in return. Even if a model merges two short lines into one sentence, the batch remains synchronized: missing lines are retried individually, failing only if the retry also errors out. Speaker turn newlines are preserved during translation, and timing is always managed by the backend.

Because dialogues form continuous narrative arcs, batches are not translated in isolation. Each request includes 4 preceding lines (with their newly generated translations) and 2 subsequent lines as read-only context, alongside speaker tags from diarization to ensure characters maintain consistent tone, persona, and honorifics. The model also outputs an evolving glossary (proper nouns, character relationships, recurring terms) that carries over to subsequent batches (capped at 40 items to prevent prompt bloat). Retried lines receive the exact same context. This involves zero extra API calls—only additional input context tokens.

### Translation Styles & Custom Rules

A translation can be semantically accurate yet miss the stylistic vibe: for example, `大哥` means "eldest brother", but martial arts / drama audiences expect "big brother / boss". The **Style** selector in the Translation panel lets you choose rule presets:
- `Auto by source language`: Maps Chinese to Sino-Vietnamese conventions, Korean preserves honorifics (*oppa/unnie/sunbae*), Japanese preserves suffixes (*senpai, -san, -chan*), and others use neutral phrasing.
- `GenZ / Colloquial` and `Formal` presets for manual styling.

Each preset includes two components: prompt-injected instructions and a **pinned glossary** initialized from batch 1—the model may add new terms during translation but cannot alter or delete pinned terms.

The **Custom Rules** input field allows personalized overrides (one rule per line):

```text
大哥 → big brother
Doctor = bác sĩ
Young informal tone, avoid archaic terms in modern scenes
```

Lines containing `→`, `->`, `=>`, or `=` are parsed as pinned glossary terms and **override preset entries**; other lines are treated as direct prompt instructions. To blend styles (e.g. a Chinese drama with modern slang), select the closest preset and write specific overrides in this field. It is limited to 2,000 characters and 40 pinned glossary terms (preset items are truncated before custom user terms). Preferences are saved per project.

Presets are defined in [backend/translation_style.py](backend/translation_style.py)—adding new languages or genres simply requires adding an entry to `STYLES` (and `LANGUAGE_STYLES` for automatic selection).

Hosted providers enforce rate limits (requests per second / tokens per minute). When encountering HTTP 429, the app honors the `Retry-After` header (or applies exponential backoff up to 1 minute) under a dedicated `HTTP_RATE_LIMIT_RETRIES` budget. If rate limits persist, throttle request pacing with `LLM_MIN_INTERVAL_SECONDS` (e.g., `1.1` for Mistral's free tier). Any completed batches are flushed to disk before an error is raised.

## Dubbing

Once a project has translations, AutoCC reads every cue out loud, lays the
recordings onto one track at their own timestamps, and mixes that over the
original audio.

The default engine is `edge-tts` — Microsoft's free neural endpoint, already in
`requirements.txt`. Configure it in `.env`:

```dotenv
TTS_PROVIDER=edge
TTS_VOICE=vi-VN-HoaiMyNeural
DUB_ORIGINAL_GAIN=0.25
```

The Vietnamese voices are `vi-VN-HoaiMyNeural` (female) and
`vi-VN-NamMinhNeural` (male). `edge-tts --list-voices` lists everything the
provider offers.

### Fitting a line to its cue

A translated line almost never takes exactly as long to say as the cue it belongs
to. Three strategies handle it, each tried only when the one before it was not
enough:

1. **Speed it up** — `atempo` up to `DUB_MAX_SPEEDUP` (1.25 by default). The
   cheapest fix, and the only one that keeps the sync exact.
2. **Let it spill** — run past the cue by at most `DUB_MAX_SPILL_SECONDS` into the
   silence that follows, keeping a guard before the next cue.
3. **Ask the LLM for fewer words** — a line neither trick can save is rewritten into
   a character budget and recorded again. This is the only step that costs provider
   calls, so `DUB_SHORTEN_WITH_LLM=false` turns it off; a line then simply gets read
   as fast as the speed limit allows.

`DUB_PREFER` decides which of the first two goes first. `speed` (the default) holds
the dub against the subtitles and speeds a line up even when there is silence behind
it; `natural` spends that silence first and only speeds up what still will not fit —
a better delivery, at the cost of picture and sound drifting further apart. On a
20-line sample, `speed` sped 8 lines up and spilled none.

`prefer` can also be sent with the dub request, so both can be compared on the same
project without restarting the server:

```bash
curl -X POST "http://127.0.0.1:8000/api/jobs/<job-id>/dub"   -H "Content-Type: application/json" -d '{"prefer":"natural"}'
```

### When the subtitles change after a dub

Every run records a fingerprint of exactly what it voiced — the words and the timings
of each cue that had something to say. Edit a translation or move a cue afterwards and
`dub_stale` turns `true`: the panel says so, and exporting a dubbed MP4 asks for
confirmation first. The recording is neither deleted nor blocked — it is out of date,
not broken — but it can no longer be shipped by accident.

Every run reports how many lines ended up in each category, and how many were still
too long after all three.

### Cache and export

Each voiced line is cached under `runtime/<job>/dub/`, keyed by
provider + voice + text. Editing one cue and dubbing again costs that one line;
so does stopping a run and picking it up later. Deleting a project deletes the
cache with it.

Play the result back from the dubbing panel — the video mutes itself and follows
along. **Export dubbed MP4** writes the dub as the default audio track; tick *Keep
the original audio as a second track* to ship both and let the viewer choose.

## Quick Verification

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 -m pytest
py -3.12 -m compileall backend
```

## Frontend Development (Tailwind CSS)

The frontend uses Tailwind CSS v4 via a standalone `tailwindcss.exe` binary (no Node.js/npm required):

- **Watch mode (Development):**
  ```powershell
  .\build-css.ps1 -Watch
  ```
- **Production build:**
  ```powershell
  .\build-css.ps1
  ```
- Source file: `frontend/input.css` (contains `@import "tailwindcss";` and `@import "./custom.css";`).
- Output file: `frontend/styles.css` (loaded directly by the browser).

## MVP Architecture

```text
frontend/index.html            3-pane shell: inspector · stage + timeline · cue list
frontend/app.js                entry point: sequentially mounts feature modules
frontend/core/store.js         document state + event bus (job:loaded, cues:changed, cue:patched…)
frontend/core/router.js        screen navigation via data-screen + URL hash
frontend/core/api.js           all backend API calls, single point of URL definitions
frontend/core/dom.js           $, element(), pointer capture utilities
frontend/core/feedback.js      toasts, status bar, save state indicator
frontend/core/format.js        timecode, reading speed (CPS), formatting helpers
frontend/features/transport.js player, playhead, subtitle overlay, play-while-scrubbing
frontend/features/timeline-view.js connects timeline engine to application state
frontend/features/cuelist.js   cue list view + selected cue state ownership
frontend/features/inspector.js timecode inputs, cue text editor, CPS meter
frontend/features/editing.js   add / split / merge / delete / shift cue timings
frontend/features/history.js   snapshot-based undo / redo manager
frontend/features/jobs.js      job lifecycle, SSE listeners, auto-save
frontend/features/dashboard.js Projects screen: metrics, project grid, search/filter/delete
frontend/features/pipeline.js  sidebar: media sources, capabilities, AI execution, file export
frontend/features/shell.js     layout splitters, light/dark theme, full-window file drop
frontend/features/keymap.js    central keyboard shortcuts handler
frontend/lib/timeline-engine.js canvas ruler, audio waveform, draggable clips, playhead, zoom
backend/app.py                 assembly: middleware, domain-to-HTTP error mapping, routers + static files
backend/config.py              settings from .env + logger configuration (`autocc.*`)
backend/httpclient.py          unified outgoing HTTP client with retry/backoff for transient errors
backend/api/system.py          /api/health, /api/capabilities
backend/api/jobs.py            job lifecycle: creation, cue updates, export, background tasks, SSE
backend/api/media.py           video streaming (HTTP Range), thumbnails, waveforms, subtitle muxing
backend/jobs/model.py          job data models, status definitions, client projections
backend/jobs/store.py          job state manager: concurrency locks, atomic writes, change listeners
backend/jobs/runner.py         bounded worker pool + JobContext (progress, checkpoints)
backend/jobs/tasks.py          background tasks: transcription, speaker turn analysis, translation
backend/media.py               ffmpeg commands execution + media probe
backend/subtitles.py           SRT & VTT parsers and formatters
backend/ai.py                  adapters for faster-whisper, Deepgram, and OpenAI-compatible LLMs
backend/tts.py                 speech synthesis adapters (edge-tts, plus a mock voice for tests)
backend/dubbing.py             fitting lines to cues, the segment cache, and track assembly
runtime/<job-id>/              video, subtitles, waveform.json, and temporary job metadata
```

Three core design principles ensure the backend remains clean and maintainable:

1. **No mutation outside `store.edit(job_id)`.** Prevents race conditions where background workers and HTTP handlers overwrite each other or save out-of-order revisions.
2. **Minimal lock duration.** Workers read data, execute long-running AI processes *without holding locks*, and only acquire locks when writing results. `GET /api/jobs/{id}` requests never block on transcriptions.
3. **Lower layers are HTTP-agnostic.** `store` and `jobs/` raise domain errors (`JobNotFound`, `JobConflict`), which `app.py` translates to appropriate HTTP status codes (404, 409).

Background tasks run in a bounded worker pool (`MAX_CONCURRENT_JOBS`). Queued jobs wait instead of competing for CPU, and all failures transition the job to `status=error` with complete tracebacks logged.

The frontend is built using standard ES modules without a bundling step. Core rule: **modules never call render functions of other modules**—they modify state and `emit` events. Each module manages only its own DOM subtree and control states. Adding a new screen (such as the Project Dashboard) is done by creating a new feature module and mounting it in `app.js`.

### Key Endpoints

- `POST /api/jobs/{id}/transcribe` — Re-transcribe using the cached video in `runtime/<job-id>/` (supports `provider`, `model`, `source_language`, `analyze_speakers`). Prompts for user confirmation as existing cues will be replaced.

**Dashboard Endpoints:**
- `GET /api/jobs` — Overview of all projects in `runtime/` (cue count, duration, translation progress, disk size, last modified timestamp), without cue bodies.
- `DELETE /api/jobs/{id}` — Permanently removes the project directory from disk.
- `GET /api/jobs/{id}/thumbnail` — Extracts a video frame at the 10% timestamp cached at `runtime/<job-id>/thumb.jpg`.

**Timeline Endpoints:**
- `GET /api/jobs/{id}/video` — Streams video with HTTP Range support for smooth seeking.
- `GET /api/jobs/{id}/waveform` — Decodes audio into 20 amplitude peaks/sec cached at `runtime/<job-id>/waveform.json`.

### Real-Time Job Progress via SSE

Every job snapshot includes a `progress` field (`null` when idle):

```json
{ "phase": "translating", "current": 40, "total": 60, "ratio": 0.6667, "message": "Translated 40/60 lines" }
```

`phase` is one of `queued`, `transcribing`, `analyzing`, or `translating`. `total` is `null` when a stage cannot predict its total workload upfront (e.g. Deepgram cloud API reports progress messages, whereas `faster-whisper` reports progress along timeline timestamps).

Progress updates are broadcasted via SSE without writing to disk on every tick. For translations, each completed batch is immediately flushed to disk so that failures late in the process do not forfeit earlier progress.

## Keyboard Shortcuts

| Shortcut | Action |
| --- | --- |
| `Space` | Play / Pause |
| `←` `→` | Step backward / forward 1 frame (hold `Shift` for 1-second jump) |
| `Ctrl ←` `Ctrl →` | Jump to previous / next cue |
| `Ctrl Z` · `Ctrl Y` | Undo · Redo (browser undo takes precedence inside text inputs) |
| `A` · `S` · `G` · `Delete` | Add · Split · Merge · Delete cue |
| `I` · `O` | Set In / Out point at current playhead position |
| `N` · `F` · `+` `−` | Toggle Snapping · Fit timeline to view · Zoom in / out |
| `Ctrl ↵` | Run AI transcription |
| `F1` | Show keyboard shortcuts reference |

## Current Limitations

- **Local Storage:** Job state is saved in local directory structures; multi-user accounts and database storage are not yet implemented.
- **Authentication:** No API authentication layer. The server binds to `127.0.0.1` and restricts CORS to localhost; do not expose to public networks without an authentication proxy.
- **Video Export:** "Mux into video" creates a soft subtitle track inside the MP4 container; burned-in / hardcoded subtitles are not yet supported.
- **Synchronous Video Muxing:** Video subtitle muxing is currently handled synchronously via HTTP response streaming rather than as a background job.
- **Sequential LLM Turn Analysis:** Speaker turn analysis runs sequentially in batches, which may take time on lengthy transcripts. Progress is displayed, but cancellation midway is not yet supported.
- **LLM Availability:** AI translation requires an active LLM endpoint (local or remote).
- **Deepgram Cloud Dependency:** Deepgram transcription requires an active internet connection, an API key, and uploads audio data to their servers.

## License

This project is licensed under the terms of the [MIT License](LICENSE).


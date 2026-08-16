/**
 * The left sidebar: file inputs, engine capabilities, and the run/export buttons.
 * Everything that talks to the AI pipeline or produces a deliverable lives here.
 */

import { $ } from "../core/dom.js";
import { api } from "../core/api.js";
import { confirmAction } from "../core/confirm.js";
import { reportError, setStatus, toast } from "../core/feedback.js";
import { formatFileSize } from "../core/format.js";
import { cues, hasCues, isProcessing, on, onAny, setCapabilities, state } from "../core/store.js";
import { adoptJob, bindPreviewToJob, noteLocalPreview, stopEvents, watchJob } from "./jobs.js";
import { timeline } from "./timeline-view.js";
import { showLocalPreview } from "./transport.js";

/* ── Sources ──────────────────────────────────────────────────── */

const pickedVideo = () => $("#video-file").files?.[0] ?? null;

/** True when the open project still has its video on the server. */
const serverVideoReady = () => !pickedVideo() && Boolean(state.job?.video_available);

/** The source slot must show the project's own video after reopening it —
 *  an empty file input made it look like the video had been lost. */
function renderSourceSlot() {
  const file = pickedVideo();
  const label = file
    ? `${file.name} · ${formatFileSize(file.size)}`
    : serverVideoReady()
      ? `${state.job.video_name || "video"} · đã lưu trên máy chủ`
      : "Kéo thả hoặc bấm chọn · MP4, MOV, MKV";
  $("#video-name").textContent = label;
  $("#video-drop").classList.toggle("has-file", Boolean(file) || serverVideoReady());
  $("#transcribe-label").textContent = serverVideoReady() ? "Chạy lại nhận dạng" : "Tạo phụ đề";
}

export function acceptVideoFile(file) {
  if (!file) {
    renderSourceSlot();
    refreshButtons();
    return;
  }
  renderSourceSlot();
  showLocalPreview(file);
  noteLocalPreview();
  setStatus("Đã nạp video — xem trước sẵn sàng");
  refreshButtons();
}

export async function importSubtitleFile(file) {
  $("#subtitle-name").textContent = `${file.name} · ${formatFileSize(file.size)}`;
  $("#subtitle-drop").classList.add("has-file");
  try {
    setStatus("Đang nhập phụ đề…", "busy");
    const job = await api.importSubtitle(file);
    stopEvents();
    adoptJob(job);
    timeline.fit();
    setStatus(`Đã nhập ${job.cues.length} cue`);
    toast(`Đã nhập ${job.cues.length} cue`, "success");
  } catch (error) {
    reportError(error);
  }
}

/* ── Runs ─────────────────────────────────────────────────────── */

/** The engine settings both the upload and the re-run paths send. */
function engineForm() {
  const form = new FormData();
  form.append("source_language", $("#source-language").value);
  form.append("provider", $("#transcription-provider").value);
  form.append("model", $("#transcription-model").value);
  form.append("analyze_speakers", String($("#speaker-analysis").checked));
  return form;
}

export async function transcribe() {
  const file = pickedVideo();
  if (!file) return rerunTranscription();

  const form = engineForm();
  form.append("video", file);
  try {
    setStatus("Đang tải video lên…", "busy");
    const job = await api.transcribe(form);
    bindPreviewToJob(job.id);
    adoptJob(job);
    toast("Đã đưa video vào hàng đợi AI", "success");
    watchJob(job.id);
  } catch (error) {
    reportError(error);
  }
}

/** Recognise again using the copy the server kept, so reopening a project does
 *  not mean re-uploading a multi-hundred-megabyte file. */
async function rerunTranscription() {
  const job = state.job;
  if (!job?.video_available) return toast("Chọn video trước đã", "error");

  if (hasCues()) {
    const confirmed = await confirmAction({
      title: "Chạy lại nhận dạng?",
      target: job.video_name || "video",
      note: `${cues().length} cue hiện tại (kèm bản dịch và mọi chỉnh tay) sẽ bị thay bằng kết quả mới.`,
      confirmLabel: "Chạy lại",
      cancelLabel: "Giữ nguyên",
    });
    if (!confirmed) return;
  }

  try {
    setStatus("Đang xếp hàng nhận dạng lại…", "busy");
    const updated = await api.retranscribe(job.id, engineForm());
    adoptJob(updated);
    toast("Đang chạy lại trên video đã lưu — không cần tải lên", "success");
    watchJob(updated.id);
  } catch (error) {
    reportError(error);
  }
}

export async function translate() {
  if (!hasCues()) return toast("Cần có cue trước khi dịch", "error");
  try {
    const job = await api.translate(state.job.id, $("#target-language").value);
    adoptJob(job, { keepSelection: true });
    toast("Đã đưa phụ đề vào hàng đợi dịch", "success");
    watchJob(job.id);
  } catch (error) {
    reportError(error);
  }
}

export async function reanalyzeSpeakers() {
  if (!hasCues()) return toast("Cần có cue trước khi phân tích", "error");
  try {
    const job = await api.analyzeSpeakers(state.job.id);
    adoptJob(job, { keepSelection: true });
    toast("Đang phân tích lại lượt thoại, không chạy lại Deepgram", "success");
    watchJob(job.id);
  } catch (error) {
    reportError(error);
  }
}

/* ── Deliverables ─────────────────────────────────────────────── */

function downloadSubtitle(track, format) {
  if (!state.job) return toast("Chưa có project", "error");
  const link = document.createElement("a");
  link.href = api.downloadUrl(state.job.id, track, format);
  link.click();
}

async function muxVideo() {
  if (!state.job?.video_available) return toast("Project này không có video để ghép", "error");
  try {
    setStatus("Đang ghép phụ đề vào video…", "busy");
    const blob = await api.mux(state.job.id);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${(state.job.video_name || "video").replace(/\.[^.]+$/, "")}.subtitled.mp4`;
    link.click();
    URL.revokeObjectURL(link.href);
    setStatus("Đã xuất video có phụ đề");
    toast("Đã xuất video có soft subtitle", "success");
  } catch (error) {
    reportError(error);
  }
}

/* ── Capabilities ─────────────────────────────────────────────── */

function syncModelOptions(preferred = "") {
  const provider = $("#transcription-provider").value;
  const select = $("#transcription-model");
  const capabilities = state.capabilities;
  const configured = provider === "deepgram" ? capabilities?.deepgram_model : capabilities?.whisper_model;
  const options = [...(capabilities?.transcription_models?.[provider] || [])];

  if (configured && !options.some((item) => item.value === configured)) {
    options.push({ value: configured, label: `${configured} — từ .env` });
  }
  if (options.length) {
    select.replaceChildren(
      ...options.map((item) => {
        const option = document.createElement("option");
        option.value = item.value;
        option.textContent = item.label;
        return option;
      }),
    );
  }
  const target = preferred || configured || options[0]?.value || "";
  if ([...select.options].some((option) => option.value === target)) select.value = target;
  select.disabled = options.length === 0;
  $("#transcription-model-label").textContent =
    provider === "deepgram" ? "Model Deepgram" : "Model Whisper";
  updateEngineChip();
}

function updateEngineChip() {
  const provider = $("#transcription-provider").value;
  const model = $("#transcription-model").value;
  const capabilities = state.capabilities;
  const ready =
    provider === "deepgram" ? capabilities?.deepgram_configured : capabilities?.whisper_available;
  const name = provider === "deepgram" ? "Deepgram" : "Whisper";
  $("#engine-chip").dataset.state = ready === false ? "down" : "ok";
  $("#engine-label").textContent = ready === false ? `${name} chưa sẵn sàng` : `${name} · ${model || "ready"}`;
}

function renderCapabilityNote(capabilities) {
  const whisper = capabilities.whisper_available ? capabilities.whisper_model : "chưa cài";
  const deepgram = capabilities.deepgram_configured ? capabilities.deepgram_model : "thiếu API key";
  const translation = capabilities.translation_configured
    ? capabilities.translation_provider
    : `${capabilities.translation_provider} (chưa cấu hình)`;
  const speakerAnalysis = $("#speaker-analysis");
  speakerAnalysis.disabled = !capabilities.speaker_analysis_configured;
  if (!capabilities.speaker_analysis_configured) speakerAnalysis.checked = false;
  const dialogueAI = capabilities.speaker_analysis_configured
    ? capabilities.speaker_analysis_model
    : "chưa cấu hình";
  $("#capability-note").textContent =
    `Deepgram: ${deepgram} · Whisper: ${whisper} · AI lượt thoại: ${dialogueAI} · Dịch: ${translation} · ffmpeg: ${capabilities.ffmpeg ? "OK" : "thiếu"}`;
}

export async function loadCapabilities() {
  try {
    const capabilities = await api.capabilities();
    setCapabilities(capabilities);
    const providerSelect = $("#transcription-provider");
    if ($(`#transcription-provider option[value="${capabilities.transcription_provider}"]`)) {
      providerSelect.value = capabilities.transcription_provider;
    }
    syncModelOptions();
    renderCapabilityNote(capabilities);
  } catch {
    $("#capability-note").textContent = "Không đọc được cấu hình backend.";
    $("#engine-chip").dataset.state = "down";
    $("#engine-label").textContent = "Backend không phản hồi";
  }
}

/* ── Availability ─────────────────────────────────────────────── */

function refreshButtons() {
  const blocked = isProcessing();
  const cued = hasCues();
  renderSourceSlot();
  // Either a freshly picked file or the project's own stored video will do.
  $("#transcribe-btn").disabled = blocked || (!pickedVideo() && !state.job?.video_available);
  $("#reanalyze-speakers-btn").disabled =
    blocked || !cued || !state.capabilities?.speaker_analysis_configured;
  $("#translate-btn").disabled = blocked || !cued;
  $("#download-source-srt").disabled = !cued;
  $("#download-translated-srt").disabled = !cued;
  $("#download-vtt").disabled = !cued;
  $("#mux-btn").disabled = !cued || !state.job?.video_available;
}

export function mountPipeline() {
  $("#video-file").addEventListener("change", (event) => acceptVideoFile(event.target.files[0]));
  $("#subtitle-file").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) importSubtitleFile(file);
  });

  $("#transcribe-btn").addEventListener("click", transcribe);
  $("#reanalyze-speakers-btn").addEventListener("click", reanalyzeSpeakers);
  $("#translate-btn").addEventListener("click", translate);
  $("#transcription-provider").addEventListener("change", () => syncModelOptions());
  $("#transcription-model").addEventListener("change", updateEngineChip);

  $("#download-source-srt").addEventListener("click", () => downloadSubtitle("source", "srt"));
  $("#download-translated-srt").addEventListener("click", () => downloadSubtitle("translated", "srt"));
  $("#download-vtt").addEventListener("click", () => downloadSubtitle("translated", "vtt"));
  $("#mux-btn").addEventListener("click", muxVideo);

  onAny(["job:loaded", "cues:changed"], refreshButtons);
  on("capabilities:loaded", refreshButtons);
  refreshButtons();
}

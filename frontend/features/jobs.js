/**
 * Job lifecycle: adopt a job into the UI, follow it over SSE, persist cue edits.
 *
 * Saving is reactive — any module that emits `cues:changed` or `cue:patched`
 * gets a debounced save for free, so commands never call the API themselves.
 */

import { $ } from "../core/dom.js";
import { api } from "../core/api.js";
import { reportError, setSaveState, setStatus, toast } from "../core/feedback.js";
import { cues, on, setJob, setSelection, state } from "../core/store.js";
import { resetHistory } from "./history.js";
import { setWaveformNote, timeline } from "./timeline-view.js";
import { claimPreviewFor, clearVideo, isVideoReady, loadJobVideo } from "./transport.js";

const JOB_KEY = "autocc.lastJob";
const SAVE_DEBOUNCE_MS = 420;

let eventSource = null;
let saveTimer = 0;

/* ── Adoption ─────────────────────────────────────────────────── */

export function adoptJob(job, { keepSelection = false } = {}) {
  const previousId = state.job?.id ?? null;
  const isNewJob = job.id !== previousId;

  setJob(job);
  localStorage.setItem(JOB_KEY, job.id);
  renderProjectChip(job);
  renderProgress(job);
  if (!keepSelection) setSelection(-1, "reset");

  if (!isNewJob) return;
  resetHistory();
  timeline.setWaveform(null);
  setWaveformNote(job.video_available ? "Đang đọc dạng sóng…" : "Project không có audio");
  loadJobVideo(job, api.videoUrl(job.id));
  if (job.video_available) loadWaveform(job.id);
}

function renderProjectChip(job) {
  $("#project-name").textContent = job.video_name || job.subtitle_name || "Project không tên";
  $("#project-kind").textContent = job.kind === "transcription" ? "VIDEO" : "PHỤ ĐỀ";
  $("#status-language").textContent = job.detected_language || job.source_language || "—";
}

function renderProgress(job) {
  const processing = job.status === "processing";
  $("#progress-rail").classList.toggle("hidden", !processing);
  if (!processing) return;

  // The stop is cooperative: the worker lands on it at its next checkpoint, and
  // a provider call already in flight has to return first. Saying so is the
  // difference between "the button did nothing" and "it is finishing a step".
  const stopping = Boolean(job.cancel_requested);
  $("#progress-copy").textContent = stopping
    ? "Đang dừng… chờ bước đang chạy kết thúc"
    : job.kind === "transcription" && !cues().length
      ? "AI đang nghe audio và dựng timestamp…"
      : job.speaker_analysis_status === "processing"
        ? "AI đang phân tích ngữ cảnh và tách lượt thoại…"
        : "AI đang dịch từng cue…";
  $("#cancel-job-btn").disabled = stopping;
  $("#cancel-job-label").textContent = stopping ? "Đang dừng…" : "Hủy";
  setStatus(stopping ? "Đang dừng" : "Đang xử lý", "busy");
}

async function cancelRun() {
  const job = state.job;
  if (!job || job.status !== "processing" || job.cancel_requested) return;
  $("#cancel-job-btn").disabled = true;
  try {
    adoptJob(await api.cancel(job.id), { keepSelection: true });
    toast("Đã yêu cầu dừng — chờ bước đang chạy kết thúc", "success");
  } catch (error) {
    $("#cancel-job-btn").disabled = false;
    reportError(error);
  }
}

async function loadWaveform(jobId) {
  try {
    const data = await api.waveform(jobId);
    if (state.job?.id !== jobId) return;
    timeline.setWaveform(data);
  } catch (error) {
    if (state.job?.id !== jobId) return;
    setWaveformNote(
      error.message.includes("ffmpeg") ? "Cần ffmpeg để hiện dạng sóng" : "Không đọc được dạng sóng",
    );
  }
}

/** Wipe the editor after its project is deleted, without reloading the page. */
export function forgetJob() {
  stopEvents();
  clearTimeout(saveTimer);
  localStorage.removeItem(JOB_KEY);
  setJob(null);
  setSelection(-1, "reset");
  resetHistory();
  clearVideo();
  timeline.setWaveform(null);
  setWaveformNote("Chưa mở project");
  $("#project-name").textContent = "Chưa mở project";
  $("#project-kind").textContent = "TRỐNG";
  $("#status-language").textContent = "—";
  $("#progress-rail").classList.add("hidden");
  setSaveState("—");
  setStatus("Sẵn sàng");
}

export const hasLastJob = () => Boolean(localStorage.getItem(JOB_KEY));

/** Called when a video is picked before any job exists. */
export function noteLocalPreview() {
  if (!state.job?.video_available) setWaveformNote("Dạng sóng hiện sau khi tạo phụ đề");
}

export const bindPreviewToJob = claimPreviewFor;

/* ── Live updates ─────────────────────────────────────────────── */

export function stopEvents() {
  eventSource?.close();
  eventSource = null;
}

export function watchJob(jobId) {
  stopEvents();
  const source = new EventSource(api.eventsUrl(jobId));
  eventSource = source;

  source.addEventListener("job", (event) => {
    if (eventSource !== source) return;
    let job;
    try {
      job = JSON.parse(event.data);
    } catch {
      return stopEvents();
    }
    if (job.id !== jobId) return;

    adoptJob(job, { keepSelection: job.status === "processing" });
    if (job.status === "completed") onJobCompleted(job);
    else if (job.status === "cancelled") onJobCancelled(job);
    else if (job.status === "error") {
      stopEvents();
      reportError(new Error(job.error || "Job thất bại"));
    }
  });

  source.onerror = () => {
    if (eventSource === source) setStatus("Mất kết nối SSE, đang thử lại…", "busy");
  };
}

/** A stop is not a failure: whatever the worker had already saved is now the
 *  project, and the pipeline buttons are the way back in. */
function onJobCancelled(job) {
  stopEvents();
  resetHistory();
  setSaveState("Đã lưu", "saved");
  const translated = job.cues.filter((cue) => (cue.translation || "").trim()).length;
  const kept = translated ? `${translated}/${job.cues.length} cue đã dịch được giữ lại` : `${job.cues.length} cue`;
  setStatus(`Đã dừng · ${kept}`);
  toast(`Đã dừng tiến trình · ${kept}`, "success");
  if (isVideoReady()) timeline.fit();
}

function onJobCompleted(job) {
  stopEvents();
  resetHistory(); // the AI rewrote every cue — older snapshots no longer belong to this take
  setSaveState("Đã lưu", "saved");
  if (job.speaker_analysis_status === "failed") {
    const warning = job.speaker_analysis_error || "AI không phân tích được lượt thoại";
    setStatus(`Đã tạo phụ đề · ${warning}`, "error");
    toast(`Đã tạo phụ đề, nhưng ${warning}`, "error");
  } else if (job.speaker_analysis_status === "partial") {
    const warning = job.speaker_analysis_error || "một số cue được giữ nguyên";
    setStatus(`Hoàn tất một phần · ${warning}`, "error");
    toast(`Đã tách phần hợp lệ; ${warning}`, "error");
  } else {
    setStatus("Hoàn tất");
    const report = job.speaker_analysis_report;
    const analyzed =
      job.speaker_analysis_status === "completed" && report
        ? ` · audio ${report.acoustic_split_cues} · AI ${report.ai_modified_cues}`
        : job.speaker_analysis_status === "completed"
          ? " · đã tách lượt thoại"
          : "";
    toast(`Xong · ${job.cues.length} cue${analyzed}`, "success");
  }
  if (isVideoReady()) timeline.fit();
}

/* ── Persistence ──────────────────────────────────────────────── */

function queueSave() {
  if (!state.job) return;
  setSaveState("Đang lưu…", "saving");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const payload = cues().map((cue) => ({
      id: cue.id ?? 0,
      start: Number(cue.start.toFixed(3)),
      end: Number(cue.end.toFixed(3)),
      text: cue.text || "",
      translation: cue.translation || "",
      speaker: cue.speaker ?? null,
    }));
    try {
      const saved = await api.saveCues(state.job.id, payload);
      // Keep local cues: the user may have typed while the request was in flight.
      Object.assign(state.job, { ...saved, cues: state.job.cues });
      setSaveState("Đã lưu", "saved");
      setStatus("Đã lưu thay đổi");
    } catch (error) {
      setSaveState("Lưu lỗi", "error");
      reportError(error);
    }
  }, SAVE_DEBOUNCE_MS);
}

/* ── Mount ────────────────────────────────────────────────────── */

export async function restoreLastJob() {
  const jobId = localStorage.getItem(JOB_KEY);
  if (!jobId) return;
  try {
    const job = await api.job(jobId);
    adoptJob(job);
    setStatus("Đã mở lại project gần nhất");
    if (job.status === "processing") watchJob(job.id);
  } catch {
    localStorage.removeItem(JOB_KEY);
  }
}

export function mountJobs() {
  on("cues:changed", queueSave);
  on("cue:patched", queueSave);
  $("#cancel-job-btn").addEventListener("click", cancelRun);
  window.addEventListener("beforeunload", stopEvents);
}

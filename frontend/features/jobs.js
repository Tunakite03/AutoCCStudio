/**
 * Job lifecycle: adopt a job into the UI, follow it over SSE, persist cue edits.
 *
 * Saving is reactive — any module that emits `cues:changed` or `cue:patched`
 * gets a debounced save for free, so commands never call the API themselves.
 */

import { $ } from "../core/dom.js";
import { api } from "../core/api.js";
import { reportError, setSaveState, setStatus, toast } from "../core/feedback.js";
import { t, tm } from "../core/i18n.js";
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
  setWaveformNote(t(job.video_available ? "wave.loading" : "wave.noAudio"));
  loadJobVideo(job, api.videoUrl(job.id));
  if (job.video_available) loadWaveform(job.id);
}

function renderProjectChip(job) {
  $("#project-name").textContent =
    job.video_name || job.subtitle_name || t("project.untitled");
  $("#project-kind").textContent = t(
    job.kind === "transcription" ? "project.kindVideo" : "project.kindSubtitle",
  );
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
  // The worker's own progress line wins when it has one: it names the step and
  // counts it. The per-phase fallbacks below only cover the gap before the first
  // tick arrives.
  $("#progress-copy").textContent = stopping
    ? t("run.stopping")
    : tm(job.progress?.message) ||
      (job.kind === "transcription" && !cues().length
        ? t("run.listening")
        : job.speaker_analysis_status === "processing"
          ? t("run.analyzing")
          : t("run.translating"));
  $("#cancel-job-btn").disabled = stopping;
  $("#cancel-job-label").textContent = t(stopping ? "action.cancelling" : "action.cancel");
  setStatus(t(stopping ? "status.stopping" : "status.processing"), "busy");
}

async function cancelRun() {
  const job = state.job;
  if (!job || job.status !== "processing" || job.cancel_requested) return;
  $("#cancel-job-btn").disabled = true;
  try {
    adoptJob(await api.cancel(job.id), { keepSelection: true });
    toast(t("toast.stopRequested"), "success");
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
      t(error.message.includes("ffmpeg") ? "wave.needFfmpeg" : "wave.unreadable"),
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
  setWaveformNote(t("project.none"));
  $("#project-name").textContent = t("project.none");
  $("#project-kind").textContent = t("project.kindEmpty");
  $("#status-language").textContent = "—";
  $("#progress-rail").classList.add("hidden");
  setSaveState(t("save.none"));
  setStatus(t("status.ready"));
}

export const hasLastJob = () => Boolean(localStorage.getItem(JOB_KEY));

/** Called when a video is picked before any job exists. */
export function noteLocalPreview() {
  if (!state.job?.video_available) setWaveformNote(t("wave.afterTranscribe"));
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
      reportError(new Error(tm(job.error, "job.failed")));
    }
  });

  source.onerror = () => {
    if (eventSource === source) setStatus(t("status.sseRetry"), "busy");
  };
}

/** A stop is not a failure: whatever the worker had already saved is now the
 *  project, and the pipeline buttons are the way back in. */
function onJobCancelled(job) {
  stopEvents();
  resetHistory();
  setSaveState(t("save.saved"), "saved");
  const translated = job.cues.filter((cue) => (cue.translation || "").trim()).length;
  const kept = translated
    ? t("job.keptTranslated", { done: translated, total: job.cues.length })
    : t("job.keptCues", { count: job.cues.length });
  setStatus(t("status.stoppedWith", { kept }));
  toast(t("toast.stoppedWith", { kept }), "success");
  if (isVideoReady()) timeline.fit();
}

function onJobCompleted(job) {
  stopEvents();
  resetHistory(); // the AI rewrote every cue — older snapshots no longer belong to this take
  setSaveState(t("save.saved"), "saved");
  if (job.speaker_analysis_status === "failed") {
    const warning = tm(job.speaker_analysis_error, "job.analysisFailed");
    setStatus(t("status.subtitlesWithWarning", { warning }), "error");
    toast(t("toast.subtitlesWithWarning", { warning }), "error");
  } else if (job.speaker_analysis_status === "partial") {
    const warning = tm(job.speaker_analysis_error, "job.someCuesKept");
    setStatus(t("status.partialWithWarning", { warning }), "error");
    toast(t("toast.partialWithWarning", { warning }), "error");
  } else {
    setStatus(t("status.done"));
    const report = job.speaker_analysis_report;
    const extra =
      job.speaker_analysis_status === "completed" && report
        ? t("job.analysisCounts", {
            acoustic: report.acoustic_split_cues,
            ai: report.ai_modified_cues,
          })
        : job.speaker_analysis_status === "completed"
          ? t("job.turnsSplit")
          : "";
    toast(t("toast.doneCues", { count: job.cues.length, extra }), "success");
  }
  if (isVideoReady()) timeline.fit();
}

/* ── Persistence ──────────────────────────────────────────────── */

function queueSave() {
  if (!state.job) return;
  setSaveState(t("save.saving"), "saving");
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
      setSaveState(t("save.saved"), "saved");
      setStatus(t("status.savedChanges"));
    } catch (error) {
      setSaveState(t("save.failed"), "error");
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
    setStatus(t("status.reopened"));
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

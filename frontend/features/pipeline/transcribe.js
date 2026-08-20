/**
 * Pipeline — Audio/Video upload and Speech-to-Text Transcription.
 *
 * Manages video and subtitle file selection, engine configuration (Whisper / Deepgram),
 * source audio language confirmation guards, and transcription job triggers.
 */

import { $ } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { confirmAction } from "../../core/confirm.js";
import { reportError, setStatus, toast } from "../../core/feedback.js";
import { formatFileSize } from "../../core/format.js";
import { optionLabel, t } from "../../core/i18n.js";
import { cues, hasCues, isProcessing, state } from "../../core/store.js";
import { adoptJob, bindPreviewToJob, noteLocalPreview, stopEvents, watchJob } from "../jobs.js";
import { timeline } from "../timeline-view.js";
import { showLocalPreview } from "../transport.js";

/* ── Sources ──────────────────────────────────────────────────── */

export const pickedVideo = () => $("#video-file").files?.[0] ?? null;

/** True when the open project still has its video on the server. */
export const serverVideoReady = () => !pickedVideo() && Boolean(state.job?.video_available);

/** The source slot must show the project's own video after reopening it —
 *  an empty file input made it look like the video had been lost. */
export function renderSourceSlot() {
  const file = pickedVideo();
  const label = file
    ? `${file.name} · ${formatFileSize(file.size)}`
    : serverVideoReady()
      ? t("source.videoOnServer", { name: state.job.video_name || "video" })
      : t("source.videoHint");
  $("#video-name").textContent = label;
  $("#video-drop").classList.toggle("has-file", Boolean(file) || serverVideoReady());
  $("#transcribe-label").textContent = t(
    serverVideoReady() ? "action.retranscribe" : "action.transcribe",
  );
}

export function acceptVideoFile(file, refreshCallback) {
  if (!file) {
    renderSourceSlot();
    refreshCallback?.();
    return;
  }
  renderSourceSlot();
  showLocalPreview(file);
  noteLocalPreview();
  setStatus(t("status.videoLoaded"));
  refreshCallback?.();
}

export async function importSubtitleFile(file) {
  $("#subtitle-name").textContent = `${file.name} · ${formatFileSize(file.size)}`;
  $("#subtitle-drop").classList.add("has-file");
  try {
    setStatus(t("status.importingSubtitle"), "busy");
    const job = await api.importSubtitle(file);
    stopEvents();
    adoptJob(job);
    timeline.fit();
    const imported = t("status.importedCues", { count: job.cues.length });
    setStatus(imported);
    toast(imported, "success");
  } catch (error) {
    reportError(error);
  }
}

/* ── Runs ─────────────────────────────────────────────────────── */

/** The engine settings both the upload and the re-run paths send. */
export function engineForm() {
  const form = new FormData();
  form.append("source_language", $("#source-language").value);
  form.append("provider", $("#transcription-provider").value);
  form.append("model", $("#transcription-model").value);
  form.append("analyze_speakers", String($("#speaker-analysis").checked));
  return form;
}

/* ── Audio language guard ─────────────────────────────────────── */

/**
 * Why a run would start without an explicit audio language, or "" when the
 * user did pick one.
 *
 * `multi` counts as unset on faster-whisper only: it is a Deepgram Nova-3 mode,
 * and the local engine drops it back to auto-detect (backend/ai.py) — a silent
 * downgrade the picker's own label ("Nova-3 Multilingual") hides.
 */
export function unsetLanguageReason() {
  const language = $("#source-language")?.value || "";
  const deepgram = $("#transcription-provider")?.value === "deepgram";
  if (!language || language === "auto") return "auto";
  if (language === "multi" && !deepgram) return "multi";
  return "";
}

/** The wording differs per engine because the failure mode differs per engine. */
function languageWarning(reason) {
  const deepgram = $("#transcription-provider")?.value === "deepgram";
  const engine = deepgram ? "Deepgram" : "Whisper";
  const detail = t(deepgram ? "guard.deepgramDetail" : "guard.whisperDetail");

  if (reason === "multi") {
    return {
      title: t("guard.multiTitle"),
      hint: t("guard.multiHint"),
      note: t("guard.multiNote", { engine, detail }),
      confirmLabel: t("guard.multiConfirm"),
    };
  }
  return {
    title: t("guard.autoTitle"),
    hint: t("guard.autoHint"),
    note: t("guard.autoNote", { detail }),
    confirmLabel: t("guard.autoConfirm"),
  };
}

/** Inline hint under the picker — the warning has to be visible *before* the
 *  click, otherwise the confirm is the first time the user hears about it. */
export function syncLanguageHint() {
  const node = $("#source-language-hint");
  if (!node) return;
  const reason = unsetLanguageReason();
  node.style.display = reason ? "flex" : "none";
  if (reason) $("#source-language-hint-text").textContent = languageWarning(reason).hint;
}

/** A cancelled confirm has to land the user on the control it asked about. */
function focusLanguagePicker() {
  const select = $("#source-language");
  if (!select) return;
  select.scrollIntoView({ block: "center", behavior: "smooth" });
  select.focus({ preventScroll: true });
  // Focus alone reads as nothing on a select that was already in view.
  select.style.outline = "2px solid var(--color-accent)";
  select.style.outlineOffset = "1px";
  setTimeout(() => {
    select.style.outline = "";
    select.style.outlineOffset = "";
  }, 1600);
  try {
    select.showPicker?.();
  } catch {
    /* needs transient activation; focus already made the point */
  }
}

/** Answered once, not once per run — reset whenever the engine or the language
 *  changes, because that is a different decision than the one acknowledged. */
let acknowledgedLanguage = "";

export function resetAcknowledgedLanguage() {
  acknowledgedLanguage = "";
}

/**
 * Confirm a run that would start without an explicit audio language.
 * Returns true to proceed, false when the user went back to pick one.
 */
async function confirmLanguageSelection() {
  const reason = unsetLanguageReason();
  if (!reason || reason === acknowledgedLanguage) return true;

  const warning = languageWarning(reason);
  const engine = $("#transcription-provider")?.value === "deepgram" ? "Deepgram" : "Whisper";
  const select = $("#source-language");
  const chosen = select?.options[select.selectedIndex]?.textContent.trim() || t("lang.auto");
  const confirmed = await confirmAction({
    title: warning.title,
    target: `${engine} · ${$("#transcription-model")?.value || t("guard.defaultModel")} · ${chosen}`,
    note: warning.note,
    confirmLabel: warning.confirmLabel,
    cancelLabel: t("guard.pickLanguageAgain"),
    variant: "warning",
  });
  if (!confirmed) {
    focusLanguagePicker();
    return false;
  }
  acknowledgedLanguage = reason;
  return true;
}

/** Guards the button against a second click while a run is being kicked off —
 *  for a large video the upload itself can take a while, and refreshButtons()
 *  only reacts once the job comes back, leaving the button clickable the
 *  whole time otherwise. */
let submitting = false;

export async function transcribe() {
  if (submitting) return;
  const file = pickedVideo();
  if (!file) return rerunTranscription();

  submitting = true;
  $("#transcribe-btn").disabled = true;
  try {
    if (!(await confirmLanguageSelection())) return;

    const form = engineForm();
    form.append("video", file);
    setStatus(t("status.uploadingVideo"), "busy");
    const job = await api.transcribe(form);
    bindPreviewToJob(job.id);
    adoptJob(job);
    toast(t("toast.queuedForAi"), "success");
    watchJob(job.id);
  } catch (error) {
    reportError(error);
  } finally {
    submitting = false;
    if (!isProcessing()) $("#transcribe-btn").disabled = false;
  }
}

/** Recognise again using the copy the server kept, so reopening a project does
 *  not mean re-uploading a multi-hundred-megabyte file. */
export async function rerunTranscription() {
  if (submitting) return;
  const job = state.job;
  if (!job?.video_available) return toast(t("toast.pickVideoFirst"), "error");

  submitting = true;
  $("#transcribe-btn").disabled = true;
  try {
    // Language first: it is a fixable input, and the overwrite confirm should be
    // the last gate before the cues are actually thrown away.
    if (!(await confirmLanguageSelection())) return;

    if (hasCues()) {
      const confirmed = await confirmAction({
        title: t("confirm.rerunTitle"),
        target: job.video_name || "video",
        note: t("confirm.rerunNote", { count: cues().length }),
        confirmLabel: t("confirm.rerunOk"),
        cancelLabel: t("confirm.keep"),
      });
      if (!confirmed) return;
    }

    setStatus(t("status.queueingRerun"), "busy");
    const updated = await api.retranscribe(job.id, engineForm());
    adoptJob(updated);
    toast(t("toast.rerunOnStored"), "success");
    watchJob(updated.id);
  } catch (error) {
    reportError(error);
  } finally {
    submitting = false;
    if (!isProcessing()) $("#transcribe-btn").disabled = false;
  }
}

/* ── Capabilities & Options ───────────────────────────────────── */

export function syncModelOptions(preferred = "") {
  const provider = $("#transcription-provider").value;
  const select = $("#transcription-model");
  const capabilities = state.capabilities;
  const configured = provider === "deepgram" ? capabilities?.deepgram_model : capabilities?.whisper_model;
  const options = [...(capabilities?.transcription_models?.[provider] || [])];

  if (configured && !options.some((item) => item.value === configured)) {
    options.push({ value: configured, hint: { code: "model.fromEnv" } });
  }
  if (options.length) {
    select.replaceChildren(
      ...options.map((item) => {
        const option = document.createElement("option");
        option.value = item.value;
        option.textContent = optionLabel(item);
        return option;
      }),
    );
  }
  const target = preferred || configured || options[0]?.value || "";
  if ([...select.options].some((option) => option.value === target)) select.value = target;
  select.disabled = options.length === 0;
  $("#transcription-model-label").textContent = t(
    provider === "deepgram" ? "field.modelDeepgram" : "field.modelWhisper",
  );
  updateEngineChip();
  // Whether "multi" counts as unset depends on the engine, so the hint has to
  // follow the provider as well as the language.
  syncLanguageHint();
}

export function updateEngineChip() {
  const provider = $("#transcription-provider").value;
  const model = $("#transcription-model").value;
  const capabilities = state.capabilities;
  const ready =
    provider === "deepgram" ? capabilities?.deepgram_configured : capabilities?.whisper_available;
  const name = provider === "deepgram" ? "Deepgram" : "Whisper";
  $("#engine-chip").dataset.state = ready === false ? "down" : "ok";
  $("#engine-label").textContent =
    ready === false ? t("engine.notReady", { name }) : `${name} · ${model || "ready"}`;
}

/** Put the language the engine actually heard into the picker. */
export function adoptDetectedLanguage(job) {
  const sourceLang = job.source_language || job.detected_language;
  const select = $("#source-language");
  if (!sourceLang || select.value === sourceLang) return;
  if (![...select.options].some((option) => option.value === sourceLang)) return;
  select.value = sourceLang;
  // A language the run reported is not one the user confirmed here.
  acknowledgedLanguage = "";
  syncLanguageHint();
}

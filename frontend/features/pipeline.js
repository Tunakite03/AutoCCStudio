/**
 * The left sidebar: file inputs, engine capabilities, and the run/export buttons.
 * Everything that talks to the AI pipeline or produces a deliverable lives here.
 */

import { $ } from "../core/dom.js";
import { api } from "../core/api.js";
import { confirmAction } from "../core/confirm.js";
import { reportError, setStatus, toast } from "../core/feedback.js";
import { formatFileSize } from "../core/format.js";
import { optionLabel, t, tm } from "../core/i18n.js";
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
      ? t("source.videoOnServer", { name: state.job.video_name || "video" })
      : t("source.videoHint");
  $("#video-name").textContent = label;
  $("#video-drop").classList.toggle("has-file", Boolean(file) || serverVideoReady());
  $("#transcribe-label").textContent = t(
    serverVideoReady() ? "action.retranscribe" : "action.transcribe",
  );
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
  setStatus(t("status.videoLoaded"));
  refreshButtons();
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
function engineForm() {
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
function unsetLanguageReason() {
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
function syncLanguageHint() {
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

export async function transcribe() {
  const file = pickedVideo();
  if (!file) return rerunTranscription();

  if (!(await confirmLanguageSelection())) return;

  const form = engineForm();
  form.append("video", file);
  try {
    setStatus(t("status.uploadingVideo"), "busy");
    const job = await api.transcribe(form);
    bindPreviewToJob(job.id);
    adoptJob(job);
    toast(t("toast.queuedForAi"), "success");
    watchJob(job.id);
  } catch (error) {
    reportError(error);
  }
}

/** Recognise again using the copy the server kept, so reopening a project does
 *  not mean re-uploading a multi-hundred-megabyte file. */
async function rerunTranscription() {
  const job = state.job;
  if (!job?.video_available) return toast(t("toast.pickVideoFirst"), "error");

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

  try {
    setStatus(t("status.queueingRerun"), "busy");
    const updated = await api.retranscribe(job.id, engineForm());
    adoptJob(updated);
    toast(t("toast.rerunOnStored"), "success");
    watchJob(updated.id);
  } catch (error) {
    reportError(error);
  }
}

/* ── Target language guard ────────────────────────────────────── */

/** Base ISO code — "en-US" and "en" are one language for this comparison. */
const fullCode = (code) => String(code || "").trim().toLowerCase().replace("_", "-");
const baseCode = (code) => fullCode(code).split("-")[0];

/** The only options in the picker whose written form differs from the 中文
 *  (giản thể) target, so 繁體 → 中文 is a real conversion, not a no-op. */
const TRADITIONAL_CHINESE = new Set(["zh-tw", "zh-hk", "zh-hant"]);

/**
 * What the run will actually translate *from*. The engine's detected language
 * beats the picker, the same precedence the backend resolves it with
 * (backend/api/jobs.py). "" while the source is still unknown — auto-detect
 * that has not run yet says nothing about the transcript.
 */
function effectiveSourceLanguage() {
  const fromJob = state.job?.detected_language || state.job?.source_language;
  if (fromJob) return fromJob;
  const picked = $("#source-language")?.value || "";
  return picked === "auto" || picked === "multi" ? "" : picked;
}

/** Human name for a language code, borrowed from the source picker's options. */
function languageLabel(code) {
  const select = $("#source-language");
  const match = [...(select?.options || [])].find((option) => option.value === code);
  return match ? match.textContent.trim() : code;
}

/**
 * The clash worth warning about, or null. A translation into the language the
 * cues are already in still costs a provider call per batch and comes back
 * saying almost the same thing — nothing in the backend short-circuits it.
 */
function targetLanguageClash() {
  const source = effectiveSourceLanguage();
  if (!source) return null;
  const select = $("#target-language");
  const option = select?.options[select.selectedIndex];
  const target = option?.dataset.lang || "";
  if (!target || baseCode(source) !== baseCode(target)) return null;
  return {
    key: `${source}->${select.value}`,
    source,
    label: option.textContent.trim(),
    // Softer wording where the conversion is real: en-US → English changes
    // nothing, 繁體中文 → 中文 rewrites every line.
    rewritesText: TRADITIONAL_CHINESE.has(fullCode(source)),
  };
}

function syncTargetHint() {
  const node = $("#target-language-hint");
  if (!node) return;
  const clash = targetLanguageClash();
  node.style.display = clash ? "flex" : "none";
  if (!clash) return;
  $("#target-language-hint-text").textContent = clash.rewritesText
    ? t("hint.regionVariant", { source: clash.source, target: clash.label })
    : t("hint.alreadyTarget", { target: clash.label });
}

/** Answered once per source→target pair, not once per run. */
let acknowledgedTarget = "";

async function confirmTargetLanguage() {
  const clash = targetLanguageClash();
  if (!clash || clash.key === acknowledgedTarget) return true;

  const sourceName = languageLabel(clash.source);
  const confirmed = await confirmAction({
    title: t(clash.rewritesText ? "guard.sameLanguageTitle" : "guard.targetEqualsSourceTitle"),
    target: `${sourceName} → ${clash.label}`,
    note: clash.rewritesText
      ? t("guard.sameLanguageNote", { source: sourceName, target: clash.label })
      : t("guard.targetEqualsSourceNote", { target: clash.label }),
    confirmLabel: t("guard.translateAnyway"),
    cancelLabel: t("guard.changeTarget"),
    variant: "warning",
  });
  if (!confirmed) {
    $("#target-language")?.focus({ preventScroll: true });
    return false;
  }
  acknowledgedTarget = clash.key;
  return true;
}

const translatedCount = () => cues().filter((cue) => (cue.translation || "").trim()).length;

/** `fromCue` is 0-based; 0 means the whole project. Cues before it keep the
 *  translation they already carry, which is what makes a stopped run resumable
 *  instead of something you pay for twice. */
async function runTranslation(fromCue) {
  try {
    const job = await api.translate(
      state.job.id,
      $("#target-language").value,
      $("#translation-style").value,
      $("#translation-style-notes").value,
      $("#translation-provider").value,
      $("#translation-model").value,
      fromCue,
    );
    adoptJob(job, { keepSelection: true });
    toast(
      fromCue
        ? t("toast.translatingFrom", { cue: fromCue + 1 })
        : t("toast.translationQueued"),
      "success",
    );
    watchJob(job.id);
  } catch (error) {
    reportError(error);
  }
}

export async function translate() {
  if (!hasCues()) return toast(t("toast.needCuesToTranslate"), "error");

  // Before the overwrite confirm: the target language is still fixable here,
  // the translations about to be thrown away are not.
  if (!(await confirmTargetLanguage())) return;

  const done = translatedCount();
  if (done) {
    const confirmed = await confirmAction({
      title: t("confirm.retranslateTitle"),
      target: state.job.video_name || state.job.subtitle_name || t("project.thisOne"),
      note: t("confirm.retranslateNote", { count: done }),
      confirmLabel: t("confirm.retranslateOk"),
      cancelLabel: t("confirm.keep"),
    });
    if (!confirmed) return;
  }
  runTranslation(0);
}

/** Pick up where a stopped run left off, or re-do one scene onwards. */
export async function translateFromSelection() {
  if (!hasCues()) return toast(t("toast.needCuesToTranslate"), "error");
  if (state.selected < 0) return toast(t("toast.pickCueToTranslateFrom"), "error");
  if (!(await confirmTargetLanguage())) return;
  runTranslation(state.selected);
}

export async function reanalyzeSpeakers() {
  if (!hasCues()) return toast(t("toast.needCuesToAnalyze"), "error");
  try {
    const job = await api.analyzeSpeakers(state.job.id);
    adoptJob(job, { keepSelection: true });
    toast(t("toast.reanalyzing"), "success");
    watchJob(job.id);
  } catch (error) {
    reportError(error);
  }
}

/* ── Deliverables ─────────────────────────────────────────────── */

function downloadSubtitle(track, format) {
  if (!state.job) return toast(t("toast.noProject"), "error");
  const link = document.createElement("a");
  link.href = api.downloadUrl(state.job.id, track, format);
  link.click();
}

async function muxVideo() {
  if (!state.job?.video_available) return toast(t("toast.noVideoToMux"), "error");
  try {
    setStatus(t("status.muxing"), "busy");
    const blob = await api.mux(state.job.id);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${(state.job.video_name || "video").replace(/\.[^.]+$/, "")}.subtitled.mp4`;
    link.click();
    URL.revokeObjectURL(link.href);
    setStatus(t("status.muxed"));
    toast(t("toast.muxed"), "success");
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

function syncTranslationModelOptions(preferred = "") {
  const provider = $("#translation-provider").value;
  const select = $("#translation-model");
  const capabilities = state.capabilities;
  const configured =
    provider === "transformers"
      ? capabilities?.translation_model
      : capabilities?.llm_model || capabilities?.translation_model;
  const options = [...(capabilities?.translation_models?.[provider] || [])];

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
  $("#translation-model-label").textContent = t(
    provider === "transformers" ? "field.modelTransformers" : "field.modelLlm",
  );
  if (capabilities) renderCapabilityNote(capabilities);
}

function updateEngineChip() {
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

function renderCapabilityNote(capabilities) {
  const whisper = capabilities.whisper_available
    ? capabilities.whisper_model
    : t("capability.notInstalled");
  const deepgram = capabilities.deepgram_configured
    ? capabilities.deepgram_model
    : t("capability.missingKey");

  const currentTransProvider = $("#translation-provider")?.value || capabilities.translation_provider;
  const currentTransModel = $("#translation-model")?.value || capabilities.translation_model;
  const translationReady =
    currentTransProvider === "transformers"
      ? capabilities.transformers_available && Boolean(currentTransModel)
      : capabilities.llm_configured;
  // Naming the endpoint makes a mismatched model obvious before the run
  // fails: "gpt-4o @ api.mistral.ai" is visibly wrong, "gpt-4o" is not.
  const endpoint =
    currentTransProvider === "transformers" ? "" : capabilities.llm_endpoint || "";
  const translation = translationReady
    ? `${currentTransModel || currentTransProvider}${endpoint ? ` @ ${endpoint}` : ""}`
    : t("capability.providerNotConfigured", { provider: currentTransProvider });

  const speakerAnalysis = $("#speaker-analysis");
  speakerAnalysis.disabled = !capabilities.speaker_analysis_configured;
  if (!capabilities.speaker_analysis_configured) speakerAnalysis.checked = false;
  const dialogueAI = capabilities.speaker_analysis_configured
    ? capabilities.speaker_analysis_model
    : t("capability.notConfigured");
  $("#capability-note").textContent = t("capability.note", {
    deepgram,
    whisper,
    dialogue: dialogueAI,
    translation,
    ffmpeg: t(capabilities.ffmpeg ? "capability.ok" : "capability.missing"),
  });
}

/** The style presets live in the backend, so the picker is built from them. */
function syncStyleOptions(capabilities) {
  const options = capabilities?.translation_styles || [];
  if (!options.length) return;
  const select = $("#translation-style");
  const preferred = select.value;
  select.replaceChildren(
    ...options.map((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = tm({ code: item.label_code });
      return option;
    }),
  );
  if ([...select.options].some((option) => option.value === preferred)) {
    select.value = preferred;
  }
}

/** Show a reopened project the style, provider and model it was actually translated with. */
function restoreTranslationFromJob() {
  const job = state.job;
  if (!job) return;
  const styleSelect = $("#translation-style");
  if (job.translation_style && [...styleSelect.options].some((o) => o.value === job.translation_style)) {
    styleSelect.value = job.translation_style;
  }
  if (typeof job.translation_style_notes === "string") {
    $("#translation-style-notes").value = job.translation_style_notes;
  }
  const providerSelect = $("#translation-provider");
  if (job.translation_provider && [...providerSelect.options].some((o) => o.value === job.translation_provider)) {
    providerSelect.value = job.translation_provider;
    syncTranslationModelOptions(job.translation_model || "");
  } else if (job.translation_model) {
    syncTranslationModelOptions(job.translation_model);
  }
  const sourceLang = job.source_language || job.detected_language;
  const sourceLangSelect = $("#source-language");
  if (sourceLang && [...sourceLangSelect.options].some((o) => o.value === sourceLang)) {
    sourceLangSelect.value = sourceLang;
    // Reopening a project restores a language the user never confirmed here.
    acknowledgedLanguage = "";
  }
  // A reopened project brings its own detected language, so the clash the user
  // acknowledged on the previous project says nothing about this one.
  acknowledgedTarget = "";
  syncLanguageHint();
  syncTargetHint();
}

export async function loadCapabilities() {
  try {
    const capabilities = await api.capabilities();
    setCapabilities(capabilities);
    const providerSelect = $("#transcription-provider");
    if ($(`#transcription-provider option[value="${capabilities.transcription_provider}"]`)) {
      providerSelect.value = capabilities.transcription_provider;
    }
    const transProviderSelect = $("#translation-provider");
    if ($(`#translation-provider option[value="${capabilities.translation_provider}"]`)) {
      transProviderSelect.value = capabilities.translation_provider;
    }
    syncModelOptions();
    syncTranslationModelOptions();
    syncStyleOptions(capabilities);
    restoreTranslationFromJob();
    renderCapabilityNote(capabilities);
  } catch {
    $("#capability-note").textContent = t("capability.unreadable");
    $("#engine-chip").dataset.state = "down";
    $("#engine-label").textContent = t("engine.backendSilent");
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
  // A project that already has translations is never translated again, it is
  // *re*-translated — and that word is the warning the confirm then spells out.
  $("#translate-label").textContent = t(
    translatedCount() ? "action.retranslateAll" : "action.translateAll",
  );
  $("#translate-from-btn").disabled = blocked || !cued || state.selected < 0;
  $("#translate-from-label").textContent =
    state.selected >= 0
      ? t("action.translateFromCue", { cue: state.selected + 1 })
      : t("action.translateFrom");
  $("#download-source-srt").disabled = !cued;
  $("#download-translated-srt").disabled = !cued;
  $("#download-vtt").disabled = !cued;
  $("#mux-btn").disabled = !cued || !state.job?.video_available;
  // The clash only becomes knowable once a run has detected the source language,
  // so the hint has to follow the job, not just the pickers.
  syncTargetHint();
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
  $("#translate-from-btn").addEventListener("click", translateFromSelection);
  $("#transcription-provider").addEventListener("change", () => {
    // A different engine is a different decision — ask again.
    acknowledgedLanguage = "";
    syncModelOptions();
  });
  $("#source-language").addEventListener("change", () => {
    acknowledgedLanguage = "";
    syncLanguageHint();
    // A project with no detected language yet takes its source from this picker.
    syncTargetHint();
  });
  $("#target-language").addEventListener("change", () => {
    acknowledgedTarget = "";
    syncTargetHint();
  });
  $("#transcription-model").addEventListener("change", updateEngineChip);
  $("#translation-provider").addEventListener("change", () => syncTranslationModelOptions());
  $("#translation-model").addEventListener("change", () => {
    if (state.capabilities) renderCapabilityNote(state.capabilities);
  });

  $("#download-source-srt").addEventListener("click", () => downloadSubtitle("source", "srt"));
  $("#download-translated-srt").addEventListener("click", () => downloadSubtitle("translated", "srt"));
  $("#download-vtt").addEventListener("click", () => downloadSubtitle("translated", "vtt"));
  $("#mux-btn").addEventListener("click", muxVideo);

  on("job:loaded", restoreTranslationFromJob);
  // selection:changed too — the resume button names the cue it would start from.
  onAny(["job:loaded", "cues:changed", "selection:changed"], refreshButtons);
  on("capabilities:loaded", refreshButtons);
  refreshButtons();
  syncLanguageHint();
}

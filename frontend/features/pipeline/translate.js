/**
 * Pipeline — AI Translation and Speaker Re-analysis.
 *
 * Manages target language selection, dialect & source clash guards, full and
 * partial (from selection) translation requests, and translation model options.
 */

import { $ } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { confirmAction } from "../../core/confirm.js";
import { reportError, setStatus, toast } from "../../core/feedback.js";
import { optionLabel, t } from "../../core/i18n.js";
import { cues, hasCues, state } from "../../core/store.js";
import { adoptJob, watchJob } from "../jobs.js";
import {
  SAVED_PREFIX,
  refreshStyleButtons,
  savedStyleById,
  selectedSavedStyle,
  setAppliedStyleNotes,
  setLastStyleValue,
  styleRequest,
} from "./presets.js";
import { adoptDetectedLanguage } from "./transcribe.js";

/* ── Target language guard ────────────────────────────────────── */

/** Base ISO code — "en-US" and "en" are one language for this comparison. */
export const fullCode = (code) => String(code || "").trim().toLowerCase().replace("_", "-");
export const baseCode = (code) => fullCode(code).split("-")[0];

/** The only options in the picker whose written form differs from the 中文
 *  (giản thể) target, so 繁體 → 中文 is a real conversion, not a no-op. */
export const TRADITIONAL_CHINESE = new Set(["zh-tw", "zh-hk", "zh-hant"]);

/**
 * What the run will actually translate *from*. The engine's detected language
 * beats the picker, the same precedence the backend resolves it with
 * (backend/api/jobs.py). "" while the source is still unknown — auto-detect
 * that has not run yet says nothing about the transcript.
 */
export function effectiveSourceLanguage() {
  const fromJob = state.job?.detected_language || state.job?.source_language;
  if (fromJob) return fromJob;
  const picked = $("#source-language")?.value || "";
  return picked === "auto" || picked === "multi" ? "" : picked;
}

/** Human name for a language code, borrowed from the source picker's options. */
export function languageLabel(code) {
  const select = $("#source-language");
  const match = [...(select?.options || [])].find((option) => option.value === code);
  return match ? match.textContent.trim() : code;
}

/**
 * The clash worth warning about, or null. A translation into the language the
 * cues are already in still costs a provider call per batch and comes back
 * saying almost the same thing — nothing in the backend short-circuits it.
 */
export function targetLanguageClash() {
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

export function syncTargetHint() {
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

export function resetAcknowledgedTarget() {
  acknowledgedTarget = "";
}

export async function confirmTargetLanguage() {
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

export const translatedCount = () => cues().filter((cue) => (cue.translation || "").trim()).length;

/** `fromCue` is 0-based; 0 means the whole project. Cues before it keep the
 *  translation they already carry, which is what makes a stopped run resumable
 *  instead of something you pay for twice. */
export async function runTranslation(fromCue) {
  try {
    const { style, notes, ref } = styleRequest();
    const job = await api.translate(
      state.job.id,
      $("#target-language").value,
      style,
      notes,
      $("#translation-provider").value,
      $("#translation-model").value,
      fromCue,
      ref,
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

/* ── Translation Model Options ────────────────────────────────── */

export function syncTranslationModelOptions(preferred = "", onRenderCapabilityNote) {
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
  if (capabilities && onRenderCapabilityNote) onRenderCapabilityNote(capabilities);
}

// The last project these controls were filled in from. `job:loaded` fires on
// every progress tick as well as on opening a project, and re-applying a running
// job's settings each tick pulled the style picker and the rules box out from
// under anyone who was in the middle of changing them.
let restoredJobId = null;

/**
 * Show a reopened project the style, provider and model it was actually
 * translated with — once per project, not once per tick.
 *
 * `force` is for the boot race: the project can be adopted before the pickers
 * have their options, and the second pass has to be allowed to finish the job.
 */
export function restoreTranslationFromJob({ force = false } = {}) {
  const job = state.job;
  if (!job) return;

  // Not gated: recognition reports the language it heard partway through a run,
  // and the picker has to follow it the moment it lands.
  adoptDetectedLanguage(job);
  if (!force && job.id === restoredJobId) return;
  restoredJobId = job.id;

  const styleSelect = $("#translation-style");
  // The saved style it was translated with, when that style is still around;
  // otherwise the preset underneath, which is what actually did the work.
  const savedKey = job.translation_style_ref && savedStyleById(job.translation_style_ref)
    ? SAVED_PREFIX + job.translation_style_ref
    : "";
  const wanted = savedKey || job.translation_style;
  if (wanted && [...styleSelect.options].some((o) => o.value === wanted)) {
    styleSelect.value = wanted;
  }
  if (typeof job.translation_style_notes === "string") {
    $("#translation-style-notes").value = job.translation_style_notes;
  }
  // Rules that still match the style they came from may be replaced silently by
  // the next pick; anything else is treated as hand-written and is asked about.
  const saved = selectedSavedStyle();
  setAppliedStyleNotes(
    saved && saved.notes.trim() === (job.translation_style_notes || "").trim()
      ? saved.notes
      : null,
  );
  setLastStyleValue(styleSelect.value);
  refreshStyleButtons();
  const providerSelect = $("#translation-provider");
  if (job.translation_provider && [...providerSelect.options].some((o) => o.value === job.translation_provider)) {
    providerSelect.value = job.translation_provider;
    syncTranslationModelOptions(job.translation_model || "");
  } else if (job.translation_model) {
    syncTranslationModelOptions(job.translation_model);
  }
  // A reopened project brings its own detected language, so the clash the user
  // acknowledged on the previous project says nothing about this one.
  acknowledgedTarget = "";
  syncTargetHint();
}

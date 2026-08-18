/**
 * Pipeline feature coordinator.
 *
 * Coordinates file ingestion, speech recognition, translation, dubbing,
 * and media export. Assembles listeners for the left sidebar controls.
 */

import { $ } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { t } from "../../core/i18n.js";
import { hasCues, isProcessing, on, onAny, setCapabilities, state } from "../../core/store.js";
import {
  acceptVideoFile as acceptVideoFileSource,
  adoptDetectedLanguage,
  importSubtitleFile,
  pickedVideo,
  renderSourceSlot,
  rerunTranscription,
  resetAcknowledgedLanguage,
  syncLanguageHint,
  syncModelOptions,
  transcribe,
  updateEngineChip,
} from "./transcribe.js";
import {
  confirmTargetLanguage,
  reanalyzeSpeakers,
  resetAcknowledgedTarget,
  restoreTranslationFromJob,
  syncTargetHint,
  syncTranslationModelOptions,
  translate,
  translateFromSelection,
  translatedCount,
} from "./translate.js";
import {
  applySelectedStyle,
  deleteStyle,
  loadSavedStyles,
  saveStyle,
  syncStyleOptions,
} from "./presets.js";
import {
  dubProject,
  renderDubState,
  renderGainValue,
  syncVoiceOptions,
} from "./dubbing.js";
import { downloadSubtitle, muxVideo } from "./export.js";

/* Re-exports for public consumption */
export {
  importSubtitleFile,
  transcribe,
  rerunTranscription,
  translate,
  translateFromSelection,
  reanalyzeSpeakers,
  dubProject,
};

export function acceptVideoFile(file) {
  acceptVideoFileSource(file, refreshButtons);
}

/* ── Capability Presentation ─────────────────────────────────── */

export function renderCapabilityNote(capabilities) {
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
  const dub = capabilities.dubbing_configured
    ? capabilities.tts_voice || capabilities.tts_provider
    : t("capability.notConfigured");
  $("#capability-note").textContent = t("capability.note", {
    deepgram,
    whisper,
    dialogue: dialogueAI,
    translation,
    dub,
    ffmpeg: t(capabilities.ffmpeg ? "capability.ok" : "capability.missing"),
  });
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
    syncTranslationModelOptions("", renderCapabilityNote);
    syncStyleOptions(capabilities);
    await loadSavedStyles();
    syncVoiceOptions(state.job?.dubbing_voice);
    if (typeof capabilities.dub_original_gain === "number") {
      $("#dub-gain").value = String(Math.round(capabilities.dub_original_gain * 100));
    }
    $("#dub-shorten").checked = Boolean(capabilities.dub_shorten_with_llm);
    $("#dub-shorten").disabled = !capabilities.dub_shorten_with_llm;
    renderGainValue();
    restoreTranslationFromJob({ force: true });
    renderCapabilityNote(capabilities);
  } catch {
    $("#capability-note").textContent = t("capability.unreadable");
    $("#engine-chip").dataset.state = "down";
    $("#engine-label").textContent = t("engine.backendSilent");
  }
}

/* ── Availability ─────────────────────────────────────────────── */

export function refreshButtons() {
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
  $("#dub-btn").disabled = blocked || !cued || !state.capabilities?.dubbing_configured;
  // Same wording rule as translation: a project that already has a dub is never
  // dubbed again, it is *re*-dubbed, and the confirm says what that costs.
  $("#dub-label").textContent = t(
    state.job?.dub_audio_available ? "action.redub" : "action.dub",
  );
  $("#mux-dub-btn").disabled = !state.job?.dub_audio_available || !state.job?.video_available;
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
    resetAcknowledgedLanguage();
    syncModelOptions();
  });
  $("#source-language").addEventListener("change", () => {
    resetAcknowledgedLanguage();
    syncLanguageHint();
    // A project with no detected language yet takes its source from this picker.
    syncTargetHint();
  });
  $("#target-language").addEventListener("change", () => {
    resetAcknowledgedTarget();
    syncTargetHint();
  });
  $("#transcription-model").addEventListener("change", updateEngineChip);
  $("#translation-provider").addEventListener("change", () =>
    syncTranslationModelOptions("", renderCapabilityNote),
  );
  $("#translation-style").addEventListener("change", applySelectedStyle);
  $("#style-save").addEventListener("click", saveStyle);
  $("#style-delete").addEventListener("click", deleteStyle);
  $("#translation-model").addEventListener("change", () => {
    if (state.capabilities) renderCapabilityNote(state.capabilities);
  });

  $("#dub-btn").addEventListener("click", dubProject);
  $("#dub-gain").addEventListener("input", renderGainValue);

  $("#download-source-srt").addEventListener("click", () => downloadSubtitle("source", "srt"));
  $("#download-translated-srt").addEventListener("click", () => downloadSubtitle("translated", "srt"));
  $("#download-vtt").addEventListener("click", () => downloadSubtitle("translated", "vtt"));
  $("#mux-btn").addEventListener("click", () => muxVideo("original"));
  $("#mux-dub-btn").addEventListener("click", () =>
    muxVideo($("#dub-keep-original").checked ? "both" : "dubbed"),
  );

  on("job:loaded", () => restoreTranslationFromJob());
  // Every SSE tick re-adopts the job, so this is also how the preview appears
  // the moment a running dub finishes.
  on("job:loaded", renderDubState);
  // selection:changed too — the resume button names the cue it would start from.
  onAny(["job:loaded", "cues:changed", "selection:changed"], refreshButtons);
  on("capabilities:loaded", refreshButtons);
  refreshButtons();
  syncLanguageHint();
}

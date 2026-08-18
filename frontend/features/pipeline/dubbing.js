/**
 * Pipeline — Voice Synthesis and AI Dubbing.
 *
 * Manages TTS voice options, dubbing parameters (gain, shortening), dub audio
 * streaming / preview attachment, and fitting report presentation.
 */

import { $ } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { confirmAction } from "../../core/confirm.js";
import { reportError, toast } from "../../core/feedback.js";
import { optionLabel, t, tm } from "../../core/i18n.js";
import { hasCues, state } from "../../core/store.js";
import { adoptJob, watchJob } from "../jobs.js";
import { setDubTrack } from "../transport.js";

export function dubOptions() {
  return {
    voice: $("#dub-voice").value,
    provider: state.capabilities?.tts_provider || "",
    originalGain: Number($("#dub-gain").value) / 100,
    shorten: $("#dub-shorten").checked,
  };
}

export async function dubProject() {
  if (!hasCues()) return toast(t("toast.needCuesToDub"), "error");
  if (!state.capabilities?.dubbing_configured) {
    return toast(t("toast.dubNotConfigured"), "error");
  }

  // Re-dubbing throws away a render that took as long as the video is: worth a
  // confirm, exactly like re-translating is.
  if (state.job?.dub_audio_available) {
    const confirmed = await confirmAction({
      title: t("confirm.redubTitle"),
      target: state.job.video_name || state.job.subtitle_name || t("project.thisOne"),
      note: t("confirm.redubNote"),
      confirmLabel: t("confirm.redubOk"),
      cancelLabel: t("confirm.keep"),
    });
    if (!confirmed) return;
  }

  try {
    const job = await api.dub(state.job.id, dubOptions());
    adoptJob(job, { keepSelection: true });
    toast(t("toast.dubQueued"), "success");
    watchJob(job.id, "dubbing");
  } catch (error) {
    reportError(error);
  }
}

/** How the run went, in the terms the fitting stage actually works in. */
export function dubReportText(report) {
  const fits = report.fits || {};
  let text = t("dub.report", {
    voiced: report.voiced_cues ?? 0,
    total: report.total_cues ?? 0,
  });
  text += t("dub.reportFits", {
    spedUp: fits.sped_up || 0,
    spill: fits.spill || 0,
    shortened: report.shortened_cues || 0,
  });
  if (fits.overflow) text += t("dub.reportOverflow", { count: fits.overflow });
  return text;
}

/** Point the preview player at this project's dub, or hide it when there is none. */
export function renderDubState() {
  const job = state.job;
  const available = Boolean(job?.dub_audio_available);
  $("#dub-preview-wrap").hidden = !available;
  setDubTrack(available ? api.dubAudioUrl(job.id, job.revision) : null);

  if (job?.dubbing_voice && [...$("#dub-voice").options].some((o) => o.value === job.dubbing_voice)) {
    $("#dub-voice").value = job.dubbing_voice;
  }
  // A failure, a partial run or an outdated take is reported here rather than in
  // the run banner: they are about the dub, and the banner is gone by the time
  // anyone reads it. Staleness comes first — it is the one the user can act on
  // right now, and a run report next to it would only say the opposite.
  const note = $("#dub-report");
  const stale = Boolean(job?.dub_stale);
  const failed = job?.dubbing_status === "failed" || job?.dubbing_status === "partial";
  const report = job?.dubbing_report;
  note.classList.toggle("text-crit", stale || failed);
  note.textContent = stale
    ? t("dub.stale")
    : failed
      ? tm(job.dubbing_error, "job.dubFailed")
      : report
        ? dubReportText(report)
        : "";
}

/** The voices the configured provider offers. Built from capabilities, like the models. */
export function syncVoiceOptions(preferred = "") {
  const options = state.capabilities?.tts_voices || [];
  const select = $("#dub-voice");
  if (!options.length) {
    select.replaceChildren();
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.replaceChildren(
    ...options.map((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = optionLabel(item);
      return option;
    }),
  );
  const wanted = preferred || state.capabilities?.tts_voice || "";
  if ([...select.options].some((option) => option.value === wanted)) select.value = wanted;
}

export function renderGainValue() {
  $("#dub-gain-value").textContent = t("dub.gainValue", { percent: $("#dub-gain").value });
}

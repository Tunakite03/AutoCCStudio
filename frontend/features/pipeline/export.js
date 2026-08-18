/**
 * Pipeline — Deliverables and Media Export.
 *
 * Manages subtitle file downloads (SRT / VTT for source or translated tracks),
 * and MP4 muxing with subtitles or dubbed voiceover tracks.
 */

import { $ } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { confirmAction } from "../../core/confirm.js";
import { reportError, setStatus, toast } from "../../core/feedback.js";
import { t } from "../../core/i18n.js";
import { state } from "../../core/store.js";

export function downloadSubtitle(track, format) {
  if (!state.job) return toast(t("toast.noProject"), "error");
  const link = document.createElement("a");
  link.href = api.downloadUrl(state.job.id, track, format);
  link.click();
}

/** `audio` is original | dubbed | both — the same render, a different soundtrack. */
export async function muxVideo(audio = "original") {
  if (!state.job?.video_available) return toast(t("toast.noVideoToMux"), "error");
  const dubbed = audio !== "original";
  if (dubbed && !state.job?.dub_audio_available) return toast(t("toast.noDubYet"), "error");

  // Exporting a dub made before the last round of cue edits is the one way this
  // feature can hand someone a wrong file without ever looking broken.
  if (dubbed && state.job?.dub_stale) {
    const confirmed = await confirmAction({
      title: t("confirm.staleDubTitle"),
      target: state.job.video_name || state.job.subtitle_name || t("project.thisOne"),
      note: t("confirm.staleDubNote"),
      confirmLabel: t("confirm.staleDubOk"),
      cancelLabel: t("confirm.keep"),
    });
    if (!confirmed) return;
  }

  try {
    setStatus(t(dubbed ? "status.dubExporting" : "status.muxing"), "busy");
    const blob = await api.mux(state.job.id, audio);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    const stem = (state.job.video_name || "video").replace(/\.[^.]+$/, "");
    link.download = `${stem}.${dubbed ? "dubbed" : "subtitled"}.mp4`;
    link.click();
    URL.revokeObjectURL(link.href);
    setStatus(t(dubbed ? "toast.dubExported" : "status.muxed"));
    toast(t(dubbed ? "toast.dubExported" : "toast.muxed"), "success");
  } catch (error) {
    reportError(error);
  }
}

/**
 * Owns the video element: playback, playhead time, caption overlay.
 *
 * Time is the one value that changes 60 times a second, so it is kept here as a
 * plain variable rather than in the store — views get it through `time:changed`.
 */

import { $, $$, element } from "../core/dom.js";
import { clamp, formatTimecode } from "../core/format.js";
import { cues, emit, on, onAny, setActiveCue, state } from "../core/store.js";
import { toast } from "../core/feedback.js";

const player = $("#player");
const viewer = $("#viewer");
const overlay = $("#caption-overlay");
const app = $(".app");

const local = {
  ready: false,
  virtualTime: 0,
  captionTrack: "both",
  frameStep: 1 / 25,
  previewUrl: null,
  previewJobId: null,
  resumeAfterScrub: false,
};

/* ── Time ─────────────────────────────────────────────────────── */

export function duration() {
  if (local.ready && Number.isFinite(player.duration) && player.duration > 0) return player.duration;
  const list = cues();
  if (!list.length) return 30;
  return Math.max(list[list.length - 1].end + 5, 10);
}

export const currentTime = () => (local.ready ? player.currentTime : local.virtualTime);
export const isVideoReady = () => local.ready;
export const isPlaying = () => local.ready && !player.paused;
export const frameStep = () => local.frameStep;

export function seek(time) {
  const target = clamp(time, 0, duration());
  if (local.ready) player.currentTime = target;
  else local.virtualTime = target;
  publishTime();
}

function publishTime() {
  const time = currentTime();
  $("#tc-current").textContent = formatTimecode(time);
  emit("time:changed", { time });
  refreshActiveCue();
}

/** Re-resolve which cue sits under the playhead — call after cues shift. */
export function refreshActiveCue() {
  const time = currentTime();
  setActiveCue(cues().findIndex((cue) => time >= cue.start && time < cue.end));
}

/* ── Playback ─────────────────────────────────────────────────── */

let rafId = 0;

function tick() {
  publishTime();
  rafId = player.paused ? 0 : requestAnimationFrame(tick);
}

export function togglePlay() {
  if (!local.ready) return toast("Project này chưa có video để phát", "error");
  // Mid-scrub the transport still reads "playing", so a click here means stop —
  // the element is already paused, all that's left is to cancel the resume.
  if (local.resumeAfterScrub) {
    local.resumeAfterScrub = false;
    app.classList.remove("is-playing");
    return;
  }
  if (player.paused) player.play().catch((error) => toast(error.message, "error"));
  else player.pause();
}

/* Scrubbing holds playback rather than ending it: drag to find the frame, let go
   and the video picks up from where you dropped the playhead. */
export function holdPlayback() {
  if (!local.ready || player.paused) return;
  local.resumeAfterScrub = true;
  player.pause();
}

export function releasePlayback() {
  if (!local.resumeAfterScrub) return;
  local.resumeAfterScrub = false;
  player.play().catch(() => {
    /* the user may have switched projects mid-drag */
  });
}

/* ── Sources ──────────────────────────────────────────────────── */

/** Show a picked file before any upload — the preview should never wait on the AI. */
export function showLocalPreview(file) {
  if (local.previewUrl) URL.revokeObjectURL(local.previewUrl);
  local.previewUrl = URL.createObjectURL(file);
  local.previewJobId = null;
  local.ready = false;
  player.src = local.previewUrl;
  player.load();
}

/** Tie the preview already on screen to the job that was just created from it. */
export const claimPreviewFor = (jobId) => {
  local.previewJobId = jobId;
};

export const hasLocalPreview = () => Boolean(local.previewUrl);

export function loadJobVideo(job, videoUrl) {
  // The local preview already shows this exact file — reloading it from the
  // server would only throw away a working player and the current position.
  if (local.previewUrl && local.previewJobId === job.id) return;
  if (!job.video_available && local.previewUrl) return;

  local.ready = false;
  local.virtualTime = 0;
  viewer.classList.remove("has-video");
  if (job.video_available) {
    player.src = videoUrl;
  } else {
    player.removeAttribute("src");
    player.load();
  }
}

/** Drop whatever is loaded — used when the open project is deleted. */
export function clearVideo() {
  if (local.previewUrl) URL.revokeObjectURL(local.previewUrl);
  local.previewUrl = null;
  local.previewJobId = null;
  local.ready = false;
  local.virtualTime = 0;
  player.pause();
  player.removeAttribute("src");
  player.load();
  viewer.classList.remove("has-video");
  $("#tc-total").textContent = `/ ${formatTimecode(0)}`;
  publishTime();
}

/* ── Caption overlay ──────────────────────────────────────────── */

/** Match the caption box to the letterboxed picture, not the whole stage. */
function syncFrameGeometry() {
  const { videoWidth, videoHeight } = player;
  const box = viewer.getBoundingClientRect();
  if (!videoWidth || !videoHeight || !box.width) return;
  const scale = Math.min(box.width / videoWidth, box.height / videoHeight);
  const width = videoWidth * scale;
  const height = videoHeight * scale;
  overlay.style.setProperty("--frame-width", `${Math.round(width)}px`);
  overlay.style.setProperty("--frame-bottom", `${Math.round((box.height - height) / 2 + height * 0.05)}px`);
  overlay.style.setProperty("--caption-size", `${clamp(width * 0.045, 13, 30).toFixed(1)}px`);
}

/* Font size stays in CSS — it tracks the letterboxed picture through
   --caption-size, which resizeOverlay() writes on every layout change. */
const CAPTION_LINE =
  "caption-line px-3 py-[3px] rounded-[4px] bg-[rgba(0,0,0,0.72)] font-semibold leading-[1.35] " +
  "whitespace-pre-line [text-shadow:0_1px_3px_rgba(0,0,0,0.8)]";

export function renderOverlay() {
  const cue = cues()[state.activeCue];
  overlay.dataset.track = local.captionTrack;
  overlay.replaceChildren();
  if (!cue || local.captionTrack === "off") return;

  const wants = (track) => local.captionTrack === "both" || local.captionTrack === track;
  if (wants("source") && cue.text) {
    overlay.appendChild(element("div", `${CAPTION_LINE} source text-white`, cue.text));
  }
  if (wants("translation") && cue.translation) {
    overlay.appendChild(element("div", `${CAPTION_LINE} translated text-[#f6d47a]`, cue.translation));
  }
}

/* ── Mount ────────────────────────────────────────────────────── */

export function mountTransport() {
  $("#play-btn").addEventListener("click", togglePlay);
  $("#step-back").addEventListener("click", () => seek(currentTime() - local.frameStep));
  $("#step-fwd").addEventListener("click", () => seek(currentTime() + local.frameStep));

  $("#mute-btn").addEventListener("click", () => {
    player.muted = !player.muted;
    app.classList.toggle("is-muted", player.muted);
  });

  $("#playback-rate").addEventListener("change", (event) => {
    player.playbackRate = Number(event.target.value);
  });

  $$(".seg").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.classList.contains("is-active")));
    button.addEventListener("click", () => {
      $$(".seg").forEach((item) => {
        item.classList.remove("is-active");
        item.setAttribute("aria-pressed", "false");
      });
      button.classList.add("is-active");
      button.setAttribute("aria-pressed", "true");
      local.captionTrack = button.dataset.track;
      renderOverlay();
    });
  });

  player.addEventListener("loadedmetadata", () => {
    local.ready = true;
    viewer.classList.add("has-video");
    $("#tc-total").textContent = `/ ${formatTimecode(player.duration)}`;
    syncFrameGeometry();
    emit("video:ready", { duration: player.duration });
  });

  player.addEventListener("error", () => {
    if (!player.getAttribute("src")) return;
    local.ready = false;
    viewer.classList.remove("has-video");
    toast("Trình duyệt không phát được codec của video này. Timeline vẫn dùng được.", "error");
  });

  player.addEventListener("play", () => {
    app.classList.add("is-playing");
    if (!rafId) rafId = requestAnimationFrame(tick);
  });

  player.addEventListener("pause", () => {
    // A scrub hold is not a stop — leave the transport reading as playing so the
    // button doesn't flicker between play and pause on every drag.
    if (!local.resumeAfterScrub) app.classList.remove("is-playing");
    cancelAnimationFrame(rafId);
    rafId = 0;
    publishTime();
  });

  player.addEventListener("seeked", publishTime);
  new ResizeObserver(syncFrameGeometry).observe(viewer);

  // The overlay shows whatever cue is live, and follows edits to that cue.
  on("active-cue:changed", renderOverlay);
  on("cue:patched", ({ index }) => {
    if (index === state.activeCue) renderOverlay();
  });
  onAny(["cues:changed", "job:loaded"], () => {
    refreshActiveCue();
    renderOverlay();
  });

  $("#tc-total").textContent = `/ ${formatTimecode(0)}`;
}

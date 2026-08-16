/**
 * Binds the timeline engine to app state.
 *
 * The engine in lib/ knows nothing about jobs, saving or undo — it asks for cues
 * through callbacks and reports gestures back. That separation is what makes it
 * reusable, and what would let it drop into a React tree unchanged.
 */

import { $ } from "../core/dom.js";
import { on, state } from "../core/store.js";
import { MAX_PPS, MIN_PPS, createTimeline } from "../lib/timeline-engine.js";
import { selectCue } from "./cuelist.js";
import { nextGestureKey, updateCueTimes } from "./editing.js";
import { previewTimes } from "./inspector.js";
import {
  currentTime,
  duration,
  holdPlayback,
  isPlaying,
  releasePlayback,
  seek,
} from "./transport.js";

let engine = null;

/** Safe façade: other modules may call these before the engine is mounted. */
export const timeline = {
  render: () => engine?.render(),
  repaint: () => engine?.repaint(),
  fit: () => engine?.fit(),
  resolvePendingFit: () => engine?.resolvePendingFit(),
  zoomBy: (factor) => engine?.zoomBy(factor),
  setPps: (value) => engine?.setPps(value),
  setSnap: (value) => engine?.setSnap(value),
  setFollow: (value) => engine?.setFollow(value),
  setWaveform: (data) => engine?.setWaveform(data),
  scrollToTime: (time) => engine?.scrollToTime(time),
};

export function setWaveformNote(text) {
  $("#wave-note").textContent = text;
}

const zoomToSlider = (pps) =>
  String(Math.round((100 * Math.log(pps / MIN_PPS)) / Math.log(MAX_PPS / MIN_PPS)));

export function mountTimelineView() {
  engine = createTimeline({
    scroller: $("#timeline-scroll"),
    canvas: $("#timeline-canvas"),
    rulerCanvas: $("#ruler-canvas"),
    waveCanvas: $("#wave-canvas"),
    waveNote: $("#wave-note"),
    clipLane: $("#clip-lane"),
    playhead: $("#playhead"),
    getCues: () => state.job?.cues ?? [],
    getDuration: duration,
    getTime: currentTime,
    getSelected: () => state.selected,
    onSeek: seek,
    onSelect: (index) => selectCue(index, { source: "timeline" }),
    onCommit: (index, times, mode) =>
      updateCueTimes(
        index,
        times,
        nextGestureKey(),
        `${mode === "move" ? "dời" : "co giãn"} cue ${index + 1}`,
      ),
    onDragTime: previewTimes,
    onZoom: (pps) => {
      $("#status-zoom").textContent = `${Math.round(pps)} px/s`;
      $("#zoom-range").value = zoomToSlider(pps);
    },
    onScrubStart: holdPlayback,
    onScrubEnd: releasePlayback,
    isPlaying,
  });

  /* Toolbar */
  const toggle = (button, apply) => {
    button.addEventListener("click", () => {
      const active = !button.classList.contains("is-active");
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      apply(active);
    });
  };
  toggle($("#snap-btn"), (value) => engine.setSnap(value));
  toggle($("#follow-btn"), (value) => engine.setFollow(value));

  $("#zoom-in").addEventListener("click", () => engine.zoomBy(1.35));
  $("#zoom-out").addEventListener("click", () => engine.zoomBy(1 / 1.35));
  $("#zoom-fit").addEventListener("click", () => engine.fit());
  $("#zoom-range").addEventListener("input", (event) => {
    engine.setPps(MIN_PPS * (MAX_PPS / MIN_PPS) ** (Number(event.target.value) / 100));
  });

  /* State */
  on("job:loaded", () => engine.render());
  on("cues:changed", () => engine.render());
  on("cue:patched", ({ index }) => engine.patch(index));
  on("time:changed", () => engine.renderPlayhead());
  on("selection:changed", ({ index, source }) => {
    engine.refreshSelection();
    // Don't yank the view when the click came from the timeline itself.
    if (source !== "timeline" && index >= 0) engine.scrollToTime(state.job.cues[index].start);
  });
  on("video:ready", () => {
    engine.render();
    engine.fit();
  });
  // Canvases measure zero while the editor is hidden — re-measure on return, and
  // finish any fit that was requested (video loaded) while it was off screen.
  on("screen:changed", ({ name }) => {
    if (name !== "editor") return;
    engine.render();
    engine.resolvePendingFit();
  });

  window.addEventListener("resize", () => engine.render());

  engine.render();
  $("#status-zoom").textContent = `${Math.round(engine.pps)} px/s`;
  $("#zoom-range").value = zoomToSlider(engine.pps);
}

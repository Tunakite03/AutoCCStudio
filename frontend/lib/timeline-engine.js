/**
 * Timeline engine: ruler, waveform, draggable/trimmable subtitle clips, playhead.
 * Owns no application state — the host passes accessors and receives commits.
 */

import { capturePointer as capture, releasePointer as release } from "../core/dom.js";
import {
  MIN_CUE_DURATION,
  charsPerSecond,
  clamp,
  cpsSeverity,
  cssVar,
  cueDuration,
  formatRulerLabel,
  shortTimecode,
} from "../core/format.js";

const MIN_PPS = 4;
const MAX_PPS = 400;
const SNAP_PIXELS = 7;
const TICK_STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800];

export function createTimeline({
  scroller,
  canvas,
  rulerCanvas,
  waveCanvas,
  waveNote,
  clipLane,
  playhead,
  getCues,
  getDuration,
  getTime,
  getSelected,
  onSeek,
  onSelect,
  onCommit,
  onDragTime,
  onZoom,
  onScrubStart,
  onScrubEnd,
  isPlaying = () => false,
}) {
  let pps = 60;
  let snapEnabled = true;
  let followPlayhead = true;
  let waveform = null;
  let clips = [];
  let snapGuide = null;
  let frameQueued = false;
  let pendingFit = false;

  const rulerCtx = rulerCanvas.getContext("2d");
  const waveCtx = waveCanvas.getContext("2d");

  const timeToX = (time) => time * pps;
  const xToTime = (x) => x / pps;

  function totalWidth() {
    return Math.max(scroller.clientWidth, getDuration() * pps + 48);
  }

  function tickStep() {
    return TICK_STEPS.find((step) => step * pps >= 74) ?? TICK_STEPS[TICK_STEPS.length - 1];
  }

  /* ── Painting ─────────────────────────────────────────────── */

  function sizeCanvas(element, context, height) {
    const dpr = window.devicePixelRatio || 1;
    const width = scroller.clientWidth;
    element.style.width = `${width}px`;
    element.style.height = `${height}px`;
    element.width = Math.max(1, Math.round(width * dpr));
    element.height = Math.max(1, Math.round(height * dpr));
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { width, height };
  }

  function paintRuler() {
    const height = rulerCanvas.parentElement.clientHeight;
    const { width } = sizeCanvas(rulerCanvas, rulerCtx, height);
    const offset = scroller.scrollLeft;
    rulerCtx.clearRect(0, 0, width, height);

    const step = tickStep();
    const minor = step / 5;
    const withMillis = step < 1;
    const startTime = Math.floor(xToTime(offset) / minor) * minor;
    const endTime = xToTime(offset + width);

    rulerCtx.strokeStyle = cssVar("--grid-line-strong");
    rulerCtx.fillStyle = cssVar("--muted");
    rulerCtx.font = `10px ${cssVar("--font-mono") || "monospace"}`;
    rulerCtx.textBaseline = "alphabetic";
    rulerCtx.lineWidth = 1;

    for (let time = startTime; time <= endTime; time += minor) {
      const x = Math.round(timeToX(time) - offset) + 0.5;
      if (x < -1 || x > width + 1) continue;
      const isMajor = Math.abs(time / step - Math.round(time / step)) < 1e-6;
      rulerCtx.beginPath();
      rulerCtx.moveTo(x, isMajor ? height - 11 : height - 5);
      rulerCtx.lineTo(x, height);
      rulerCtx.stroke();
      if (isMajor) rulerCtx.fillText(formatRulerLabel(time, withMillis), x + 4, height - 14);
    }
  }

  function paintWaveform() {
    const height = waveCanvas.parentElement.clientHeight;
    const { width } = sizeCanvas(waveCanvas, waveCtx, height);
    waveCtx.clearRect(0, 0, width, height);
    waveNote.classList.toggle("hidden", Boolean(waveform));
    if (!waveform || !waveform.peaks.length) return;

    const offset = scroller.scrollLeft;
    const middle = height / 2;
    const { peaks, resolution } = waveform;
    waveCtx.fillStyle = cssVar("--wave");

    for (let x = 0; x < width; x += 1) {
      const from = Math.floor(xToTime(offset + x) * resolution);
      const to = Math.max(from + 1, Math.floor(xToTime(offset + x + 1) * resolution));
      if (from >= peaks.length) break;
      let peak = 0;
      for (let index = from; index < to && index < peaks.length; index += 1) {
        if (peaks[index] > peak) peak = peaks[index];
      }
      const amplitude = (peak / 255) * (middle - 2);
      if (amplitude <= 0) continue;
      waveCtx.fillRect(x, middle - amplitude, 1, amplitude * 2);
    }
  }

  function paintCanvasChrome() {
    paintRuler();
    paintWaveform();
  }

  function schedulePaint() {
    if (frameQueued) return;
    frameQueued = true;
    requestAnimationFrame(() => {
      frameQueued = false;
      paintCanvasChrome();
    });
  }

  /* ── Clips ────────────────────────────────────────────────── */

  /* `clip-text` / `clip-handle` stay on the elements: custom.css hangs the
     narrow-clip and hover-grip rules off them. The rest is utilities. */
  const CLIP_TEXT =
    "clip-text overflow-hidden text-[11px] leading-[1.3] text-ellipsis line-clamp-2 whitespace-pre-line";
  const CLIP_HANDLE =
    "clip-handle absolute top-0 bottom-0 w-2 cursor-ew-resize bg-transparent " +
    "transition-[background-color] duration-[110ms]";

  function buildClip(cue, index) {
    const element = document.createElement("div");
    element.className = "clip";
    element.dataset.index = String(index);
    element.tabIndex = -1;

    const body = document.createElement("div");
    body.className = "clip-body min-w-0 flex-1 flex flex-col gap-0.5 px-[7px] py-[5px] pointer-events-none";

    const meta = document.createElement("div");
    meta.className = "clip-meta mono flex items-center gap-1.5 text-faint text-[9.5px] tracking-[0.02em]";
    const number = document.createElement("span");
    number.className = "clip-index text-accent font-bold";
    number.textContent = String(index + 1).padStart(2, "0");
    const span = document.createElement("span");
    span.className = "clip-span";
    meta.append(number, span);

    const text = document.createElement("div");
    text.className = `${CLIP_TEXT} text-text-dim`;

    const translation = document.createElement("div");
    translation.className = `${CLIP_TEXT} clip-translation text-accent`;

    body.append(meta, text, translation);

    const left = document.createElement("span");
    left.className = `${CLIP_HANDLE} left-0`;
    left.dataset.mode = "trim-start";
    const right = document.createElement("span");
    right.className = `${CLIP_HANDLE} right-0`;
    right.dataset.mode = "trim-end";

    const severity = document.createElement("span");
    severity.className = "clip-severity absolute left-0 right-0 bottom-0 h-0.5 bg-ok";

    element.append(left, body, right, severity);
    element.addEventListener("pointerdown", onClipPointerDown);
    return { element, span, text, translation };
  }

  function paintClip(clip, cue, index) {
    const width = Math.max(6, timeToX(cueDuration(cue)));
    clip.element.style.transform = `translateX(${timeToX(cue.start)}px)`;
    clip.element.style.width = `${width}px`;
    clip.element.classList.toggle("is-narrow", width < 66);
    clip.element.classList.toggle("is-selected", index === getSelected());
    clip.element.dataset.severity = cpsSeverity(charsPerSecond(cue));
    clip.span.textContent = `${shortTimecode(cue.start)} · ${cueDuration(cue).toFixed(2)}s`;
    clip.text.textContent = cue.text || "";
    clip.translation.textContent = cue.translation || "";
  }

  function renderClips() {
    const cues = getCues();
    while (clips.length > cues.length) clips.pop().element.remove();
    while (clips.length < cues.length) {
      const clip = buildClip(cues[clips.length], clips.length);
      clips.push(clip);
      clipLane.appendChild(clip.element);
    }
    cues.forEach((cue, index) => paintClip(clips[index], cue, index));
  }

  function render() {
    canvas.style.width = `${totalWidth()}px`;
    clipLane.style.setProperty("--grid-step", `${tickStep() * pps}px`);
    renderClips();
    renderPlayhead();
    schedulePaint();
  }

  function renderPlayhead() {
    const time = getTime();
    playhead.style.transform = `translateX(${timeToX(time)}px)`;
    const cues = getCues();
    clips.forEach((clip, index) => {
      const cue = cues[index];
      clip.element.classList.toggle("is-active-cue", Boolean(cue) && time >= cue.start && time < cue.end);
    });
    if (followPlayhead && isPlaying()) keepPlayheadVisible(time);
  }

  function keepPlayheadVisible(time) {
    const x = timeToX(time);
    const left = scroller.scrollLeft;
    const width = scroller.clientWidth;
    if (x < left + 40 || x > left + width - 80) {
      scroller.scrollLeft = Math.max(0, x - width * 0.35);
    }
  }

  function refreshSelection() {
    const selected = getSelected();
    clips.forEach((clip, index) => clip.element.classList.toggle("is-selected", index === selected));
  }

  /* ── Snapping ─────────────────────────────────────────────── */

  function snapTargets(skipIndex) {
    const targets = [0, getTime()];
    getCues().forEach((cue, index) => {
      if (index === skipIndex) return;
      targets.push(cue.start, cue.end);
    });
    return targets;
  }

  function applySnap(value, targets) {
    if (!snapEnabled) return { value, guide: null };
    const tolerance = SNAP_PIXELS / pps;
    let best = null;
    for (const target of targets) {
      const distance = Math.abs(target - value);
      if (distance <= tolerance && (best === null || distance < Math.abs(best - value))) best = target;
    }
    return best === null ? { value, guide: null } : { value: best, guide: best };
  }

  function showGuide(time) {
    if (time === null) {
      snapGuide?.remove();
      snapGuide = null;
      return;
    }
    if (!snapGuide) {
      snapGuide = document.createElement("div");
      snapGuide.className =
        "snap-guide absolute left-0 top-0 bottom-0 z-[4] w-px bg-accent opacity-85 pointer-events-none";
      canvas.appendChild(snapGuide);
    }
    snapGuide.style.transform = `translateX(${timeToX(time)}px)`;
  }

  /* ── Clip dragging ────────────────────────────────────────── */

  function onClipPointerDown(event) {
    if (event.button !== 0) return;
    const element = event.currentTarget;
    const index = Number(element.dataset.index);
    const cues = getCues();
    const cue = cues[index];
    if (!cue) return;

    onSelect(index);
    refreshSelection();
    event.preventDefault();

    const mode = event.target.dataset.mode || "move";
    const startX = event.clientX;
    const origin = { start: cue.start, end: cue.end };
    const duration = cueDuration(cue);
    const lower = index > 0 ? cues[index - 1].end : 0;
    const upper = index < cues.length - 1 ? cues[index + 1].start : Math.max(getDuration(), cue.end + 600);
    const targets = snapTargets(index);
    let moved = false;
    let next = { ...origin };

    capture(element, event.pointerId);
    element.classList.add("is-dragging");

    const onMove = (moveEvent) => {
      const delta = (moveEvent.clientX - startX) / pps;
      if (!moved && Math.abs(moveEvent.clientX - startX) < 2) return;
      // Hold playback for the gesture: a moving playhead would auto-scroll the
      // lane out from under the pointer and skew the drag.
      if (!moved) onScrubStart?.();
      moved = true;
      let guide = null;

      if (mode === "move") {
        let start = clamp(origin.start + delta, lower, Math.max(lower, upper - duration));
        const snapStart = applySnap(start, targets);
        const snapEnd = applySnap(start + duration, targets);
        if (snapStart.guide !== null) {
          start = snapStart.value;
          guide = snapStart.guide;
        } else if (snapEnd.guide !== null) {
          start = snapEnd.value - duration;
          guide = snapEnd.guide;
        }
        start = clamp(start, lower, Math.max(lower, upper - duration));
        next = { start, end: start + duration };
      } else if (mode === "trim-start") {
        let start = clamp(origin.start + delta, lower, origin.end - MIN_CUE_DURATION);
        const snapped = applySnap(start, targets);
        start = clamp(snapped.value, lower, origin.end - MIN_CUE_DURATION);
        guide = snapped.guide;
        next = { start, end: origin.end };
      } else {
        let end = clamp(origin.end + delta, origin.start + MIN_CUE_DURATION, upper);
        const snapped = applySnap(end, targets);
        end = clamp(snapped.value, origin.start + MIN_CUE_DURATION, upper);
        guide = snapped.guide;
        next = { start: origin.start, end };
      }

      showGuide(guide);
      element.style.transform = `translateX(${timeToX(next.start)}px)`;
      element.style.width = `${Math.max(6, timeToX(next.end - next.start))}px`;
      onDragTime?.(next, index);
    };

    const onUp = () => {
      release(element, event.pointerId);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      element.classList.remove("is-dragging");
      showGuide(null);
      if (!moved) {
        onSeek(cue.start); // a plain click just jumps there; playback carries on
        return;
      }
      onScrubEnd?.();
      onCommit(
        index,
        { start: Number(next.start.toFixed(3)), end: Number(next.end.toFixed(3)) },
        mode,
      );
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }

  /* ── Scrubbing ────────────────────────────────────────────── */

  function scrubFrom(event, element) {
    // The lane spans the whole scrolled canvas, so its rect already carries the scroll offset.
    const seekTo = (clientX) => {
      const rect = element.getBoundingClientRect();
      onSeek(clamp(xToTime(clientX - rect.left), 0, getDuration()));
    };
    onScrubStart?.();
    seekTo(event.clientX);
    capture(element, event.pointerId);
    const onMove = (moveEvent) => seekTo(moveEvent.clientX);
    const onUp = () => {
      release(element, event.pointerId);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      onScrubEnd?.();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }

  rulerCanvas.parentElement.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    scrubFrom(event, rulerCanvas.parentElement);
  });

  waveCanvas.parentElement.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    scrubFrom(event, waveCanvas.parentElement);
  });

  clipLane.addEventListener("pointerdown", (event) => {
    if (event.target !== clipLane || event.button !== 0) return;
    onSelect(-1);
    refreshSelection();
    scrubFrom(event, clipLane);
  });

  /* ── Zoom + scroll ────────────────────────────────────────── */

  function setPps(value, anchorClientX) {
    const previous = pps;
    const next = clamp(value, MIN_PPS, MAX_PPS);
    if (Math.abs(next - previous) < 0.001) return;
    const rect = scroller.getBoundingClientRect();
    const anchorX = anchorClientX === undefined ? rect.width / 2 : anchorClientX - rect.left;
    const anchorTime = (scroller.scrollLeft + anchorX) / previous;
    pps = next;
    render();
    scroller.scrollLeft = Math.max(0, anchorTime * pps - anchorX);
    onZoom?.(pps);
  }

  scroller.addEventListener("scroll", schedulePaint, { passive: true });

  scroller.addEventListener(
    "wheel",
    (event) => {
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        setPps(pps * (event.deltaY < 0 ? 1.14 : 1 / 1.14), event.clientX);
      } else if (Math.abs(event.deltaY) > Math.abs(event.deltaX)) {
        event.preventDefault();
        scroller.scrollLeft += event.deltaY;
      }
    },
    { passive: false },
  );

  const observer = new ResizeObserver(() => {
    canvas.style.width = `${totalWidth()}px`;
    schedulePaint();
  });
  observer.observe(scroller);

  return {
    render,
    renderPlayhead,
    refreshSelection,
    repaint: schedulePaint,
    /** Redraw a single clip after an in-place edit — cheaper than a full render. */
    patch(index) {
      const cue = getCues()[index];
      if (cue && clips[index]) paintClip(clips[index], cue, index);
    },
    get pps() {
      return pps;
    },
    setPps,
    zoomBy(factor) {
      setPps(pps * factor);
    },
    /** Fitting needs a laid-out scroller; while the editor is hidden it measures
     *  zero, so remember the request and honour it once the screen is shown. */
    fit() {
      const duration = getDuration();
      const width = scroller.clientWidth;
      if (duration <= 0) return;
      if (width < 80) {
        pendingFit = true;
        return;
      }
      pendingFit = false;
      setPps((width - 48) / duration);
    },
    resolvePendingFit() {
      if (pendingFit) this.fit();
    },
    scrollToTime(time) {
      scroller.scrollLeft = Math.max(0, timeToX(time) - scroller.clientWidth * 0.35);
    },
    setSnap(value) {
      snapEnabled = value;
    },
    setFollow(value) {
      followPlayhead = value;
    },
    setWaveform(data) {
      waveform = data;
      schedulePaint();
    },
  };
}

export { MIN_PPS, MAX_PPS };

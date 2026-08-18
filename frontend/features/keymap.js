/**
 * Every keyboard shortcut, in one table-like place.
 * Keep this file as the single answer to "what does key X do?".
 */

import { $, isTypingTarget } from "../core/dom.js";
import { state } from "../core/store.js";
import { addCue, deleteCue, markPoint, mergeCue, splitCue } from "./editing.js";
import { redo, undo } from "./history.js";
import { stepCue } from "./cuelist.js";
import { transcribe } from "./pipeline/index.js";
import { openShortcuts } from "./shell.js";
import { timeline } from "./timeline-view.js";
import { currentTime, frameStep, seek, togglePlay } from "./transport.js";

const clickIfEnabled = (selector) => {
  const button = $(selector);
  if (!button.disabled) button.click();
};

export function mountKeymap() {
  document.addEventListener("keydown", (event) => {
    const typing = isTypingTarget(event.target);
    const modified = event.ctrlKey || event.metaKey;

    if (modified && event.key === "Enter") {
      event.preventDefault();
      if (!$("#transcribe-btn").disabled) transcribe();
      return;
    }
    // Inside a text field the browser's own per-field undo is the better tool,
    // and its input events keep our state in sync — so leave it alone there.
    if (modified && !typing) {
      const key = event.key.toLowerCase();
      const isRedo = key === "y" || (key === "z" && event.shiftKey);
      if (isRedo || key === "z") {
        event.preventDefault();
        if (isRedo) redo();
        else undo();
        return;
      }
    }
    if (event.key === "F1") {
      event.preventDefault();
      openShortcuts();
      return;
    }
    if (typing) return;

    const nudge = event.shiftKey ? 1 : frameStep();
    switch (event.key) {
      case " ":
        event.preventDefault();
        togglePlay();
        return;
      case "ArrowLeft":
        event.preventDefault();
        if (modified) stepCue(-1);
        else seek(currentTime() - nudge);
        return;
      case "ArrowRight":
        event.preventDefault();
        if (modified) stepCue(1);
        else seek(currentTime() + nudge);
        return;
      case "Delete":
      case "Backspace":
        if (state.selected >= 0) {
          event.preventDefault();
          deleteCue();
        }
        return;
      default:
        break;
    }

    if (modified || event.altKey) return;

    switch (event.key.toLowerCase()) {
      case "a":
        if (!$("#add-cue-btn").disabled) addCue();
        break;
      case "s":
        if (!$("#split-btn").disabled) splitCue();
        break;
      case "g":
        if (!$("#merge-btn").disabled) mergeCue();
        break;
      case "i":
        markPoint("start");
        break;
      case "o":
        markPoint("end");
        break;
      case "n":
        clickIfEnabled("#snap-btn");
        break;
      case "f":
        timeline.fit();
        break;
      case "m":
        clickIfEnabled("#mute-btn");
        break;
      case "+":
      case "=":
        timeline.zoomBy(1.35);
        break;
      case "-":
        timeline.zoomBy(1 / 1.35);
        break;
      default:
        break;
    }
  });
}

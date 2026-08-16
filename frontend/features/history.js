/**
 * Undo/redo over cue snapshots.
 *
 * Snapshots, not diffs: a cue list is small enough that copying it is cheaper
 * than maintaining inverse operations for six different commands.
 */

import { $ } from "../core/dom.js";
import { setStatus } from "../core/feedback.js";
import { cues, emit, isProcessing, onAny, renumberCues, state } from "../core/store.js";

const LIMIT = 120;
/** Consecutive edits sharing a key inside this window collapse into one step,
 *  so a burst of typing undoes as a phrase rather than one character at a time. */
const COALESCE_MS = 1200;

const past = [];
const future = [];
let lastKey = null;
let lastAt = 0;

const snapshot = (title) => ({
  title,
  selected: state.selected,
  cues: cues().map((cue) => ({ ...cue })),
});

/** Record the state as it stands *before* a mutation. Call it right before editing. */
export function pushHistory(key, title) {
  if (!state.job) return;
  const now = performance.now();
  if (past.length && lastKey === key && now - lastAt < COALESCE_MS) {
    lastAt = now;
    future.length = 0;
    return;
  }
  past.push(snapshot(title));
  if (past.length > LIMIT) past.shift();
  future.length = 0;
  lastKey = key;
  lastAt = now;
  refreshButtons();
}

export function resetHistory() {
  past.length = 0;
  future.length = 0;
  lastKey = null;
  refreshButtons();
}

function restore(entry) {
  state.job.cues = entry.cues.map((cue) => ({ ...cue }));
  renumberCues();
  state.selected = entry.selected >= 0 && entry.selected < cues().length ? entry.selected : -1;
  emit("cues:changed", { reason: "history" });
  emit("selection:changed", { index: state.selected, source: "history" });
}

export function undo() {
  if (isProcessing()) return;
  const entry = past.pop();
  if (!entry) return;
  future.push(snapshot(entry.title));
  lastKey = null;
  restore(entry);
  setStatus(`Đã hoàn tác: ${entry.title}`);
  refreshButtons();
}

export function redo() {
  if (isProcessing()) return;
  const entry = future.pop();
  if (!entry) return;
  past.push(snapshot(entry.title));
  lastKey = null;
  restore(entry);
  setStatus(`Đã làm lại: ${entry.title}`);
  refreshButtons();
}

function refreshButtons() {
  const undoButton = $("#undo-btn");
  const redoButton = $("#redo-btn");
  if (!undoButton || !redoButton) return;
  const blocked = isProcessing();
  undoButton.disabled = blocked || !past.length;
  redoButton.disabled = blocked || !future.length;
  const back = past[past.length - 1];
  const forward = future[future.length - 1];
  undoButton.title = back ? `Hoàn tác: ${back.title} (Ctrl Z)` : "Hoàn tác (Ctrl Z)";
  redoButton.title = forward ? `Làm lại: ${forward.title} (Ctrl Y)` : "Làm lại (Ctrl Y)";
}

export function mountHistory() {
  $("#undo-btn").addEventListener("click", undo);
  $("#redo-btn").addEventListener("click", redo);
  onAny(["job:loaded", "cues:changed", "cue:patched"], refreshButtons);
  refreshButtons();
}

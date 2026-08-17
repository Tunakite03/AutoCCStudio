/**
 * Undo/redo over cue snapshots.
 *
 * Two step shapes. Structural commands (add, split, merge, delete) copy the
 * whole list, because that is the only thing that can put an order back. The
 * commands that fire in bursts — retiming a clip, typing in the inspector —
 * touch one cue and store one cue, which is what keeps a long project's history
 * from costing tens of megabytes.
 */

import { $ } from "../core/dom.js";
import { setStatus } from "../core/feedback.js";
import { t } from "../core/i18n.js";
import { cueAt, cues, emit, isProcessing, onAny, renumberCues, state } from "../core/store.js";

const LIMIT = 120;
/** Full snapshots copy every cue, so a step count alone is a poor bound: 120
 *  steps over a 1500-cue project would keep 180.000 objects alive. Cap the
 *  copies as well, and let the step count be whatever fits under it. */
const MAX_RETAINED_CUES = 60_000;
/** Consecutive edits sharing a key inside this window collapse into one step,
 *  so a burst of typing undoes as a phrase rather than one character at a time. */
const COALESCE_MS = 1200;

const past = [];
const future = [];
let lastKey = null;
let lastAt = 0;

const boundedSelection = (index) => (index >= 0 && index < cues().length ? index : -1);

const snapshot = (title) => ({
  title,
  selected: state.selected,
  cues: cues().map((cue) => ({ ...cue })),
});

const cueStep = (title, index) => ({
  title,
  selected: state.selected,
  index,
  cue: { ...cueAt(index) },
});

/** Mirror an entry's shape, so the opposite stack can put back what we replace. */
const inverseOf = (entry) =>
  entry.index === undefined ? snapshot(entry.title) : cueStep(entry.title, entry.index);

/** A one-cue step only means anything while that cue still exists. */
const applies = (entry) => entry.index === undefined || Boolean(cueAt(entry.index));

/** Bound the stack by retained cue copies, not just by step count. */
function trim() {
  while (past.length > LIMIT) past.shift();
  let retained = past.reduce((sum, entry) => sum + (entry.cues?.length ?? 1), 0);
  // Keep one step no matter what — a single edit must stay undoable.
  while (past.length > 1 && retained > MAX_RETAINED_CUES) {
    retained -= past[0].cues?.length ?? 1;
    past.shift();
  }
}

/**
 * Record the state as it stands *before* a mutation. Call it right before editing.
 *
 * Pass `index` when the command only touches that one cue; the step then costs
 * one object rather than a copy of the whole list, and undoing it repaints one
 * row instead of rebuilding the list and the clip lane.
 */
export function pushHistory(key, title, index) {
  if (!state.job) return;
  const now = performance.now();
  if (past.length && lastKey === key && now - lastAt < COALESCE_MS) {
    lastAt = now;
    future.length = 0;
    return;
  }
  past.push(index === undefined ? snapshot(title) : cueStep(title, index));
  trim();
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
  if (entry.index === undefined) {
    state.job.cues = entry.cues.map((cue) => ({ ...cue }));
    renumberCues();
    state.selected = boundedSelection(entry.selected);
    emit("cues:changed", { reason: "history" });
  } else {
    // Nothing moved, so ids hold and the views only need the one cue repainted.
    Object.assign(cueAt(entry.index), entry.cue);
    state.selected = boundedSelection(entry.selected);
    emit("cue:patched", { index: entry.index });
  }
  emit("selection:changed", { index: state.selected, source: "history" });
}

export function undo() {
  if (isProcessing()) return;
  const entry = past.pop();
  if (!entry) return;
  // A one-cue step whose cue is gone can only have been stranded by a structural
  // change; drop it rather than write into whatever sits at that index now.
  if (!applies(entry)) return refreshButtons();
  future.push(inverseOf(entry));
  lastKey = null;
  restore(entry);
  setStatus(t("status.undone", { title: entry.title }));
  refreshButtons();
}

export function redo() {
  if (isProcessing()) return;
  const entry = future.pop();
  if (!entry) return;
  if (!applies(entry)) return refreshButtons();
  past.push(inverseOf(entry));
  lastKey = null;
  restore(entry);
  setStatus(t("status.redone", { title: entry.title }));
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
  undoButton.title = back ? t("tool.undoOf", { title: back.title }) : t("tool.undo");
  redoButton.title = forward ? t("tool.redoOf", { title: forward.title }) : t("tool.redo");
}

export function mountHistory() {
  $("#undo-btn").addEventListener("click", undo);
  $("#redo-btn").addEventListener("click", redo);
  onAny(["job:loaded", "cues:changed", "cue:patched"], refreshButtons);
  refreshButtons();
}

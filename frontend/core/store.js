/**
 * Document state and the event bus every view listens on.
 *
 * The rule that keeps this app maintainable: a module never calls another
 * module's render function. It changes state and emits, and whoever cares
 * re-draws itself. That is what lets a second screen (dashboard) read the same
 * data without the editor knowing it exists.
 *
 * Events
 *   job:loaded        {job, previousId}  a job arrived from the server — no save
 *   cues:changed      {reason}           local structural edit (count/order) — saves
 *   cue:patched       {index}            local in-place edit of one cue — saves
 *   selection:changed {index, source}    source: list | timeline | keyboard | reset
 *   active-cue:changed{index}            cue under the playhead
 *   time:changed      {time}             playhead moved (fires per frame while playing)
 *   capabilities:loaded {capabilities}
 *
 * High-frequency values (playhead position, drag deltas) deliberately stay out
 * of state — transport.js owns them and emits only what views need.
 */

export const state = {
  job: null,
  capabilities: null,
  selected: -1,
  activeCue: -1,
};

export const cues = () => state.job?.cues ?? [];
export const cueAt = (index) => cues()[index] ?? null;
export const selectedCue = () => cueAt(state.selected);
export const hasCues = () => cues().length > 0;
export const isProcessing = () => state.job?.status === "processing";

const handlers = new Map();

export function on(event, handler) {
  if (!handlers.has(event)) handlers.set(event, new Set());
  handlers.get(event).add(handler);
  return () => handlers.get(event)?.delete(handler);
}

/** Subscribe one handler to several events — the common case for a view. */
export function onAny(events, handler) {
  const offs = events.map((event) => on(event, handler));
  return () => offs.forEach((off) => off());
}

export function emit(event, payload) {
  handlers.get(event)?.forEach((handler) => handler(payload ?? {}));
}

export function setJob(job) {
  const previousId = state.job?.id ?? null;
  state.job = job;
  emit("job:loaded", { job, previousId });
}

export function setSelection(index, source = "list") {
  const bounded = index >= 0 && index < cues().length ? index : -1;
  if (bounded === state.selected) return;
  state.selected = bounded;
  emit("selection:changed", { index: bounded, source });
}

export function setActiveCue(index) {
  if (index === state.activeCue) return;
  state.activeCue = index;
  emit("active-cue:changed", { index });
}

export function setCapabilities(capabilities) {
  state.capabilities = capabilities;
  emit("capabilities:loaded", { capabilities });
}

/** Cue ids are positional — renumber after any structural change. */
export function renumberCues() {
  cues().forEach((cue, index) => {
    cue.id = index + 1;
  });
}

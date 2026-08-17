/**
 * Cue editing commands: add, split, merge, delete, retime.
 *
 * Every command follows the same shape — validate, push history, mutate, emit.
 * Nothing here touches the DOM except the dock buttons it owns.
 */

import { $ } from "../core/dom.js";
import { api } from "../core/api.js";
import { MIN_CUE_DURATION, cueDuration } from "../core/format.js";
import { toast } from "../core/feedback.js";
import { t } from "../core/i18n.js";
import { cueAt, cues, emit, isProcessing, onAny, renumberCues, state } from "../core/store.js";
import { pushHistory } from "./history.js";
import { currentTime } from "./transport.js";


function afterStructuralChange(selectIndex) {
  renumberCues();
  emit("cues:changed", { reason: "edit" });
  // Indices shifted, so the selection is announced even when the number is the
  // same — the cue living at that index is a different one now.
  state.selected = selectIndex >= 0 && selectIndex < cues().length ? selectIndex : -1;
  emit("selection:changed", { index: state.selected, source: "edit" });
}

export function updateCueTimes(index, { start, end }, historyKey, title) {
  const cue = cueAt(index);
  if (!cue) return;
  const nextStart = Math.max(0, start);
  const nextEnd = Math.max(nextStart + MIN_CUE_DURATION, end);
  // A drag that ran into a neighbour changes nothing — don't spend an undo step on it.
  if (cue.start === nextStart && cue.end === nextEnd) return;
  pushHistory(historyKey, title, index);
  cue.start = nextStart;
  cue.end = nextEnd;
  emit("cue:patched", { index });
}

export function updateCueText(index, field, value, historyKey, title) {
  const cue = cueAt(index);
  if (!cue || cue[field] === value) return;
  pushHistory(historyKey, title, index);
  cue[field] = value;
  emit("cue:patched", { index });
}

export function addCue() {
  if (!state.job) return toast(t("toast.noProject"), "error");
  const list = cues();
  const at = currentTime();
  const blocked = list.some((cue) => at < cue.end && at + MIN_CUE_DURATION > cue.start);
  const start = blocked ? (list[list.length - 1]?.end ?? 0) + 0.05 : at;
  const next = list.find((cue) => cue.start > start);
  const end = Math.min(start + 2, next ? next.start - 0.02 : start + 2);
  if (end - start < MIN_CUE_DURATION) return toast(t("toast.noRoomForCue"), "error");
  pushHistory("add", t("history.addCue"));
  list.push({ id: 0, start, end, text: "", translation: "", speaker: null });
  list.sort((a, b) => a.start - b.start);
  afterStructuralChange(list.findIndex((cue) => cue.start === start));
  $("#insp-text").focus();
}

export function splitCue() {
  const index = state.selected;
  const cue = cueAt(index);
  if (!cue) return toast(t("toast.pickCueToSplit"), "error");
  const at = currentTime();
  if (at <= cue.start + MIN_CUE_DURATION || at >= cue.end - MIN_CUE_DURATION) {
    return toast(t("toast.playheadInsideCue"), "error");
  }
  // Split the text at the same proportion as the split point, on a word boundary.
  const ratio = (at - cue.start) / cueDuration(cue);
  const cut = (value) => {
    const text = value || "";
    if (!text) return ["", ""];
    const target = Math.round(text.length * ratio);
    const space = text.lastIndexOf(" ", target);
    const pivot = space > text.length * 0.2 ? space : target;
    return [text.slice(0, pivot).trim(), text.slice(pivot).trim()];
  };
  const [textA, textB] = cut(cue.text);
  const [transA, transB] = cut(cue.translation);
  pushHistory("split", t("history.splitCue", { cue: index + 1 }));
  const tail = {
    id: 0,
    start: at,
    end: cue.end,
    text: textB,
    translation: transB,
    speaker: cue.speaker ?? null,
  };
  cue.end = at;
  cue.text = textA;
  cue.translation = transA;
  cues().splice(index + 1, 0, tail);
  afterStructuralChange(index + 1);
}

export function mergeCue() {
  const index = state.selected;
  const list = cues();
  const cue = list[index];
  const next = list[index + 1];
  if (!cue || !next) return toast(t("toast.noNextCue"), "error");
  pushHistory("merge", t("history.mergeCue", { cue: index + 1 }));
  cue.end = next.end;
  const speakerChanged = cue.speaker != null && next.speaker != null && cue.speaker !== next.speaker;
  const separator = speakerChanged ? "\n" : " ";
  cue.text = [cue.text, next.text].filter(Boolean).join(separator);
  cue.translation = [cue.translation, next.translation].filter(Boolean).join(separator);
  list.splice(index + 1, 1);
  afterStructuralChange(index);
}

export function deleteCue() {
  const index = state.selected;
  if (index < 0 || !cueAt(index)) return;
  pushHistory("delete", t("history.deleteCue", { cue: index + 1 }));
  cues().splice(index, 1);
  afterStructuralChange(Math.min(index, cues().length - 1));
}

export function markPoint(edge) {
  const index = state.selected;
  const cue = cueAt(index);
  if (!cue) return toast(t("toast.pickCueFirst"), "error");
  const at = currentTime();
  if (edge === "start" && at >= cue.end - MIN_CUE_DURATION) {
    return toast(t("toast.inBeforeOut"), "error");
  }
  if (edge === "end" && at <= cue.start + MIN_CUE_DURATION) {
    return toast(t("toast.outAfterIn"), "error");
  }
  updateCueTimes(
    index,
    { start: edge === "start" ? at : cue.start, end: edge === "end" ? at : cue.end },
    `mark:${index}:${edge}`,
    t(edge === "start" ? "history.markIn" : "history.markOut", { cue: index + 1 }),
  );
}

export async function splitAllLongCues() {
  if (!state.job) return toast(t("toast.noProject"), "error");
  const list = cues();
  if (!list.length) return toast(t("toast.noCuesToSplit"), "error");

  try {
    pushHistory("split-long", t("history.splitLong"));
    const updated = await api.splitLongCues(state.job.id);
    state.job.cues = updated.cues;
    afterStructuralChange(Math.min(state.selected, cues().length - 1));
    toast(t("toast.normalizedCues", { count: cues().length }), "success");
  } catch (error) {
    toast(t("toast.splitFailed", { message: error.message }), "error");
  }
}

/** Unique per gesture so two quick drags of one clip stay two undo steps. */
let gestureId = 0;
export const nextGestureKey = () => `drag:${(gestureId += 1)}`;

function refreshButtons() {
  const blocked = isProcessing();
  const hasSelection = state.selected >= 0;
  const hasAnyCues = cues().length > 0;
  $("#add-cue-btn").disabled = !state.job || blocked;
  $("#split-btn").disabled = !hasSelection || blocked;
  $("#merge-btn").disabled = !hasSelection || state.selected >= cues().length - 1 || blocked;
  $("#delete-btn").disabled = !hasSelection || blocked;
  const splitLongBtn = $("#split-long-btn");
  if (splitLongBtn) splitLongBtn.disabled = !hasAnyCues || blocked;
}

export function mountEditing() {
  $("#add-cue-btn").addEventListener("click", addCue);
  $("#split-btn").addEventListener("click", splitCue);
  $("#merge-btn").addEventListener("click", mergeCue);
  $("#delete-btn").addEventListener("click", deleteCue);
  const splitLongBtn = $("#split-long-btn");
  if (splitLongBtn) splitLongBtn.addEventListener("click", splitAllLongCues);
  $("#mark-in").addEventListener("click", () => markPoint("start"));
  $("#mark-out").addEventListener("click", () => markPoint("end"));
  onAny(["job:loaded", "cues:changed", "selection:changed"], refreshButtons);
  refreshButtons();
}


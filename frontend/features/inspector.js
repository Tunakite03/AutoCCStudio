/** The selected-cue inspector: timecodes, text, and the reading-speed meters. */

import { $ } from "../core/dom.js";
import {
  MIN_CUE_DURATION,
  charsPerSecond,
  cpsSeverity,
  cueDuration,
  formatTimecode,
  parseTimecode,
} from "../core/format.js";
import { toast } from "../core/feedback.js";
import { t } from "../core/i18n.js";
import { cueAt, cues, on, selectedCue, state } from "../core/store.js";
import { stepCue } from "./cuelist.js";
import { updateCueText, updateCueTimes } from "./editing.js";

const field = {
  root: $("#inspector"),
  index: $("#inspector-index"),
  start: $("#insp-start"),
  end: $("#insp-end"),
  duration: $("#insp-duration"),
  text: $("#insp-text"),
  translation: $("#insp-translation"),
  chars: $("#meter-chars"),
  cps: $("#meter-cps"),
  cpsWrap: $("#meter-cps-wrap"),
  gap: $("#meter-gap"),
};

export function renderInspector() {
  const cue = selectedCue();
  field.root.dataset.empty = cue ? "false" : "true";
  if (!cue) {
    field.index.textContent = "—";
    field.start.value = "";
    field.end.value = "";
    field.duration.textContent = "0.000";
    field.text.value = "";
    field.translation.value = "";
    field.chars.textContent = "0";
    field.cps.textContent = "0.0";
    field.cpsWrap.dataset.severity = "none";
    field.gap.textContent = "—";
    return;
  }

  field.index.textContent = t("inspector.index", {
    index: String(state.selected + 1).padStart(2, "0"),
    total: cues().length,
  });
  // Never overwrite the box the user is typing in.
  if (document.activeElement !== field.start) field.start.value = formatTimecode(cue.start);
  if (document.activeElement !== field.end) field.end.value = formatTimecode(cue.end);
  if (document.activeElement !== field.text) field.text.value = cue.text || "";
  if (document.activeElement !== field.translation) field.translation.value = cue.translation || "";
  renderMeters();
}

function renderMeters() {
  const cue = selectedCue();
  if (!cue) return;
  const line = (cue.translation || cue.text || "").replace(/\s+/g, " ").trim();
  const cps = charsPerSecond(cue);
  field.duration.textContent = cueDuration(cue).toFixed(3);
  field.chars.textContent = String(line.length);
  field.cps.textContent = cps.toFixed(1);
  field.cpsWrap.dataset.severity = cpsSeverity(cps);
  const next = cueAt(state.selected + 1);
  field.gap.textContent = next ? `${(next.start - cue.end).toFixed(2)}s` : "—";
}

/** Live feedback while a clip is being dragged — commits happen on release. */
export function previewTimes({ start, end }) {
  field.start.value = formatTimecode(start);
  field.end.value = formatTimecode(end);
  field.duration.textContent = (end - start).toFixed(3);
}

function bindTimeField(input, edge) {
  input.addEventListener("change", () => {
    const cue = selectedCue();
    if (!cue) return;
    const parsed = parseTimecode(input.value);
    if (parsed === null) {
      renderInspector();
      return toast(t("toast.badTimecode"), "error");
    }
    const start = edge === "start" ? parsed : cue.start;
    const end = edge === "end" ? parsed : cue.end;
    if (end - start < MIN_CUE_DURATION) {
      renderInspector();
      return toast(t("toast.cueTooShort", { seconds: MIN_CUE_DURATION }), "error");
    }
    updateCueTimes(
      state.selected,
      { start, end },
      `time:${state.selected}:${edge}`,
      t("history.editTime", { cue: state.selected + 1 }),
    );
    renderInspector();
  });
}

function bindTextField(input, name) {
  input.addEventListener("input", () => {
    if (!selectedCue()) return;
    updateCueText(
      state.selected,
      name,
      input.value,
      `${name}:${state.selected}`,
      t(name === "text" ? "history.editSource" : "history.editTranslation", {
        cue: state.selected + 1,
      }),
    );
  });
}

export function mountInspector() {
  bindTimeField(field.start, "start");
  bindTimeField(field.end, "end");
  bindTextField(field.text, "text");
  bindTextField(field.translation, "translation");

  $("#insp-prev").addEventListener("click", () => stepCue(-1));
  $("#insp-next").addEventListener("click", () => stepCue(1));

  on("selection:changed", renderInspector);
  on("job:loaded", renderInspector);
  on("cues:changed", renderInspector);
  on("cue:patched", ({ index }) => {
    if (index === state.selected) renderInspector();
  });
  renderInspector();
}

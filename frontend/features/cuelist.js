/**
 * The cue list panel, and the app's notion of "which cue is current".
 *
 * Rows are pooled, not rebuilt: a structural change only adds or removes the
 * difference and repaints through cached child references. One delegated click
 * listener serves the whole list, so a thousand cues cost one listener rather
 * than a thousand closures.
 */

import { $, element } from "../core/dom.js";
import { charsPerSecond, clamp, cpsSeverity, cueDuration, shortTimecode } from "../core/format.js";
import { t } from "../core/i18n.js";
import { cueAt, cues, on, setSelection, state } from "../core/store.js";
import { seek } from "./transport.js";

const listNode = $("#cue-list");
const emptyNode = $("#list-empty");
let rows = [];

/* Semantic names are kept as query hooks (paintRow reaches for them) and as
   the anchors for the selected/active rules in custom.css — the look itself
   is carried by the utilities alongside them. */
const ROW =
  "cue-row grid grid-cols-[26px_minmax(0,1fr)] gap-2 px-2 py-[7px] border border-transparent rounded-sm " +
  "text-left cursor-pointer transition-[background-color,border-color] duration-[110ms] hover:bg-raised";
const ROW_TEXT = "overflow-hidden text-[12px] leading-[1.4] text-ellipsis line-clamp-2 whitespace-pre-line";

/** A row's position never changes once built, so the index label is painted here
 *  and only the cue-dependent parts are touched on repaint. */
function buildRow(index) {
  const el = element("button", ROW);
  el.type = "button";
  el.dataset.index = String(index);

  const number = element(
    "span",
    "cue-row-index mono text-faint text-[10.5px] pt-px",
    String(index + 1).padStart(2, "0"),
  );
  const main = element("span", "cue-row-main min-w-0 grid gap-[3px]");
  const time = element("span", "cue-row-time mono flex items-center gap-[7px] text-muted text-[10px]");
  const range = element("span");
  const dur = element("span", "dur text-faint");
  const tag = element("span", "cps-tag ml-auto px-1 rounded-[3px] text-[9.5px] font-semibold");
  time.append(range, dur, tag);
  const text = element("span", `cue-row-text ${ROW_TEXT} text-text-dim`);
  const translation = element("span", `cue-row-translation ${ROW_TEXT} text-accent`);
  main.append(time, text, translation);
  el.append(number, main);

  return { el, range, dur, tag, text, translation };
}

function paintRow(row, cue, index) {
  const cps = charsPerSecond(cue);
  row.range.textContent = `${shortTimecode(cue.start)} → ${shortTimecode(cue.end)}`;
  row.dur.textContent = `${cueDuration(cue).toFixed(2)}s`;
  row.tag.dataset.severity = cpsSeverity(cps);
  row.tag.textContent = `${cps.toFixed(1)} cps`;
  row.text.textContent = cue.text || "—";
  row.translation.textContent = cue.translation || "";
  row.el.classList.toggle("is-selected", index === state.selected);
  row.el.classList.toggle("is-active", index === state.activeCue);
}

export function renderList() {
  const list = cues();
  $("#cue-count").textContent = String(list.length);
  $("#status-cues").textContent = t("status.cues", { count: list.length });

  if (!list.length) {
    rows.length = 0;
    listNode.replaceChildren(emptyNode);
    return;
  }
  if (emptyNode.parentElement) emptyNode.remove();

  // Grow or shrink to fit, then repaint — the rows that survive keep their DOM.
  while (rows.length > list.length) rows.pop().el.remove();
  if (rows.length < list.length) {
    const fragment = document.createDocumentFragment();
    while (rows.length < list.length) {
      const row = buildRow(rows.length);
      rows.push(row);
      fragment.appendChild(row.el);
    }
    listNode.appendChild(fragment);
  }
  list.forEach((cue, index) => paintRow(rows[index], cue, index));
}

function patchRow(index) {
  const row = rows[index];
  const cue = cueAt(index);
  if (row && cue) paintRow(row, cue, index);
}

function refreshFlags() {
  rows.forEach((row, index) => {
    row.el.classList.toggle("is-selected", index === state.selected);
    row.el.classList.toggle("is-active", index === state.activeCue);
  });
}

/* ── Selection: the list owns which cue is current ────────────── */

export function selectCue(index, { seek: shouldSeek = false, source = "list" } = {}) {
  setSelection(index, source);
  if (state.selected < 0) return;
  if (shouldSeek) seek(cueAt(state.selected).start);
}

export function stepCue(direction) {
  const list = cues();
  if (!list.length) return;
  const from = state.selected < 0 ? (direction > 0 ? -1 : list.length) : state.selected;
  selectCue(clamp(from + direction, 0, list.length - 1), { seek: true, source: "keyboard" });
}

export function mountCueList() {
  $("#prev-cue").addEventListener("click", () => stepCue(-1));
  $("#next-cue").addEventListener("click", () => stepCue(1));

  // One delegated listener for the whole list — rows carry their index in the DOM.
  listNode.addEventListener("click", (event) => {
    const row = event.target.closest(".cue-row");
    if (!row) return;
    selectCue(Number(row.dataset.index), { seek: true, source: "list" });
  });

  on("job:loaded", renderList);
  on("cues:changed", renderList);
  on("cue:patched", ({ index }) => patchRow(index));
  on("active-cue:changed", refreshFlags);
  on("selection:changed", ({ source }) => {
    refreshFlags();
    if (source !== "list" && state.selected >= 0) {
      rows[state.selected]?.el.scrollIntoView({ block: "nearest" });
    }
  });
  renderList();
}

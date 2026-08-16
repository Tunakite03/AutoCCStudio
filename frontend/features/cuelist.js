/**
 * The cue list panel, and the app's notion of "which cue is current".
 *
 * Rows are built once per structural change and patched in place afterwards —
 * a full rebuild on every keystroke would throw away scroll position and cost
 * more than it saves on a list of several hundred rows.
 */

import { $, element } from "../core/dom.js";
import { charsPerSecond, clamp, cpsSeverity, cueDuration, shortTimecode } from "../core/format.js";
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

function buildRow(cue, index) {
  const row = element("button", ROW);
  row.type = "button";
  row.dataset.index = String(index);

  const number = element(
    "span",
    "cue-row-index mono text-faint text-[10.5px] pt-px",
    String(index + 1).padStart(2, "0"),
  );
  const main = element("span", "cue-row-main min-w-0 grid gap-[3px]");
  const time = element("span", "cue-row-time mono flex items-center gap-[7px] text-muted text-[10px]");
  time.append(
    element("span"),
    element("span", "dur text-faint"),
    element("span", "cps-tag ml-auto px-1 rounded-[3px] text-[9.5px] font-semibold"),
  );
  main.append(
    time,
    element("span", `cue-row-text ${ROW_TEXT} text-text-dim`),
    element("span", `cue-row-translation ${ROW_TEXT} text-accent`),
  );
  row.append(number, main);

  row.addEventListener("click", () => selectCue(index, { seek: true, source: "list" }));
  return row;
}

function paintRow(row, cue, index) {
  const cps = charsPerSecond(cue);
  const [range, dur, tag] = row.querySelector(".cue-row-time").children;
  range.textContent = `${shortTimecode(cue.start)} → ${shortTimecode(cue.end)}`;
  dur.textContent = `${cueDuration(cue).toFixed(2)}s`;
  tag.dataset.severity = cpsSeverity(cps);
  tag.textContent = `${cps.toFixed(1)} cps`;
  row.querySelector(".cue-row-text").textContent = cue.text || "—";
  row.querySelector(".cue-row-translation").textContent = cue.translation || "";
  row.classList.toggle("is-selected", index === state.selected);
  row.classList.toggle("is-active", index === state.activeCue);
}

export function renderList() {
  const list = cues();
  $("#cue-count").textContent = String(list.length);
  $("#status-cues").textContent = `${list.length} cue`;
  listNode.replaceChildren();
  rows = [];

  if (!list.length) {
    listNode.appendChild(emptyNode);
    return;
  }

  const fragment = document.createDocumentFragment();
  list.forEach((cue, index) => {
    const row = buildRow(cue, index);
    paintRow(row, cue, index);
    fragment.appendChild(row);
    rows.push(row);
  });
  listNode.appendChild(fragment);
}

function patchRow(index) {
  const row = rows[index];
  const cue = cueAt(index);
  if (row && cue) paintRow(row, cue, index);
}

function refreshFlags() {
  rows.forEach((row, index) => {
    row.classList.toggle("is-selected", index === state.selected);
    row.classList.toggle("is-active", index === state.activeCue);
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

  on("job:loaded", renderList);
  on("cues:changed", renderList);
  on("cue:patched", ({ index }) => patchRow(index));
  on("active-cue:changed", refreshFlags);
  on("selection:changed", ({ source }) => {
    refreshFlags();
    if (source !== "list" && state.selected >= 0) {
      rows[state.selected]?.scrollIntoView({ block: "nearest" });
    }
  });
  renderList();
}

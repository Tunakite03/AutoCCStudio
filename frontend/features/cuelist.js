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

function buildRow(cue, index) {
  const row = element("button", "cue-row");
  row.type = "button";
  row.dataset.index = String(index);

  const number = element("span", "cue-row-index", String(index + 1).padStart(2, "0"));
  const main = element("span", "cue-row-main");
  const time = element("span", "cue-row-time");
  time.append(element("span"), element("span", "dur"), element("span", "cps-tag"));
  main.append(time, element("span", "cue-row-text"), element("span", "cue-row-translation"));
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

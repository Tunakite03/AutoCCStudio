/** Window furniture: resizable panes, theme, shortcuts dialog, global file drop. */

import { $, $$, capturePointer } from "../core/dom.js";
import { clamp } from "../core/format.js";
import { toast } from "../core/feedback.js";
import { LOCALES, currentLocale, setLocale, t } from "../core/i18n.js";
import { timeline } from "./timeline-view.js";
import { acceptVideoFile, importSubtitleFile } from "./pipeline/index.js";

const LAYOUT_KEY = "autocc.layout";
const THEME_KEY = "autocc.theme";
const BOUNDS = {
  left: [200, 450],
  right: [260, 560],
  dock: [150, 620],
  inspector: [200, 450],
};

const layout = { left: 268, right: 340, dock: 272, inspector: 390 };

/* ── Resizable panes ──────────────────────────────────────────── */

// Below this window width the workbench drops to a single stacked column
// (see --breakpoint-stack in custom.css) and the left/right tracks stop
// applying, so there is nothing to clamp for. Above it, left+right are still
// two fixed-px grid tracks — a width saved on a wide monitor would otherwise
// overflow a narrower one, since fixed tracks never shrink below their value.
const STACK_BREAKPOINT = 960;
const CENTER_MIN = 320;
const SPLITTER_RAILS = 10; // two 5px splitters between the three columns

function viewportClamp(left, right) {
  if (window.innerWidth < STACK_BREAKPOINT) return { left, right };
  const cap = window.innerWidth - CENTER_MIN - SPLITTER_RAILS;
  if (left + right <= cap) return { left, right };
  const scale = Math.max(0, cap) / (left + right);
  return {
    left: Math.max(BOUNDS.left[0], Math.floor(left * scale)),
    right: Math.max(BOUNDS.right[0], Math.floor(right * scale)),
  };
}

function applyLayout() {
  const root = document.documentElement.style;
  const { left, right } = viewportClamp(layout.left, layout.right);
  root.setProperty("--w-left", `${left}px`);
  root.setProperty("--w-right", `${right}px`);
  root.setProperty("--h-dock", `${layout.dock}px`);
  root.setProperty("--h-inspector", `${layout.inspector}px`);
}

/* These custom properties live on :root, so writing them invalidates style for
   the whole document — every clip and cue row included. A pointermove burst must
   collapse into one write per frame or the drag pays that cost several times
   over between two painted frames. */
let layoutFrame = 0;

function scheduleLayout() {
  if (layoutFrame) return;
  layoutFrame = requestAnimationFrame(() => {
    layoutFrame = 0;
    applyLayout();
  });
}

/** Apply the pending value now — the drag is over and callers need final sizes. */
function flushLayout() {
  if (layoutFrame) cancelAnimationFrame(layoutFrame);
  layoutFrame = 0;
  applyLayout();
}

function persistLayout() {
  localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
}

function loadLayout() {
  try {
    Object.assign(layout, JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}"));
  } catch {
    /* corrupt entry — keep defaults */
  }
  for (const [key, [min, max]] of Object.entries(BOUNDS)) {
    layout[key] = clamp(Number(layout[key]) || layout[key], min, max);
  }
  applyLayout();
}

function initSplitters() {
  const workbench = $("#workbench");

  $$(".splitter").forEach((splitter) => {
    const key = splitter.dataset.split;
    const vertical =
      splitter.classList.contains("splitter-h") ||
      key === "dock" ||
      key === "inspector" ||
      splitter.getAttribute("aria-orientation") === "horizontal";
    // Left pane grows with the pointer; right pane, dock and inspector grow against it.
    const direction = key === "left" ? 1 : -1;

    const resize = (value) => {
      const [min, max] = BOUNDS[key];
      layout[key] = clamp(value, min, max);
      scheduleLayout();
    };

    splitter.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const origin = vertical ? event.clientY : event.clientX;
      const startValue = layout[key];
      capturePointer(splitter, event.pointerId);
      splitter.classList.add("is-active");
      workbench.classList.add(vertical ? "is-resizing-v" : "is-resizing");

      const onMove = (moveEvent) => {
        const delta =
          (vertical ? moveEvent.clientY : moveEvent.clientX) - origin;
        resize(startValue + delta * direction);
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        splitter.classList.remove("is-active");
        workbench.classList.remove("is-resizing", "is-resizing-v");
        // The timeline measures the scroller, so the last size has to be live first.
        flushLayout();
        persistLayout();
        timeline.render();
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    });

    splitter.addEventListener("keydown", (event) => {
      const nudge = {
        ArrowLeft: -16,
        ArrowRight: 16,
        ArrowUp: -16,
        ArrowDown: 16,
      }[event.key];
      if (nudge === undefined) return;
      event.preventDefault();
      resize(layout[key] + nudge * direction);
      flushLayout();
      persistLayout();
      timeline.render();
    });
  });
}

/* ── Theme ────────────────────────────────────────────────────── */

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_KEY, theme);
  timeline.repaint();
}

/* ── Shortcuts dialog ─────────────────────────────────────────── */

export const openShortcuts = () => $("#shortcuts-dialog").showModal();

/* ── Whole-window file drop ───────────────────────────────────── */

function initGlobalDrop() {
  const veil = $("#drop-veil");
  let depth = 0;
  const isFileDrag = (event) =>
    [...(event.dataTransfer?.types || [])].includes("Files");

  window.addEventListener("dragenter", (event) => {
    if (!isFileDrag(event)) return;
    depth += 1;
    veil.classList.add("is-visible");
  });
  window.addEventListener("dragover", (event) => {
    if (isFileDrag(event)) event.preventDefault();
  });
  window.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) veil.classList.remove("is-visible");
  });
  window.addEventListener("drop", (event) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    depth = 0;
    veil.classList.remove("is-visible");
    const file = event.dataTransfer.files[0];
    if (!file) return;
    if (/\.(srt|vtt)$/i.test(file.name)) return importSubtitleFile(file);

    // Mirror the drop into the file input so the run button has something to send.
    const transfer = new DataTransfer();
    transfer.items.add(file);
    $("#video-file").files = transfer.files;
    acceptVideoFile(file);
    toast(t("toast.videoDropped"), "info");
  });
}

/* ── Interface language ───────────────────────────────────────── */

/** Built here rather than in the markup so the list stays with the catalogue. */
function initLocalePicker() {
  const select = $("#ui-locale");
  if (!select) return;
  select.replaceChildren(
    ...LOCALES.map(({ code, name }) => {
      const option = document.createElement("option");
      option.value = code;
      option.textContent = name;
      return option;
    })
  );
  select.value = currentLocale();
  select.addEventListener("change", (event) => setLocale(event.target.value));
}

export function mountShell() {
  applyTheme(localStorage.getItem(THEME_KEY) || "dark");
  loadLayout();
  initSplitters();
  initGlobalDrop();
  initLocalePicker();

  // Re-clamp on resize: shrinking the window (or rotating a tablet) can put the
  // saved pane widths back over the viewport cap even though nothing was dragged.
  window.addEventListener("resize", scheduleLayout);

  $("#theme-btn").addEventListener("click", () => {
    applyTheme(
      document.documentElement.dataset.theme === "light" ? "dark" : "light"
    );
  });
  $("#shortcuts-btn").addEventListener("click", openShortcuts);
  $("#shortcuts-close").addEventListener("click", () =>
    $("#shortcuts-dialog").close()
  );
}

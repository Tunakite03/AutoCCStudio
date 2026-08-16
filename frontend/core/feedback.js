/** The three ways the app talks back: toast, status bar, save indicator. */

import { $ } from "./dom.js";

let toastTimer = 0;

export function toast(message, kind = "info") {
  const node = $("#toast");
  node.textContent = message;
  node.dataset.kind = kind;
  node.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("visible"), 4200);
}

export function setStatus(message, kind = "") {
  const node = $("#status-message");
  node.textContent = message;
  node.dataset.kind = kind;
}

export function setSaveState(label, kind = "") {
  const node = $("#save-state");
  node.textContent = label;
  node.dataset.kind = kind;
}

/** Most failures need both a transient toast and a persistent status line. */
export function reportError(error) {
  const message = error instanceof Error ? error.message : String(error);
  setStatus(message, "error");
  toast(message, "error");
}

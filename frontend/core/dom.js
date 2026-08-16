/** DOM lookup and building helpers. */

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

/** element("span", "cue-row-index", "01") */
export function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

/** Drags must survive the pointer leaving the element; capture is best-effort. */
export function capturePointer(target, pointerId) {
  try {
    target.setPointerCapture(pointerId);
  } catch {
    /* window-level listeners carry the drag regardless */
  }
}

export function releasePointer(target, pointerId) {
  try {
    target.releasePointerCapture(pointerId);
  } catch {
    /* already released */
  }
}

export const isTypingTarget = (node) =>
  node instanceof HTMLElement && node.matches("input, textarea, select");

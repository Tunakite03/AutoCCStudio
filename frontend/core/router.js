/**
 * Screen switching. Small on purpose: one screen visible at a time, the URL hash
 * remembers which, and everything else reacts to the `screen:changed` event.
 *
 * Screens are found by `data-screen` in the markup, so adding one is markup plus
 * a mount call — no registry to keep in sync.
 */

import { $$ } from "./dom.js";
import { emit } from "./store.js";

let current = "";

export const currentScreen = () => current;

/** Scoped to the container: a bare [data-screen] would also match <html>, which
 *  carries the active-screen marker for CSS. */
const screenNodes = () => $$(".screens > [data-screen]");

export function showScreen(name, { silent = false } = {}) {
  const screens = screenNodes();
  const target = screens.find((screen) => screen.dataset.screen === name);
  if (!target) return;

  current = name;
  document.documentElement.dataset.activeScreen = name; // lets CSS hide editor-only chrome
  screens.forEach((screen) => {
    screen.hidden = screen !== target;
  });
  $$(".nav-tab").forEach((tab) => {
    const active = tab.dataset.goto === name;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-current", active ? "page" : "false");
  });
  if (!silent && window.location.hash !== `#/${name}`) {
    window.location.hash = `#/${name}`;
  }
  emit("screen:changed", { name });
}

const fromHash = () => window.location.hash.replace(/^#\/?/, "").trim();

export function startRouter(fallback) {
  $$(".nav-tab").forEach((tab) => {
    tab.addEventListener("click", () => showScreen(tab.dataset.goto));
  });
  window.addEventListener("hashchange", () => {
    const name = fromHash();
    if (name && name !== current) showScreen(name, { silent: true });
  });
  showScreen(fromHash() || fallback, { silent: true });
}

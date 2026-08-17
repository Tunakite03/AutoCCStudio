/**
 * Interface text, in one place per language.
 *
 * Three kinds of string arrive here. Static markup carries `data-i18n` and is
 * filled once at boot. Code calls `t("key", params)`. And the backend sends
 * `{code, params}` pairs — errors, progress ticks, option hints — which `tm()`
 * resolves against the same catalogue, so a message chosen by the server is
 * still written in the language of the browser reading it.
 *
 * Language names in the pickers are deliberately *not* catalogue entries: the
 * markup holds the endonym and `Intl.DisplayNames` supplies the rest, which is
 * how a hundred options stay out of two hundred catalogue lines.
 */

import { en } from "../i18n/en.js";
import { vi } from "../i18n/vi.js";

const CATALOGUES = { vi, en };
const STORAGE_KEY = "autocc.locale";
const FALLBACK = "vi";

/** Offered in the picker, by endonym — a language names itself in every locale. */
export const LOCALES = [
  { code: "vi", name: "Tiếng Việt" },
  { code: "en", name: "English" },
];

function detect() {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored && CATALOGUES[stored]) return stored;
  const offered = navigator.languages?.length ? navigator.languages : [navigator.language || ""];
  const preferred = offered
    .map((tag) => String(tag).toLowerCase().split("-")[0])
    .find((code) => CATALOGUES[code]);
  return preferred || FALLBACK;
}

let locale = detect();

export const currentLocale = () => locale;

/**
 * Switch language and reload.
 *
 * A live swap would mean every view re-rendering its own text on demand — a lot
 * of surface, and a lot of ways to leave one label behind, for a setting that is
 * changed once. The reload costs a round trip to a local server and is exact.
 */
export function setLocale(next) {
  if (!CATALOGUES[next] || next === locale) return;
  localStorage.setItem(STORAGE_KEY, next);
  location.reload();
}

const isMessage = (value) =>
  Boolean(value) && typeof value === "object" && typeof value.code === "string";

/** `{name}` placeholders. A param that is itself a message is rendered first. */
function fill(template, params) {
  return template.replace(/\{(\w+)\}/g, (whole, name) => {
    if (!(name in params)) return whole;
    const value = params[name];
    return isMessage(value) ? tm(value) : String(value);
  });
}

export function t(key, params) {
  const template = CATALOGUES[locale][key] ?? CATALOGUES[FALLBACK][key];
  if (template === undefined) {
    // Loud, but never fatal: the key shows on screen and is grep-able from there.
    console.warn(`i18n: no entry for "${key}"`);
    return key;
  }
  return params ? fill(template, params) : template;
}

/**
 * Render a `{code, params}` pair from the backend.
 *
 * Tolerates a plain string because a job.json written before the codes existed
 * still carries one, and shows an unknown code as itself rather than swallowing
 * a failure the user needs to see.
 */
export function tm(message, fallbackKey) {
  if (message === null || message === undefined || message === "") {
    return fallbackKey ? t(fallbackKey) : "";
  }
  if (typeof message === "string") return message;
  if (!isMessage(message)) return String(message);
  return t(message.code, message.params || {});
}

/** An option built from a capabilities entry: `Nova-3 — recommended`. */
export function optionLabel({ value, name, hint }) {
  const shown = name || value;
  return hint ? `${shown} — ${tm(hint)}` : shown;
}

/* ── Static markup ────────────────────────────────────────────────── */

export function applyDocumentLanguage() {
  document.documentElement.lang = locale;
}

/**
 * Fill every `data-i18n*` node under `root`.
 *
 *   data-i18n="key"                     textContent
 *   data-i18n-html="key"                innerHTML, for text with a <kbd> in it
 *   data-i18n-attr="title:key;aria-label:other"
 */
export function applyStaticText(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  // Catalogue text only — never anything a user, a file or a provider wrote.
  root.querySelectorAll("[data-i18n-html]").forEach((node) => {
    node.innerHTML = t(node.dataset.i18nHtml);
  });
  root.querySelectorAll("[data-i18n-attr]").forEach((node) => {
    for (const pair of node.dataset.i18nAttr.split(";")) {
      const [name, key] = pair.split(":").map((part) => part.trim());
      if (name && key) node.setAttribute(name, t(key));
    }
  });
}

const LANGUAGE_TAG = /^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$/;

/**
 * Append the interface-language name to each option of a language picker.
 *
 * The markup keeps the endonym — 日本語 is 日本語 to everyone — and Intl adds
 * "Tiếng Nhật" or "Japanese" beside it. Options that carry their own `data-i18n`
 * (auto-detect, multilingual) are left alone, as is anything Intl has no name
 * for: the endonym on its own is still a correct label.
 */
export function annotateLanguageOptions(select) {
  if (!select) return;
  let names;
  try {
    names = new Intl.DisplayNames([locale], { type: "language" });
  } catch {
    return;
  }
  for (const option of select.options) {
    const code = option.dataset.lang || option.value;
    if (option.dataset.i18n || !LANGUAGE_TAG.test(code)) continue;
    const endonym = option.textContent.trim();
    let named;
    try {
      named = names.of(code);
    } catch {
      continue;
    }
    if (!named || named === code || named === endonym) continue;
    option.textContent = `${endonym} · ${named}`;
  }
}

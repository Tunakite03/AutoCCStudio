/** Shared formatting and caption-quality helpers. */

import { currentLocale, t } from "./i18n.js";

export const MIN_CUE_DURATION = 0.12;

/** Reading speed thresholds used by broadcast subtitling guides (chars per second). */
export const CPS_COMFORTABLE = 17;
export const CPS_LIMIT = 21;

export const clamp = (value, min, max) => Math.min(Math.max(value, min), max);

export function formatTimecode(seconds) {
  const total = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = Math.floor(total % 60);
  const millis = Math.round((total - Math.floor(total)) * 1000);
  const pad = (value, size = 2) => String(value).padStart(size, "0");
  return `${pad(hours)}:${pad(minutes)}:${pad(secs)}.${pad(millis, 3)}`;
}

/** MM:SS.mmm — the density subtitle work actually reads at. */
export const shortTimecode = (seconds) => formatTimecode(seconds).slice(3);

/** Short label for the ruler: drops hours until the timeline actually needs them. */
export function formatRulerLabel(seconds, withMillis) {
  const total = Math.max(0, seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value) => String(Math.floor(value)).padStart(2, "0");
  const tail = withMillis ? secs.toFixed(1).padStart(4, "0") : pad(secs);
  return hours > 0 ? `${hours}:${pad(minutes)}:${tail}` : `${pad(minutes)}:${tail}`;
}

/** Accepts 12.5 · 1:02.5 · 00:01:02.500 and returns seconds, or null when unreadable. */
export function parseTimecode(input) {
  const raw = String(input ?? "").trim().replace(",", ".");
  if (!raw) return null;
  const parts = raw.split(":");
  if (parts.some((part) => part !== "" && !/^\d*\.?\d*$/.test(part))) return null;
  const numbers = parts.map((part) => Number(part || 0));
  if (numbers.some((value) => !Number.isFinite(value))) return null;
  if (numbers.length === 1) return numbers[0];
  if (numbers.length === 2) return numbers[0] * 60 + numbers[1];
  if (numbers.length === 3) return numbers[0] * 3600 + numbers[1] * 60 + numbers[2];
  return null;
}

export const cueDuration = (cue) => Math.max(0, Number(cue.end) - Number(cue.start));

/** Characters the viewer must read per second — the number that decides if a cue is usable. */
export function charsPerSecond(cue) {
  const line = (cue.translation || cue.text || "").replace(/\s+/g, " ").trim();
  const duration = cueDuration(cue);
  if (!line || duration <= 0) return 0;
  return line.length / duration;
}

export function cpsSeverity(cps) {
  if (cps <= 0) return "none";
  if (cps <= CPS_COMFORTABLE) return "ok";
  if (cps <= CPS_LIMIT) return "warn";
  return "crit";
}

/** Compact running time for lists: 1:23:45 or 4:07. */
export function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
}

/** Each step is the point at which the unit below it stops being readable. */
const RELATIVE_STEPS = [
  [60, "second", 1],
  [3600, "minute", 60],
  [86400, "hour", 3600],
  [604800, "day", 86400],
];

/** "3 phút trước" / "3 minutes ago" — Intl owns the wording and the plural. */
export function formatRelativeTime(epochSeconds) {
  if (!epochSeconds) return t("time.never");
  const locale = currentLocale();
  const elapsed = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (elapsed < 45) return t("time.justNow");
  const relative = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  for (const [limit, unit, divisor] of RELATIVE_STEPS) {
    if (elapsed < limit) return relative.format(-Math.round(elapsed / divisor), unit);
  }
  return new Date(epochSeconds * 1000).toLocaleDateString(locale);
}

export function formatFileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 KB";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export const cssVar = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

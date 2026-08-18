/** Every backend call in one place — the only module that knows a URL shape. */

import { t, tm } from "./i18n.js";

/**
 * What went wrong, in this locale.
 *
 * The backend sends `detail` as `{code, params}`, so the sentence is written
 * here rather than there. FastAPI's own validation errors arrive as a list and
 * mean nothing to a user, so those fall back to the status code.
 */
function failureText(body, status) {
  const detail = body !== null && typeof body === "object" ? body.detail : body;
  if (Array.isArray(detail)) return t("err.http", { status });
  return tm(detail) || t("err.http", { status });
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  // 204 has no body by definition, but FastAPI still labels it as JSON — and
  // parsing an empty body throws, which turned a successful delete into an error.
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(failureText(body, response.status));
  return body;
}

export const api = {
  capabilities: () => request("/api/capabilities"),

  jobs: () => request("/api/jobs"),

  job: (jobId) => request(`/api/jobs/${jobId}`),

  deleteJob: (jobId) => request(`/api/jobs/${jobId}`, { method: "DELETE" }),

  waveform: (jobId) => request(`/api/jobs/${jobId}/waveform`),

  importSubtitle(file) {
    const form = new FormData();
    form.append("file", file);
    return request("/api/jobs/import-subtitle", { method: "POST", body: form });
  },

  transcribe: (form) => request("/api/jobs/transcribe", { method: "POST", body: form }),

  /** Re-run recognition on the video the job already holds — no upload. */
  retranscribe: (jobId, form) => request(`/api/jobs/${jobId}/transcribe`, { method: "POST", body: form }),

  analyzeSpeakers: (jobId) =>
    request(`/api/jobs/${jobId}/analyze-speakers`, { method: "POST" }),

  splitLongCues: (jobId) =>
    request(`/api/jobs/${jobId}/split-long-cues`, { method: "POST" }),


  /** The styles this install's users have saved. Presets ride on capabilities;
   *  these change whenever somebody saves one, so they have their own call. */
  styles: () => request("/api/styles"),

  createStyle: (name, base, notes) =>
    request("/api/styles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, base, notes }),
    }),

  /** Only the fields given are changed — a rename leaves the rules alone. */
  updateStyle: (styleId, changes) =>
    request(`/api/styles/${styleId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }),

  deleteStyle: (styleId) => request(`/api/styles/${styleId}`, { method: "DELETE" }),

  /** `fromCue` is 0-based; cues before it keep the translation they already have. */
  translate: (
    jobId,
    targetLanguage,
    style = "auto",
    styleNotes = "",
    provider = "",
    model = "",
    fromCue = 0,
    styleRef = "",
  ) =>
    request(`/api/jobs/${jobId}/translate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_language: targetLanguage,
        style,
        style_notes: styleNotes,
        provider,
        model,
        from_cue: fromCue,
        style_ref: styleRef,
      }),
    }),

  /** Read the project aloud. `null` fields mean "use the server's own default". */
  dub: (jobId, { voice = "", provider = "", originalGain = null, shorten = null } = {}) =>
    request(`/api/jobs/${jobId}/dub`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        voice,
        provider,
        original_gain: originalGain,
        shorten,
      }),
    }),

  /** Ask the worker to stop. It lands at its next checkpoint, not instantly. */
  cancel: (jobId) => request(`/api/jobs/${jobId}/cancel`, { method: "POST" }),

  saveCues: (jobId, cues) =>
    request(`/api/jobs/${jobId}/cues`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cues }),
    }),

  /** `audio` is original | dubbed | both — what the exported MP4 will play. */
  async mux(jobId, audio = "original") {
    const response = await fetch(`/api/jobs/${jobId}/mux?audio=${audio}`, { method: "POST" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(tm(body.detail, "err.muxFailed"));
    }
    return response.blob();
  },

  videoUrl: (jobId) => `/api/jobs/${jobId}/video`,
  // Cache-busted by revision: a second dub of the same project is a different
  // recording at the same URL, and the player would otherwise keep the first.
  dubAudioUrl: (jobId, revision = 0) => `/api/jobs/${jobId}/dub-audio?v=${revision}`,
  thumbnailUrl: (jobId) => `/api/jobs/${jobId}/thumbnail`,
  eventsUrl: (jobId) => `/api/jobs/${jobId}/events`,
  downloadUrl: (jobId, track, format) => `/api/jobs/${jobId}/download?track=${track}&format=${format}`,
};

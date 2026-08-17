/** Every backend call in one place — the only module that knows a URL shape. */

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    throw new Error((typeof body === "object" ? body.detail : body) || `Lỗi ${response.status}`);
  }
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


  /** `fromCue` is 0-based; cues before it keep the translation they already have. */
  translate: (
    jobId,
    targetLanguage,
    style = "auto",
    styleNotes = "",
    provider = "",
    model = "",
    fromCue = 0,
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

  async mux(jobId) {
    const response = await fetch(`/api/jobs/${jobId}/mux`, { method: "POST" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || "Không ghép được phụ đề vào video");
    }
    return response.blob();
  },

  videoUrl: (jobId) => `/api/jobs/${jobId}/video`,
  thumbnailUrl: (jobId) => `/api/jobs/${jobId}/thumbnail`,
  eventsUrl: (jobId) => `/api/jobs/${jobId}/events`,
  downloadUrl: (jobId, track, format) => `/api/jobs/${jobId}/download?track=${track}&format=${format}`,
};

/**
 * The projects screen: everything on disk, what state it is in, and what it costs.
 *
 * It reads its own list from the API rather than the editor's store — a job the
 * editor never opened is still a project here.
 */

import { $, $$, element } from "../core/dom.js";
import { api } from "../core/api.js";
import { confirmAction } from "../core/confirm.js";
import { reportError, setStatus, toast } from "../core/feedback.js";
import { formatDuration, formatFileSize, formatRelativeTime } from "../core/format.js";
import { on, state } from "../core/store.js";
import { showScreen } from "../core/router.js";
import { adoptJob, forgetJob, stopEvents, watchJob } from "./jobs.js";

const grid = $("#project-grid");
const emptyNote = $("#dash-empty");

let projects = [];
let filter = "all";
let search = "";

/* ── Data ─────────────────────────────────────────────────────── */

export async function refreshProjects() {
  try {
    const { jobs } = await api.jobs();
    projects = jobs;
    render();
  } catch (error) {
    reportError(error);
  }
}

const isTranslated = (project) =>
  project.cue_count > 0 && project.translated_count >= project.cue_count;

/** Anything a person would want to look at again: failed, empty, or still running. */
const needsAttention = (project) =>
  project.status === "error" || project.status === "processing" || project.cue_count === 0;

function visibleProjects() {
  const needle = search.trim().toLowerCase();
  return projects.filter((project) => {
    if (needle && !project.name.toLowerCase().includes(needle)) return false;
    if (filter === "translated") return isTranslated(project);
    if (filter === "untranslated") return !isTranslated(project) && project.cue_count > 0;
    if (filter === "problem") return needsAttention(project);
    return true;
  });
}

/* ── Rendering ────────────────────────────────────────────────── */

function renderStats() {
  const totalDuration = projects.reduce((sum, item) => sum + item.duration_seconds, 0);
  const totalCues = projects.reduce((sum, item) => sum + item.cue_count, 0);
  const totalTranslated = projects.reduce((sum, item) => sum + item.translated_count, 0);
  const totalBytes = projects.reduce((sum, item) => sum + item.size_bytes, 0);
  const withVideo = projects.filter((item) => item.video_available).length;
  const attention = projects.filter(needsAttention).length;

  $("#stat-projects").textContent = String(projects.length);
  $("#stat-projects-note").textContent = attention
    ? `${withVideo} có video · ${attention} cần xem lại`
    : `${withVideo} có video`;

  $("#stat-duration").textContent = formatDuration(totalDuration);
  $("#stat-duration-note").textContent = `${projects.filter((item) => item.cue_count).length} project có phụ đề`;

  $("#stat-cues-total").textContent = totalCues.toLocaleString("vi-VN");
  const share = totalCues ? Math.round((totalTranslated / totalCues) * 100) : 0;
  $("#stat-cues-note").textContent = `${share}% đã dịch`;

  $("#stat-disk").textContent = formatFileSize(totalBytes);
  const reclaimable = projects.filter(needsAttention).reduce((sum, item) => sum + item.size_bytes, 0);
  $("#stat-disk-note").textContent = reclaimable
    ? `${formatFileSize(reclaimable)} ở project cần xem lại`
    : "workspace gọn gàng";

  $("#dash-subtitle").textContent = projects.length
    ? `${projects.length} project trong workspace cục bộ`
    : "Workspace đang trống";
}

function statusOf(project) {
  if (project.status === "processing") return { label: "Đang xử lý", tone: "busy" };
  if (project.status === "error") return { label: "Lỗi", tone: "error" };
  if (!project.cue_count) return { label: "Chưa có cue", tone: "idle" };
  if (isTranslated(project)) return { label: "Đã dịch", tone: "done" };
  return { label: "Chưa dịch", tone: "idle" };
}

function buildCard(project) {
  const card = element("article", "project-card");
  card.dataset.id = project.id;
  if (project.id === state.job?.id) card.classList.add("is-open");

  /* Thumbnail */
  const media = element("button", "project-thumb");
  media.type = "button";
  media.title = "Mở project";
  if (project.video_available) {
    const image = element("img");
    image.loading = "lazy";
    image.alt = "";
    image.src = api.thumbnailUrl(project.id);
    // No ffmpeg or no decodable frame — fall back to the placeholder mark.
    image.addEventListener("error", () => image.remove(), { once: true });
    media.appendChild(image);
  }
  media.insertAdjacentHTML(
    "beforeend",
    project.video_available
      ? '<span class="thumb-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M9 7.5 17 12l-8 4.5Z"/></svg></span>'
      : '<span class="thumb-mark subtitle-only" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M5 7h14M5 11h9M5 15h12"/></svg></span>',
  );
  const status = statusOf(project);
  const badge = element("span", `project-status ${status.tone}`, status.label);
  media.appendChild(badge);
  media.addEventListener("click", () => openProject(project.id));

  /* Body */
  const body = element("div", "project-body");
  // Not "project-name": the titlebar already owns that class and its nowrap rule
  // would cancel this card's two-line clamp.
  const title = element("h3", "card-name", project.name);
  title.title = project.name;

  const meta = element("p", "project-meta mono");
  const language = project.detected_language || project.source_language || "—";
  meta.textContent = `${project.cue_count} cue · ${formatDuration(project.duration_seconds)} · ${language}`;

  body.append(title, meta);

  /* Translation progress — the one number that says how far the work got */
  if (project.cue_count) {
    const done = Math.round((project.translated_count / project.cue_count) * 100);
    const bar = element("div", "progress");
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", `Đã dịch ${done}%`);
    const fill = element("span", "progress-fill");
    fill.style.width = `${done}%`;
    if (done === 100) fill.classList.add("is-complete");
    bar.appendChild(fill);
    const legend = element("span", "progress-legend mono", `${done}% dịch`);
    body.append(bar, legend);
  }

  /* Footer */
  const footer = element("div", "project-foot");
  footer.append(
    element("span", "project-time", formatRelativeTime(project.updated_at)),
    element("span", "project-size mono", formatFileSize(project.size_bytes)),
  );

  const actions = element("div", "project-actions");
  const open = element("button", "tool", "Mở");
  open.type = "button";
  open.addEventListener("click", () => openProject(project.id));
  const remove = element("button", "tool danger icon");
  remove.type = "button";
  remove.title = "Xóa project";
  remove.setAttribute("aria-label", `Xóa ${project.name}`);
  remove.innerHTML = '<svg viewBox="0 0 24 24"><path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"/></svg>';
  remove.addEventListener("click", () => askDelete(project));
  actions.append(open, remove);
  footer.appendChild(actions);

  card.append(media, body, footer);
  return card;
}

function render() {
  renderStats();
  const visible = visibleProjects();
  $("#dash-count").textContent = `${visible.length} project`;
  grid.replaceChildren(...visible.map(buildCard));

  emptyNote.hidden = visible.length > 0;
  if (!visible.length) {
    const filtered = projects.length > 0;
    $("#dash-empty-title").textContent = filtered ? "Không có project nào khớp" : "Chưa có project nào";
    $("#dash-empty-note").textContent = filtered
      ? "Thử bỏ bộ lọc hoặc xóa từ khóa tìm kiếm."
      : "Tạo phụ đề cho video đầu tiên để nó xuất hiện ở đây.";
  }
}

/* ── Actions ──────────────────────────────────────────────────── */

async function openProject(jobId) {
  try {
    const job = await api.job(jobId);
    stopEvents();
    adoptJob(job);
    showScreen("editor");
    setStatus(`Đã mở ${job.video_name || job.subtitle_name || "project"}`);
    if (job.status === "processing") watchJob(job.id);
  } catch (error) {
    reportError(error);
  }
}

async function askDelete(project) {
  const confirmed = await confirmAction({
    title: "Xóa project?",
    target: project.name,
    note: "Video, phụ đề và metadata trong thư mục project sẽ bị xóa khỏi đĩa. Không hoàn tác được.",
    confirmLabel: "Xóa vĩnh viễn",
    cancelLabel: "Giữ lại",
  });
  if (!confirmed) return;
  try {
    await api.deleteJob(project.id);
    if (state.job?.id === project.id) forgetJob();
    projects = projects.filter((item) => item.id !== project.id);
    render();
    toast(`Đã xóa ${project.name}`, "success");
  } catch (error) {
    reportError(error);
  }
}

/* ── Mount ────────────────────────────────────────────────────── */

export function mountDashboard() {
  $("#dash-search").addEventListener("input", (event) => {
    search = event.target.value;
    render();
  });

  $$("#dash-filters .seg").forEach((button) => {
    button.addEventListener("click", () => {
      $$("#dash-filters .seg").forEach((item) => item.classList.remove("is-active"));
      button.classList.add("is-active");
      filter = button.dataset.filter;
      render();
    });
  });

  $("#dash-new").addEventListener("click", () => {
    showScreen("editor");
    $("#video-file").click();
  });

  // The list goes stale while the editor works, so re-read it on every visit.
  on("screen:changed", ({ name }) => {
    if (name === "projects") refreshProjects();
  });
}

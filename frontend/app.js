/**
 * AutoCC Studio — entry point.
 *
 * Boot order matters twice: the shell sets the theme before the timeline paints
 * its canvases, and every view mounts before data arrives so the first
 * `job:loaded` finds subscribers waiting.
 *
 * Adding a screen means writing one more feature module, giving its markup a
 * `data-screen` attribute, and mounting it here — no existing module changes,
 * because views talk to the store, not to each other.
 */

import { startRouter } from "./core/router.js";
import { mountCueList } from "./features/cuelist.js";
import { mountDashboard } from "./features/dashboard.js";
import { mountEditing } from "./features/editing.js";
import { mountHistory } from "./features/history.js";
import { mountInspector } from "./features/inspector.js";
import { hasLastJob, mountJobs, restoreLastJob } from "./features/jobs.js";
import { mountKeymap } from "./features/keymap.js";
import { loadCapabilities, mountPipeline } from "./features/pipeline.js";
import { mountShell } from "./features/shell.js";
import { mountTimelineView } from "./features/timeline-view.js";
import { mountTransport } from "./features/transport.js";

function boot() {
  mountShell();
  mountTransport();
  mountTimelineView();
  mountCueList();
  mountInspector();
  mountEditing();
  mountHistory();
  mountJobs();
  mountPipeline();
  mountDashboard();
  mountKeymap();

  // Land where the work is: the editor when a project was open, otherwise the list.
  startRouter(hasLastJob() ? "editor" : "projects");

  loadCapabilities();
  restoreLastJob();
}

boot();

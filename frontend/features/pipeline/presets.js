/**
 * Pipeline — Translation Style Presets and User-Saved Styles.
 *
 * Manages preset style definitions, user custom styles CRUD (backend /api/styles),
 * style select options binding, and rules box overwrite protection.
 */

import { $ } from "../../core/dom.js";
import { api } from "../../core/api.js";
import { confirmAction, promptAction } from "../../core/confirm.js";
import { reportError, toast } from "../../core/feedback.js";
import { t, tm } from "../../core/i18n.js";

/**
 * A saved style is a *shortcut*, not a third kind of style.
 *
 * It holds a preset plus a block of house rules, and choosing it does nothing
 * more than put those two into the controls that were already there. So the
 * translator never learns about saved styles, the rules box always shows what
 * will actually be sent, and deleting a style cannot change how an old project
 * was translated — the project kept the preset and the rules, not a reference.
 *
 * The prefix keeps the picker's values apart: no backend style key contains ":".
 */
export const SAVED_PREFIX = "saved:";

let presetStyles = [];
let savedStyles = [];
// The rules we last wrote into the box ourselves. Anything else in there was
// typed by a person and is never overwritten without asking.
let appliedStyleNotes = null;
// What the picker was on before the current change, so a refused overwrite can
// put it back rather than leaving a style selected whose rules were not applied.
let lastStyleValue = "";

export const savedStyleById = (id) => savedStyles.find((style) => style.id === id) || null;

/** The saved style the picker is on, or null when it is on a preset. */
export function selectedSavedStyle() {
  const value = $("#translation-style").value;
  return value.startsWith(SAVED_PREFIX)
    ? savedStyleById(value.slice(SAVED_PREFIX.length))
    : null;
}

/** What the translate call actually sends: a preset key and the rules box.
 *  `ref` only names the shortcut, so the picker can show it again later. */
export function styleRequest() {
  const saved = selectedSavedStyle();
  return {
    style: saved ? saved.base : $("#translation-style").value,
    notes: $("#translation-style-notes").value,
    ref: saved ? saved.id : "",
  };
}

/** The style presets live in the backend, so the picker is built from them. */
export function renderStyleOptions() {
  if (!presetStyles.length) return;
  const select = $("#translation-style");
  const preferred = select.value;
  const option = (value, label) => {
    const node = document.createElement("option");
    node.value = value;
    node.textContent = label;
    return node;
  };

  const nodes = presetStyles.map((item) => option(item.value, tm({ code: item.label_code })));
  if (savedStyles.length) {
    const group = document.createElement("optgroup");
    group.label = t("style.savedGroup");
    group.append(...savedStyles.map((style) => option(SAVED_PREFIX + style.id, style.name)));
    nodes.push(group);
  }
  select.replaceChildren(...nodes);

  if ([...select.options].some((item) => item.value === preferred)) {
    select.value = preferred;
  }
  lastStyleValue = select.value;
  refreshStyleButtons();
}

export function syncStyleOptions(capabilities) {
  presetStyles = capabilities?.translation_styles || [];
  renderStyleOptions();
}

export async function loadSavedStyles() {
  try {
    savedStyles = (await api.styles()).styles || [];
  } catch {
    // Presets still translate, so a list that will not load is not worth a
    // toast on boot — the next save reports the real failure.
    savedStyles = [];
  }
  renderStyleOptions();
}

export function refreshStyleButtons() {
  $("#style-delete").hidden = !selectedSavedStyle();
}

export const presetLabelCode = (value) =>
  presetStyles.find((item) => item.value === value)?.label_code || "style.auto";

/**
 * Picking a style fills the rules box with what it holds, because that box is
 * what travels to the translator — leaving the previous style's rules under a
 * new name would translate the film with rules nobody chose.
 *
 * Text the user typed themselves is only replaced after they say so.
 */
export async function applySelectedStyle() {
  const select = $("#translation-style");
  const box = $("#translation-style-notes");
  const saved = selectedSavedStyle();
  const wanted = saved ? saved.notes : "";
  const current = box.value.trim();

  if (current === wanted.trim()) {
    appliedStyleNotes = saved ? saved.notes : null;
    lastStyleValue = select.value;
    return refreshStyleButtons();
  }

  const ours = appliedStyleNotes !== null && current === appliedStyleNotes.trim();
  if (current && !ours) {
    const replace = await confirmAction({
      title: t("style.replaceNotesTitle"),
      target: saved ? saved.name : tm({ code: presetLabelCode(select.value) }),
      note: t("style.replaceNotesNote"),
      confirmLabel: t("action.replaceNotes"),
      cancelLabel: t("action.keepNotes"),
      variant: "warning",
    });
    if (!replace) {
      // Their rules stay, so the style that would have replaced them cannot.
      select.value = lastStyleValue;
      return refreshStyleButtons();
    }
  }

  box.value = wanted;
  appliedStyleNotes = saved ? saved.notes : null;
  lastStyleValue = select.value;
  refreshStyleButtons();
}

/** Save the picker and the rules box together, under a name of the user's own. */
export async function saveStyle() {
  const saved = selectedSavedStyle();
  const name = await promptAction({
    title: t("style.saveTitle"),
    note: t("style.saveNote"),
    value: saved ? saved.name : "",
    placeholder: t("style.namePlaceholder"),
    maxLength: 60,
    confirmLabel: t("action.save"),
  });
  if (name === null) return;

  const base = saved ? saved.base : $("#translation-style").value;
  const notes = $("#translation-style-notes").value;
  const existing = savedStyles.find(
    (style) => style.name.toLowerCase() === name.toLowerCase(),
  );

  try {
    let style;
    if (existing) {
      // Reusing the name of a style that is not the one open is an overwrite,
      // and the backend would refuse it as a duplicate anyway.
      if (existing.id !== saved?.id) {
        const overwrite = await confirmAction({
          title: t("style.overwriteTitle"),
          target: existing.name,
          note: t("style.overwriteNote"),
          confirmLabel: t("action.overwrite"),
          variant: "warning",
        });
        if (!overwrite) return;
      }
      style = await api.updateStyle(existing.id, { base, notes });
    } else {
      style = await api.createStyle(name, base, notes);
    }

    await loadSavedStyles();
    $("#translation-style").value = SAVED_PREFIX + style.id;
    appliedStyleNotes = style.notes;
    lastStyleValue = $("#translation-style").value;
    refreshStyleButtons();
    toast(t("toast.styleSaved", { name: style.name }), "success");
  } catch (error) {
    reportError(error);
  }
}

export async function deleteStyle() {
  const saved = selectedSavedStyle();
  if (!saved) return;
  const confirmed = await confirmAction({
    title: t("style.deleteTitle"),
    target: saved.name,
    note: t("style.deleteNote"),
    confirmLabel: t("action.delete"),
  });
  if (!confirmed) return;

  try {
    await api.deleteStyle(saved.id);
    await loadSavedStyles();
    // Back to the preset it was built on, with its rules left in the box:
    // deleting the shortcut is not a request to stop translating this way.
    $("#translation-style").value = saved.base;
    appliedStyleNotes = null;
    lastStyleValue = saved.base;
    refreshStyleButtons();
    toast(t("toast.styleDeleted", { name: saved.name }), "success");
  } catch (error) {
    reportError(error);
  }
}

export function setAppliedStyleNotes(val) {
  appliedStyleNotes = val;
}

export function setLastStyleValue(val) {
  lastStyleValue = val;
}

/**
 * One modal for every destructive action, awaited like `window.confirm`.
 *
 *   if (!(await confirmAction({ title: t("confirm.deleteTitle"), ... }))) return;
 *
 * `promptAction` is the same dialog with a single text field, awaited like
 * `window.prompt`: it answers with the typed string, or `null` if the user
 * backed out. Both share one `<dialog>`, so each opening states whether the
 * field is there — a stale input left visible would ask for a name nobody wants.
 *
 * Callers pass text, not keys: most of these sentences name the thing being
 * acted on, so they are composed with `t()` at the call site.
 *
 * The answer comes from the buttons and the Escape key, never from the dialog's
 * `close` event: that event is not dispatched in every embedded Chrome build,
 * and a confirmation that never resolves silently swallows the action.
 */

import { $ } from "./dom.js";
import { t } from "./i18n.js";

const DANGER_BUTTON =
  "h-8 px-3.5 rounded-sm bg-crit text-white text-[12.5px] font-semibold transition-[filter] duration-120 hover:brightness-110";
const ACCENT_BUTTON =
  "h-8 px-3.5 rounded-sm bg-accent text-accent-ink text-[12.5px] font-semibold transition-[filter] duration-120 hover:brightness-110";

/**
 * Open the shared dialog and resolve with `true`/`false`, or — when `field` is
 * given — with the typed string or `null`.
 */
function openDialog({
  title,
  target = "",
  note = "",
  confirmLabel = t("action.agree"),
  cancelLabel = t("action.cancel"),
  variant = "danger",
  field = null,
}) {
  const dialog = $("#confirm-dialog");
  const okButton = $("#confirm-ok");
  const cancelButton = $("#confirm-cancel");
  const targetNode = $("#confirm-target");
  const input = $("#confirm-input");

  $("#confirm-title").textContent = title;
  targetNode.textContent = target;
  targetNode.hidden = !target;
  $("#confirm-note").textContent = note;
  okButton.textContent = confirmLabel;
  cancelButton.textContent = cancelLabel;

  input.hidden = !field;
  if (field) {
    input.value = field.value || "";
    input.placeholder = field.placeholder || "";
    if (field.maxLength) input.maxLength = field.maxLength;
    else input.removeAttribute("maxlength");
  }

  okButton.className =
    variant === "warning" || variant === "primary" || variant === "accent"
      ? ACCENT_BUTTON
      : DANGER_BUTTON;

  return new Promise((resolve) => {
    let settled = false;

    const finish = (answer) => {
      if (settled) return;
      settled = true;
      okButton.removeEventListener("click", onOk);
      cancelButton.removeEventListener("click", onCancel);
      input.removeEventListener("keydown", onInputKey);
      dialog.removeEventListener("keydown", onKey);
      dialog.removeEventListener("close", onDismiss);
      dialog.removeEventListener("cancel", onDismiss);
      if (dialog.open) dialog.close();
      resolve(answer);
    };

    const accept = () => {
      if (!field) return finish(true);
      // An empty name is not an answer, it is a half-finished one: keep the
      // dialog open rather than saving something the picker cannot show.
      const typed = input.value.trim();
      if (!typed) return input.focus();
      finish(typed);
    };

    const onOk = () => accept();
    const onCancel = () => finish(field ? null : false);
    const onDismiss = () => finish(field ? null : false);
    const onInputKey = (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        accept();
      }
    };
    const onKey = (event) => {
      if (event.key === "Escape") finish(field ? null : false);
    };

    okButton.addEventListener("click", onOk);
    cancelButton.addEventListener("click", onCancel);
    input.addEventListener("keydown", onInputKey);
    dialog.addEventListener("keydown", onKey);
    dialog.addEventListener("close", onDismiss);
    dialog.addEventListener("cancel", onDismiss);
    dialog.showModal();
    // The field is the reason the dialog opened, so it takes the caret; without
    // this the first keystroke lands on the confirm button instead.
    if (field) input.select();
  });
}

export function confirmAction(options) {
  return openDialog({ ...options, field: null });
}

/** The typed text, trimmed — or `null` if the user cancelled. */
export function promptAction({
  title,
  note = "",
  value = "",
  placeholder = "",
  maxLength = 0,
  confirmLabel = t("action.agree"),
  cancelLabel = t("action.cancel"),
  variant = "accent",
}) {
  return openDialog({
    title,
    note,
    confirmLabel,
    cancelLabel,
    variant,
    field: { value, placeholder, maxLength },
  });
}

/**
 * One modal for every destructive action, awaited like `window.confirm`.
 *
 *   if (!(await confirmAction({ title: t("confirm.deleteTitle"), ... }))) return;
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

export function confirmAction({
  title,
  target = "",
  note = "",
  confirmLabel = t("action.agree"),
  cancelLabel = t("action.cancel"),
  variant = "danger",
}) {
  const dialog = $("#confirm-dialog");
  const okButton = $("#confirm-ok");
  const cancelButton = $("#confirm-cancel");
  const targetNode = $("#confirm-target");

  $("#confirm-title").textContent = title;
  targetNode.textContent = target;
  targetNode.hidden = !target;
  $("#confirm-note").textContent = note;
  okButton.textContent = confirmLabel;
  cancelButton.textContent = cancelLabel;

  if (variant === "warning" || variant === "primary" || variant === "accent") {
    okButton.className =
      "h-8 px-3.5 rounded-sm bg-accent text-accent-ink text-[12.5px] font-semibold transition-[filter] duration-120 hover:brightness-110";
  } else {
    okButton.className =
      "h-8 px-3.5 rounded-sm bg-crit text-white text-[12.5px] font-semibold transition-[filter] duration-120 hover:brightness-110";
  }

  return new Promise((resolve) => {
    let settled = false;

    const finish = (answer) => {
      if (settled) return;
      settled = true;
      okButton.removeEventListener("click", onOk);
      cancelButton.removeEventListener("click", onCancel);
      dialog.removeEventListener("keydown", onKey);
      dialog.removeEventListener("close", onDismiss);
      dialog.removeEventListener("cancel", onDismiss);
      if (dialog.open) dialog.close();
      resolve(answer);
    };

    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onDismiss = () => finish(false);
    const onKey = (event) => {
      if (event.key === "Escape") finish(false);
    };

    okButton.addEventListener("click", onOk);
    cancelButton.addEventListener("click", onCancel);
    dialog.addEventListener("keydown", onKey);
    dialog.addEventListener("close", onDismiss);
    dialog.addEventListener("cancel", onDismiss);
    dialog.showModal();
  });
}

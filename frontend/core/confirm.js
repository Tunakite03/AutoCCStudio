/**
 * One modal for every destructive action, awaited like `window.confirm`.
 *
 *   if (!(await confirmAction({ title: "Xóa project?", ... }))) return;
 *
 * The answer comes from the buttons and the Escape key, never from the dialog's
 * `close` event: that event is not dispatched in every embedded Chrome build,
 * and a confirmation that never resolves silently swallows the action.
 */

import { $ } from "./dom.js";

export function confirmAction({
  title,
  target = "",
  note = "",
  confirmLabel = "Đồng ý",
  cancelLabel = "Hủy",
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

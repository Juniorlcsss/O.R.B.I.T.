import { useEffect, useRef } from "react";

/**
 * The keyboard contract every overlay in the console owes its operator.
 */
const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export default function useDialogChrome({ open, onClose, dialogRef, closeOnEscape = true }) {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const closeOnEscapeRef = useRef(closeOnEscape);
  closeOnEscapeRef.current = closeOnEscape;

  useEffect(() => {
    if (!open) return undefined;

    const opener = document.activeElement;
    const nodesIn = () => [...(dialogRef.current?.querySelectorAll(FOCUSABLE) || [])];

    (nodesIn()[0] || dialogRef.current)?.focus?.();

    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        if (!closeOnEscapeRef.current) return;
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const nodes = nodesIn();
      if (nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && (document.activeElement === first || !dialogRef.current?.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !dialogRef.current?.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      opener?.focus?.();
    };
  }, [open, dialogRef]);
}

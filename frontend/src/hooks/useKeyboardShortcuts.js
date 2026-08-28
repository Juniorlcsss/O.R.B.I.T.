import { useEffect } from "react";

/**
 * Global single-key shortcuts for the command center.
 * 
 * @param {object} handlers
 * @param {() => void} handlers.onTriggerAlert Space — open the alert dialog.
 * @param {() => void} handlers.onToggleFullscreen F — fullscreen the globe.
 * @param {() => void} handlers.onToggleHelp ? — open the shortcut reference.
 * @param {boolean} handlers.enabled False while any dialog owns the keyboard.
 */
export default function useKeyboardShortcuts({
  onTriggerAlert,
  onToggleFullscreen,
  onToggleHelp,
  enabled = true,
}) {
  useEffect(() => {
    if (!enabled) return undefined;

    function isEditable(node) {
      if (!node) return false;
      if (node.isContentEditable) return true;
      const tag = node.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    }

    function onKeyDown(event) {
      // Let the browser and the OS keep their own chords.
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isEditable(event.target)) return;

      switch (event.key) {
        case " ":
        case "Spacebar":
          event.preventDefault(); // otherwise the page scrolls too
          onTriggerAlert?.();
          break;
        case "f":
        case "F":
          event.preventDefault();
          onToggleFullscreen?.();
          break;
        case "?":
          event.preventDefault();
          onToggleHelp?.();
          break;
        default:
          break;
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled, onTriggerAlert, onToggleFullscreen, onToggleHelp]);
}

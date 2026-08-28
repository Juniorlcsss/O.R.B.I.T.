import { useRef } from "react";
import useDialogChrome from "../hooks/useDialogChrome.js";
import { IconClose } from "./icons.jsx";

/**
 * Keyboard reference, opened with `?`.
 */
const GROUPS = [
  {
    title: "Command center",
    hint: "Available whenever no dialog is open and you are not typing in a field.",
    keys: [
      ["Space", "Open the conjunction-alert dialog"],
      ["F", "Fullscreen the orbital view"],
      ["?", "Open this reference"],
      ["Esc", "Close the open dialog"],
    ],
  },
  {
    title: "Orbital view",
    hint: "Applies while the globe has focus — click it once to give it focus.",
    keys: [
      ["drag / arrows", "Orbit the globe"],
      ["wheel / + -", "Zoom"],
      ["double-click", "Track an object"],
      ["Esc", "Release tracking"],
      ["L", "Freeze the vertical axis"],
      ["R", "Reset the view"],
    ],
  },
];

export default function ShortcutHelp({ open, onClose }) {
  const dialogRef = useRef(null);
  useDialogChrome({ open, onClose, dialogRef });

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink-900/80 p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="shortcut-help-title"
        className="w-full max-w-lg overflow-hidden rounded border border-hair bg-ink-800 shadow-2xl"
      >
        <div className="flex items-center gap-3 border-b border-hair px-5 py-3">
          <h2 id="shortcut-help-title" className="font-mono text-sm tracking-wide text-fg">
            Keyboard shortcuts
          </h2>
          <button
            onClick={onClose}
            aria-label="Close keyboard shortcuts"
            className="ml-auto rounded border border-hair p-1.5 text-fg-3 transition-colors duration-150 ease-console hover:border-hairlit hover:text-fg"
          >
            <IconClose size={12} />
          </button>
        </div>

        <div className="max-h-[70vh] overflow-y-auto">
          {GROUPS.map((group) => (
            <section key={group.title} className="border-t border-hair px-5 py-4 first:border-t-0">
              <h3 className="eyebrow">{group.title}</h3>
              <p className="mt-1 text-sm text-fg-3">{group.hint}</p>
              <dl className="mt-3 space-y-1.5">
                {group.keys.map(([key, description]) => (
                  <div key={`${group.title}-${key}`} className="flex items-baseline gap-3">
                    <dt className="w-32 shrink-0">
                      <kbd className="rounded border border-hair bg-ink-700 px-1.5 py-0.5 font-mono text-2xs text-fg-2">
                        {key}
                      </kbd>
                    </dt>
                    <dd className="text-sm text-fg-2">{description}</dd>
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}

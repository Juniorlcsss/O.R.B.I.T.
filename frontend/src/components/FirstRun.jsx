import { useEffect, useState } from "react";
import { IconClose } from "./icons.jsx";

/*
 * Orientation for the first sixty seconds.
 */
const SEEN_KEY = "orbit.intro-dismissed.v1";

/** localStorage throws outright in some privacy modes; never break boot over it. */
function readDismissed() {
  try {
    return window.localStorage.getItem(SEEN_KEY) === "1";
  } catch {
    return false;
  }
}

function rememberDismissed() {
  try {
    window.localStorage.setItem(SEEN_KEY, "1");
  } catch {
    /* a session-only dismissal is still a dismissal */
  }
}

const STEPS = [
  [
    "1",
    "The globe is live",
    "Real objects from Space-Track, propagated with SGP4. Amber lines are screened close approaches.",
  ],
  [
    "2",
    "Trigger a conjunction alert",
    "Injects a collision warning at the fleet commander, exactly as a real feed would.",
  ],
  [
    "3",
    "Watch the fleet decide",
    "Fifteen agents triage, screen, debate and negotiate. Every step is audited left; two safety gates sit on the right.",
  ],
];

export default function FirstRun({ onStart }) {
  const [dismissed, setDismissed] = useState(true);

  // Read on mount rather than in useState's initialiser so a server-rendered
  // or storage-less environment renders the same thing on both passes.
  useEffect(() => {
    setDismissed(readDismissed());
  }, []);

  function close() {
    setDismissed(true);
    rememberDismissed();
  }

  if (dismissed) return null;

  return (
    <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center p-6">
      <section
        aria-label="Getting started"
        className="pointer-events-auto w-[420px] max-w-full rounded-lg border border-hairlit bg-ink-800/95 shadow-2xl backdrop-blur-sm"
      >
        <header className="flex items-start justify-between gap-3 border-b border-hair px-5 py-3.5">
          <div>
            <h2 className="text-md text-fg">Autonomous conjunction response</h2>
            <p className="mt-0.5 text-sm text-fg-3">
              A satellite fleet that detects a collision risk, argues about the fix, and manoeuvres — without
              waiting for a human.
            </p>
          </div>
          <button
            onClick={close}
            aria-label="Dismiss"
            className="shrink-0 text-fg-3 transition-colors hover:text-fg"
          >
            <IconClose size={14} />
          </button>
        </header>

        <ol className="space-y-3 px-5 py-4">
          {STEPS.map(([index, title, body]) => (
            <li key={index} className="flex gap-3">
              <span className="mt-px flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-hairlit font-mono text-2xs tracking-normal text-fg-2">
                {index}
              </span>
              <div className="min-w-0">
                <p className="text-sm text-fg">{title}</p>
                <p className="mt-0.5 text-sm leading-relaxed text-fg-3">{body}</p>
              </div>
            </li>
          ))}
        </ol>

        <footer className="flex items-center gap-3 border-t border-hair px-5 py-3.5">
          <button
            onClick={() => {
              close();
              onStart?.();
            }}
            className="btn-primary"
          >
            Trigger conjunction alert
          </button>
          <button onClick={close} className="btn-quiet">
            Explore first
          </button>
        </footer>
      </section>
    </div>
  );
}

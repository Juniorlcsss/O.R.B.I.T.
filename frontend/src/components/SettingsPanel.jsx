import { useEffect, useRef } from "react";
import useSettings from "../hooks/useSettings.jsx";
import { CVD_MODES, SCALES, TYPEFACES } from "../lib/accessibility.js";
import { IconClose } from "./icons.jsx";
import StatusMark from "./StatusMark.jsx";

const FOCUSABLE = 'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

const SHORTCUTS = [
  ["drag / arrows", "Orbit the globe"],
  ["wheel / + −", "Zoom"],
  ["double-click", "Track an object"],
  ["esc", "Release tracking"],
  ["L", "Freeze the vertical axis"],
  ["R", "Reset the view"],
];

function Group({ title, hint, children }) {
  return (
    <section className="border-t border-hair px-5 py-4 first:border-t-0">
      <h3 className="eyebrow">{title}</h3>
      {hint && <p className="mt-1 text-sm text-fg-3">{hint}</p>}
      <div className="mt-2.5">{children}</div>
    </section>
  );
}

const COLUMNS = { 2: "grid-cols-2", 4: "grid-cols-4" };

/** Radio group rendered as a segmented control, keyboard-operable as a group. */
function Choice({ label, value, options, onChange, columns = 2 }) {
  return (
    <div role="radiogroup" aria-label={label} className={`grid gap-1.5 ${COLUMNS[columns]}`}>
      {options.map((option) => {
        const active = value === option.id;
        return (
          <button
            key={String(option.id)}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(option.id)}
            className={`rounded border px-2.5 py-2 text-left transition-colors duration-150 ease-console ${
              active
                ? "border-accent bg-accent-soft text-fg"
                : "border-hair text-fg-2 hover:border-hairlit hover:text-fg"
            }`}
          >
            <span className="block text-sm">{option.label}</span>
            {option.note && <span className="mt-0.5 block text-2xs tracking-normal text-fg-3">{option.note}</span>}
          </button>
        );
      })}
    </div>
  );
}

function Toggle({ label, hint, checked, disabled, onChange }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="flex w-full items-start gap-3 rounded py-1.5 text-left disabled:opacity-50"
    >
      <span
        aria-hidden="true"
        className={`mt-0.5 flex h-4 w-7 shrink-0 items-center rounded-full border transition-colors duration-150 ease-console ${
          checked ? "border-accent bg-accent-soft" : "border-hair bg-ink-900"
        }`}
      >
        <span
          className={`h-2.5 w-2.5 rounded-full transition-transform duration-150 ease-console ${
            checked ? "translate-x-[15px] bg-accent" : "translate-x-[3px] bg-fg-3"
          }`}
        />
      </span>
      <span className="min-w-0">
        <span className="block text-sm text-fg">{label}</span>
        {hint && <span className="mt-0.5 block text-2xs tracking-normal text-fg-3">{hint}</span>}
      </span>
    </button>
  );
}

/** Live proof that the chosen preset actually separates the mission states. */
function PalettePreview() {
  const { palette } = useSettings();
  return (
    <div className="rounded border border-hair bg-ink-900 p-3">
      <div className="flex flex-wrap gap-x-5 gap-y-2">
        {[
          ["nominal", "approved"],
          ["caution", "advisory"],
          ["alert", "rejected"],
        ].map(([tone, meaning]) => (
          <span key={tone} className="flex items-center gap-1.5 text-xs text-fg-2">
            <StatusMark tone={tone} />
            {meaning}
          </span>
        ))}
      </div>
      <div className="mt-2.5 flex flex-wrap gap-x-5 gap-y-2 border-t border-hair pt-2.5 text-xs text-fg-2">
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: palette.satellite }} />
          satellite
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: palette.debris }} />
          debris
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: palette.accent }} />
          selection
        </span>
      </div>
    </div>
  );
}

export default function SettingsPanel({ open, onClose }) {
  const { settings, update, reset } = useSettings();
  const dialogRef = useRef(null);
  const returnFocusRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    returnFocusRef.current = document.activeElement;
    dialogRef.current?.querySelector(FOCUSABLE)?.focus();

    // Escape closes; Tab is trapped so keyboard users cannot fall out of the
    // dialog into the globe controls behind it.
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const nodes = [...(dialogRef.current?.querySelectorAll(FOCUSABLE) || [])];
      if (nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      returnFocusRef.current?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  const monochrome = settings.cvd === "monochrome";

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-end bg-ink-900/70 backdrop-blur-sm"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        className="flex h-full w-[420px] max-w-full flex-col border-l border-hairlit bg-ink-800"
      >
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-hair px-5 py-4">
          <div>
            <h2 id="settings-title" className="text-md text-fg">
              Display &amp; accessibility
            </h2>
            <p className="mt-0.5 text-sm text-fg-3">Saved to this browser. Applies everywhere, globe included.</p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close settings"
            className="shrink-0 text-fg-3 transition-colors hover:text-fg"
          >
            <IconClose size={14} />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto">
          <Group
            title="Colour vision"
            hint="This console encodes decisions in colour. Pick the preset that keeps approved, advisory and rejected separable for you."
          >
            <Choice
              label="Colour vision preset"
              value={settings.cvd}
              options={CVD_MODES}
              onChange={(cvd) => update({ cvd })}
            />
            <div className="mt-3">
              <PalettePreview />
            </div>
          </Group>

          <Group title="Redundant coding">
            <Toggle
              label="Shape-code every status"
              hint={
                monochrome
                  ? "Always on in monochrome — shape is the only remaining channel."
                  : "Circle, triangle and diamond alongside the colour, and dashed conjunction lines by risk band."
              }
              checked={settings.shapes}
              disabled={monochrome}
              onChange={(shapes) => update({ shapes })}
            />
          </Group>

          <Group title="Contrast">
            <Choice
              label="Contrast"
              value={settings.contrast}
              options={[
                { id: "standard", label: "Standard", note: "Console default" },
                { id: "high", label: "High", note: "Brighter text and seams" },
              ]}
              onChange={(contrast) => update({ contrast })}
            />
          </Group>

          <Group title="Interface scale" hint="Scales type, spacing and globe labels together.">
            <Choice
              label="Interface scale"
              value={settings.scale}
              options={SCALES}
              onChange={(scale) => update({ scale })}
              columns={4}
            />
          </Group>

          <Group title="Typeface">
            <Choice
              label="Typeface"
              value={settings.typeface}
              options={TYPEFACES}
              onChange={(typeface) => update({ typeface })}
            />
          </Group>

          <Group title="Motion">
            <Choice
              label="Motion"
              value={settings.motion}
              options={[
                { id: "full", label: "Full", note: "Idle drift, eased camera" },
                { id: "reduced", label: "Reduced", note: "No drift, no blinking" },
              ]}
              onChange={(motion) => update({ motion })}
            />
          </Group>

          <Group title="Globe labels">
            <Choice
              label="Globe labels"
              value={settings.labels}
              options={[
                { id: "auto", label: "Auto", note: "Debris named when close in" },
                { id: "all", label: "Always all", note: "Every object named" },
              ]}
              onChange={(labels) => update({ labels })}
            />
          </Group>

          <Group title="Alerts">
            <Toggle
              label="Audio cue on critical events"
              hint="A short tone on high-risk screens and armor rejections."
              checked={settings.audio}
              onChange={(audio) => update({ audio })}
            />
            <Toggle
              label="Announce decisions to screen readers"
              hint="Politely announces terminal mission outcomes, not every poll."
              checked={settings.announce}
              onChange={(announce) => update({ announce })}
            />
          </Group>

          <Group title="Keyboard">
            <dl className="space-y-1.5">
              {SHORTCUTS.map(([keys, meaning]) => (
                <div key={keys} className="flex items-baseline justify-between gap-4">
                  <dt className="font-mono text-2xs tracking-normal text-fg-2">{keys}</dt>
                  <dd className="text-sm text-fg-3">{meaning}</dd>
                </div>
              ))}
            </dl>
          </Group>
        </div>

        <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-hair px-5 py-3.5">
          <button onClick={reset} className="btn-quiet">
            Reset to system defaults
          </button>
          <button onClick={onClose} className="btn-primary">
            Done
          </button>
        </footer>
      </div>
    </div>
  );
}

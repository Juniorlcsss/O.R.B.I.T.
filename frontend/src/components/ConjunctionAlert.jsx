import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/api.js";
import { num } from "../lib/format.js";
import { IconClose } from "./icons.jsx";

const DEMO_MESSAGE =
  "URGENT conjunction data message: LANCASTER_ORBIT_1 has a close approach with debris object FENGYUN_1C_DEB from the Fengyun-1C fragmentation event. TCA within hours. Immediate screening requested by Space-Track.";

const PRIORITIES = ["ROUTINE", "URGENT", "CRITICAL"];

function outcomeTone(status) {
  if (status === "EXECUTION_AUTHORIZED") return "border-nominal/40 text-nominal";
  if (/REJECT|BLOCK|DISPATCH/.test(status)) return "border-alert/40 text-alert";
  return "border-caution/40 text-caution";
}

function Field({ label, children }) {
  return (
    <label className="block">
      <span className="eyebrow mb-1.5 block">{label}</span>
      {children}
    </label>
  );
}

export default function ConjunctionAlert({ open, onClose, satellites, debris, onMissionComplete }) {
  const [satId, setSatId] = useState("LANCASTER_ORBIT_1");
  const [debrisId, setDebrisId] = useState("FENGYUN_1C_DEB");
  const [priority, setPriority] = useState("URGENT");
  const [message, setMessage] = useState(DEMO_MESSAGE);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const dialogRef = useRef(null);

  // Escape closes the dialog — but never mid-flight, where a half-run mission
  // would lose its trace before the operator has read the outcome.
  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape" && !running) {
        event.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown, true);
    dialogRef.current?.focus();
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [open, running, onClose]);

  if (!open) return null;

  async function submit(event) {
    event.preventDefault();
    setRunning(true);
    setResult(null);
    setError(null);
    try {
      const response = await apiFetch("/api/conjunction_alert", {
        method: "POST",
        body: JSON.stringify({
          sat_id: satId,
          debris_id: debrisId,
          alert_source: "COMMAND_CENTER_UI",
          priority,
          raw_message: message,
        }),
      });
      setResult(response);
      onMissionComplete({ request: { sat_id: satId }, response });
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-900/80 p-6 backdrop-blur-sm"
      onMouseDown={(event) => event.target === event.currentTarget && !running && onClose()}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Trigger conjunction alert"
        tabIndex={-1}
        className="w-[540px] max-w-full rounded-lg border border-hairlit bg-ink-800 focus:outline-none"
      >
        <header className="flex items-center justify-between border-b border-hair px-5 py-3.5">
          <div>
            <h2 className="text-md text-fg">Trigger conjunction alert</h2>
            <p className="mt-0.5 text-sm text-fg-3">
              Injects a conjunction data message at the fleet commander, exactly as Space-Track would.
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={running}
            aria-label="Close"
            className="shrink-0 text-fg-3 transition-colors hover:text-fg disabled:opacity-40"
          >
            <IconClose size={14} />
          </button>
        </header>

        <form onSubmit={submit} className="space-y-4 p-5">
          <div className="grid grid-cols-2 gap-4">
            <Field label="Protected asset">
              <select value={satId} onChange={(e) => setSatId(e.target.value)} className="field">
                {satellites.map((sat) => (
                  <option key={sat.id} value={sat.id}>
                    {sat.id}
                  </option>
                ))}
                {satellites.length === 0 && <option value="LANCASTER_ORBIT_1">LANCASTER_ORBIT_1</option>}
              </select>
            </Field>
            <Field label="Secondary object">
              <select value={debrisId} onChange={(e) => setDebrisId(e.target.value)} className="field">
                {debris.map((obj) => (
                  <option key={obj.id} value={obj.id}>
                    {obj.id}
                  </option>
                ))}
                {debris.length === 0 && <option value="FENGYUN_1C_DEB">FENGYUN_1C_DEB</option>}
              </select>
            </Field>
          </div>

          <Field label="Priority">
            <div className="flex gap-px overflow-hidden rounded border border-hair bg-hair">
              {PRIORITIES.map((level) => (
                <button
                  key={level}
                  type="button"
                  onClick={() => setPriority(level)}
                  className={`flex-1 px-2 py-2 font-mono text-xs transition-colors duration-150 ease-console ${
                    priority === level ? "bg-ink-500 text-fg" : "bg-ink-900 text-fg-3 hover:text-fg-2"
                  }`}
                >
                  {level.toLowerCase()}
                </button>
              ))}
            </div>
          </Field>

          <Field label="Raw feed message">
            <textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              rows={4}
              className="field resize-none font-sans text-sm leading-relaxed"
            />
          </Field>

          {result && (
            <div className={`rounded border px-3 py-2.5 ${outcomeTone(result.status)}`}>
              <p className="font-mono text-sm">{result.status.toLowerCase().replace(/_/g, " ")}</p>
              <dl className="mt-1.5 grid grid-cols-2 gap-x-4 gap-y-0.5 font-mono text-2xs tracking-normal text-fg-2">
                <div className="flex justify-between">
                  <dt className="text-fg-3">risk</dt>
                  <dd>{result.risk_band ?? "—"}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-fg-3">miss</dt>
                  <dd>{result.miss_distance_km != null ? `${num(result.miss_distance_km * 1000, 0)} m` : "—"}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-fg-3">Pc</dt>
                  <dd>{result.pc != null ? result.pc.toExponential(2) : "—"}</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-fg-3">action</dt>
                  <dd className="truncate">{result.action_taken ?? "—"}</dd>
                </div>
              </dl>
              <p className="mt-1.5 truncate font-mono text-2xs tracking-normal text-fg-3">trace {result.trace_id}</p>
            </div>
          )}

          {error && (
            <p className="rounded border border-alert/40 px-3 py-2.5 text-sm text-alert">
              Mission failed to start — {error}
            </p>
          )}

          <div className="flex items-center gap-4 pt-1">
            <button type="submit" disabled={running} className="btn-primary">
              {running ? "Executing…" : "Launch mission"}
            </button>
            <p className="text-sm text-fg-3">
              Triage &rarr; SGP4 screening &rarr; negotiation &rarr; two armor gates. Nothing is stubbed.
            </p>
          </div>
        </form>
      </div>
    </div>
  );
}

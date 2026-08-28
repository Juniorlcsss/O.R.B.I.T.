import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "../lib/api.js";
import { num, objectLabel } from "../lib/format.js";
import useDialogChrome from "../hooks/useDialogChrome.js";
import { IconClose } from "./icons.jsx";

/*
 * Composed from the live picture rather than a scripted scenario: the alert
 * names whichever real objects are actually on the board. With no live data
 * there is no honest alert to pre-fill, so the operator writes one.
 */
function composeMessage(satId, debrisId, conjunction) {
  if (!satId || !debrisId) return "";
  const tca = conjunction?.tca_utc ? ` TCA ${conjunction.tca_utc}.` : "";
  const miss =
    typeof conjunction?.miss_distance_km === "number"
      ? ` Screened miss distance ${(conjunction.miss_distance_km * 1000).toFixed(0)} m.`
      : "";
  return (
    `URGENT conjunction data message: ${satId} has a close approach with ` +
    `${debrisId}.${tca}${miss} Immediate screening requested.`
  );
}

/**
 * Option text for the secondary-object list.
 */
function secondaryOption(object, encounter) {
  const label = objectLabel(object);
  if (!encounter) return `${label} — not screened`;
  const metres = typeof encounter.miss_distance_km === "number" ? ` · ${(encounter.miss_distance_km * 1000).toFixed(0)} m` : "";
  return `${label} — ${encounter.risk_band || "screened"}${metres}`;
}

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

export default function ConjunctionAlert({
  open,
  onClose,
  assets,
  secondaries,
  conjunctions,
  onMissionComplete,
}) {
  const [satId, setSatId] = useState("");
  const [debrisId, setDebrisId] = useState("");
  const [priority, setPriority] = useState("URGENT");
  const [message, setMessage] = useState("");
  const [messageEdited, setMessageEdited] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const dialogRef = useRef(null);


  useEffect(() => {
    if (!open) return;
    const worst = (conjunctions || [])
      .slice()
      .sort((a, b) => (b.probability_of_collision || 0) - (a.probability_of_collision || 0))[0];
    // Only accept an id the corresponding <select>
    const inList = (id, list) => list.some((item) => item.id === id);
    const nextSat = inList(worst?.sat_id, assets) ? worst.sat_id : assets[0]?.id || "";
    const nextDebris = inList(worst?.debris_id, secondaries) ? worst.debris_id : secondaries[0]?.id || "";
    setSatId(nextSat);
    setDebrisId(nextDebris);
    if (!messageEdited) setMessage(composeMessage(nextSat, nextDebris, worst));
  }, [open, conjunctions, assets, secondaries, messageEdited]);


  useDialogChrome({ open, onClose, dialogRef, closeOnEscape: !running });

  const encounterBySecondary = useMemo(() => {
    const index = new Map();
    for (const c of conjunctions || []) {
      if (c.debris_id) index.set(c.debris_id, c);
      if (c.sat_id) index.set(c.sat_id, c);
    }
    return index;
  }, [conjunctions]);

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
                {assets.map((sat) => (
                  <option key={sat.id} value={sat.id}>
                    {objectLabel(sat)}
                  </option>
                ))}
                {/* No invented stand-in: with no live objects there is
                    nothing truthful to offer, and a placeholder id would
                    dispatch a mission against an object that is not there. */}
                {assets.length === 0 && <option value="">no commandable asset in view</option>}
              </select>
            </Field>
            <Field label="Secondary object">
              <select value={debrisId} onChange={(e) => setDebrisId(e.target.value)} className="field">
                {secondaries.map((obj) => (
                  <option key={obj.id} value={obj.id}>
                    {secondaryOption(obj, encounterBySecondary.get(obj.id))}
                  </option>
                ))}
                {secondaries.length === 0 && <option value="">no live secondary in view</option>}
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
              onChange={(e) => {
                setMessageEdited(true);
                setMessage(e.target.value);
              }}
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

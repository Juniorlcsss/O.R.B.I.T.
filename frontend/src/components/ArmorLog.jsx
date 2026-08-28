import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../lib/api.js";
import { TONE, toneOf } from "../lib/format.js";
import { IconShield } from "./icons.jsx";
import StatusMark from "./StatusMark.jsx";

/** Trip codes the Armor emits, either as an explicit list or a failed check. */
function violationsOf(record) {
  const payload = record.payload || {};
  const codes = [];
  if (Array.isArray(payload.violations)) codes.push(...payload.violations.map(String));
  if (payload.checks && typeof payload.checks === "object") {
    for (const [check, outcome] of Object.entries(payload.checks)) {
      if (String(outcome).startsWith("FAIL")) codes.push(check);
    }
  }
  return [...new Set(codes)];
}

export default function ArmorLog({ feedEvents }) {
  const [selectedTrace, setSelectedTrace] = useState(null);
  const [shieldKey, setShieldKey] = useState(null);
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const [manualInput, setManualInput] = useState("");

  // The five most recent traces that actually passed through the Armor.
  const traces = useMemo(() => {
    const seen = new Map();
    for (const record of feedEvents) {
      if (!record.trace_id || record.trace_id === "-" || record.trace_id === "startup") continue;
      if (record.event_type === "MISSION_STATUS" || record.event_type === "MODEL_ARMOR_SWEEP") {
        seen.set(record.trace_id, record);
      }
    }
    return [...seen.values()].slice(-5).reverse();
  }, [feedEvents]);

  useEffect(() => {
    if (!selectedTrace && traces.length) setSelectedTrace(traces[0].trace_id);
  }, [traces, selectedTrace]);


  const latestRejection = useMemo(() => {
    for (let i = feedEvents.length - 1; i >= 0; i -= 1) {
      const record = feedEvents[i];
      if (record?.event_type !== "MODEL_ARMOR_SWEEP") continue;
      if (String(record.status || "").toUpperCase().includes("REJECT")) {
        return `${record.trace_id}:${record.timestamp}`;
      }
      return null;
    }
    return null;
  }, [feedEvents]);

  useEffect(() => {
    if (latestRejection) setShieldKey(latestRejection);
  }, [latestRejection]);

  useEffect(() => {
    let alive = true;
    setReport(null);
    setError(null);
    if (!selectedTrace) return undefined;
    apiFetch(`/api/armor_report/${encodeURIComponent(selectedTrace)}`)
      .then((data) => alive && setReport(data))
      .catch((err) => alive && setError(String(err.message || err)));
    return () => {
      alive = false;
    };
  }, [selectedTrace]);

  return (
    <section className="relative flex flex-col">
      {shieldKey && (
        <span
          key={shieldKey}
          aria-hidden="true"
          onAnimationEnd={() => setShieldKey(null)}
          className="animate-armor-shield pointer-events-none absolute inset-0 z-30 flex items-center justify-center"
        >
          <span className="flex flex-col items-center gap-2 text-alert">
            <IconShield size={56} />
            <span className="font-mono text-2xs uppercase tracking-[0.2em]">Maneuver rejected</span>
          </span>
        </span>
      )}
      <header className="flex shrink-0 items-center justify-between gap-2 px-4 py-3">
        <h2 className="eyebrow flex items-center gap-1.5">
          <IconShield size={11} />
          Model armor
        </h2>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (manualInput.trim()) setSelectedTrace(manualInput.trim());
          }}
        >
          <input
            value={manualInput}
            onChange={(event) => setManualInput(event.target.value)}
            placeholder="trace id"
            aria-label="Replay a trace by id"
            className="w-28 rounded border border-hair bg-ink-900 px-2 py-1 font-mono text-2xs tracking-normal text-fg placeholder:text-fg-3 focus:border-accent focus:outline-none"
          />
        </form>
      </header>

      {traces.length > 0 && (
        <div className="flex shrink-0 gap-4 overflow-x-auto border-b border-hair px-4">
          {traces.map((record) => {
            const active = selectedTrace === record.trace_id;
            return (
              <button
                key={record.trace_id}
                onClick={() => setSelectedTrace(record.trace_id)}
                title={`${record.trace_id} · ${record.status}`}
                className={`shrink-0 border-b-2 pb-2 font-mono text-2xs tracking-normal transition-colors duration-150 ease-console ${
                  active ? "border-accent text-fg" : "border-transparent text-fg-3 hover:text-fg-2"
                }`}
              >
                {record.trace_id.slice(0, 8)}
              </button>
            );
          })}
        </div>
      )}

      <div className="px-4 py-2">
        {error && <p className="py-2 text-xs text-alert">{error}</p>}
        {!error && !report && selectedTrace && <p className="py-2 text-sm text-fg-3">Replaying trace…</p>}
        {!selectedTrace && !error && (
          <p className="py-2 text-sm text-fg-3">Run a mission to populate the armor ledger.</p>
        )}

        {report?.events
          ?.slice()
          .reverse()
          .map((record) => {
            const toneName = toneOf(record);
            const tone = TONE[toneName];
            const violations = violationsOf(record);
            return (
              <div key={`${report.trace_id}-${record.seq}`} className="border-b border-hair py-1.5 last:border-0">
                <div className="flex items-baseline gap-2">
                  <StatusMark tone={toneName} className="translate-y-px" />
                  <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg-2">
                    {String(record.event_type).toLowerCase()}
                  </span>
                  <span className={`shrink-0 font-mono text-2xs tracking-normal ${tone.text}`}>
                    {String(record.status).slice(0, 22).toLowerCase()}
                  </span>
                </div>
                {violations.length > 0 && (
                  <p className="mt-0.5 pl-3.5 font-mono text-2xs tracking-normal text-alert">
                    {violations.join(" · ")}
                  </p>
                )}
              </div>
            );
          })}
      </div>
    </section>
  );
}

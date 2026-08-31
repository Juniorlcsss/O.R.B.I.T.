import { humanise, num } from "../lib/format.js";
import { IconClose } from "./icons.jsx";

/*
 * The verdict used to live and die inside the alert dialog: close it and the
 * one thing the whole fleet was convened to produce was gone. This keeps the
 * last mission's decision on the console until another one replaces it, so
 * the outcome can be read alongside the audit trail that produced it.
 */
function tone(status) {
  if (status === "EXECUTION_AUTHORIZED") return { text: "text-nominal", border: "border-nominal/40", dot: "bg-nominal" };
  if (/REJECT|BLOCK|DISPATCH/.test(status)) return { text: "text-alert", border: "border-alert/40", dot: "bg-alert" };
  return { text: "text-caution", border: "border-caution/40", dot: "bg-caution" };
}

function Cell({ label, children }) {
  return (
    <div className="flex justify-between gap-2">
      <dt className="text-fg-3">{label}</dt>
      <dd className="truncate text-fg-2">{children}</dd>
    </div>
  );
}

export default function MissionOutcome({ mission, onDismiss }) {
  if (!mission) return null;
  const { status, risk_band, miss_distance_km, pc, action_taken, armor_violations, trace_id } = mission.response;
  const skin = tone(status);

  return (
    <section aria-label="Mission outcome" className="px-4 py-3">
      <header className="mb-2 flex items-center justify-between gap-2">
        <h2 className="eyebrow">Mission outcome</h2>
        <button
          onClick={onDismiss}
          aria-label="Dismiss mission outcome"
          className="shrink-0 text-fg-3 transition-colors duration-150 ease-console hover:text-fg"
        >
          <IconClose size={11} />
        </button>
      </header>

      <div className={`rounded border px-3 py-2.5 ${skin.border}`}>
        <p className={`flex items-center gap-2 font-mono text-sm ${skin.text}`}>
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${skin.dot}`} aria-hidden="true" />
          {String(status).toLowerCase().replace(/_/g, " ")}
        </p>

        <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-0.5 font-mono text-2xs tracking-normal">
          <Cell label="risk">{risk_band ?? "—"}</Cell>
          <Cell label="miss">{miss_distance_km != null ? `${num(miss_distance_km * 1000, 0)} m` : "—"}</Cell>
          <Cell label="Pc">{pc != null ? pc.toExponential(2) : "—"}</Cell>
          <Cell label="action">{action_taken ? humanise(action_taken).toLowerCase() : "—"}</Cell>
        </dl>

        {/* An authorised manoeuvre that tripped an armor check is not a clean
            pass, and the console should not let that read as one. */}
        {armor_violations?.length > 0 && (
          <p className="mt-2 font-mono text-2xs tracking-normal text-alert">
            armor · {armor_violations.join(" · ")}
          </p>
        )}

        <p className="mt-2 truncate font-mono text-2xs tracking-normal text-fg-3" title={trace_id}>
          trace {trace_id}
        </p>
      </div>
    </section>
  );
}

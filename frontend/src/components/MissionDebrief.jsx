import { useEffect, useRef, useState } from "react";
import { fetchDebrief } from "../lib/api.js";
import useDialogChrome from "../hooks/useDialogChrome.js";
import { IconActivity } from "./icons.jsx";

const POLL_INTERVAL_MS = 3_000;
const POLL_BUDGET_MS = 45_000;

/**
 * Autonomous Veo mission-debrief viewer. Polls GET /api/debrief/{id} until
 * the artifact is READY (or the budget expires), then renders either the
 * real Veo video or the labelled simulated reconstruction.
 */
export default function MissionDebrief({ open, onClose, conjunctionId }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const attemptRef = useRef(0);
  const dialogRef = useRef(null);

  useEffect(() => {
    if (!open || !conjunctionId) {
      setReport(null);
      setError(null);
      attemptRef.current = 0;
      return;
    }
    let alive = true;
    let timer = null;

    async function poll() {
      try {
        const data = await fetchDebrief(conjunctionId);
        if (!alive) return;
        setReport(data);
        setError(null);
        const status = String(data.debrief_status || "");
        const settled = /READY|FAILED/.test(status);
        const expired = (attemptRef.current += 1) * POLL_INTERVAL_MS > POLL_BUDGET_MS;
        if (!settled && !expired) timer = setTimeout(poll, POLL_INTERVAL_MS);
      } catch (err) {
        if (!alive) return;
        // 404 before the pipeline has persisted anything is normal for a
        // few seconds post-mission; keep polling within budget.
        if (String(err.message).startsWith("404") && (attemptRef.current += 1) * POLL_INTERVAL_MS <= POLL_BUDGET_MS) {
          timer = setTimeout(poll, POLL_INTERVAL_MS);
          return;
        }
        setError(err.message);
      }
    }

    poll();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [open, conjunctionId]);

  useDialogChrome({ open: open && Boolean(conjunctionId), onClose, dialogRef });

  if (!open || !conjunctionId) return null;

  const status = String(report?.debrief_status || (error ? "ERROR" : "PENDING"));
  const ready = status === "READY";
  const failed = status === "FAILED" || status === "ERROR";
  const generating = !ready && !failed;
  const isVeo = report?.mode === "veo" && report?.video_url;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink-900/80 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label="Mission debrief"
      onClick={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="w-full max-w-xl overflow-hidden rounded-lg border border-hair bg-ink-800 shadow-2xl focus:outline-none"
      >
        <header className="flex items-center gap-3 border-b border-hair px-4 py-3">
          <span className="text-accent">
            <IconActivity size={14} />
          </span>
          <h2 className="eyebrow flex-1">Mission debrief</h2>
          <span
            className={`rounded px-2 py-0.5 font-mono text-2xs tracking-normal ${
              ready ? "bg-nominal/15 text-nominal" : failed ? "bg-alert/15 text-alert" : "bg-caution/15 text-caution"
            }`}
          >
            {ready ? (isVeo ? "VEO RENDER" : "SIMULATED RECON") : failed ? "FAILED" : "GENERATING…"}
          </span>
          <button onClick={onClose} className="btn-quiet text-xs">
            Close
          </button>
        </header>

        <div className="max-h-[70vh] overflow-y-auto p-4">
          {generating && (
            <div className="flex flex-col items-center gap-3 py-10">
              <p className="font-mono text-xs uppercase tracking-[0.3em] text-fg-3">
                Veo is rendering the encounter
              </p>
              <span className="block h-px w-48 overflow-hidden bg-hair">
                <span className="animate-sweep block h-px w-1/4 bg-accent" />
              </span>
              <p className="font-mono text-2xs text-fg-3">{conjunctionId}</p>
            </div>
          )}

          {failed && (
            <p className="rounded border border-alert/40 bg-alert/10 px-3 py-2 text-sm text-alert">
              Debrief generation failed{report?.error ? `: ${report.error}` : error ? `: ${error}` : "."}
            </p>
          )}

          {ready && (
            <>
              {isVeo ? (
                <video key={report.video_url} src={report.video_url} controls autoPlay className="w-full rounded border border-hair" />
              ) : report.poster_svg ? (
                <figure>
                  <img src={report.poster_svg} alt="Simulated reconstruction of the resolved conjunction" className="w-full rounded border border-hair" />
                  <figcaption className="mt-1.5 font-mono text-2xs tracking-normal text-fg-3">
                    Simulated reconstruction — set ORBIT_ENABLE_REAL_VEO=1 for a true Veo render
                  </figcaption>
                </figure>
              ) : null}
              {report.summary && (
                <p className="mt-3 border-l-2 border-accent/50 pl-3 text-sm leading-relaxed text-fg-2">{report.summary}</p>
              )}
              {report.prompt && (
                <details className="mt-3">
                  <summary className="cursor-pointer font-mono text-2xs uppercase tracking-normal text-fg-3 hover:text-fg-2">
                    Veo prompt
                  </summary>
                  <p className="mt-1.5 rounded bg-ink-700 p-2.5 font-mono text-2xs leading-relaxed tracking-normal text-fg-2">
                    {report.prompt}
                  </p>
                </details>
              )}
            </>
          )}
        </div>

        <footer className="border-t border-hair px-4 py-2.5">
          <p className="truncate font-mono text-2xs tracking-normal text-fg-3">
            conjunction: {conjunctionId} &middot; generated autonomously by the fleet
          </p>
        </footer>
      </div>
    </div>
  );
}

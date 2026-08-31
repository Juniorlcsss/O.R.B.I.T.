import { useEffect, useMemo, useRef, useState } from "react";
import { beep, playEventAudio } from "../lib/api.js";
import { age, humanise, monogram, TONE, toneOf } from "../lib/format.js";
import { IconActivity, IconChevron } from "./icons.jsx";
import StatusMark from "./StatusMark.jsx";
import useSettings from "../hooks/useSettings.jsx";

/** Fields worth surfacing on the row itself, in the order an operator scans. */
const DETAIL_KEYS = ["risk_band", "miss_distance_km", "pc", "delta_v_ms", "reason", "violations", "error"];

/**
 * The API gateway audits every HTTP request, which includes this dashboard's
 * own 2 s telemetry poll. Those records belong in the ledger but they are not
 * fleet decisions, and left unfiltered they bury every real one.
 */
function isPollNoise(record) {
  return (
    record.agent_name === "orbit.api.gateway" &&
    record.event_type === "HTTP_REQUEST" &&
    String(record.status).toUpperCase() === "OK"
  );
}

function detailPairs(record) {
  const payload = record.payload || {};
  const pairs = [];
  for (const key of DETAIL_KEYS) {
    if (payload[key] === undefined || payload[key] === null) continue;
    const raw = payload[key];
    const value = Array.isArray(raw) ? raw.join(", ") : typeof raw === "object" ? JSON.stringify(raw) : String(raw);
    pairs.push([key.replace(/_/g, " "), value]);
    if (pairs.length === 2) break;
  }
  return pairs;
}

function fullPayloadEntries(record) {
  const payload = record.payload || {};
  return Object.entries(payload)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([key, value]) => [
      key.replace(/_/g, " "),
      typeof value === "object" ? JSON.stringify(value, null, 2) : String(value),
    ]);
}

/**
 * Fold runs of identical consecutive records into one row with a count, the
 * way any log console does. Keeps a burst of retries from scrolling the
 * decision that caused it off the screen.
 */
function collapseRuns(records) {
  const rows = [];
  for (const record of records) {
    const previous = rows[rows.length - 1];
    if (
      previous &&
      previous.agent_name === record.agent_name &&
      previous.event_type === record.event_type &&
      previous.status === record.status
    ) {
      previous.repeat += 1;
      previous.timestamp = record.timestamp;
      previous.seq = record.seq;
      continue;
    }
    rows.push({ ...record, repeat: 1, firstSeq: record.seq });
  }
  return rows;
}

/**
 * Map an audit record onto its mission-control audio cue (served by the
 * Lyria-backed /api/audio endpoint). Order matters: edge-autonomy events
 * win over generic dispatch cues, and MISSION_STATUS is resolved by status.
 */
function audioCueFor(record) {
  const type = String(record.event_type || "");
  const status = String(record.status || "");
  if (/^EDGE_(AUTONOMY_ENGAGED|DECISION_FINAL|EMERGENCY_DODGE)/.test(type)) return "EDGE_AUTONOMOUS";
  if (type === "MISSION_STATUS") {
    if (/EXECUTION_AUTHORIZED|DODGE_EXECUTED/.test(status)) return "MANEUVER_AUTHORIZED";
    if (/BLOCKED|REJECTED/.test(status)) return "MANEUVER_BLOCKED";
    if (/HUMAN_DISPATCH|STANDOFF/.test(status)) return "HUMAN_DISPATCH";
  }
  if (/CIRCUIT_BREAKER_TRIPPED|HUMAN_DISPATCH/.test(type)) return "HUMAN_DISPATCH";
  if (/HIGH/.test(`${record.payload?.risk_band ?? ""} ${status} ${type}`.toUpperCase())) return "ALERT_DETECTED";
  return null;
}

export default function MissionFeed({ events, connected }) {
  const { settings } = useSettings();
  const [nowMs, setNowMs] = useState(Date.now());
  const [showAll, setShowAll] = useState(false);
  const [expanded, setExpanded] = useState(() => new Set());
  const listRef = useRef(null);
  const stickRef = useRef(true);

  useEffect(() => {
    const timer = setInterval(() => setNowMs(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, []);

  // Audible cue on the events an operator must not miss, each class with
  // its own generated identity (Lyria-backed). Opt-out, because an
  // unexpected tone is hostile in a shared room and useless with the volume
  // muted; the colour, shape and text encodings all stand on their own.
  useEffect(() => {
    if (!settings.audio || !events.length) return;
    const cue = audioCueFor(events[events.length - 1]);
    if (!cue) return;
    playEventAudio(cue).then((played) => {
      if (!played) beep(cue === "MANEUVER_BLOCKED" ? 330 : 660);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events.length, settings.audio]);

  function toggleRow(key) {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const noiseCount = useMemo(() => events.filter(isPollNoise).length, [events]);

  // Newest first, so "latest" means the top of the list.
  const rows = useMemo(() => {
    const kept = showAll ? events : events.filter((record) => !isPollNoise(record));
    return collapseRuns(kept).reverse();
  }, [events, showAll]);

  useEffect(() => {
    if (stickRef.current && listRef.current) listRef.current.scrollTop = 0;
  }, [rows.length]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 items-center justify-between px-4 pb-2 pt-3">
        <h2 className="eyebrow">Mission feed</h2>
        <span className={`flex items-center gap-1.5 text-xs ${connected ? "text-fg-2" : "text-alert"}`}>
          <IconActivity size={12} />
          {connected ? "streaming" : "disconnected"}
        </span>
      </header>

      <div className="flex shrink-0 items-center gap-4 border-b border-hair px-4 pb-2">
        {[
          ["fleet", false],
          ["all", true],
        ].map(([label, value]) => (
          <button
            key={label}
            onClick={() => setShowAll(value)}
            className={`-mb-px border-b pb-1.5 text-xs transition-colors duration-150 ease-console ${
              showAll === value ? "border-accent text-fg" : "border-transparent text-fg-3 hover:text-fg-2"
            }`}
          >
            {label}
          </button>
        ))}
        <span className="ml-auto font-mono text-2xs tracking-normal text-fg-3">
          {showAll ? `${rows.length} rows` : `${noiseCount} polls hidden`}
        </span>
      </div>

      <div
        ref={listRef}
        onScroll={(event) => {
          stickRef.current = event.currentTarget.scrollTop < 24;
        }}
        className="min-h-0 flex-1 overflow-y-auto"
      >
        {rows.length === 0 && (
          <p className="px-4 py-8 text-center text-sm text-fg-3">
            {connected
              ? "No fleet decisions yet. Trigger a conjunction alert to start a mission."
              : "Waiting for the audit stream…"}
          </p>
        )}

        {rows.map((record) => {
          const toneName = toneOf(record);
          const tone = TONE[toneName];
          const pairs = detailPairs(record);
          const rowKey = `${record.firstSeq ?? record.seq}-${record.trace_id}`;
          const isOpen = expanded.has(rowKey);
          const entries = isOpen ? fullPayloadEntries(record) : [];
          return (
            <article
              key={rowKey}
              className="animate-feed-in relative border-b border-hair transition-colors duration-150 ease-console hover:bg-ink-700"
            >
              {/* Status rail: the only colour on an otherwise grey row. */}
              <span className={`absolute inset-y-0 left-0 w-0.5 ${tone.rail}`} />

              <button
                type="button"
                onClick={() => toggleRow(rowKey)}
                aria-expanded={isOpen}
                aria-controls={`feed-detail-${rowKey}`}
                className="block w-full py-2 pl-5 pr-4 text-left focus:outline-none focus-visible:bg-ink-700"
              >
                <div className="flex items-center gap-2">
                  <StatusMark tone={toneName} />
                  <span className="rounded-sm bg-ink-600 px-1 py-0.5 font-mono text-2xs tracking-normal text-fg-2">
                    {monogram(record.agent_name)}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-sm text-fg">{humanise(record.agent_name)}</span>
                  {record.repeat > 1 && (
                    <span className="shrink-0 rounded-sm bg-ink-600 px-1 font-mono text-2xs tracking-normal text-fg-2">
                      &times;{record.repeat}
                    </span>
                  )}
                  <span className="shrink-0 font-mono text-2xs tracking-normal text-fg-3">
                    {age(record.timestamp, nowMs)}
                  </span>
                  <IconChevron open={isOpen} size={10} className="shrink-0 text-fg-3" />
                </div>

                <div className="mt-0.5 flex items-baseline gap-2 pl-7">
                  <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg-2">
                    {String(record.event_type).toLowerCase()}
                  </span>
                  <span className={`shrink-0 font-mono text-2xs tracking-normal ${tone.text}`}>
                    {isOpen ? String(record.status).toLowerCase() : String(record.status).slice(0, 26).toLowerCase()}
                  </span>
                </div>

                {!isOpen && pairs.length > 0 && (
                  <dl className="mt-0.5 flex flex-wrap gap-x-3 pl-7 font-mono text-2xs tracking-normal">
                    {pairs.map(([label, value]) => (
                      <div key={label} className="flex min-w-0 gap-1.5">
                        <dt className="text-fg-3">{label}</dt>
                        <dd className="truncate text-fg-2">{value}</dd>
                      </div>
                    ))}
                  </dl>
                )}
              </button>

              {isOpen && (
                <div
                  id={`feed-detail-${rowKey}`}
                  className="border-t border-hair bg-ink-900/60 px-4 py-2 pl-12"
                >
                  {entries.length === 0 && (
                    <p className="font-mono text-2xs tracking-normal text-fg-3">No payload recorded.</p>
                  )}
                  <dl className="space-y-1">
                    {entries.map(([label, value]) => (
                      <div key={label}>
                        <dt className="font-mono text-2xs tracking-normal text-fg-3">{label}</dt>
                        <dd className="whitespace-pre-wrap break-words font-mono text-2xs leading-relaxed tracking-normal text-fg-2">
                          {value}
                        </dd>
                      </div>
                    ))}
                  </dl>
                  <p className="mt-2 break-all font-mono text-2xs tracking-normal text-fg-3">
                    {record.timestamp} &middot; trace {record.trace_id}
                  </p>
                </div>
              )}
            </article>
          );
        })}
      </div>
    </div>
  );
}

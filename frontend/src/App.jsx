import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CesiumViewer from "./components/CesiumViewer.jsx";
import MissionFeed from "./components/MissionFeed.jsx";
import FleetStatus from "./components/FleetStatus.jsx";
import ArmorLog from "./components/ArmorLog.jsx";
import ConjunctionAlert from "./components/ConjunctionAlert.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";
import MissionDebrief from "./components/MissionDebrief.jsx";
import { IconSettings } from "./components/icons.jsx";
import useLiveFeed from "./hooks/useLiveFeed.js";
import useOrbitalState from "./hooks/useOrbitalState.js";
import useAgentTree from "./hooks/useAgentTree.js";
import useSettings from "./hooks/useSettings.jsx";
import { clockOf, humanise } from "./lib/format.js";

/** A mission that produced no terminal audit record is abandoned after this. */
const MISSION_TIMEOUT_MS = 60_000;

function Mark() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <circle cx="10" cy="10" r="3.4" fill="currentColor" />
      <ellipse
        cx="10"
        cy="10"
        rx="8.6"
        ry="3.5"
        stroke="currentColor"
        strokeWidth="1.1"
        opacity="0.55"
        transform="rotate(-28 10 10)"
      />
    </svg>
  );
}

/** One compact readout in the header rail: label, state dot, value. */
function Readout({ label, tone, value }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-2xs uppercase text-fg-3">{label}</span>
      <span className={`h-1.5 w-1.5 rounded-full ${tone}`} aria-hidden="true" />
      <span className="font-mono text-xs text-fg-2">{value}</span>
    </div>
  );
}

export default function App() {
  const { events, connected } = useLiveFeed();
  const { objects, conjunctions, generatedUtc, error: orbitalError } = useOrbitalState();
  const { tree, error: treeError } = useAgentTree();
  const { settings } = useSettings();

  const [alertOpen, setAlertOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [maneuver, setManeuver] = useState(null);
  const [pending, setPending] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [announcement, setAnnouncement] = useState("");
  // Most recent resolved conjunction — the MISSION DEBRIEF entry point.
  const [debriefId, setDebriefId] = useState(null);
  const [debriefOpen, setDebriefOpen] = useState(false);
  const announcedRef = useRef(new Set());

  const satellites = useMemo(() => objects.filter((o) => o.type === "satellite"), [objects]);
  const debris = useMemo(() => objects.filter((o) => o.type === "debris"), [objects]);

  const highRiskActive = useMemo(
    () => (conjunctions || []).some((c) => c.risk_band === "HIGH"),
    [conjunctions]
  );

  const handleSelect = useCallback((id) => setSelectedId(id ?? null), []);

  function handleMissionComplete({ request, response }) {
    setPending({
      satId: request.sat_id,
      traceId: response.trace_id,
      expiresAt: Date.now() + MISSION_TIMEOUT_MS,
    });
    if (response.status === "EXECUTION_AUTHORIZED") {
      setManeuver({ satId: request.sat_id, startedAt: Date.now() });
    }
    // The fleet documented its own solution — surface the debrief entry.
    if (response.conjunction_id) {
      setDebriefId(response.conjunction_id);
      setDebriefOpen(false);
    }
  }

  // Watch the audit stream for the terminal record of the in-flight mission —
  // the API response and the feed are two views of the same trace, and the
  // globe animation should follow whichever confirms first.
  useEffect(() => {
    if (!pending) return;
    if (Date.now() > pending.expiresAt) {
      setPending(null);
      return;
    }
    const terminal = events.find(
      (e) =>
        e.trace_id === pending.traceId &&
        e.event_type === "MISSION_STATUS" &&
        /EXECUTION_AUTHORIZED|BLOCKED|REJECTED/.test(e.status)
    );
    if (!terminal) return;
    if (/EXECUTION_AUTHORIZED/.test(terminal.status)) {
      setManeuver((current) => current ?? { satId: pending.satId, startedAt: Date.now() });
    }
    setPending(null);
  }, [events, pending]);

  // Screen-reader announcements. Deliberately sparse: terminal outcomes and
  // safety interventions only, never the per-poll audit chatter, so the live
  // region stays useful instead of becoming noise.
  useEffect(() => {
    if (!settings.announce) return;
    const notable = events.filter(
      (e) =>
        e.event_type === "MISSION_STATUS" ||
        e.event_type === "CIRCUIT_BREAKER_TRIPPED" ||
        /REJECTED|BLOCKED/.test(String(e.status))
    );
    const latest = notable[notable.length - 1];
    if (!latest || announcedRef.current.has(latest.seq)) return;
    announcedRef.current.add(latest.seq);
    setAnnouncement(
      `${humanise(latest.agent_name)}: ${String(latest.event_type).toLowerCase().replace(/_/g, " ")}, ${String(
        latest.status
      )
        .toLowerCase()
        .replace(/_/g, " ")}`
    );
  }, [events, settings.announce]);

  return (
    <div className="flex h-screen flex-col bg-ink-900">
      <a href="#globe" className="skip-link">
        Skip to the orbital view
      </a>

      {/* Polite live region: assistive tech only. */}
      <p className="sr-only-live" role="status" aria-live="polite" aria-atomic="true">
        {announcement}
      </p>

      <header className="relative z-20 flex shrink-0 items-center gap-5 border-b border-hair bg-ink-800 px-4 py-2.5">
        <div className="flex items-center gap-2.5">
          <span className={highRiskActive ? "text-alert" : "text-accent"}>
            <Mark />
          </span>
          <div className="leading-none">
            <h1 className="font-mono text-base font-medium tracking-[0.24em] text-fg">ORBIT</h1>
            <p className="mt-1 text-2xs uppercase text-fg-3">Orbital command center</p>
          </div>
        </div>

        <div className="ml-3 hidden items-center gap-5 border-l border-hair pl-5 md:flex">
          <Readout
            label="feed"
            tone={connected ? "bg-nominal" : "bg-alert"}
            value={connected ? "live" : "offline"}
          />
          <Readout
            label="telemetry"
            tone={orbitalError ? "bg-caution" : "bg-nominal"}
            value={orbitalError ? "stale" : "nominal"}
          />
          <Readout label="epoch" tone="bg-ink-500" value={`${clockOf(generatedUtc)}Z`} />
        </div>

        <div className="ml-auto flex items-center gap-3">
          {highRiskActive && (
            <span className="flex items-center gap-2 rounded border border-alert/40 px-2.5 py-1.5 text-sm text-alert">
              <span className="animate-blink h-1.5 w-1.5 rounded-full bg-alert" aria-hidden="true" />
              High-risk conjunction
            </span>
          )}
          {maneuver && (
            <span className="flex items-center gap-2 rounded border border-caution/40 px-2.5 py-1.5 text-sm text-caution">
              <span className="h-1.5 w-1.5 rounded-full bg-caution" aria-hidden="true" />
              Maneuver &middot; <span className="font-mono">{maneuver.satId}</span>
            </span>
          )}
          {debriefId && !debriefOpen && (
            <button
              onClick={() => setDebriefOpen(true)}
              className="flex items-center gap-2 rounded border border-accent/50 px-2.5 py-1.5 text-sm text-accent transition-colors duration-150 ease-console hover:border-accent hover:bg-accent/10"
              title="Autonomous Veo-generated mission debrief"
            >
              Mission debrief
            </button>
          )}
          <button onClick={() => setAlertOpen(true)} className="btn-primary">
            Trigger conjunction alert
          </button>
          <button
            onClick={() => setSettingsOpen(true)}
            aria-label="Display and accessibility settings"
            title="Display & accessibility"
            className="rounded border border-hair p-2 text-fg-2 transition-colors duration-150 ease-console hover:border-hairlit hover:text-fg"
          >
            <IconSettings size={14} />
          </button>
        </div>

        {/* Indeterminate progress while the fleet is mid-mission. */}
        {pending && (
          <span className="absolute inset-x-0 bottom-0 h-px overflow-hidden" aria-hidden="true">
            <span className="animate-sweep block h-px w-1/4 bg-accent" />
          </span>
        )}
      </header>

      <main className="grid min-h-0 flex-1 gap-px bg-hair max-lg:flex max-lg:flex-col lg:grid-cols-[320px_minmax(0,1fr)_360px]">
        <aside aria-label="Mission feed" className="min-h-0 bg-ink-800 max-lg:h-64">
          <MissionFeed events={events} connected={connected} />
        </aside>

        <section id="globe" aria-label="Orbital view" className="relative min-h-0 bg-ink-900 max-lg:h-[58vh]">
          <CesiumViewer
            objects={objects}
            conjunctions={conjunctions}
            maneuver={maneuver}
            selectedId={selectedId}
            onSelect={handleSelect}
          />
          {orbitalError && (
            <p className="absolute right-3 top-3 rounded border border-caution/40 bg-ink-900/85 px-2.5 py-1.5 font-mono text-xs text-caution backdrop-blur-sm">
              telemetry: {orbitalError}
            </p>
          )}
        </section>

        {/* One scroll surface: sections stack top-down instead of the armor log
            being pinned to the bottom with dead space above it. */}
        <aside
          aria-label="Fleet status"
          className="min-h-0 divide-y divide-hair overflow-y-auto bg-ink-800 max-lg:h-96"
        >
          <FleetStatus
            tree={tree}
            treeError={treeError}
            satellites={satellites}
            feedEvents={events}
            selectedId={selectedId}
            onSelect={handleSelect}
          />
          <ArmorLog feedEvents={events} />
        </aside>
      </main>

      <ConjunctionAlert
        open={alertOpen}
        onClose={() => setAlertOpen(false)}
        satellites={satellites}
        debris={debris}
        onMissionComplete={handleMissionComplete}
      />
      <MissionDebrief open={debriefOpen} onClose={() => setDebriefOpen(false)} conjunctionId={debriefId} />
      <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}

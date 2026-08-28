import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CesiumViewer from "./components/CesiumViewer.jsx";
import MissionFeed from "./components/MissionFeed.jsx";
import FleetStatus from "./components/FleetStatus.jsx";
import ArmorLog from "./components/ArmorLog.jsx";
import ConjunctionAlert from "./components/ConjunctionAlert.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";
import MissionDebrief from "./components/MissionDebrief.jsx";
import FirstRun from "./components/FirstRun.jsx";
import ShortcutHelp from "./components/ShortcutHelp.jsx";
import { IconSettings } from "./components/icons.jsx";
import useLiveFeed from "./hooks/useLiveFeed.js";
import useOrbitalState from "./hooks/useOrbitalState.js";
import useAgentTree from "./hooks/useAgentTree.js";
import useSettings from "./hooks/useSettings.jsx";
import useAuthGate from "./hooks/useAuthGate.js";
import { postEvolution } from "./lib/api.js";
import useKeyboardShortcuts from "./hooks/useKeyboardShortcuts.js";
import { clockOf, countdown, humanise, isOurAsset, objectName, parseUtc } from "./lib/format.js";

/** How long a mission may go without a terminal audit record before we treat
 *  it as abandoned. */
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

/** One compact readout in the header rail: a label, a state dot, a value. */
function Readout({ label, tone, value }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-2xs uppercase text-fg-3">{label}</span>
      <span className={`h-1.5 w-1.5 rounded-full ${tone}`} aria-hidden="true" />
      <span className="font-mono text-xs text-fg-2">{value}</span>
    </div>
  );
}

function SystemStatus({ connected, worstFuel, armorTripped, provenance }) {
  const simulated = provenance?.simulated;
  const dataSegment =
    simulated === false
      ? { label: "DATA LIVE", tone: "text-nominal" }
      : simulated === true
        ? { label: "DATA SIMULATED", tone: "text-caution" }
        : { label: "DATA ACQUIRING", tone: "text-fg-3" };

  const segments = [
    {
      key: "fleet",
      label: connected ? "FLEET ONLINE" : "FLEET OFFLINE",
      tone: connected ? "text-nominal" : "text-alert",
    },
    {
      key: "fuel",
      label:
        worstFuel === null || worstFuel === undefined
          ? "FUEL UNKNOWN"
          : worstFuel > 50
            ? "FUEL OPTIMAL"
            : worstFuel > 20
              ? `FUEL LOW ${Math.round(worstFuel)}%`
              : `FUEL CRITICAL ${Math.round(worstFuel)}%`,
      tone:
        worstFuel === null || worstFuel === undefined
          ? "text-fg-3"
          : worstFuel > 50
            ? "text-nominal"
            : worstFuel > 20
              ? "text-caution"
              : "text-alert",
    },
    {
      key: "armor",
      label: armorTripped ? "ARMOR REJECTED" : "ARMOR ACTIVE",
      tone: armorTripped ? "text-alert" : "text-nominal",
    },
    { key: "data", ...dataSegment },
  ];

  return (
    <div className="hidden items-center gap-2 font-mono text-2xs tracking-wider xl:flex">
      {segments.map((segment, index) => (
        <span key={segment.key} className="flex items-center gap-2">
          {index > 0 && <span className="text-hairlit" aria-hidden="true">|</span>}
          <span className={segment.tone}>{segment.label}</span>
        </span>
      ))}
    </div>
  );
}

export default function App() {
  const { events, connected } = useLiveFeed();
  const [exerciseOn, setExerciseOn] = useState(false);
  const { objects, conjunctions, provenance, generatedUtc, error: orbitalError } = useOrbitalState(
    undefined,
    exerciseOn
  );
  const { tree, error: treeError } = useAgentTree();
  const { settings } = useSettings();
  const authRejected = useAuthGate();

  const [alertOpen, setAlertOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [maneuver, setManeuver] = useState(null);
  const [pending, setPending] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [announcement, setAnnouncement] = useState("");
  const [debriefId, setDebriefId] = useState(null);
  const [debriefOpen, setDebriefOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [evolving, setEvolving] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [fleetSummary, setFleetSummary] = useState({ worstFuel: null, assetCount: 0 });
  const handleFleetSummary = useCallback((summary) => setFleetSummary(summary), []);
  const announcedRef = useRef(new Set());

  const satellites = useMemo(() => objects.filter((o) => o.type === "satellite"), [objects]);


  const assets = useMemo(() => objects.filter(isOurAsset), [objects]);
  const secondaries = useMemo(() => objects.filter((o) => !isOurAsset(o)), [objects]);


  const fleetConjunctions = useMemo(() => {
    const ours = new Set(assets.map((a) => a.id));
    return (conjunctions || []).filter((c) => ours.has(c.sat_id) || ours.has(c.debris_id));
  }, [conjunctions, assets]);

  const highRiskActive = useMemo(
    () => fleetConjunctions.some((c) => c.risk_band === "HIGH"),
    [fleetConjunctions]
  );


  const nextTca = useMemo(() => {
    const times = fleetConjunctions
      .map((c) => parseUtc(c.tca_utc))
      .filter((t) => Number.isFinite(t));
    return times.length ? Math.min(...times) : NaN;
  }, [fleetConjunctions]);

  useEffect(() => {
    if (!Number.isFinite(nextTca)) return undefined;
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [nextTca]);


  const armorTripped = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i -= 1) {
      const record = events[i];
      if (record?.event_type !== "MODEL_ARMOR_SWEEP") continue;
      return String(record.status || "").toUpperCase().includes("REJECT");
    }
    return false;
  }, [events]);

  const maneuverName = useMemo(() => {
    if (!maneuver) return "";
    return objectName(objects.find((o) => o.id === maneuver.satId)) || maneuver.satId;
  }, [maneuver, objects]);

  const handleSelect = useCallback((id) => setSelectedId(id ?? null), []);

  const anyDialogOpen = alertOpen || settingsOpen || debriefOpen || helpOpen;

  const toggleFullscreen = useCallback(() => {
    const node = document.getElementById("globe");
    if (!node) return;

    if (document.fullscreenElement){
      document.exitFullscreen?.().catch(() => {});
    }
    else{
      node.requestFullscreen?.().catch(() => {});
    }
  }, []);

  const openAlert = useCallback(() => setAlertOpen(true), []);
  const toggleHelp = useCallback(() => setHelpOpen((open) => !open), []);

  useKeyboardShortcuts({
    onTriggerAlert: openAlert,
    onToggleFullscreen: toggleFullscreen,
    onToggleHelp: toggleHelp,
    enabled: !anyDialogOpen,
  });


  const triggerEvolution = useCallback(async () => {
    setEvolving(true);
    try {
      const result = await postEvolution();
      setAnnouncement(
        `Evolution cycle complete: ${result?.status || "finished"}.`
      );
    } catch (error) {
      setAnnouncement(`Evolution cycle failed: ${error.message}`);
    } finally {
      setEvolving(false);
    }
  }, []);

  function handleMissionComplete({ request, response }) {
    setPending({
      satId: request.sat_id,
      traceId: response.trace_id,
      expiresAt: Date.now() + MISSION_TIMEOUT_MS,
    });
    if (response.status === "EXECUTION_AUTHORIZED") {
      setManeuver({ satId: request.sat_id, startedAt: Date.now() });
    }
    // The fleet documented its own solution, so surface the debrief entry.
    if (response.conjunction_id) {
      setDebriefId(response.conjunction_id);
      setDebriefOpen(false);
    }
  }

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
    <div
      className={`relative flex h-screen flex-col bg-ink-900 ${
        highRiskActive ? "alert-perimeter" : ""
      }`}
    >
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
          {Number.isFinite(nextTca) && (
            <Readout
              label="next tca"
              tone={highRiskActive ? "bg-alert" : "bg-caution"}
              value={countdown(nextTca, nowMs)}
            />
          )}
        </div>

        <SystemStatus
          connected={connected}
          worstFuel={fleetSummary.worstFuel}
          armorTripped={armorTripped}
          provenance={provenance}
        />

        <div className="ml-auto flex items-center gap-3">
          {highRiskActive && (
            <span
              className="flex items-center gap-2 rounded border border-alert/40 px-2.5 py-1.5 text-sm text-alert"
              title="A high-risk close approach involving an asset this fleet commands"
            >
              <span className="animate-blink h-1.5 w-1.5 rounded-full bg-alert" aria-hidden="true" />
              High risk to our asset
            </span>
          )}
          {maneuver && (
            <span className="flex items-center gap-2 rounded border border-caution/40 px-2.5 py-1.5 text-sm text-caution">
              <span className="h-1.5 w-1.5 rounded-full bg-caution" aria-hidden="true" />
              Maneuver &middot; <span className="font-mono">{maneuverName}</span>
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
          {/*
           * Opt-in overlay for the coordination exercise. Two manoeuvrable
           * payloads almost never appear together in real conjunction data,
           * which leaves the negotiation path with no live encounter to run
           * against. This drops a clearly-labelled simulated pair onto the
           * map. It stays off by default, so the command picture reads as
           * live unless somebody asks for otherwise.
           */}
          <button
            onClick={() => setExerciseOn((on) => !on)}
            aria-pressed={exerciseOn}
            className={`rounded border px-2.5 py-1.5 text-sm transition-colors duration-150 ease-console ${
              exerciseOn
                ? "border-caution bg-caution/10 text-caution"
                : "border-hair text-fg-3 hover:border-caution/50 hover:text-caution"
            }`}
            title="Overlay the simulated payload-vs-payload pair used to demonstrate operator coordination"
          >
            {exerciseOn ? "Exercise overlay: on" : "Exercise overlay: off"}
          </button>
          <button onClick={() => setAlertOpen(true)} className="btn-primary">
            Trigger conjunction alert
          </button>
          <button
            onClick={triggerEvolution}
            disabled={evolving}
            title="Run one self-evolution cycle: re-tune the screening policy from mission history"
            className="rounded border border-hair px-2.5 py-1.5 text-sm text-fg-3 transition-colors duration-150 ease-console hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
          >
            {evolving ? "Evolving…" : "Trigger evolution"}
          </button>
          <button
            onClick={toggleHelp}
            aria-label="Keyboard shortcuts"
            title="Keyboard shortcuts (?)"
            className="rounded border border-hair px-2.5 py-1.5 font-mono text-sm text-fg-2 transition-colors duration-150 ease-console hover:border-hairlit hover:text-fg"
          >
            ?
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

      {/*
       * A 401 says the key is missing or wrong. It does not say the fleet
       * is down. Without this banner the console scatters three unrelated
       * little errors and simply reads as broken, which is precisely what
       * anyone cloning the repo and skipping frontend/.env.example would
       * see first.
       */}
      {authRejected && (
        <div
          role="alert"
          className="shrink-0 border-b border-alert/40 bg-alert/10 px-4 py-2 text-sm text-alert"
        >
          API requests are being rejected (401). Copy{" "}
          <code className="font-mono text-xs">frontend/.env.example</code> to{" "}
          <code className="font-mono text-xs">frontend/.env.local</code> and set{" "}
          <code className="font-mono text-xs">VITE_ORBIT_API_KEY</code> to match{" "}
          <code className="font-mono text-xs">ORBIT_API_KEY</code> in the backend&rsquo;s{" "}
          <code className="font-mono text-xs">.env</code>, then reload.
        </div>
      )}

      {/*
       * Stacks to a single column below `lg`. The children carry fixed
       * heights that add up to more than the viewport, and `body` is
       * `overflow: hidden`, so with no scroller here the fleet status and
       * armor log were clipped away entirely, unreachable on a phone.
       */}
      <main className="grid min-h-0 flex-1 gap-px bg-hair max-lg:flex max-lg:flex-col max-lg:overflow-y-auto lg:grid-cols-[320px_minmax(0,1fr)_360px]">
        <aside aria-label="Mission feed" className="min-h-0 bg-ink-800 max-lg:h-64 max-lg:shrink-0">
          <MissionFeed events={events} connected={connected} />
        </aside>

        <section
          id="globe"
          aria-label="Orbital view"
          className="relative min-h-0 bg-ink-900 max-lg:h-[58vh] max-lg:shrink-0"
        >
          <CesiumViewer
            objects={objects}
            conjunctions={conjunctions}
            maneuver={maneuver}
            selectedId={selectedId}
            onSelect={handleSelect}
          />
          <FirstRun onStart={() => setAlertOpen(true)} />
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
          className="min-h-0 divide-y divide-hair overflow-y-auto bg-ink-800 max-lg:h-auto max-lg:shrink-0 max-lg:overflow-visible"
        >
          <FleetStatus
            tree={tree}
            treeError={treeError}
            satellites={satellites}
            provenance={provenance}
            feedEvents={events}
            selectedId={selectedId}
            onSelect={handleSelect}
            onFleetSummary={handleFleetSummary}
          />
          <ArmorLog feedEvents={events} />
        </aside>
      </main>

      <ConjunctionAlert
        open={alertOpen}
        onClose={() => setAlertOpen(false)}
        assets={assets}
        secondaries={secondaries}
        conjunctions={conjunctions}
        onMissionComplete={handleMissionComplete}
      />
      <MissionDebrief open={debriefOpen} onClose={() => setDebriefOpen(false)} conjunctionId={debriefId} />
      <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <ShortcutHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
    </div>
  );
}

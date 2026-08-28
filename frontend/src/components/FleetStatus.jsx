import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../lib/api.js";
import { clockOf, humanise, num, objectLabel } from "../lib/format.js";
import { breakerRollup, deriveAgentActivity, STATE_LABEL, STATE_TONE } from "../lib/agentActivity.js";
import { IconChevron } from "./icons.jsx";
import StatusMark from "./StatusMark.jsx";

const BREAKER_POLICY = "3 attempts, backoff 1s / 2s / 4s";
const STATE_POLL_MS = 30_000;
const ACTIVITY_TICK_MS = 5_000;

/**
 * One node of the live ADK agent tree.
 *
 * Depth is drawn with a guide rail rather than raw indentation so the
 * delegation chain stays readable four levels down. Class name, model and
 * temperature all live on one quiet mono line under the agent's name — shown
 * as-is, because forcing `LlmAgent` through an uppercase transform produced
 * unreadable runs like `FLEETCOMMANDERPIPELINE`.
 */
// Written out in full so Tailwind's scanner can see every class it must emit.
const STATE_TEXT = {
  running: "text-caution",
  ok: "text-nominal",
  retrying: "text-caution",
  tripped: "text-alert",
  idle: "text-fg-3",
  standby: "text-fg-3",
};

function AgentEvents({ entry }) {
  if (!entry || entry.events.length === 0) {
    return (
      <p className="py-1 font-mono text-2xs tracking-normal text-fg-3">
        No audit records yet for this agent.
      </p>
    );
  }
  return (
    <ul className="space-y-0.5 py-1">
      {entry.events.map((record) => (
        <li key={record.seq} className="flex items-baseline gap-2">
          <span className="shrink-0 font-mono text-2xs tracking-normal text-fg-3">
            {clockOf(record.timestamp)}
          </span>
          <span className="min-w-0 flex-1 truncate font-mono text-2xs tracking-normal text-fg-2">
            {String(record.event_type || "").toLowerCase()}
          </span>
          <span className="shrink-0 font-mono text-2xs tracking-normal text-fg-3">
            {String(record.status || "").toLowerCase()}
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * One node of the live ADK agent tree.
 */
function AgentNode({ node, depth = 0, activity, selected, onSelect }) {
  const [open, setOpen] = useState(depth < 2);
  const children = node.children || [];
  const tools = node.tools || [];
  const toolless = node.type === "LlmAgent" && tools.length === 0;

  const entry = activity.get(node.name);
  const state = entry?.state || "standby";
  const tone = STATE_TONE[state] || "info";
  const isSelected = selected === node.name;
  const live = state === "running" || state === "tripped";

  return (
    <div className={depth > 0 ? "ml-2 border-l border-hair pl-3" : ""}>
      <div className={`flex w-full items-start gap-1.5 rounded py-1 ${isSelected ? "bg-ink-700" : ""}`}>
        <button
          type="button"
          onClick={() => children.length && setOpen((value) => !value)}
          aria-label={children.length ? `${open ? "Collapse" : "Expand"} ${humanise(node.name)}` : undefined}
          aria-expanded={children.length ? open : undefined}
          tabIndex={children.length ? 0 : -1}
          className={`mt-0.5 shrink-0 text-fg-3 ${children.length ? "hover:text-fg" : "invisible"}`}
        >
          <IconChevron open={open} size={11} />
        </button>
        <button
          type="button"
          onClick={() => onSelect(isSelected ? null : node.name)}
          aria-pressed={isSelected}
          className="group min-w-0 flex-1 text-left"
        >
          <span className="flex items-center gap-1.5">
            <StatusMark tone={tone} size={9} className={live ? "animate-blink" : ""} />
            <span className="min-w-0 flex-1 truncate text-sm text-fg transition-colors group-hover:text-accent">
              {humanise(node.name)}
            </span>
            <span className={`shrink-0 text-2xs uppercase ${STATE_TEXT[state] || "text-fg-3"}`}>
              {STATE_LABEL[state]}
            </span>
          </span>
          <span className="mt-0.5 block truncate font-mono text-2xs tracking-normal text-fg-3">
            {[node.type, node.model, node.temperature != null ? `T ${node.temperature}` : null]
              .filter(Boolean)
              .join(" · ")}
          </span>
          {tools.length > 0 && (
            <span className="mt-0.5 block truncate font-mono text-2xs tracking-normal text-fg-2">
              {tools.map((tool) => `${tool}()`).join("  ")}
            </span>
          )}
          {toolless && (
            <span className="mt-0.5 block font-mono text-2xs tracking-normal text-fg-3">routing only, no tools</span>
          )}
        </button>
      </div>
      {isSelected && (
        <div className="ml-3 border-l border-accent/40 pl-3">
          <AgentEvents entry={entry} />
        </div>
      )}
      {open &&
        children.map((child) => (
          <AgentNode
            key={child.name}
            node={child}
            depth={depth + 1}
            activity={activity}
            selected={selected}
            onSelect={onSelect}
          />
        ))}
    </div>
  );
}

// Written out in full so Tailwind's scanner can see every class it must emit.
const FUEL_TONES = {
  nominal: { text: "text-nominal", bar: "bg-nominal" },
  caution: { text: "text-caution", bar: "bg-caution" },
  alert: { text: "text-alert", bar: "bg-alert" },
};

function FuelRow({ satellite, state, active, onSelect }) {
  const satId = satellite.id;
  const fuel = Number(state?.fuel_percentage ?? 100);
  const health = Number(state?.thruster_health ?? 100);
  const dvUsed = Number(state?.total_dv_expended ?? 0);
  const tone = FUEL_TONES[fuel > 50 ? "nominal" : fuel > 20 ? "caution" : "alert"];

  return (
    <button
      type="button"
      onClick={() => onSelect?.(active ? null : satId)}
      className={`relative block w-full rounded px-2.5 py-2 pl-3 text-left transition-colors duration-150 ease-console ${
        active ? "bg-ink-700" : "hover:bg-ink-700/60"
      }`}
    >
      {active && <span className="absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-accent" />}
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-xs text-fg" title={satId}>
          {objectLabel(satellite)}
        </span>
        <span className="shrink-0 font-mono text-2xs tracking-normal text-fg-3">
          thr {num(health, 0)}% &middot; &Delta;v {num(dvUsed, 1)}
        </span>
      </div>
      {/* Meter, not a rule: the empty track stays visible at a full tank. */}
      <div className="mt-1.5 flex items-center gap-2">
        <span className="h-1 flex-1 overflow-hidden rounded-full bg-ink-500">
          <span
            className={`block h-full ${tone.bar} transition-[width] duration-700 ease-console`}
            style={{ width: `${Math.min(Math.max(fuel, 0), 100)}%` }}
          />
        </span>
        <span className={`w-11 shrink-0 text-right font-mono text-2xs tracking-normal ${tone.text}`}>
          {num(fuel, 1)}%
        </span>
      </div>
    </button>
  );
}

/**
 * A third-party spacecraft we screen against and negotiate with.
 */
function CounterpartyRow({ object, active, onSelect }) {
  return (
    <button
      type="button"
      onClick={() => onSelect?.(active ? null : object.id)}
      aria-pressed={active}
      className={`relative block w-full rounded px-2.5 py-2 pl-3 text-left transition-colors duration-150 ease-console ${
        active ? "bg-ink-700" : "hover:bg-ink-700/60"
      }`}
    >
      {active && <span className="absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-accent" />}
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-xs text-fg-2" title={object.id}>
          {object.name || object.id}
        </span>
        <span className="shrink-0 font-mono text-2xs tracking-normal text-fg-3">
          NORAD {object.norad_id}
        </span>
      </div>
      <div className="mt-0.5 truncate font-mono text-2xs tracking-normal text-fg-3">
        {/*
         * Live objects have no operator attribution we can honestly claim, so
         * fall back to what the catalogue does tell us. "cannot manoeuvre" is
         * a fact about debris; "manoeuvrability unknown" is a fact about the
         * data — a catalogued payload may be defunct or passive by design.
         */}
        {object.operator
          ? `${object.operator} · counterparty`
          : `${(object.object_type || "object").toLowerCase()} · ${
              object.manoeuvrability === "none" ? "cannot manoeuvre" : "manoeuvrability unknown"
            }`}
      </div>
    </button>
  );
}

export default function FleetStatus({ tree, treeError, satellites, provenance, feedEvents, selectedId, onSelect, onFleetSummary }) {
  const [states, setStates] = useState({});
  const [selectedAgent, setSelectedAgent] = useState(null);
  const [tick, setTick] = useState(0);
  const live = provenance?.simulated === false;
  const owned = useMemo(() => satellites.filter((sat) => sat.owned && !sat.exercise), [satellites]);
  const counterparties = useMemo(
    () => satellites.filter((sat) => !sat.owned && !sat.exercise),
    [satellites]
  );
  const exercise = useMemo(() => satellites.filter((sat) => sat.exercise), [satellites]);
  const satelliteIds = owned.map((sat) => sat.id).join(",");

  // A quiet heartbeat so "running" can decay to "idle" without new events.
  useEffect(() => {
    const timer = setInterval(() => setTick((value) => value + 1), ACTIVITY_TICK_MS);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let alive = true;
    async function load() {
      const ids = satelliteIds ? satelliteIds.split(",") : [];
      const results = await Promise.all(
        ids.map(async (id) => {
          try {
            return [id, await apiFetch(`/api/satellite_state/${encodeURIComponent(id)}`)];
          } catch {
            // A missing Memory Bank record is a legitimate state, not an error.
            return [id, null];
          }
        })
      );
      if (alive) setStates(Object.fromEntries(results));
    }
    load();
    const timer = setInterval(load, STATE_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [satelliteIds]);

  /*
   * Publish the fleet's worst fuel margin to the header status strip.
   */
  const worstFuel = useMemo(() => {
    const readings = owned
      .map((sat) => states[sat.id]?.fuel_percentage)
      .filter((value) => Number.isFinite(Number(value)))
      .map(Number);
    return readings.length ? Math.min(...readings) : null;
  }, [owned, states]);

  useEffect(() => {
    onFleetSummary?.({ worstFuel, assetCount: owned.length });
  }, [worstFuel, owned.length, onFleetSummary]);

  // eslint-disable-next-line react-hooks/exhaustive-deps -- `tick` is the
  // intentional staleness heartbeat; it has no other role in the derivation.
  const activity = useMemo(() => deriveAgentActivity(feedEvents), [feedEvents, tick]);
  const breakers = useMemo(() => breakerRollup(activity), [activity]);

  // No inner scroller: the whole right column scrolls as one surface, so the
  // sections stack tight instead of leaving a dead gap above the armor log.
  return (
    <div className="flex flex-col divide-y divide-hair">
      <section className="px-4 py-3">
        <div className="flex items-baseline justify-between">
          <h2 className="eyebrow">Agent fleet</h2>
          <span className="font-mono text-2xs tracking-normal text-fg-3">select an agent for its audit trail</span>
          {tree && <span className="font-mono text-2xs tracking-normal text-fg-3">v{tree.fleet_version}</span>}
        </div>
        <div className="mt-2">
          {treeError && <p className="text-xs text-alert">agent_tree unavailable — {treeError}</p>}
          {tree?.root && (
            <AgentNode
              node={tree.root}
              activity={activity}
              selected={selectedAgent}
              onSelect={setSelectedAgent}
            />
          )}
          {!tree?.root && !treeError && <p className="py-2 text-sm text-fg-3">Discovering fleet…</p>}
        </div>
      </section>

      <section className="px-2 py-3">
        <div className="flex items-baseline justify-between px-2">
          <h2 className="eyebrow">Protected asset</h2>
          <span className="font-mono text-2xs tracking-normal text-fg-3">
            {live ? "live orbit" : "simulated"}
          </span>
        </div>
        {live && (
          /*
           * In live mode the protected asset is a real spacecraft we do not
           * operate. Its orbit is real; its fuel and thruster state are not
           * telemetry and must never be presented as such.
           */
          <p className="mt-1 px-2 font-mono text-2xs leading-relaxed tracking-normal text-fg-3">
            Orbit from live element set. Flight state below is simulated —
            O.R.B.I.T. holds no telemetry or manoeuvre authority for this vehicle.
          </p>
        )}
        <div className="mt-1.5 space-y-0.5">
          {owned.map((sat) => (
            <FuelRow
              key={sat.id}
              satellite={sat}
              state={states[sat.id]}
              active={selectedId === sat.id}
              onSelect={onSelect}
            />
          ))}
          {owned.length === 0 && (
            <p className="px-2 py-2 text-sm text-fg-3">No operated asset in the catalogue.</p>
          )}
        </div>
      </section>

      <section className="px-2 py-3">
        <div className="flex items-baseline justify-between px-2">
          <h2 className="eyebrow">Tracked counterparties</h2>
          <span className="font-mono text-2xs tracking-normal text-fg-3">
            {live ? "real objects · screened, not commanded" : "screened, not commanded"}
          </span>
        </div>
        <div className="mt-1.5 space-y-0.5">
          {counterparties.map((sat) => (
            <CounterpartyRow
              key={sat.id}
              object={sat}
              active={selectedId === sat.id}
              onSelect={onSelect}
            />
          ))}
          {counterparties.length === 0 && (
            <p className="px-2 py-2 text-sm text-fg-3">No third-party spacecraft in view.</p>
          )}
        </div>
      </section>

      {exercise.length > 0 && (
        <section className="px-2 py-3">
          <div className="flex items-baseline justify-between px-2">
            <h2 className="eyebrow text-caution">Coordination exercise</h2>
            <span className="font-mono text-2xs tracking-normal text-caution">simulated</span>
          </div>
          <p className="mt-1 px-2 font-mono text-2xs leading-relaxed tracking-normal text-fg-3">
            Not real spacecraft. Both objects can manoeuvre, which is what makes
            operator-to-operator coordination possible — a pairing real
            conjunction data almost never provides.
          </p>
          <div className="mt-1.5 space-y-0.5">
            {exercise.map((sat) => (
              <button
                key={sat.id}
                type="button"
                onClick={() => onSelect?.(selectedId === sat.id ? null : sat.id)}
                aria-pressed={selectedId === sat.id}
                className={`relative block w-full rounded px-2.5 py-2 pl-3 text-left transition-colors duration-150 ease-console ${
                  selectedId === sat.id ? "bg-ink-700" : "hover:bg-ink-700/60"
                }`}
              >
                {selectedId === sat.id && (
                  <span className="absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-caution" />
                )}
                <div className="flex items-baseline gap-2">
                  <span className="min-w-0 flex-1 truncate text-xs text-caution" title={sat.id}>
                    {sat.name || sat.id}
                  </span>
                  <span className="shrink-0 font-mono text-2xs tracking-normal text-fg-3">
                    NORAD {sat.norad_id}
                  </span>
                </div>
                <div className="mt-0.5 truncate font-mono text-2xs tracking-normal text-fg-3">
                  {sat.role === "exercise_asset" ? "our asset" : "partner operator"} · simulated
                </div>
              </button>
            ))}
          </div>
        </section>
      )}

      <section className="px-4 py-3">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="eyebrow">Circuit breakers</h2>
          <span className="truncate font-mono text-2xs tracking-normal text-fg-3">{BREAKER_POLICY}</span>
        </div>
        <div className="mt-2 space-y-1">
          {breakers.map(({ agent, state, tripped }) => (
            <div key={agent} className="flex items-center gap-2">
              <StatusMark
                tone={STATE_TONE[state] || "info"}
                className={tripped || state === "running" ? "animate-blink" : ""}
              />
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg-2">{agent}</span>
              <span className={`shrink-0 text-2xs uppercase ${tripped ? "text-alert" : "text-fg-3"}`}>
                {tripped ? "tripped" : state === "retrying" ? "retrying" : "closed"}
              </span>
            </div>
          ))}
          {breakers.length === 0 && (
            <p className="text-sm text-fg-3">All breakers closed — no trips observed this session.</p>
          )}
        </div>
      </section>
    </div>
  );
}

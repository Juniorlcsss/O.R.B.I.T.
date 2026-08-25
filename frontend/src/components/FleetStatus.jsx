import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../lib/api.js";
import { humanise, num } from "../lib/format.js";
import { IconChevron } from "./icons.jsx";
import StatusMark from "./StatusMark.jsx";

const BREAKER_POLICY = "3 attempts, backoff 1s / 2s / 4s";
const STATE_POLL_MS = 30_000;

/**
 * One node of the live ADK agent tree.
 *
 * Depth is drawn with a guide rail rather than raw indentation so the
 * delegation chain stays readable four levels down. Class name, model and
 * temperature all live on one quiet mono line under the agent's name — shown
 * as-is, because forcing `LlmAgent` through an uppercase transform produced
 * unreadable runs like `FLEETCOMMANDERPIPELINE`.
 */
function AgentNode({ node, depth = 0 }) {
  const [open, setOpen] = useState(depth < 2);
  const children = node.children || [];
  const tools = node.tools || [];
  const toolless = node.type === "LlmAgent" && tools.length === 0;

  return (
    <div className={depth > 0 ? "ml-2 border-l border-hair pl-3" : ""}>
      <button
        type="button"
        onClick={() => children.length && setOpen((value) => !value)}
        className="group flex w-full items-start gap-1.5 py-1 text-left"
      >
        <span className={`mt-0.5 shrink-0 text-fg-3 ${children.length ? "" : "invisible"}`}>
          <IconChevron open={open} size={11} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm text-fg transition-colors group-hover:text-accent">
            {humanise(node.name)}
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
        </span>
      </button>
      {open && children.map((child) => <AgentNode key={child.name} node={child} depth={depth + 1} />)}
    </div>
  );
}

/**
 * Derive per-agent breaker state from the audit stream: an agent is tripped
 * only if its most recent record was the trip itself.
 */
function breakerStatuses(events) {
  const lastAny = new Map();
  const lastTrip = new Map();
  for (const record of events) {
    if (!record.agent_name || record.agent_name === "orbit.api.gateway") continue;
    lastAny.set(record.agent_name, record);
    if (record.event_type === "CIRCUIT_BREAKER_TRIPPED") lastTrip.set(record.agent_name, record);
  }
  return [...lastAny.keys()].sort().map((agent) => {
    const trippedAt = Date.parse(lastTrip.get(agent)?.timestamp || 0) || 0;
    const activeAt = Date.parse(lastAny.get(agent)?.timestamp || 0) || 0;
    return { agent, tripped: trippedAt >= activeAt && trippedAt > 0 };
  });
}

// Written out in full so Tailwind's scanner can see every class it must emit.
const FUEL_TONES = {
  nominal: { text: "text-nominal", bar: "bg-nominal" },
  caution: { text: "text-caution", bar: "bg-caution" },
  alert: { text: "text-alert", bar: "bg-alert" },
};

function FuelRow({ satId, state, active, onSelect }) {
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
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg">{satId}</span>
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

export default function FleetStatus({ tree, treeError, satellites, feedEvents, selectedId, onSelect }) {
  const [states, setStates] = useState({});
  const satelliteIds = satellites.map((sat) => sat.id).join(",");

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

  const breakers = useMemo(() => breakerStatuses(feedEvents), [feedEvents]);

  // No inner scroller: the whole right column scrolls as one surface, so the
  // sections stack tight instead of leaving a dead gap above the armor log.
  return (
    <div className="flex flex-col divide-y divide-hair">
      <section className="px-4 py-3">
        <div className="flex items-baseline justify-between">
          <h2 className="eyebrow">Agent fleet</h2>
          {tree && <span className="font-mono text-2xs tracking-normal text-fg-3">v{tree.fleet_version}</span>}
        </div>
        <div className="mt-2">
          {treeError && <p className="text-xs text-alert">agent_tree unavailable — {treeError}</p>}
          {tree?.root && <AgentNode node={tree.root} />}
          {!tree?.root && !treeError && <p className="py-2 text-sm text-fg-3">Discovering fleet…</p>}
        </div>
      </section>

      <section className="px-2 py-3">
        <h2 className="eyebrow px-2">Protected assets</h2>
        <div className="mt-1.5 space-y-0.5">
          {satellites.map((sat) => (
            <FuelRow
              key={sat.id}
              satId={sat.id}
              state={states[sat.id]}
              active={selectedId === sat.id}
              onSelect={onSelect}
            />
          ))}
          {satellites.length === 0 && <p className="px-2 py-2 text-sm text-fg-3">No assets in the catalogue.</p>}
        </div>
      </section>

      <section className="px-4 py-3">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="eyebrow">Circuit breakers</h2>
          <span className="truncate font-mono text-2xs tracking-normal text-fg-3">{BREAKER_POLICY}</span>
        </div>
        <div className="mt-2 space-y-1">
          {breakers.map(({ agent, tripped }) => (
            <div key={agent} className="flex items-center gap-2">
              <StatusMark tone={tripped ? "alert" : "nominal"} className={tripped ? "animate-blink" : ""} />
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg-2">{agent}</span>
              <span className={`shrink-0 text-2xs uppercase ${tripped ? "text-alert" : "text-fg-3"}`}>
                {tripped ? "tripped" : "closed"}
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

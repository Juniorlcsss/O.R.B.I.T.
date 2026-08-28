/*
 * Per-agent runtime state, derived from the audit stream the Command Center
 * already receives over SSE.
 */


const RUNNING_STALE_MS = 45_000;

/** Most recent records retained per agent for the detail view. */
const DETAIL_EVENTS = 12;

export const STATE_TONE = {
  running: "caution",
  ok: "nominal",
  retrying: "caution",
  tripped: "alert",
  idle: "info",
  standby: "info",
};

export const STATE_LABEL = {
  running: "running",
  ok: "ok",
  retrying: "retrying",
  tripped: "tripped",
  idle: "idle",
  standby: "standby",
};

const LOUD_SEVERITIES = new Set(["ERROR", "CRITICAL", "ALERT", "EMERGENCY"]);

function deriveState(record, now) {
  if (!record) return "standby";
  const type = record.event_type;
  const status = String(record.status || "").toUpperCase();
  const at = Date.parse(record.timestamp) || 0;

  if (type === "CIRCUIT_BREAKER_TRIPPED") return "tripped";
  if (type === "CIRCUIT_BREAKER_STATE") {
    if (status === "TRIPPED") return "tripped";
    if (status === "RETRYING") return "retrying";
    return "ok";
  }
  if (type === "AGENT_INVOCATION") {
    if (status === "RUNNING") return now - at > RUNNING_STALE_MS ? "idle" : "running";
    if (status === "FAILED") return "retrying";
    return "ok";
  }
  return LOUD_SEVERITIES.has(String(record.severity || "").toUpperCase()) ? "retrying" : "idle";
}


export function deriveAgentActivity(events, now = Date.now()) {
  const byAgent = new Map();
  for (const record of events) {
    const name = record?.agent_name;
    if (!name) continue;
    let entry = byAgent.get(name);
    if (!entry) {
      entry = { agent: name, events: [], last: null };
      byAgent.set(name, entry);
    }
    entry.events.push(record);
    entry.last = record;
  }
  for (const entry of byAgent.values()) {
    entry.events = entry.events.slice(-DETAIL_EVENTS).reverse();
    entry.state = deriveState(entry.last, now);
    entry.at = Date.parse(entry.last?.timestamp) || 0;
  }
  return byAgent;
}


export function breakerRollup(activity) {
  return [...activity.values()]
    .filter((entry) => !entry.agent.startsWith("orbit.") && !entry.agent.startsWith("geap_sim.") && !entry.agent.startsWith("tools."))
    .sort((a, b) => a.agent.localeCompare(b.agent))
    .map((entry) => ({ agent: entry.agent, state: entry.state, tripped: entry.state === "tripped" }));
}

/* Presentation helpers. Backend identifiers stay authoritative; these only
 * decide how an operator reads them on screen. */

const ACRONYMS = new Set(["id", "utc", "pc", "dv", "tca", "sgp4", "adk", "geap", "api"]);

/** `safety_officer` → `Safety Officer`; keeps known acronyms upper-cased. */
export function humanise(identifier) {
  if (!identifier) return "";
  return String(identifier)
    .replace(/[_.]/g, " ")
    .split(/\s+/)
    .filter(Boolean)
    .map((word) =>
      ACRONYMS.has(word.toLowerCase())
        ? word.toUpperCase()
        : word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()
    )
    .join(" ");
}

/** Two-letter agent monogram, e.g. `astrodynamics_specialist` → `AS`. */
export function monogram(identifier) {
  if (!identifier) return "··";
  const words = String(identifier).replace(/[_.]/g, " ").split(/\s+/).filter(Boolean);
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

/** Compact age string, e.g. `4s`, `12m`, `3h`. */
export function age(isoTimestamp, nowMs) {
  const then = Date.parse(isoTimestamp);
  if (Number.isNaN(then)) return "—";
  const seconds = Math.max(0, Math.round((nowMs - then) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h`;
}

/**
 * Map an audit record onto one of four operator-facing tones.
 * Order matters: a rejection inside an "execution" event is still a rejection.
 */
export function toneOf(record) {
  const haystack = `${record.status ?? ""} ${record.event_type ?? ""}`.toUpperCase();
  if (/TRIPPED|REJECT|BLOCK|CRITICAL|FAIL|DENIED|VIOLATION/.test(haystack)) return "alert";
  if (/HELD|STANDOFF|DISPATCH|DEGRADED|WARN|RETRY|PENDING/.test(haystack)) return "caution";
  if (/APPROVED|AUTHORIZED|HEALTHY|NOMINAL|CLEAR|OK\b/.test(haystack)) return "nominal";
  return "info";
}

/** Tailwind fragments per tone. One table, so tones can never drift apart. */
export const TONE = {
  nominal: { text: "text-nominal", rail: "bg-nominal", dot: "bg-nominal" },
  caution: { text: "text-caution", rail: "bg-caution", dot: "bg-caution" },
  alert: { text: "text-alert", rail: "bg-alert", dot: "bg-alert" },
  info: { text: "text-fg-2", rail: "bg-ink-500", dot: "bg-fg-3" },
};

/** Fixed-width number with graceful fallback for missing telemetry. */
export function num(value, digits = 1, fallback = "—") {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : fallback;
}

/** `2026-08-25T12:33:41Z` → `12:33:41`. */
export function clockOf(iso) {
  return typeof iso === "string" && iso.length >= 19 ? iso.slice(11, 19) : "--:--:--";
}

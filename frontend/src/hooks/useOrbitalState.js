import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api.js";

const POLL_INTERVAL_MS = 2000;

export default function useOrbitalState(intervalMs = POLL_INTERVAL_MS, exercise = false) {
  const [objects, setObjects] = useState([]);
  const [conjunctions, setConjunctions] = useState([]);
  const [provenance, setProvenance] = useState({ simulated: null, source: null });
  const [generatedUtc, setGeneratedUtc] = useState(null);
  const [lastUpdatedMs, setLastUpdatedMs] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let cancelled = false;
    let timer = null;

    async function poll() {
      try {
        const snapshot = await apiFetch(`/api/orbital_state${exercise ? "?exercise=true" : ""}`);
        if (cancelled) return;
        setObjects(snapshot.objects || []);
        setConjunctions(snapshot.conjunctions || []);
        setProvenance({
          simulated: snapshot.simulated !== false,
          source: snapshot.source || null,
          protectedSatId: snapshot.protected_sat_id || null,
          responseMode: snapshot.response_mode || null,
          exerciseActive: snapshot.exercise_active === true,
        });
        setGeneratedUtc(snapshot.generated_utc);
        setLastUpdatedMs(Date.now());
        setError(
          snapshot.status === "unavailable"
            ? `live orbital data unavailable — ${snapshot.reason || "Space-Track unreachable"}`
            : null
        );
      } catch (err) {
        if (!cancelled) setError(String(err.message || err));
      } finally {
        if (!cancelled) timer = setTimeout(poll, intervalMs);
      }
    }

    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [intervalMs, exercise]);

  return { objects, conjunctions, provenance, generatedUtc, lastUpdatedMs, error };
}

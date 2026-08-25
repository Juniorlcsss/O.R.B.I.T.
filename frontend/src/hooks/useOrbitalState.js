import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/api.js";

const POLL_INTERVAL_MS = 2000;

export default function useOrbitalState(intervalMs = POLL_INTERVAL_MS) {
  const [objects, setObjects] = useState([]);
  const [conjunctions, setConjunctions] = useState([]);
  const [generatedUtc, setGeneratedUtc] = useState(null);
  const [lastUpdatedMs, setLastUpdatedMs] = useState(null);
  const [error, setError] = useState(null);
  const timerRef = useRef(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;

    async function poll() {
      try {
        const snapshot = await apiFetch("/api/orbital_state");
        if (!aliveRef.current) return;
        setObjects(snapshot.objects || []);
        setConjunctions(snapshot.conjunctions || []);
        setGeneratedUtc(snapshot.generated_utc);
        setLastUpdatedMs(Date.now());
        setError(null);
      } catch (err) {
        if (aliveRef.current) setError(String(err.message || err));
      } finally {
        if (aliveRef.current) timerRef.current = setTimeout(poll, intervalMs);
      }
    }

    poll();
    return () => {
      aliveRef.current = false;
      clearTimeout(timerRef.current);
    };
  }, [intervalMs]);

  return { objects, conjunctions, generatedUtc, lastUpdatedMs, error };
}

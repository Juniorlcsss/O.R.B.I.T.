import { useEffect, useRef, useState } from "react";
import { apiHeaders } from "../lib/api.js";

// The gateway audits every 2 s telemetry poll, so the raw stream is mostly
// housekeeping. Keep a deep client buffer or real mission decisions age out
// of the ledger within a few minutes.
const MAX_EVENTS = 900;
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 10000];

export default function useLiveFeed() {
  const [events, setEvents] = useState([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(null);
  const attemptRef = useRef(0);
  const abortRef = useRef(null);
  const closedRef = useRef(false);

  useEffect(() => {
    closedRef.current = false;

    async function connect() {
      while (!closedRef.current) {
        const controller = new AbortController();
        abortRef.current = controller;
        try {
          const response = await fetch("/api/live_feed", {
            headers: apiHeaders(),
            signal: controller.signal,
          });
          if (!response.ok || !response.body) throw new Error(`live_feed ${response.status}`);
          setConnected(true);
          setError(null);
          attemptRef.current = 0;

          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          let streamError = null;

          while (!closedRef.current) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const frames = buffer.split("\n\n");
            buffer = frames.pop() || "";
            for (const frame of frames) {
              const dataLines = frame
                .split("\n")
                .filter((line) => line.startsWith("data:"))
                .map((line) => line.slice(5).trim());
              if (dataLines.length === 0) continue;
              try {
                const record = JSON.parse(dataLines.join("\n"));
                setEvents((prev) => {
                  if (prev.length && prev[prev.length - 1].seq >= record.seq) return prev;
                  const next = [...prev, record];
                  return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
                });
              } catch {
                /* skip malformed frame */
              }
            }
          }
          if (streamError) throw streamError;
          throw new Error("stream ended");
        } catch (err) {
          if (controller.signal.aborted || closedRef.current) return;
          setConnected(false);
          setError(String(err.message || err));
          const delay = RECONNECT_DELAYS_MS[Math.min(attemptRef.current, RECONNECT_DELAYS_MS.length - 1)];
          attemptRef.current += 1;
          await new Promise((resolve) => setTimeout(resolve, delay));
        }
      }
    }

    connect();
    return () => {
      closedRef.current = true;
      abortRef.current?.abort();
      setConnected(false);
    };
  }, []);

  return { events, connected, error };
}

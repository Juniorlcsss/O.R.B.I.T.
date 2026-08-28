const API_KEY = import.meta.env.VITE_ORBIT_API_KEY || "";

export function apiHeaders(extra = {}) {
  const headers = { "Content-Type": "application/json", ...extra };
  if (API_KEY) headers["X-API-KEY"] = API_KEY;
  return headers;
}

export const AUTH_FAILED_EVENT = "orbit:auth-failed";

export function reportAuthFailure(status) {
  if (status !== 401 && status !== 403) return;
  window.dispatchEvent(new CustomEvent(AUTH_FAILED_EVENT, { detail: { status } }));
}

export async function apiFetch(path, options = {}) {
  const response = await fetch(path, { ...options, headers: apiHeaders(options.headers) });
  if (!response.ok) {
    reportAuthFailure(response.status);
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body);
    } catch {
      /* keep status-line detail */
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

let audioCtx = null;

export function beep(frequency = 660, durationMs = 120, gain = 0.05) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    const osc = audioCtx.createOscillator();
    const amp = audioCtx.createGain();
    osc.type = "sine";
    osc.frequency.value = frequency;
    amp.gain.setValueAtTime(gain, audioCtx.currentTime);
    amp.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + durationMs / 1000);
    osc.connect(amp).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + durationMs / 1000);
  } catch {
    /* audio is a nicety; never break the feed over it */
  }
}

// --- Mission-control audio cues (Lyria-backed GET /api/audio/{event}) -------
// <audio src> cannot send X-API-KEY, so clips are fetched as blobs and kept
// as object URLs for the session; one download per cue type, ever.

const audioClipCache = new Map();

export async function playEventAudio(eventType) {
  try {
    let clipUrl = audioClipCache.get(eventType);
    if (!clipUrl) {
      const response = await fetch(`/api/audio/${encodeURIComponent(eventType)}`, { headers: apiHeaders() });
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      clipUrl = URL.createObjectURL(await response.blob());
      audioClipCache.set(eventType, clipUrl);
    }
    const element = new Audio(clipUrl);
    element.volume = 0.6;
    await element.play();
    return true;
  } catch {
    return false;
  }
}

// --- Autonomous Veo mission debriefs ----------------------------------------

export function fetchDebrief(conjunctionId) {
  return apiFetch(`/api/debrief/${encodeURIComponent(conjunctionId)}`);
}

/**
 * Run one self-evolution cycle
 * 
 * @returns {Promise<object>} The cycle result (status, before/after policy).
 */
export function postEvolution() {
  return apiFetch("/api/evolution/trigger", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

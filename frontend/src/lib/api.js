const API_KEY = import.meta.env.VITE_ORBIT_API_KEY || "";

export function apiHeaders(extra = {}) {
  const headers = { "Content-Type": "application/json", ...extra };
  if (API_KEY) headers["X-API-KEY"] = API_KEY;
  return headers;
}

export async function apiFetch(path, options = {}) {
  const response = await fetch(path, { ...options, headers: apiHeaders(options.headers) });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body);
    } catch {
      /* keep status-line detail */
    }
    throw new Error(detail);
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

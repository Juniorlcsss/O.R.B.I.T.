"""Project O.R.B.I.T. — autonomous Veo mission-debrief generator.

Architectural role
------------------
The fleet does not just solve conjunctions — it documents them. When a
mission terminates in ``EXECUTION_AUTHORIZED``, ``MANEUVER_BLOCKED`` or an
edge-autonomous dodge, the orchestrator queues this module as a background
task. It renders a cinematic text summary of the mission and turns it into
a short debrief video that is attached to the conjunction record in the
GEAP memory bank (Firestore collection ``conjunctions/{id}.debrief``).

Production API note (Vertex AI Veo)
-----------------------------------
In production the video is generated with **Veo on Vertex AI** through the
google-genai SDK::

    from google import genai

    client = genai.Client(vertex=True)          # Application Default Credentials
    operation = client.models.generate_videos(
        model="veo-3.0-generate-001",           # Veo 3, Vertex AI Model Garden
        prompt=cinematic_prompt,
        config=types.GenerateVideosConfig(number_of_videos=1),
    )
    while not operation.done:                   # long-running operation
        await asyncio.sleep(10)
        operation = client.operations.get(operation)
    gcs_uri = operation.result.generated_videos[0].video.uri   # gs://...mp4

The call is gated behind ``ORBIT_ENABLE_REAL_VEO=1`` plus valid Vertex
credentials. In every other environment — including the hackathon demo
laptop — a deterministic **simulated** mode produces a fully rendered
SVG reconstruction of the encounter so the feature stays functional,
auditable and honest about what it is. The report always states which
mode produced the artifact.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Final

from geap_sim.memory_bank import get_shared_memory_bank
from geap_sim.observability import audit_logger

#: Production model identifier (Veo 3 on Vertex AI).
VEO_MODEL_ID: Final[str] = os.environ.get("ORBIT_VEO_MODEL_ID", "veo-3.0-generate-001")

#: Master switch for the real Vertex AI call. Off by default so local runs
#: never attempt paid API traffic implicitly.
_REAL_VEO_ENABLED: Final[bool] = os.environ.get("ORBIT_ENABLE_REAL_VEO", "").strip() == "1"

_DEBRIEF_TEMPLATE: Final[str] = (
    "{sat_id} versus {debris_id}: risk band {risk_band}, Pc {pc:.2e}, "
    "miss distance {miss} at TCA {tca_iso}. Outcome: {outcome}."
)


def _format_miss_distance(miss_km: float) -> str:
    """Render the headline number the way an operator says it aloud.

    A close approach is quoted in metres, not as "0.1 km" - the whole point
    of the debrief is that the encounter was 89 m, and one decimal place of
    kilometres throws that precision away.
    """
    if miss_km < 1.0:
        return f"{miss_km * 1000.0:.0f} m"
    return f"{miss_km:.1f} km"


def build_mission_summary(record: dict[str, Any]) -> str:
    """One factual sentence describing the resolved encounter."""
    outcome_map = {
        "EXECUTION_AUTHORIZED": "ground fleet authorised and uplinked an avoidance burn",
        "MANEUVER_BLOCKED_BY_ARMOR": "Model Armor blocked the manoeuvre; human operators alerted",
        "EDGE_AUTONOMOUS_DODGE_EXECUTED": f"onboard autopilot executed an autonomous "
        f"{float(record.get('our_dv_mps') or 0.0):.1f} m/s dodge without ground contact",
    }
    outcome = outcome_map.get(str(record.get("final_status", "")), str(record.get("action_taken", "logged")))
    return _DEBRIEF_TEMPLATE.format(
        sat_id=str(record.get("sat_id", "?")).upper(),
        debris_id=str(record.get("debris_id", "?")).upper(),
        risk_band=str(record.get("risk_band", "?")),
        pc=float(record.get("pc") or 0.0),
        miss=_format_miss_distance(float(record.get("miss_distance_km") or 0.0)),
        tca_iso=str(record.get("tca_iso", "?")),
        outcome=outcome,
    )


def build_veo_prompt(summary: str) -> str:
    """The exact creative brief handed to Veo (or shown beside the simulation)."""
    return (
        "Cinematic view of a small satellite dodging space debris, "
        f"mission control screens in background, text overlay: {summary}"
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Simulated mode — deterministic SVG reconstruction of the encounter
# ---------------------------------------------------------------------------


def _render_reconstruction_svg(record: dict[str, Any], summary: str) -> str:
    """Deterministic cinematic storyboard frame for the encounter.

    Same inputs always render the same frame (seeded by the conjunction ID),
    which keeps demos reproducible while still looking mission-specific.
    """
    seed = int(hashlib.sha256(str(record.get("trace_id", record.get("sat_id", "orbit"))).encode()).hexdigest()[:8], 16)
    sat_x, sat_y = 120 + (seed % 80), 210 - (seed % 40)
    deb_x, deb_y = 460 + (seed % 60), 130 + (seed % 50)
    outcome = str(record.get("final_status", "")).replace("_", " ")
    blocked = "BLOCKED" in outcome or "HELD" in outcome
    trail_color = "#ef4444" if blocked else "#38bdf8"

    def star(n: int) -> str:
        bits = []
        for i in range(n):
            sx, sy = (seed * 7919 + i * 104729) % 620 + 10, (seed * 15485863 + i * 7919) % 320 + 12
            bits.append(f'<circle cx="{sx}" cy="{sy}" r="{1 + (seed + i) % 2}" fill="#94a3b8" opacity="0.{5 + (i % 4)}"/>')
        return "".join(bits)

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
  <defs>
    <radialGradient id="earth" cx="30%" cy="75%" r="70%">
      <stop offset="0%" stop-color="#1d4ed8"/><stop offset="55%" stop-color="#0b1e4b"/><stop offset="100%" stop-color="#020617"/>
    </radialGradient>
    <linearGradient id="trail" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{trail_color}" stop-opacity="0"/><stop offset="100%" stop-color="{trail_color}" stop-opacity="0.9"/>
    </linearGradient>
  </defs>
  <rect width="640" height="360" fill="#020617"/>
  <circle cx="-60" cy="430" r="330" fill="url(#earth)"/>
  {star(26)}
  <ellipse cx="320" cy="200" rx="290" ry="110" fill="none" stroke="#1e293b" stroke-width="1" stroke-dasharray="3 7"/>
  <line x1="{sat_x - 90}" y1="{sat_y + 14}" x2="{sat_x}" y2="{sat_y}" stroke="url(#trail)" stroke-width="2"/>
  <rect x="{sat_x - 9}" y="{sat_y - 6}" width="18" height="12" rx="2" fill="#e2e8f0"/>
  <rect x="{sat_x - 22}" y="{sat_y - 3}" width="11" height="6" fill="#38bdf8"/>
  <rect x="{sat_x + 11}" y="{sat_y - 3}" width="11" height="6" fill="#38bdf8"/>
  <circle cx="{deb_x}" cy="{deb_y}" r="5" fill="#ef4444"/>
  <circle cx="{deb_x}" cy="{deb_y}" r="10" fill="none" stroke="#ef4444" stroke-opacity="0.35" stroke-dasharray="2 4"/>
  <line x1="{sat_x}" y1="{sat_y}" x2="{deb_x}" y2="{deb_y}" stroke="#f59e0b" stroke-width="1" stroke-opacity="0.5" stroke-dasharray="5 5"/>
  <text x="24" y="40" font-family="ui-monospace,monospace" font-size="15" letter-spacing="4" fill="#e2e8f0">MISSION DEBRIEF</text>
  <text x="24" y="60" font-family="ui-monospace,monospace" font-size="10" letter-spacing="2" fill="#64748b">
    SIMULATED RECONSTRUCTION — VEO UNAVAILABLE IN THIS ENVIRONMENT</text>
  <text x="24" y="308" font-family="ui-monospace,monospace" font-size="11" fill="#94a3b8">{str(record.get('sat_id', '')).upper()} × {str(record.get('debris_id', '')).upper()}</text>
  <text x="24" y="326" font-family="ui-monospace,monospace" font-size="11" fill="#94a3b8">{outcome}</text>
  <text x="24" y="344" font-family="ui-monospace,monospace" font-size="10" fill="#475569">{summary[:96]}</text>
</svg>"""
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# Real Veo call (production path, opt-in)
# ---------------------------------------------------------------------------


async def generate_debrief_video(summary: str) -> dict[str, Any]:
    """Produce the debrief artifact: real Veo when enabled, simulation otherwise.

    Returns:
        ``{"mode": "veo"|"simulated", "video_url": str|None,
           "poster_svg": str|None, "model": str|None}``
    """
    if _REAL_VEO_ENABLED:
        try:
            return await _veo_generate(summary)
        except Exception as exc:  # noqa: BLE001 — degrade to simulated, loudly
            audit_logger.log_event(
                trace_id="debrief",
                agent_name="orbit.debrief",
                event_type="DEBRIEF_VEO_CALL_FAILED",
                payload={"error_type": type(exc).__name__, "error": str(exc)[:300]},
                status="DEGRADED",
            )
    return {"mode": "simulated", "video_url": None, "poster_svg": None, "model": None}


async def _veo_generate(prompt: str) -> dict[str, Any]:
    """Blocking-shielded call against Vertex AI Veo 3 (see module docstring)."""
    from google.genai import types as genai_types
    from google import genai

    client = genai.Client(vertex=True)

    def _call() -> str:
        operation = client.models.generate_videos(
            model=VEO_MODEL_ID,
            prompt=prompt,
            config=genai_types.GenerateVideosConfig(number_of_videos=1),
        )
        while not operation.done:
            raise TimeoutError("Veo operation still running after polling budget")
        video = operation.response.generated_videos[0].video
        return getattr(video, "uri", None) or getattr(video, "gcs_uri", "")

    # The SDK exposes only synchronous polling; keep it off the event loop.
    uri = await asyncio.to_thread(_call)
    if not uri:
        raise RuntimeError("Veo returned no video URI")
    return {"mode": "veo", "video_url": uri, "poster_svg": None, "model": VEO_MODEL_ID}


# ---------------------------------------------------------------------------
# Orchestration: queue → generate → persist → audit
# ---------------------------------------------------------------------------


async def generate_and_store_debrief(conjunction_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Background task body: attach a debrief to one conjunction record.

    Never raises — failures are recorded on the document itself and in the
    audit trail so the dashboard can show an honest FAILED state.
    """
    trace_id = str(record.get("trace_id") or "debrief")
    memory = get_shared_memory_bank()
    summary = build_mission_summary(record)
    prompt = build_veo_prompt(summary)

    await memory.append_conjunction_fields(
        conjunction_id,
        {
            "debrief": {
                "status": "PENDING",
                "mode": None,
                "video_url": None,
                "poster_svg": None,
                "prompt": prompt,
                "summary": summary,
                "generated_utc": _utc_now_iso(),
                "error": None,
            }
        },
    )
    audit_logger.log_event(
        trace_id=trace_id,
        agent_name="orbit.debrief",
        event_type="DEBRIEF_QUEUED",
        payload={"conjunction_id": conjunction_id, "engine": "veo-3.0-generate-001"},
        status="QUEUED",
    )

    try:
        artifact = await generate_debrief_video(summary)
        if artifact["mode"] == "simulated":
            artifact["poster_svg"] = _render_reconstruction_svg(record, summary)
        debrief = {
            "status": "READY",
            "mode": artifact["mode"],
            "video_url": artifact["video_url"],
            "poster_svg": artifact["poster_svg"],
            "prompt": prompt,
            "summary": summary,
            "model": artifact["model"],
            "generated_utc": _utc_now_iso(),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — persisted, not raised
        debrief = {
            "status": "FAILED",
            "mode": None,
            "video_url": None,
            "poster_svg": None,
            "prompt": prompt,
            "summary": summary,
            "model": VEO_MODEL_ID,
            "generated_utc": _utc_now_iso(),
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }

    await memory.append_conjunction_fields(conjunction_id, {"debrief": debrief})
    audit_logger.log_event(
        trace_id=trace_id,
        agent_name="orbit.debrief",
        event_type="DEBRIEF_READY" if debrief["status"] == "READY" else "DEBRIEF_FAILED",
        payload={
            "conjunction_id": conjunction_id,
            "mode": debrief["mode"],
            "has_video": bool(debrief["video_url"]),
            "tag": "VEO_DEBRIEF",
        },
        status="OK" if debrief["status"] == "READY" else "FAILED",
    )
    return debrief


async def get_debrief(conjunction_id: str) -> dict[str, Any] | None:
    """Endpoint helper: fetch the debrief block for one conjunction."""
    doc = await get_shared_memory_bank().get_conjunction_event(conjunction_id)
    if doc is None:
        return None
    debrief = dict(doc.get("debrief") or {})
    return {
        "conjunction_id": doc.get("conjunction_id", conjunction_id),
        "sat_id": doc.get("sat_id"),
        "debris_id": doc.get("debris_id"),
        "final_status": doc.get("final_status"),
        "debrief_status": debrief.get("status", "NOT_QUEUED"),
        **debrief,
    }


__all__ = [
    "VEO_MODEL_ID",
    "build_mission_summary",
    "build_veo_prompt",
    "generate_and_store_debrief",
    "generate_debrief_video",
    "get_debrief",
]

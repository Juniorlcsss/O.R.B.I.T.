#!/usr/bin/env python3
"""Verify the Space-Track integration against the live API.

Run this once after putting real credentials in ``.env``. It answers the
questions that documentation alone cannot:

* Do the credentials authenticate?
* Does the GP class return propagable element sets for our catalogue?
* Which column names does ``cdm_public`` actually use for probability,
  miss distance and relative speed? (The tolerant lookup in
  ``tools.space_track_api._first`` covers both known vocabularies; this
  prints which one the live account serves so the mapping can be pinned.)

It is deliberately frugal: at most a handful of requests, well inside
Space-Track's published ceilings. It never prints the password.

    python scripts/spacetrack_probe.py
    python scripts/spacetrack_probe.py --norad 25544
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
except ImportError:  # pragma: no cover - dotenv is a declared dependency
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--norad",
        type=int,
        default=25544,
        help="Catalogue number to probe (default 25544, the ISS: always on orbit and always screened).",
    )
    parser.add_argument("--limit", type=int, default=5, help="Maximum CDM rows to request.")
    args = parser.parse_args()

    # Imported after load_dotenv: the module snapshots credentials at import.
    from tools import space_track_api as api

    print("=" * 68)
    print("Space-Track live probe")
    print("=" * 68)

    if not api.credentials_configured():
        print("\nFAIL: credentials not configured.")
        print("Set these in .env, then re-run:")
        print("    SPACETRACK_USERNAME=<your space-track.org login email>")
        print("    SPACETRACK_PASSWORD=<your space-track.org password>")
        return 2

    print(f"  username : {api.SPACETRACK_USERNAME[:3]}*** (password not shown)")
    print(f"  base url : {api.BASE_URL}")
    print(f"  budget   : <={api.MAX_REQUESTS_PER_MINUTE}/min, <={api.MAX_REQUESTS_PER_HOUR}/hr, "
          f"{api.MIN_INTERVAL_SECONDS}s spacing")

    client = api.SpaceTrackClient()

    # -- 1. login ------------------------------------------------------------
    print("\n[1/3] authenticating ...")
    try:
        client._ensure_logged_in()  # noqa: SLF001 - probe intentionally checks transport
    except api.SpaceTrackUnavailable as exc:
        print(f"  FAIL: {exc}")
        return 1
    print("  OK: session established")

    # -- 2. GP elsets --------------------------------------------------------
    print(f"\n[2/3] GP class, NORAD {args.norad} ...")
    try:
        rows = client.fetch_tle("PROBE", norad_cat_id=args.norad)
    except api.SpaceTrackUnavailable as exc:
        print(f"  FAIL: {exc}")
        return 1
    if not rows:
        print("  FAIL: no element sets returned (decayed object, or wrong catalogue number?)")
        return 1
    row = rows[0]
    print(f"  OK: {len(rows)} elset(s)")
    print(f"    object_name : {row.get('object_name')}")
    print(f"    epoch_utc   : {row.get('epoch_utc')}")
    print(f"    tle_line1   : {str(row.get('tle_line1'))[:69]}")
    print(f"    tle_line2   : {str(row.get('tle_line2'))[:69]}")

    # -- 3. cdm_public -------------------------------------------------------
    print(f"\n[3/3] cdm_public class, NORAD {args.norad} ...")
    try:
        raw = client._throttled_get(  # noqa: SLF001 - we want the unmapped row
            f"/basicspacedata/query/class/cdm_public/SAT_1_ID/{args.norad}"
            f"/orderby/TCA%20desc/limit/{args.limit}/format/json"
        )
    except api.SpaceTrackUnavailable as exc:
        print(f"  FAIL: {exc}")
        return 1

    if not raw:
        print("  0 rows for this object.")
        print("  Sampling the class unfiltered to tell 'genuinely empty' apart")
        print("  from 'wrong predicate name' ...")
        try:
            sample = client._throttled_get(  # noqa: SLF001
                "/basicspacedata/query/class/cdm_public"
                f"/orderby/TCA%20desc/limit/{args.limit}/format/json"
            )
        except api.SpaceTrackUnavailable as exc:
            print(f"  FAIL: {exc}")
            return 1
        if not sample:
            print("  Class itself is empty for this account — no CDM access.")
            return 0
        print(f"  Class returns data ({len(sample)} row(s)). Column names as served:")
        for key in sorted(sample[0].keys()):
            print(f"    {key} = {str(sample[0][key])[:48]}")
        id_cols = [k for k in sample[0] if "SAT" in k.upper() and "ID" in k.upper()]
        print("")
        print(f"  Candidate object-id columns: {id_cols or 'NONE FOUND'}")
        print("  If SAT_1_ID is absent above, that is why the filtered query")
        print("  returned nothing; repoint fetch_cdms at the real column name.")
        return 0
    else:
        print(f"  OK: {len(raw)} raw row(s). Column names as served:")
        for key in sorted(raw[0].keys()):
            print(f"    {key} = {str(raw[0][key])[:48]}")
        print("\n  Normalised through fetch_cdms mapping:")
        mapped = client.fetch_cdms(args.norad, limit=args.limit)
        for item in mapped[:3]:
            print("    " + json.dumps(item, default=str))
        unmapped = [
            field
            for field in ("pc", "miss_distance_km", "relative_velocity_km_s", "tca_iso")
            if mapped and mapped[0].get(field) is None
        ]
        if unmapped:
            print(f"\n  WARNING: these normalised fields came back None: {unmapped}")
            print("  Cross-check against the column list above and extend the")
            print("  candidate keys in tools/space_track_api.py::fetch_cdms.")

    print("\n" + "=" * 68)
    print("Probe complete.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

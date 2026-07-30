#!/usr/bin/env python3
"""
Daily build job: fetch MoE thresholds from the Wargaming API and emit a single
static JSON file for the WoT mod to consume.

Reads WG_APPLICATION_ID from the environment. Never hardcode the key — this
script is meant to run in CI (GitHub Actions secret, etc.) and the key must not
ship inside the mod.

Usage:
    export WG_APPLICATION_ID=...
    python3 build_thresholds.py --out thresholds.json

Output shape:
{
  "generated_at": 1785196800,
  "realms": {
    "na": {
      "updated_at": 1785196800,
      "thresholds": {"1": {"65": 853, "85": 1267, "95": 1613}, ...}
    },
    "eu": {...}
  },
  "vehicles": {"1": {"name": "T-34", "short_name": "T-34", "nation": "ussr",
                     "type": "mediumTank", "tier": 5, "is_premium": 0}, ...}
}
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

# Realm code -> API host suffix. NA is 'com'.
REALMS = {"na": "com", "eu": "eu"}

# 65 = 1st mark, 85 = 2nd, 95 = 3rd. Max 10 percentiles per request.
PERCENTILES = "65,85,95"

API_TIMEOUT = 30
RETRIES = 3


def _call(host, path, params):
    params = dict(params)
    params["application_id"] = APP_ID
    url = f"https://api.worldoftanks.{host}/wot/{path}/?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=API_TIMEOUT) as r:
                payload = json.load(r)
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(2 ** attempt)
            continue
        if payload.get("status") != "ok":
            raise RuntimeError(f"{path} error: {payload.get('error')}")
        return payload
    raise RuntimeError(f"{path} failed after {RETRIES} attempts: {last}")


def fetch_thresholds(host):
    """All vehicles in ONE request — omitting tank_id returns the full set."""
    d = _call(host, "tanks/mastery",
              {"distribution": "damage", "percentile": PERCENTILES})["data"]
    return d["distribution"], d["updated_at"]


def fetch_vehicles(host, language="en"):
    """Tank metadata from the encyclopedia. Paginated; limit 100."""
    out, page = {}, 1
    fields = "tank_id,name,short_name,nation,type,tier,is_premium"
    while True:
        d = _call(host, "encyclopedia/vehicles",
                  {"fields": fields, "language": language,
                   "limit": 100, "page_no": page})["data"]
        if not d:
            break
        for tid, v in d.items():
            out[tid] = {
                "name": v.get("name"),
                "short_name": v.get("short_name"),
                "nation": v.get("nation"),
                "type": v.get("type"),
                "tier": v.get("tier"),
                "is_premium": int(bool(v.get("is_premium"))),
            }
        if len(d) < 100:
            break
        page += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="thresholds.json")
    ap.add_argument("--indent", type=int, default=None,
                    help="pretty-print (default: compact, smaller payload)")
    args = ap.parse_args()

    result = {"generated_at": int(time.time()), "realms": {}}

    for realm, host in REALMS.items():
        dist, updated = fetch_thresholds(host)
        result["realms"][realm] = {"updated_at": updated, "thresholds": dist}
        print(f"{realm}: {len(dist)} vehicles, updated_at={updated}", file=sys.stderr)

    # Metadata is realm-independent for our purposes; pull once.
    result["vehicles"] = fetch_vehicles(REALMS["na"])
    print(f"metadata: {len(result['vehicles'])} vehicles", file=sys.stderr)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"),
                  indent=args.indent)

    size = os.path.getsize(args.out)
    print(f"wrote {args.out} ({size/1024:.1f} KB)", file=sys.stderr)


if __name__ == "__main__":
    APP_ID = os.environ.get("WG_APPLICATION_ID")
    if not APP_ID:
        sys.exit("WG_APPLICATION_ID not set")
    main()

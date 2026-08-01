#!/usr/bin/env python3
"""
ONE-TIME backfill of MoE history from poliroid.me.

Your own history starts the day the pipeline went live and takes 30 days to
fill. poliroid retains ~35 days now. This merges their history into your
history_{realm}.json files so the in-game graph is useful immediately.

RUN THIS ONCE. Do not schedule it, do not re-run it "just in case."

poliroid is an unauthenticated fan site hosted at someone else's expense, and
they only expose history one vehicle at a time — a full backfill is ~1,500
requests. This script is deliberately slow (default 1 req/sec, ~13 min per
realm), sequential, and identifies itself in the User-Agent. Leave it that way.

Data provenance: poliroid mirrors wot/tanks/mastery exactly (verified 21/21 on
current values across 7 tanks x 3 percentiles). Their date labels use fetch
date, the same convention as this pipeline, so entries align directly.

Existing entries sourced from WG are never overwritten — this only fills gaps.

Usage:
    python3 backfill_history.py --history-dir public/history
    python3 backfill_history.py --history-dir public/history --dry-run
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from build_thresholds import decode_series, encode_series, PERCENTILES

POLIROID = "https://poliroid.me/gunmarks/api/v2"
REALM_MAP = {"na": "com", "eu": "eu"}          # our code -> poliroid code
UA = "wot-moe-thresholds backfill (one-time; github.com/RavenStryker/wot-moe-thresholds)"

TIMEOUT = 30
RETRIES = 3


def _get(url):
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                payload = json.load(r)
            if payload.get("status") != "ok":
                raise RuntimeError(f"status={payload.get('status')}")
            return payload["data"]
        except Exception as e:                          # noqa: BLE001
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"failed after {RETRIES} attempts: {url} ({last})")


def poliroid_tank_ids(prealm):
    """Vehicles poliroid actually tracks (fewer than WG's set)."""
    d = _get(f"{POLIROID}/data/{prealm}/vehicles/{PERCENTILES}")
    return [str(v["id"]) for v in d["data"]]


def poliroid_history(prealm, tank_id):
    d = _get(f"{POLIROID}/data/{prealm}/vehicle/{tank_id}/{PERCENTILES}")
    return {r["date"]: r["marks"] for r in d["data"]}


def backfill(realm, history_dir, delay, keep_days, dry_run):
    prealm = REALM_MAP[realm]
    path = os.path.join(history_dir, f"history_{realm}.json")

    try:
        with open(path, encoding="utf-8") as f:
            hist = json.load(f)
        dates = list(hist["dates"])
        stamps = list(hist.get("updated_at", [None] * len(dates)))
        sources = list(hist.get("sources", ["wg"] * len(dates)))
        series = {t: {p: decode_series(a) for p, a in m.items()}
                  for t, m in hist["thresholds"].items()}
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        print(f"  no existing {path} — starting fresh", file=sys.stderr)
        dates, stamps, sources, series = [], [], [], {}

    print(f"  existing: {len(dates)} day(s), {len(series)} vehicles", file=sys.stderr)

    ids = poliroid_tank_ids(prealm)
    print(f"  poliroid tracks {len(ids)} vehicles on '{prealm}'", file=sys.stderr)
    est = len(ids) * delay / 60
    print(f"  estimated time: {est:.0f} min at {delay}s/request", file=sys.stderr)
    if dry_run:
        print("  --dry-run: stopping before fetching histories", file=sys.stderr)
        return

    fetched = {}                                   # date -> {tid -> marks}
    t0 = time.time()
    for i, tid in enumerate(ids, 1):
        try:
            for date, marks in poliroid_history(prealm, tid).items():
                fetched.setdefault(date, {})[tid] = marks
        except RuntimeError as e:
            print(f"    skip {tid}: {e}", file=sys.stderr)
        if i % 50 == 0 or i == len(ids):
            el = time.time() - t0
            print(f"    {i}/{len(ids)}  ({el/60:.1f} min elapsed)", file=sys.stderr)
        time.sleep(delay)

    # Merge: existing WG entries win; poliroid only fills dates we don't have.
    added = sorted(d for d in fetched if d not in dates)
    if not added:
        print("  nothing to add — history already covers these dates",
              file=sys.stderr)
        return

    all_dates = sorted(set(dates) | set(added))
    all_dates = all_dates[-keep_days:]

    old_index = {d: i for i, d in enumerate(dates)}
    new_stamps, new_sources = [], []
    for d in all_dates:
        if d in old_index:
            new_stamps.append(stamps[old_index[d]])
            new_sources.append(sources[old_index[d]])
        else:
            new_stamps.append(None)
            new_sources.append("poliroid")

    tanks = set(series) | {t for m in fetched.values() for t in m}
    merged = {}
    for tid in tanks:
        rec = {}
        for p in PERCENTILES.split(","):
            col = []
            for d in all_dates:
                if d in old_index:
                    old = series.get(tid, {}).get(p, [])
                    i = old_index[d]
                    col.append(old[i] if i < len(old) else None)
                else:
                    col.append(fetched.get(d, {}).get(tid, {}).get(p))
            rec[p] = col
        if any(v is not None for a in rec.values() for v in a):
            merged[tid] = rec

    out = {
        "encoding": "delta",
        "generated_at": int(time.time()),
        "dates": all_dates,
        "updated_at": new_stamps,
        "sources": new_sources,
        "thresholds": {t: {p: encode_series(a) for p, a in m.items()}
                       for t, m in merged.items()},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    kb = os.path.getsize(path) / 1024
    print(f"  merged: {len(all_dates)} day(s) ({all_dates[0]} .. {all_dates[-1]}), "
          f"{len(merged)} vehicles, {kb:.1f} KB", file=sys.stderr)
    print(f"  backfilled {len(added)} date(s) from poliroid", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history-dir", required=True)
    ap.add_argument("--realms", default="na,eu")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests (default 1.0 — do not lower)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be fetched, then stop")
    args = ap.parse_args()

    if args.delay < 0.5:
        sys.exit("Refusing to run with --delay below 0.5s. This hits an "
                 "unauthenticated third-party site ~1500 times; be a good guest.")

    for realm in args.realms.split(","):
        realm = realm.strip()
        if realm not in REALM_MAP:
            sys.exit(f"unknown realm '{realm}' (expected na and/or eu)")
        print(f"\n=== {realm} ===", file=sys.stderr)
        backfill(realm, args.history_dir, args.delay, args.days, args.dry_run)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
ONE-TIME fill of missing percentile series on dates already in the history.

backfill_history.py fills missing DATES. This fills missing PERCENTILES on
dates that already exist — which is what you need after adding a value to
PERCENTILES in build_thresholds.py (e.g. adding "100"), since the daily job
only populates the new series going forward.

RUN THIS ONCE per percentile change. Do not schedule it.

Like backfill_history.py, this hits poliroid.me — an unauthenticated fan site
that only exposes history one vehicle at a time. Deliberately 1 req/sec and
sequential. Leave that alone.

Only NULL slots are written. Any value already present is left untouched, so
this can never overwrite WG-sourced data.

The `sources` array is per-DATE and is not modified. After this runs, a date
labelled "wg" may hold poliroid-sourced values for the newly filled percentile.
That is recorded in the run summary and should be noted in the project docs.

Usage:
    python3 backfill_percentiles.py --history-dir public/history --dry-run
    python3 backfill_percentiles.py --history-dir public/history
    python3 backfill_percentiles.py --history-dir public/history --percentiles 100
"""

import argparse
import json
import os
import sys
import time
import urllib.request

from build_thresholds import decode_series, encode_series, PERCENTILES

POLIROID = "https://poliroid.me/gunmarks/api/v2"
REALM_MAP = {"na": "com", "eu": "eu"}
UA = ("wot-moe-thresholds percentile-fill (one-time; "
      "github.com/RavenStryker/wot-moe-thresholds)")

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


def poliroid_tank_ids(prealm, pcts):
    d = _get(f"{POLIROID}/data/{prealm}/vehicles/{pcts}")
    return {str(v["id"]) for v in d["data"]}


def poliroid_history(prealm, tank_id, pcts):
    d = _get(f"{POLIROID}/data/{prealm}/vehicle/{tank_id}/{pcts}")
    return {r["date"]: r["marks"] for r in d["data"]}


def load(path):
    with open(path, encoding="utf-8") as f:
        hist = json.load(f)
    series = {t: {p: decode_series(a) for p, a in m.items()}
              for t, m in hist["thresholds"].items()}
    return hist, series


def find_gaps(hist, series, targets):
    """-> {tank_id: [percentiles with at least one null]}, plus a gap count."""
    n = len(hist["dates"])
    need, holes = {}, 0
    for tid, rec in series.items():
        missing = []
        for p in targets:
            arr = rec.get(p)
            if arr is None:
                missing.append(p)
                holes += n
            elif any(v is None for v in arr):
                missing.append(p)
                holes += sum(1 for v in arr if v is None)
        if missing:
            need[tid] = missing
    return need, holes


def fill_realm(realm, history_dir, targets, delay, dry_run):
    prealm = REALM_MAP[realm]
    path = os.path.join(history_dir, f"history_{realm}.json")
    pcts = ",".join(targets)

    try:
        hist, series = load(path)
    except FileNotFoundError:
        print(f"  {path} not found — skipping", file=sys.stderr)
        return
    except (KeyError, json.JSONDecodeError) as e:
        print(f"  {path} unreadable ({e}) — skipping", file=sys.stderr)
        return

    dates = hist["dates"]
    n = len(dates)
    print(f"  history: {n} day(s) [{dates[0]} .. {dates[-1]}], "
          f"{len(series)} vehicles", file=sys.stderr)

    need, holes = find_gaps(hist, series, targets)
    if not need:
        print(f"  no gaps for {targets} — nothing to do", file=sys.stderr)
        return
    print(f"  {holes} empty slot(s) across {len(need)} vehicle(s)",
          file=sys.stderr)

    tracked = poliroid_tank_ids(prealm, pcts)
    todo = sorted(need.keys() & tracked, key=int)
    skipped = len(need) - len(todo)
    print(f"  poliroid tracks {len(tracked)}; {len(todo)} of our gapped "
          f"vehicles are covered ({skipped} are not and will stay null)",
          file=sys.stderr)
    print(f"  estimated time: {len(todo) * delay / 60:.0f} min at "
          f"{delay}s/request", file=sys.stderr)

    if dry_run:
        print("  --dry-run: stopping before fetching", file=sys.stderr)
        return

    date_index = {d: i for i, d in enumerate(dates)}
    filled = 0
    t0 = time.time()

    for i, tid in enumerate(todo, 1):
        try:
            remote = poliroid_history(prealm, tid, pcts)
        except RuntimeError as e:
            print(f"    skip {tid}: {e}", file=sys.stderr)
            time.sleep(delay)
            continue

        rec = series[tid]
        for p in need[tid]:
            arr = rec.setdefault(p, [None] * n)
            if len(arr) < n:                       # defensive: pad short series
                arr.extend([None] * (n - len(arr)))
            for date, marks in remote.items():
                j = date_index.get(date)
                if j is None:
                    continue                       # date outside our window
                if arr[j] is not None:
                    continue                       # NEVER overwrite existing
                v = marks.get(p)
                if v is not None:
                    arr[j] = v
                    filled += 1

        if i % 50 == 0 or i == len(todo):
            print(f"    {i}/{len(todo)}  ({(time.time()-t0)/60:.1f} min, "
                  f"{filled} slot(s) filled)", file=sys.stderr)
        time.sleep(delay)

    if not filled:
        print("  nothing filled — file left untouched", file=sys.stderr)
        return

    # Validate before writing: every series must still align to `dates`.
    bad = [(t, p) for t, rec in series.items() for p, a in rec.items()
           if len(a) != n]
    if bad:
        print(f"  ABORT: {len(bad)} series misaligned, e.g. {bad[:3]} — "
              f"file NOT written", file=sys.stderr)
        sys.exit(1)

    hist["generated_at"] = int(time.time())
    hist["thresholds"] = {t: {p: encode_series(a) for p, a in rec.items()}
                          for t, rec in series.items()}

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)

    kb = os.path.getsize(path) / 1024
    print(f"  filled {filled} slot(s); wrote {path} ({kb:.1f} KB)",
          file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history-dir", required=True)
    ap.add_argument("--realms", default="na,eu")
    ap.add_argument("--percentiles", default=None,
                    help="comma list to fill; default = all of PERCENTILES "
                         f"({PERCENTILES})")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests (default 1.0 — do not lower)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.delay < 0.5:
        sys.exit("Refusing to run below 0.5s/request. This hits an "
                 "unauthenticated third-party site; be a good guest.")

    targets = (args.percentiles or PERCENTILES).split(",")
    targets = [t.strip() for t in targets if t.strip()]
    print(f"Filling percentile(s): {targets}", file=sys.stderr)

    for realm in args.realms.split(","):
        realm = realm.strip()
        if realm not in REALM_MAP:
            sys.exit(f"unknown realm '{realm}'")
        print(f"\n=== {realm} ===", file=sys.stderr)
        fill_realm(realm, args.history_dir, targets, args.delay, args.dry_run)

    print("\nNote: `sources` is per-date and was not modified. Dates labelled "
          "'wg' may now hold poliroid-sourced values for the filled "
          "percentile(s).", file=sys.stderr)


if __name__ == "__main__":
    main()

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
import datetime
import re
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


def encode_series(vals):
    """Delta-encode. First value absolute; subsequent values are diffs.

    None marks a missing day (a failed run, or a tank that didn't exist yet).
    After a None the chain restarts, so the next present value is absolute
    again. Decoder must mirror this.
    """
    out, prev = [], None
    for v in vals:
        if v is None:
            out.append(None)
            prev = None
        elif prev is None:
            out.append(v)
            prev = v
        else:
            out.append(v - prev)
            prev = v
    return out


def decode_series(arr):
    """Inverse of encode_series. Mirror this in the mod."""
    out, prev = [], None
    for x in arr:
        if x is None:
            out.append(None)
            prev = None
        elif prev is None:
            out.append(x)
            prev = x
        else:
            prev += x
            out.append(prev)
    return out


def update_history(path, date, updated_at, distribution, keep_days):
    """Append a snapshot to a rolling per-realm history file.

    Entries are keyed by FETCH DATE (the UTC date this job ran), not by WG's
    `updated_at`.

    Why: `updated_at` does not track when the values change. It sits ~2 days
    behind and stays frozen for long stretches while the numbers themselves
    move daily — verified by comparing this endpoint against poliroid.me, whose
    newest daily entry matches WG's current values exactly (21/21 across 7
    tanks x 3 percentiles) while its `updated_at`-dated entry does not. The
    field appears to report the battle-data cutoff, not the computation time.
    Keying by it would collapse every run into one perpetually-overwritten row.

    `updated_at` is still recorded per entry, parallel to `dates`, so the mod
    can show the underlying data cutoff if useful.

    Re-running on the same UTC date replaces that entry rather than appending.
    """
    try:
        with open(path, encoding="utf-8") as f:
            hist = json.load(f)
        dates = hist["dates"]
        stamps = hist.get("updated_at", [None] * len(dates))
        # Provenance, if backfill_history.py has been run. "wg" for entries this
        # script wrote, "poliroid" for backfilled ones. Preserved across runs.
        sources = hist.get("sources", ["wg"] * len(dates))
        series = {t: {p: decode_series(a) for p, a in m.items()}
                  for t, m in hist["thresholds"].items()}
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        dates, stamps, sources, series = [], [], [], {}

    if dates and dates[-1] == date:
        # Same UTC day: drop the previous entry, re-append below.
        dates.pop()
        stamps.pop()
        sources.pop()
        for m in series.values():
            for a in m.values():
                if a:
                    a.pop()

    dates.append(date)
    stamps.append(updated_at)
    sources.append("wg")
    n = len(dates)

    for tid, marks in distribution.items():
        rec = series.setdefault(tid, {})
        for p in PERCENTILES.split(","):
            arr = rec.setdefault(p, [])
            arr.extend([None] * (n - 1 - len(arr)))   # backfill new tanks
            arr.append(marks.get(p))

    # Pad every series to full length (covers tanks absent from this run).
    for rec in series.values():
        for p in PERCENTILES.split(","):
            arr = rec.setdefault(p, [])
            arr.extend([None] * (n - len(arr)))

    # Trim to the rolling window, by date span rather than entry count so a
    # missed run doesn't silently extend how far back the window reaches.
    newest = datetime.datetime.strptime(dates[-1], "%Y-%m-%d")
    cutoff = newest - datetime.timedelta(days=keep_days - 1)
    keep = [i for i, d in enumerate(dates)
            if datetime.datetime.strptime(d, "%Y-%m-%d") >= cutoff]
    if len(keep) != n:
        dates = [dates[i] for i in keep]
        stamps = [stamps[i] for i in keep]
        sources = [sources[i] for i in keep]
        for rec in series.values():
            for p in rec:
                rec[p] = [rec[p][i] for i in keep]

    # Drop tanks with no data anywhere in the window.
    series = {t: m for t, m in series.items()
              if any(v is not None for a in m.values() for v in a)}

    out = {
        "encoding": "delta",
        "generated_at": int(time.time()),
        "dates": dates,
        "updated_at": stamps,
        "sources": sources,
        "thresholds": {t: {p: encode_series(a) for p, a in m.items()}
                       for t, m in series.items()},
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    return len(dates), len(series)


def write_snapshot(snapshot_dir, date, payload, keep_days):
    """Write a dated snapshot and prune ones outside the rolling window.

    Filenames use ISO 8601 (thresholds_YYYY-MM-DD.json) so they sort
    chronologically in directory listings and lexicographic comparisons.

    These are a human-readable archive; the mod should read the consolidated
    history_{realm}.json instead (one request instead of thirty).
    """
    os.makedirs(snapshot_dir, exist_ok=True)
    path = os.path.join(snapshot_dir, f"thresholds_{date}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    cutoff = (datetime.datetime.strptime(date, "%Y-%m-%d")
              - datetime.timedelta(days=keep_days - 1))
    pruned = []
    for name in sorted(os.listdir(snapshot_dir)):
        m = re.fullmatch(r"thresholds_(\d{4}-\d{2}-\d{2})\.json", name)
        if not m:
            continue
        if datetime.datetime.strptime(m.group(1), "%Y-%m-%d") < cutoff:
            os.remove(os.path.join(snapshot_dir, name))
            pruned.append(name)
    return path, pruned


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="thresholds.json")
    ap.add_argument("--indent", type=int, default=None,
                    help="pretty-print (default: compact, smaller payload)")
    ap.add_argument("--no-metadata", action="store_true",
                    help="omit vehicle metadata (the mod has it client-side)")
    ap.add_argument("--history-dir", default=None,
                    help="if set, maintain rolling per-realm history files here")
    ap.add_argument("--history-days", type=int, default=30,
                    help="rolling window length in days (default 30)")
    ap.add_argument("--snapshot-dir", default=None,
                    help="also write dated thresholds_YYYY-MM-DD.json archives here")
    args = ap.parse_args()

    today = time.strftime("%Y-%m-%d", time.gmtime())
    result = {"generated_at": int(time.time()), "realms": {}}

    for realm, host in REALMS.items():
        dist, updated = fetch_thresholds(host)
        result["realms"][realm] = {"updated_at": updated, "thresholds": dist}
        print(f"{realm}: {len(dist)} vehicles, updated_at={updated}", file=sys.stderr)

        if args.history_dir:
            path = os.path.join(args.history_dir, f"history_{realm}.json")
            days, tanks = update_history(
                path, today, updated, dist, args.history_days)
            print(f"{realm}: history {days} day(s), {tanks} vehicles -> {path}",
                  file=sys.stderr)

    if not args.no_metadata:
        # Metadata is realm-independent for our purposes; pull once.
        result["vehicles"] = fetch_vehicles(REALMS["na"])
        print(f"metadata: {len(result['vehicles'])} vehicles", file=sys.stderr)

    # Create the parent directory if it doesn't exist (e.g. public/ in CI).
    # abspath() handles a bare filename, where dirname() would return "".
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"),
                  indent=args.indent)

    if args.snapshot_dir:
        path, pruned = write_snapshot(args.snapshot_dir, today, result,
                                      args.history_days)
        print(f"snapshot -> {path}", file=sys.stderr)
        if pruned:
            print(f"pruned {len(pruned)} snapshot(s) outside the "
                  f"{args.history_days}-day window: {pruned[0]} .. {pruned[-1]}",
                  file=sys.stderr)

    size = os.path.getsize(args.out)
    print(f"wrote {args.out} ({size/1024:.1f} KB)", file=sys.stderr)


if __name__ == "__main__":
    APP_ID = os.environ.get("WG_APPLICATION_ID")
    if not APP_ID:
        sys.exit("WG_APPLICATION_ID not set")
    main()

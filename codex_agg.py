#!/usr/bin/env python3
"""Honest aggregation of OpenAI Codex token usage from local rollout JSONL.

Codex emits `event_msg / token_count` records carrying:
  info.total_token_usage  -- CUMULATIVE per rollout file, monotonic non-decreasing
  info.last_token_usage   -- the most recent turn's usage (repeated on duplicate events)

Therefore:
  * per-file total  = MAX of total_token_usage (never the sum -- that double counts)
  * per-interval    = DIFF of consecutive cumulative values (clamped >= 0)
                      -> gives real minute-resolution burn time series, and duplicate
                         events naturally contribute a delta of 0.

Duplicate session ids across files are reported explicitly rather than silently merged.
"""
import json
import os
import sys
from collections import defaultdict

ROOTS = [
    r"C:\Users\Admin\Documents\CodexBackups\codex_cleanup_20260521_194850\old_sessions",
    r"C:\Users\Admin\.codex\sessions",
]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex_totals.json")

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
          "reasoning_output_tokens", "total_tokens")

files = []
for root in ROOTS:
    if not os.path.isdir(root):
        continue
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.startswith("rollout-") and fn.endswith(".jsonl"):
                files.append(os.path.join(dirpath, fn))
files.sort()

stats = defaultdict(int)
stats["files"] = len(files)
stats["bytes"] = sum(os.path.getsize(p) for p in files)

sessions = {}          # key -> per-file session record
minute = defaultdict(lambda: defaultdict(int))   # "YYYY-MM-DDTHH:MM" -> field -> delta
by_model = defaultdict(lambda: defaultdict(int))         # model -> field -> delta
by_model_day = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
plan_types = defaultdict(int)
models = defaultdict(int)
cwds = defaultdict(int)
cli_versions = defaultdict(int)
sid_files = defaultdict(list)

for i, path in enumerate(files):
    if i % 50 == 0:
        print("  [%d/%d] %s" % (i, len(files), os.path.basename(path)[:60]), flush=True)
    rel = path
    meta = None
    prev = None            # previous cumulative dict
    fmax = dict.fromkeys(FIELDS, 0)
    events = 0
    resets = 0
    first_ts = last_ts = None
    session_plan = None
    session_model = None
    cur_model = None       # model in effect right now (turn_context can change it mid-session)
    file_models = defaultdict(int)
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        stats["unreadable_files"] += 1
        continue
    with fh:
        for line in fh:
            stats["lines"] += 1
            has_tok = "total_token_usage" in line
            has_meta = meta is None and '"session_meta"' in line
            has_turn = '"turn_context"' in line
            if not (has_tok or has_meta or has_turn):
                continue
            try:
                d = json.loads(line)
            except Exception:
                stats["bad_lines"] += 1
                continue
            if not isinstance(d, dict):
                continue
            pl = d.get("payload") or {}
            if not isinstance(pl, dict):
                pl = {}
            if has_meta and d.get("type") == "session_meta":
                meta = pl
                continue
            if has_turn and d.get("type") == "turn_context":
                m = pl.get("model") or pl.get("model_slug")
                if m:
                    cur_model = m
                    session_model = session_model or m
                    file_models[m] += 1
                continue
            if not has_tok or pl.get("type") != "token_count":
                continue
            info = pl.get("info") or {}
            if not isinstance(info, dict):
                continue
            cum = info.get("total_token_usage")
            if not isinstance(cum, dict):
                continue
            events += 1
            ts = d.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            rl = pl.get("rate_limits") or {}
            if isinstance(rl, dict) and rl.get("plan_type"):
                session_plan = rl["plan_type"]
                plan_types[rl["plan_type"]] += 1
            cur = {f: (cum.get(f) or 0) for f in FIELDS}
            # cumulative counter can reset (compaction / fork); treat a drop as a reset
            # and count the full new value as fresh burn.
            if prev is not None and cur["total_tokens"] < prev["total_tokens"]:
                resets += 1
                base = dict.fromkeys(FIELDS, 0)
            else:
                base = prev if prev is not None else dict.fromkeys(FIELDS, 0)
            mkey = cur_model or session_model or "unknown"
            for f in FIELDS:
                dv = cur[f] - base[f]
                if dv <= 0:
                    continue
                by_model[mkey][f] += dv
                if ts:
                    slot = ts[:16]
                    minute[slot][f] += dv
                    by_model_day[ts[:10]][mkey][f] += dv
            for f in FIELDS:
                if resets:
                    fmax[f] += max(0, cur[f] - base[f])
                else:
                    fmax[f] = max(fmax[f], cur[f])
            prev = cur

    if events == 0:
        stats["files_without_token_data"] += 1
        continue
    sid = (meta or {}).get("id") or os.path.basename(path)
    sid_files[sid].append(os.path.basename(path))
    cwd = (meta or {}).get("cwd")
    cliv = (meta or {}).get("cli_version")
    if cwd:
        cwds[cwd] += 1
    if cliv:
        cli_versions[cliv] += 1
    if session_model:
        models[session_model] += 1
    sessions[os.path.basename(path)] = {
        "session_id": sid,
        "cwd": cwd,
        "cli_version": cliv,
        "model": session_model,
        "models_in_file": dict(file_models),
        "plan_type": session_plan,
        "start": (meta or {}).get("timestamp") or first_ts,
        "first_token_ts": first_ts,
        "last_token_ts": last_ts,
        "events": events,
        "counter_resets": resets,
        "bytes": os.path.getsize(path),
        **{f: fmax[f] for f in FIELDS},
    }

totals = {f: sum(s[f] for s in sessions.values()) for f in FIELDS}
minute_totals = {f: sum(m[f] for m in minute.values()) for f in FIELDS}

# per-day and per-hour rollups
day = defaultdict(lambda: defaultdict(int))
hour = defaultdict(lambda: defaultdict(int))
for slot, fv in minute.items():
    for f, v in fv.items():
        day[slot[:10]][f] += v
        hour[slot[:13]][f] += v

dupes = {k: v for k, v in sid_files.items() if len(v) > 1}

out = {
    "roots": ROOTS,
    "scan_stats": dict(stats),
    "session_files_with_data": len(sessions),
    "distinct_session_ids": len(sid_files),
    "session_ids_in_multiple_files": dupes,
    "totals_max_cumulative": totals,
    "totals_from_minute_deltas": minute_totals,
    "plan_types": dict(plan_types),
    "models_seen": dict(models),
    "by_model": {k: dict(v) for k, v in sorted(by_model.items())},
    "by_model_day": {d: {m: dict(f) for m, f in sorted(ms.items())}
                     for d, ms in sorted(by_model_day.items())},
    "cli_versions": dict(cli_versions),
    "cwd_counts": dict(sorted(cwds.items(), key=lambda x: -x[1])),
    "by_day": {k: dict(v) for k, v in sorted(day.items())},
    "by_hour": {k: dict(v) for k, v in sorted(hour.items())},
    "by_minute": {k: dict(v) for k, v in sorted(minute.items())},
    "sessions": sessions,
}
ts_all = sorted(s["first_token_ts"] for s in sessions.values() if s["first_token_ts"])
out["first_ts"] = ts_all[0] if ts_all else None
ts_end = sorted(s["last_token_ts"] for s in sessions.values() if s["last_token_ts"])
out["last_ts"] = ts_end[-1] if ts_end else None

with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=1)

print()
print("rollout files scanned : %d  (%.2f GB)" % (stats["files"], stats["bytes"] / 1e9))
print("lines read            : {:,}".format(stats["lines"]))
print("files w/o token data  :", stats["files_without_token_data"])
print("bad json lines        :", stats["bad_lines"])
print("session files w/ data :", len(sessions))
print("distinct session ids  :", len(sid_files), " (ids in >1 file: %d)" % len(dupes))
print("counter resets seen   :", sum(s["counter_resets"] for s in sessions.values()))
print("date range            :", out["first_ts"], "->", out["last_ts"])
print("plan types            :", dict(plan_types))
print()
print("=== TOTALS (max cumulative per file) ===")
for f in FIELDS:
    print("  {:<24}: {:>15,}".format(f, totals[f]))
print()
print("=== CROSS-CHECK (sum of minute deltas) ===")
for f in FIELDS:
    d = minute_totals[f] - totals[f]
    print("  {:<24}: {:>15,}   delta vs above: {:+,}".format(f, minute_totals[f], d))
print()
print("wrote", OUT)

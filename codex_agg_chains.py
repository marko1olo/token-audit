#!/usr/bin/env python3
"""Codex token aggregation that survives INTERLEAVED counters.

WHY THIS EXISTS
---------------
`total_token_usage` is cumulative, but a single rollout file can carry MORE THAN
ONE cumulative counter, interleaved event by event -- concurrent threads or
subagents writing into the same file. Observed in
rollout-2026-04-10T02-40-41-019d7467:

    23:14:19  cum=3,065,004     <- chain A
    23:14:33  cum=1,326,125     <- chain B  (looks like a "reset")
    23:14:36  cum=3,225,875     <- chain A again
    23:14:40  cum=1,480,311     <- chain B again
    23:14:48  cum=3,396,892     <- chain A
    23:14:54  cum=1,635,308     <- chain B

The records carry no thread id, so the chains must be separated by value.

Consequences for the two naive methods:
  * MAX-per-file  -> keeps only the largest chain, silently DROPS every other
                     thread's spend. Undercounts.
  * SUM-of-deltas -> every A->B->A alternation manufactures a huge fake
                     increment, and each apparent drop gets counted as fresh
                     spend. Overcounts, badly. On the second machine's data this
                     inflated the total by 4.19x (62.29 B vs 14.88 B).

This script assigns each event to the chain it plausibly continues (the chain
whose head is <= the new value and closest to it), then sums each chain's final
value. Duplicate events are neutral (equal value -> same chain, no increment).

Placeholder records are skipped: after a compaction Codex emits
total_token_usage with input_tokens == 0 and output_tokens == 0 and
total_tokens == model_context_window. That is not spend.
"""
import json
import os
from collections import defaultdict

ROOTS = [
    r"C:\Users\Admin\Documents\CodexBackups\codex_cleanup_20260521_194850\old_sessions",
    r"C:\Users\Admin\.codex\sessions",
]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codex_chains_totals.json")
FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
          "reasoning_output_tokens", "total_tokens")

files = []
for root in ROOTS:
    if not os.path.isdir(root):
        continue
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            if fn.startswith("rollout-") and fn.endswith(".jsonl"):
                files.append(os.path.join(dp, fn))
files.sort()

stats = defaultdict(int)
stats["files"] = len(files)
minute = defaultdict(lambda: defaultdict(int))
by_model = defaultdict(lambda: defaultdict(int))
sessions = {}
naive_max = defaultdict(int)
naive_delta = defaultdict(int)
chain_hist = defaultdict(int)

for i, path in enumerate(files):
    if i % 100 == 0:
        print("  [%d/%d]" % (i, len(files)), flush=True)
    meta = None
    cur_model = None
    chains = []          # list of dicts: {"head": int, "vals": {field: int}}
    events = 0
    placeholders = 0
    ooo = 0              # out-of-order events (assigned to a non-last chain)
    # naive comparisons, computed on the same pass
    n_max = dict.fromkeys(FIELDS, 0)
    n_prev = None
    n_delta = dict.fromkeys(FIELDS, 0)
    first_ts = last_ts = None
    plan = None
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        stats["unreadable"] += 1
        continue
    with fh:
        for line in fh:
            stats["lines"] += 1
            hm = meta is None and '"session_meta"' in line
            ht = '"turn_context"' in line
            hk = "total_token_usage" in line
            if not (hm or ht or hk):
                continue
            try:
                d = json.loads(line)
            except Exception:
                stats["bad_json_lines"] += 1
                continue
            pl = d.get("payload") or {}
            if not isinstance(pl, dict):
                continue
            if hm and d.get("type") == "session_meta":
                meta = pl
                continue
            if ht and d.get("type") == "turn_context":
                m = pl.get("model") or pl.get("model_slug")
                if m:
                    cur_model = m
                continue
            if not hk or pl.get("type") != "token_count":
                continue
            info = pl.get("info") or {}
            cum = info.get("total_token_usage")
            if not isinstance(cum, dict):
                continue
            v = {f: (cum.get(f) or 0) for f in FIELDS}
            # skip post-compaction placeholders
            if v["input_tokens"] == 0 and v["output_tokens"] == 0:
                placeholders += 1
                continue
            events += 1
            ts = d.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            rl = pl.get("rate_limits") or {}
            if isinstance(rl, dict) and rl.get("plan_type"):
                plan = rl["plan_type"]

            # ---- naive max (what the old audit and my first script did) ----
            for f in FIELDS:
                n_max[f] = max(n_max[f], v[f])
            # ---- naive delta (what the second machine's run did) ----
            if n_prev is None:
                for f in FIELDS:
                    n_delta[f] += v[f]
            else:
                if v["total_tokens"] < n_prev["total_tokens"]:
                    for f in FIELDS:
                        n_delta[f] += v[f]
                else:
                    for f in FIELDS:
                        dv = v[f] - n_prev[f]
                        if dv > 0:
                            n_delta[f] += dv
            n_prev = v

            # ---- chain assignment ----
            best = -1
            best_head = -1
            for ci, ch in enumerate(chains):
                if ch["head"] <= v["total_tokens"] and ch["head"] > best_head:
                    best_head = ch["head"]
                    best = ci
            if best < 0:
                chains.append({"head": v["total_tokens"], "vals": dict(v)})
                ci = len(chains) - 1
            else:
                ci = best
                ch = chains[ci]
                # per-field increment within this chain
                inc = {f: max(0, v[f] - ch["vals"][f]) for f in FIELDS}
                ch["vals"] = dict(v)
                ch["head"] = v["total_tokens"]
                if ci != len(chains) - 1:
                    ooo += 1
                mk = cur_model or "unknown"
                if ts:
                    slot = ts[:16]
                    for f in FIELDS:
                        if inc[f]:
                            minute[slot][f] += inc[f]
                            by_model[mk][f] += inc[f]
                continue
            # brand-new chain: its opening value is all spend
            mk = cur_model or "unknown"
            if ts:
                slot = ts[:16]
                for f in FIELDS:
                    if v[f]:
                        minute[slot][f] += v[f]
                        by_model[mk][f] += v[f]

    if events == 0:
        stats["files_without_token_data"] += 1
        continue
    chain_hist[len(chains)] += 1
    tot = {f: sum(c["vals"][f] for c in chains) for f in FIELDS}
    sid = (meta or {}).get("id")
    key = sid or ("file:" + os.path.basename(path))
    rec = {
        "session_id": sid,
        "file": os.path.basename(path),
        "cwd": (meta or {}).get("cwd"),
        "model": cur_model,
        "plan_type": plan,
        "start": (meta or {}).get("timestamp") or first_ts,
        "end": last_ts,
        "events": events,
        "placeholders_skipped": placeholders,
        "chains": len(chains),
        "out_of_order_events": ooo,
        "chains_total": tot,
        "naive_max": dict(n_max),
        "naive_delta": dict(n_delta),
    }
    # dedupe strictly by session_id: keep the record with the larger total
    if key in sessions and sessions[key]["chains_total"]["total_tokens"] >= tot["total_tokens"]:
        stats["dup_session_files_dropped"] += 1
        continue
    if key in sessions:
        stats["dup_session_files_dropped"] += 1
    sessions[key] = rec
    for f in FIELDS:
        naive_max[f] += n_max[f]
        naive_delta[f] += n_delta[f]

chains_total = {f: sum(s["chains_total"][f] for s in sessions.values()) for f in FIELDS}
minute_total = {f: sum(m[f] for m in minute.values()) for f in FIELDS}


def roll(cut):
    g = defaultdict(lambda: defaultdict(int))
    for slot, mv in minute.items():
        for f, v in mv.items():
            g[slot[:cut]][f] += v
    return {k: dict(v) for k, v in sorted(g.items())}


multi = sum(1 for s in sessions.values() if s["chains"] > 1)
ts_all = sorted(s["start"] for s in sessions.values() if s["start"])
ts_end = sorted(s["end"] for s in sessions.values() if s["end"])

out = {
    "method": "CHAIN_SPLIT",
    "method_note": (
        "One rollout file can hold several interleaved cumulative counters "
        "(concurrent threads/subagents, no thread id in the record). Events are "
        "assigned to the chain they continue; the total is the sum of each "
        "chain's final value. Post-compaction placeholder records "
        "(input_tokens==0 and output_tokens==0) are skipped."
    ),
    "roots": ROOTS,
    "scan_stats": dict(stats),
    "sessions": len(sessions),
    "sessions_with_multiple_chains": multi,
    "chain_count_histogram": dict(sorted(chain_hist.items())),
    "out_of_order_events_total": sum(s["out_of_order_events"] for s in sessions.values()),
    "placeholders_skipped_total": sum(s["placeholders_skipped"] for s in sessions.values()),
    "first_ts": ts_all[0] if ts_all else None,
    "last_ts": ts_end[-1] if ts_end else None,
    "totals_chain_split": chains_total,
    "totals_from_minute_increments": minute_total,
    "totals_naive_max_per_file": dict(naive_max),
    "totals_naive_delta_per_file": dict(naive_delta),
    "by_model": {k: dict(v) for k, v in sorted(by_model.items())},
    "by_day": roll(10),
    "by_hour": roll(13),
    "by_minute": {k: dict(v) for k, v in sorted(minute.items())},
    "sessions_detail": sessions,
}
with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=1)

print()
print("files %d | sessions %d | multi-chain sessions %d"
      % (stats["files"], len(sessions), multi))
print("chain-count histogram:", dict(sorted(chain_hist.items())))
print("out-of-order events:", out["out_of_order_events_total"])
print("placeholders skipped:", out["placeholders_skipped_total"])
print("date range:", out["first_ts"], "->", out["last_ts"])
print()
w = 26
print("%-*s %18s %18s %18s" % (w, "field", "CHAIN-SPLIT", "naive max", "naive delta"))
for f in FIELDS:
    print("%-*s %18s %18s %18s" % (w, f, "{:,}".format(chains_total[f]),
                                   "{:,}".format(naive_max[f]),
                                   "{:,}".format(naive_delta[f])))
print()
print("chain-split vs naive max   : %+.3f%%"
      % (100.0 * (chains_total["total_tokens"] - naive_max["total_tokens"]) / naive_max["total_tokens"]))
print("chain-split vs naive delta : %+.3f%%"
      % (100.0 * (chains_total["total_tokens"] - naive_delta["total_tokens"]) / naive_delta["total_tokens"]))
print("minute-increment cross-check matches chain-split:",
      minute_total == chains_total)
print()
print("wrote", OUT)

#!/usr/bin/env python3
"""Deep per-model and consumption-pattern analysis of Claude Code usage.

Second pass over the same transcripts as claude_agg.py, but keeping every
deduplicated response as a row so distributions can be computed -- not just sums.
Dedupe rule is identical: one row per API message.id, keeping the record with the
largest total (streaming writes the same id several times as the answer grows).
"""
import json
import os
import statistics as st
from collections import defaultdict

ROOT = os.path.expanduser("~/.claude/projects")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_deep.json")

rows_by_id = {}
for dp, _dn, fns in os.walk(ROOT):
    for fn in fns:
        if not fn.endswith(".jsonl"):
            continue
        path = os.path.join(dp, fn)
        rel = os.path.relpath(path, ROOT)
        project = rel.split(os.sep)[0]
        for line in open(path, encoding="utf-8", errors="replace"):
            if '"assistant"' not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") != "assistant":
                continue
            m = r.get("message") or {}
            u = m.get("usage")
            if not isinstance(u, dict):
                continue
            mid = m.get("id")
            if not mid:
                continue
            inp = u.get("input_tokens") or 0
            cc = u.get("cache_creation_input_tokens") or 0
            cr = u.get("cache_read_input_tokens") or 0
            out = u.get("output_tokens") or 0
            tot = inp + cc + cr + out
            prev = rows_by_id.get(mid)
            if prev and prev["tot"] >= tot:
                continue
            rows_by_id[mid] = {
                "model": m.get("model") or "unknown",
                "ts": r.get("timestamp"),
                "session": r.get("sessionId"),
                "side": bool(r.get("isSidechain")),
                "project": project,
                "ver": r.get("version"),
                "stop": m.get("stop_reason"),
                "inp": inp, "cc": cc, "cr": cr, "out": out, "tot": tot,
            }

rows = sorted(rows_by_id.values(), key=lambda r: r["ts"] or "")
print("deduplicated responses:", len(rows))


def q(vals, p):
    if not vals:
        return 0
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(p / 100.0 * (len(s) - 1)))))
    return s[i]


def describe(vals):
    if not vals:
        return {}
    return {"n": len(vals), "sum": sum(vals), "mean": round(st.mean(vals), 1),
            "median": q(vals, 50), "p90": q(vals, 90), "p99": q(vals, 99),
            "max": max(vals), "min": min(vals)}


out = {"responses": len(rows), "period": [rows[0]["ts"], rows[-1]["ts"]]}

# ---------- per model, deep ----------
bym = defaultdict(list)
for r in rows:
    bym[r["model"]].append(r)

models = {}
for mdl, rs in bym.items():
    tot = sum(r["tot"] for r in rs)
    inp = sum(r["inp"] for r in rs)
    cc = sum(r["cc"] for r in rs)
    cr = sum(r["cr"] for r in rs)
    o = sum(r["out"] for r in rs)
    ctx = [r["inp"] + r["cc"] + r["cr"] for r in rs]      # context sent per call
    sess = {r["session"] for r in rs}
    days = {(r["ts"] or "")[:10] for r in rs if r["ts"]}
    stops = defaultdict(int)
    for r in rs:
        stops[r["stop"] or "none"] += 1
    models[mdl] = {
        "responses": len(rs),
        "sessions": len(sess),
        "active_days": len(days),
        "total": tot,
        "uncached_input": inp, "cache_write": cc, "cache_read": cr, "output": o,
        "share_of_all_tokens": round(100.0 * tot / sum(r["tot"] for r in rows), 3) if rows else 0,
        # cache effectiveness: of everything sent as context, how much was a cache read
        "cache_hit_rate_pct": round(100.0 * cr / max(1, inp + cc + cr), 2),
        "output_share_pct": round(100.0 * o / max(1, tot), 3),
        "context_per_call": describe(ctx),
        "output_per_call": describe([r["out"] for r in rs]),
        "uncached_per_call": describe([r["inp"] for r in rs]),
        "tokens_per_response_mean": round(tot / max(1, len(rs)), 1),
        "context_to_output_ratio": round((inp + cc + cr) / max(1, o), 1),
        "sidechain_responses": sum(1 for r in rs if r["side"]),
        "sidechain_tokens": sum(r["tot"] for r in rs if r["side"]),
        "stop_reasons": dict(sorted(stops.items(), key=lambda x: -x[1])),
        "first_ts": min((r["ts"] for r in rs if r["ts"]), default=None),
        "last_ts": max((r["ts"] for r in rs if r["ts"]), default=None),
    }
out["by_model"] = dict(sorted(models.items(), key=lambda x: -x[1]["total"]))

# ---------- per session ----------
bys = defaultdict(list)
for r in rows:
    bys[r["session"]].append(r)

sessions = {}
for sid, rs in bys.items():
    rs.sort(key=lambda r: r["ts"] or "")
    tot = sum(r["tot"] for r in rs)
    ts = [r["ts"] for r in rs if r["ts"]]
    dur = None
    if len(ts) > 1:
        def sec(x):
            h, m, s = int(x[11:13]), int(x[14:16]), float(x[17:23])
            return h * 3600 + m * 60 + s
        d0, d1 = ts[0][:10], ts[-1][:10]
        dur = sec(ts[-1]) - sec(ts[0]) + (86400 if d1 != d0 else 0)
    mc = defaultdict(int)
    for r in rs:
        mc[r["model"]] += r["tot"]
    sessions[sid] = {
        "responses": len(rs), "total": tot,
        "uncached_input": sum(r["uncached_input"] if False else r["inp"] for r in rs),
        "cache_read": sum(r["cr"] for r in rs),
        "output": sum(r["out"] for r in rs),
        "start": ts[0] if ts else None, "end": ts[-1] if ts else None,
        "duration_s": round(dur) if dur else None,
        "tokens_per_hour": round(tot / (dur / 3600.0)) if dur and dur > 60 else None,
        "models": dict(sorted(mc.items(), key=lambda x: -x[1])),
        "dominant_model": max(mc.items(), key=lambda x: x[1])[0] if mc else None,
        "sidechain_responses": sum(1 for r in rs if r["side"]),
        "max_single_response_context": max((r["inp"] + r["cc"] + r["cr"] for r in rs), default=0),
    }
out["session_count"] = len(sessions)
top = sorted(sessions.items(), key=lambda x: -x[1]["total"])[:25]
out["top_sessions"] = {k: v for k, v in top}

# slim list of every session for the timeline/Gantt view
out["sessions_slim"] = [
    {"id": sid[:8], "start": v["start"], "end": v["end"], "total": v["total"],
     "responses": v["responses"], "model": v["dominant_model"],
     "duration_s": v["duration_s"], "sub": v["sidechain_responses"],
     "cache_read": v["cache_read"]}
    for sid, v in sorted(sessions.items(), key=lambda x: (x[1]["start"] or ""))
    if v["total"] > 0
]

# day x hour matrix for the punchcard
dh = defaultdict(int)
for r in rows:
    if r["ts"]:
        dh["%s|%s" % (r["ts"][:10], r["ts"][11:13])] += r["tot"]
out["day_hour_matrix"] = dict(sorted(dh.items()))

# per-day cache hit rate and cost regime
dstat = defaultdict(lambda: defaultdict(int))
for r in rows:
    if not r["ts"]:
        continue
    k = r["ts"][:10]
    dstat[k]["inp"] += r["inp"]
    dstat[k]["cc"] += r["cc"]
    dstat[k]["cr"] += r["cr"]
    dstat[k]["out"] += r["out"]
    dstat[k]["n"] += 1
out["by_day_full"] = {}
for k, v in sorted(dstat.items()):
    ctx = v["inp"] + v["cc"] + v["cr"]
    out["by_day_full"][k] = {
        **dict(v), "total": ctx + v["out"],
        "cache_pct": round(100.0 * v["cr"] / ctx, 2) if ctx else 0.0,
    }

# subagent share per day
sub = defaultdict(lambda: [0, 0])
for r in rows:
    if r["ts"]:
        sub[r["ts"][:10]][1 if r["side"] else 0] += r["tot"]
out["subagent_share_by_day"] = {
    k: {"main": v[0], "sub": v[1],
        "sub_pct": round(100.0 * v[1] / (v[0] + v[1]), 2) if (v[0] + v[1]) else 0.0}
    for k, v in sorted(sub.items())}

sizes = [v["total"] for v in sessions.values()]
out["session_size_distribution"] = describe(sizes)
# how concentrated is usage
sizes_sorted = sorted(sizes, reverse=True)
grand = sum(sizes_sorted)
cum = 0
conc = {}
for n in (1, 3, 5, 10, 20, 50):
    if n <= len(sizes_sorted):
        conc["top_%d_sessions_pct" % n] = round(100.0 * sum(sizes_sorted[:n]) / grand, 2)
out["concentration"] = conc

# session size histogram (log buckets)
hist = defaultdict(int)
for s in sizes:
    if s == 0:
        hist["0"] += 1
    else:
        import math
        e = int(math.floor(math.log10(s)))
        hist["1e%d" % e] += 1
out["session_size_histogram"] = dict(sorted(hist.items()))

# ---------- the cache-read growth mechanic ----------
# within a session, does context per call grow with call index?
buckets = defaultdict(list)
for sid, rs in bys.items():
    for i, r in enumerate(rs):
        b = min(9, i // 25)          # 0-24, 25-49, ... 225+
        buckets[b].append(r["inp"] + r["cc"] + r["cr"])
out["context_growth_by_call_index"] = {
    ("calls_%d_%d" % (b * 25, b * 25 + 24) if b < 9 else "calls_225_plus"): {
        "responses": len(v), "mean_context": round(st.mean(v)),
        "median_context": q(v, 50), "p90_context": q(v, 90)}
    for b, v in sorted(buckets.items())}

# ---------- main vs subagent ----------
side = {"main": [r for r in rows if not r["side"]],
        "subagent": [r for r in rows if r["side"]]}
out["main_vs_subagent"] = {}
for k, rs in side.items():
    tot = sum(r["tot"] for r in rs)
    out["main_vs_subagent"][k] = {
        "responses": len(rs), "total": tot,
        "share_pct": round(100.0 * tot / sum(r["tot"] for r in rows), 2),
        "mean_tokens_per_response": round(tot / max(1, len(rs))),
        "output": sum(r["out"] for r in rs),
        "cache_read": sum(r["cr"] for r in rs),
        "models": dict(sorted(
            ((m, sum(r["tot"] for r in rs if r["model"] == m)) for m in bym),
            key=lambda x: -x[1])),
    }

# ---------- per model per day ----------
mpd = defaultdict(lambda: defaultdict(int))
for r in rows:
    if r["ts"]:
        mpd[(r["ts"])[:10]][r["model"]] += r["tot"]
out["by_day_model"] = {d: dict(sorted(v.items(), key=lambda x: -x[1]))
                       for d, v in sorted(mpd.items())}

# ---------- per model per hour-of-day ----------
mph = defaultdict(lambda: defaultdict(int))
for r in rows:
    if r["ts"]:
        mph[r["ts"][11:13]][r["model"]] += r["tot"]
out["by_hour_of_day_model"] = {h: dict(sorted(v.items(), key=lambda x: -x[1]))
                               for h, v in sorted(mph.items())}

# ---------- output-size distribution ----------
outs = [r["out"] for r in rows]
out["output_per_response"] = describe(outs)
ob = defaultdict(int)
for v in outs:
    if v == 0:
        ob["0"] += 1
    elif v < 100:
        ob["1-99"] += 1
    elif v < 500:
        ob["100-499"] += 1
    elif v < 2000:
        ob["500-1999"] += 1
    elif v < 8000:
        ob["2000-7999"] += 1
    else:
        ob["8000+"] += 1
out["output_size_buckets"] = dict(ob)

# ---------- burst analysis: gaps between responses ----------
gaps = []
prev = None
for r in rows:
    if not r["ts"]:
        continue
    t = r["ts"]
    if prev:
        def epoch(x):
            import datetime as dt
            return dt.datetime(int(x[0:4]), int(x[5:7]), int(x[8:10]),
                               int(x[11:13]), int(x[14:16]),
                               int(x[17:19])).timestamp()
        g = epoch(t) - epoch(prev)
        if 0 <= g < 86400:
            gaps.append(g)
    prev = t
out["gap_between_responses_s"] = describe([round(g) for g in gaps])
active = sum(g for g in gaps if g <= 300)
out["active_time_hours_gap_le_300s"] = round(active / 3600.0, 1)
out["tokens_per_active_hour"] = round(sum(r["tot"] for r in rows) / max(0.1, active / 3600.0))

with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=1, ensure_ascii=False)

# ---------- print ----------
print()
print("=" * 100)
print("PER MODEL")
print("=" * 100)
hdr = ("model", "resp", "sessions", "total", "cache hit%", "out%", "ctx/call mean", "out/call mean", "ctx:out")
print("%-18s %7s %6s %16s %9s %7s %14s %13s %8s" % hdr)
for m, v in out["by_model"].items():
    print("%-18s %7d %6d %16s %8.2f%% %6.3f%% %14s %13s %8s" % (
        m, v["responses"], v["sessions"], "{:,}".format(v["total"]),
        v["cache_hit_rate_pct"], v["output_share_pct"],
        "{:,}".format(int(v["context_per_call"].get("mean", 0))),
        "{:,}".format(int(v["output_per_call"].get("mean", 0))),
        v["context_to_output_ratio"]))
print()
print("CONTEXT GROWTH WITHIN A SESSION (the cache-read mechanic)")
for k, v in out["context_growth_by_call_index"].items():
    print("  %-18s n=%-6d mean=%13s median=%13s p90=%13s" % (
        k, v["responses"], "{:,}".format(v["mean_context"]),
        "{:,}".format(v["median_context"]), "{:,}".format(v["p90_context"])))
print()
print("MAIN vs SUBAGENT")
for k, v in out["main_vs_subagent"].items():
    print("  %-9s resp=%-6d total=%16s share=%5.2f%% mean/resp=%12s" % (
        k, v["responses"], "{:,}".format(v["total"]), v["share_pct"],
        "{:,}".format(v["mean_tokens_per_response"])))
print()
print("SESSION CONCENTRATION")
for k, v in out["concentration"].items():
    print("  %-24s %6.2f%%" % (k, v))
print("  session size: median %s, p90 %s, max %s" % (
    "{:,}".format(out["session_size_distribution"]["median"]),
    "{:,}".format(out["session_size_distribution"]["p90"]),
    "{:,}".format(out["session_size_distribution"]["max"])))
print()
print("OUTPUT SIZE per response:", out["output_size_buckets"])
print("output/call: median %s p90 %s max %s" % (
    out["output_per_response"]["median"], out["output_per_response"]["p90"],
    out["output_per_response"]["max"]))
print()
print("gaps between responses: median %ss p90 %ss" % (
    out["gap_between_responses_s"]["median"], out["gap_between_responses_s"]["p90"]))
print("active time (gaps <=300s): %s h -> %s tokens per active hour" % (
    out["active_time_hours_gap_le_300s"], "{:,}".format(out["tokens_per_active_hour"])))
print()
print("wrote", OUT)

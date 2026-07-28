#!/usr/bin/env python3
"""Honest aggregation of Claude Code token usage from local JSONL transcripts.

Counts every assistant record that carries a `message.usage` block.
Dedupes by API message id, because `--resume` / compaction copy prior
assistant messages into new session files (double-count trap).
"""
import json
import os
import sys
from collections import defaultdict

import tokenaudit_scan

ROOT = os.path.expanduser("~/.claude/projects")
HERE = os.path.dirname(os.path.abspath(__file__))

# Окно сканирования снимается ДО чтения: размер каждого файла запоминается и
# читается ровно столько. Второй проход (claude_deep.py) читает те же префиксы
# тех же файлов, поэтому два артефакта описывают один и тот же вход по
# построению. Без этого проходы расходились на 138 ответов и 18.6 млн токенов --
# транскрипты дописываются, пока их читают. Подробности в tokenaudit_scan.py.
#
# Каталог самого инструмента исключается: репозиторий лежит внутри
# ~/.claude/projects, и без этого он считает собственные выходные файлы.
WINDOW = tokenaudit_scan.ScanWindow.capture(
    [ROOT], suffix=".jsonl", skip_dirs=(HERE,), captured_by="claude_agg")
files = [t[0] for t in WINDOW.files()]
_REL = {t[0]: (t[1], t[2]) for t in WINDOW.files()}

# raw rows before dedupe
rows = []
stats = defaultdict(int)
bad_lines = 0
no_usage_assistant = 0
models_seen = set()
synthetic = 0

for path in files:
    rel = os.path.relpath(path, ROOT)
    project = rel.split(os.sep)[0]
    stats["files"] += 1
    try:
        _rel, _root = _REL[path]
        lines_iter = WINDOW.lines(path, _rel, _root)
    except (OSError, KeyError):
        stats["unreadable_files"] += 1
        continue
    if True:
        for line in lines_iter:
            line = line.strip()
            if not line:
                continue
            stats["lines"] += 1
            try:
                r = json.loads(line)
            except Exception:
                bad_lines += 1
                continue
            if not isinstance(r, dict):
                continue
            if r.get("type") != "assistant":
                continue
            stats["assistant_records"] += 1
            msg = r.get("message") or {}
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                no_usage_assistant += 1
                continue
            model = msg.get("model") or "unknown"
            models_seen.add(model)
            # Claude Code writes synthetic assistant records (e.g. API error
            # placeholders) with model "<synthetic>" and zero usage.
            if model == "<synthetic>":
                synthetic += 1
            mid = msg.get("id")
            rows.append(
                {
                    "id": mid,
                    "model": model,
                    "ts": r.get("timestamp"),
                    "session": r.get("sessionId"),
                    "sidechain": bool(r.get("isSidechain")),
                    "project": project,
                    "file": rel,
                    "version": r.get("version"),
                    "entrypoint": r.get("entrypoint"),
                    "inp": usage.get("input_tokens") or 0,
                    "out": usage.get("output_tokens") or 0,
                    "cc": usage.get("cache_creation_input_tokens") or 0,
                    "cr": usage.get("cache_read_input_tokens") or 0,
                    "tier": usage.get("service_tier"),
                    "ws": ((usage.get("server_tool_use") or {}).get("web_search_requests") or 0),
                    "wf": ((usage.get("server_tool_use") or {}).get("web_fetch_requests") or 0),
                    "e1h": ((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0),
                    "e5m": ((usage.get("cache_creation") or {}).get("ephemeral_5m_input_tokens") or 0),
                }
            )

# ---- dedupe ----
# One API response is written as several JSONL records: one per content block,
# plus incremental streaming snapshots where output_tokens grows 1 -> final.
# Verified: 13959/13960 duplicated ids are duplicated inside a single file
# (so this is block/stream splitting, not resume-copying), and where copies
# differ, input is constant while output grows. Therefore the correct
# representative per message id is the record with the LARGEST token total,
# i.e. the final complete usage snapshot. Keeping the *first* record would
# undercount output on 2442 ids.
best = {}
order = []
noid = 0
kept_keys = set()
for i, r in enumerate(rows):
    if r["id"]:
        key = ("id", r["id"])
    else:
        noid += 1
        key = ("fallback", r["session"], r["ts"], r["inp"], r["out"], r["cc"], r["cr"], i)
    total = r["inp"] + r["out"] + r["cc"] + r["cr"]
    if key not in best:
        best[key] = (total, r)
        order.append(key)
    elif total > best[key][0]:
        best[key] = (total, r)

uniq = [best[k][1] for k in order]
kept_ids = {id(r) for r in uniq}
dupe_rows = [r for r in rows if id(r) not in kept_ids]


def tot(rs):
    return {
        "n": len(rs),
        "inp": sum(r["inp"] for r in rs),
        "out": sum(r["out"] for r in rs),
        "cc": sum(r["cc"] for r in rs),
        "cr": sum(r["cr"] for r in rs),
        "ws": sum(r["ws"] for r in rs),
        "wf": sum(r["wf"] for r in rs),
        # cache-write TTL split: 1h writes cost 2x base, 5m writes 1.25x
        "e1h": sum(r["e1h"] for r in rs),
        "e5m": sum(r["e5m"] for r in rs),
    }


out = {
    "source_root": ROOT,
    "scan_stats": dict(stats),
    "bad_lines": bad_lines,
    "assistant_without_usage": no_usage_assistant,
    "synthetic_records": synthetic,
    "records_raw": len(rows),
    "records_deduped": len(uniq),
    "duplicate_records_dropped": len(dupe_rows),
    "records_missing_id": noid,
    "models_seen": sorted(models_seen),
    "totals_raw": tot(rows),
    "totals_deduped": tot(uniq),
    "totals_duplicates": tot(dupe_rows),
    "by_model": {},
    "by_day": {},
    "by_project": {},
    "by_sidechain": {},
    "by_version": {},
    "by_tier": {},
    "by_session": {},
}

g = defaultdict(list)
for r in uniq:
    g[r["model"]].append(r)
out["by_model"] = {k: tot(v) for k, v in sorted(g.items())}

g = defaultdict(list)
for r in uniq:
    day = (r["ts"] or "unknown")[:10]
    g[day].append(r)
out["by_day"] = {k: tot(v) for k, v in sorted(g.items())}

# multiple time resolutions for the charts: hour, 10-minute, minute
for _label, _cut in (("by_hour", 13), ("by_10min", 15), ("by_minute", 16)):
    g = defaultdict(list)
    for r in uniq:
        g[(r["ts"] or "unknown")[:_cut]].append(r)
    out[_label] = {k: tot(v) for k, v in sorted(g.items())}

# activity rhythm: hour-of-day and weekday profiles
g = defaultdict(list)
for r in uniq:
    if r["ts"]:
        g[r["ts"][11:13]].append(r)
out["by_hour_of_day"] = {k: tot(v) for k, v in sorted(g.items())}

import datetime as _dt

g = defaultdict(list)
for r in uniq:
    ts = r["ts"]
    if not ts:
        continue
    try:
        d = _dt.date(int(ts[0:4]), int(ts[5:7]), int(ts[8:10]))
    except ValueError:
        continue
    g["%d-%s" % (d.weekday(), d.strftime("%a"))].append(r)
out["by_weekday"] = {k: tot(v) for k, v in sorted(g.items())}

# per-day per-model, for stacked charts
g2 = defaultdict(lambda: defaultdict(list))
for r in uniq:
    g2[(r["ts"] or "unknown")[:10]][r["model"]].append(r)
out["by_day_model"] = {d: {m: tot(v) for m, v in sorted(ms.items())} for d, ms in sorted(g2.items())}

g = defaultdict(list)
for r in uniq:
    g[r["project"]].append(r)
out["by_project"] = {k: tot(v) for k, v in sorted(g.items())}

g = defaultdict(list)
for r in uniq:
    g["sidechain(subagent)" if r["sidechain"] else "main"].append(r)
out["by_sidechain"] = {k: tot(v) for k, v in sorted(g.items())}

g = defaultdict(list)
for r in uniq:
    g[r["version"] or "unknown"].append(r)
out["by_version"] = {k: tot(v) for k, v in sorted(g.items())}

g = defaultdict(list)
for r in uniq:
    g[r["tier"] or "unknown"].append(r)
out["by_tier"] = {k: tot(v) for k, v in sorted(g.items())}

g = defaultdict(list)
for r in uniq:
    g[r["session"] or "unknown"].append(r)
sess = {}
for k, v in g.items():
    t = tot(v)
    ts = sorted(x["ts"] for x in v if x["ts"])
    t["start"] = ts[0] if ts else None
    t["end"] = ts[-1] if ts else None
    t["project"] = v[0]["project"]
    sess[k] = t
out["by_session"] = sess
out["session_count"] = len(sess)

ts_all = sorted(r["ts"] for r in uniq if r["ts"])
out["first_ts"] = ts_all[0] if ts_all else None
out["last_ts"] = ts_all[-1] if ts_all else None

# Манифест окна пишется вместе с артефактом: следующий проход обязан читать
# ровно этот вход, иначе артефакты несопоставимы.
out["scan_window"] = {"files": len(WINDOW.sizes),
                      "bytes": sum(WINDOW.sizes.values()),
                      "manifest": tokenaudit_scan.MANIFEST_NAME}
WINDOW.save()
print("scan window          :", WINDOW.describe())

dst = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_totals.json")
with open(dst, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=1)

t = out["totals_deduped"]
tr = out["totals_raw"]
print("files scanned        :", stats["files"])
print("lines               :", stats["lines"])
print("assistant records   :", stats["assistant_records"])
print("usage records raw   :", len(rows))
print("usage records uniq  :", len(uniq))
print("dupes dropped       :", len(dupe_rows), "(", tot(dupe_rows)["inp"] + tot(dupe_rows)["cc"] + tot(dupe_rows)["cr"] + tot(dupe_rows)["out"], "tokens )")
print("missing message.id  :", noid)
print("bad json lines      :", bad_lines)
print("models              :", sorted(models_seen))
print("date range          :", out["first_ts"], "->", out["last_ts"])
print("sessions            :", len(sess))
print()
print("=== DEDUPED TOTALS ===")
print("  input (uncached)  : {:>15,}".format(t["inp"]))
print("  cache write       : {:>15,}".format(t["cc"]))
print("  cache read        : {:>15,}".format(t["cr"]))
print("  output            : {:>15,}".format(t["out"]))
print("  TOTAL             : {:>15,}".format(t["inp"] + t["cc"] + t["cr"] + t["out"]))
print("  web_search reqs   : {:>15,}".format(t["ws"]))
print("  web_fetch reqs    : {:>15,}".format(t["wf"]))
print()
print("=== RAW (no dedupe) TOTAL: {:,} ===".format(tr["inp"] + tr["cc"] + tr["cr"] + tr["out"]))
print("wrote", dst)

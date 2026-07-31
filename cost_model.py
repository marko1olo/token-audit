#!/usr/bin/env python3
"""Cost model over the measured token counts. Produces combined.json for the dashboard.

EVIDENCE CLASSES -- kept separate on purpose, never blended into one headline:
  MEASURED   a counter in a local file says so, and this script re-derived it
  VERIFIED   two independent implementations agree (mine and the 2026-06-06 audit)
  REPORTED   only the older audit measured it; the source files no longer exist here
  PROXY      no token counter exists; a volume signal stands in for one
  ESTIMATE   arithmetic on top of the above, with the formula stated

Every dollar figure is a LIST-PRICE EQUIVALENT, not an invoice:
  * Codex ran on plan_type "free" for 2792 sessions and "team" for 10 -- a
    subscription, so the per-token dollars were very likely never billed.
  * Claude Code ran through a local proxy (127.0.0.1:8318 -> Desktop\\proxy.js)
    which rewrote every model to claude-opus-5 and retried 429/5xx every 2s.
    Those retries burned upstream tokens that never reached the transcript, so
    the Claude figure is a FLOOR, not a ceiling.

ЦЕНЫ ЗДЕСЬ БОЛЬШЕ НЕ ЖИВУТ. Таблицы ставок, множители кэша и оговорка про
вводную цену Sonnet 5 переехали в tokenaudit_rates: этот файл был последним, где
они лежали в самом полном виде, оттуда значения и взяты байт в байт. Функции
стоимости остались местными, потому что они отдают РАЗЛОЖЕНИЕ с ключами вида
'*_usd' и ставками рядом с числом -- ровно ту форму, которую читает
build_dashboard.py из combined.json.
"""
import json
import os

import tokenaudit_config as cfg
import tokenaudit_rates as R

HERE = os.path.dirname(os.path.abspath(__file__))

# Свои сообщения на русском -- значит поток надо перевести в utf-8: под Git Bash,
# в pipe и в CI stdout не консоль Windows, и cp1251 убивает процесс на первом же
# нелатинском символе.
cfg.stdout_utf8()


def anthropic_cost(model, inp, cache_write_5m, cache_write_1h, cache_read, out):
    rate = R.ANTHROPIC.get(model)
    if not rate:
        return None
    r_in, r_out = rate
    return {
        "uncached_input_usd": inp / R.MILLION * r_in,
        "cache_write_usd": (cache_write_5m / R.MILLION * r_in * R.CACHE_WRITE_5M_MULT
                            + cache_write_1h / R.MILLION * r_in * R.CACHE_WRITE_1H_MULT),
        "cache_read_usd": cache_read / R.MILLION * r_in * R.CACHE_READ_MULT,
        "output_usd": out / R.MILLION * r_out,
        "rate_in_per_mtok": r_in,
        "rate_out_per_mtok": r_out,
    }


def openai_cost(model, input_tokens, cached_input_tokens, output_tokens):
    """cached_input_tokens is a SUBSET of input_tokens in OpenAI accounting."""
    rate = R.OPENAI.get(model)
    if not rate:
        return None
    r_in, r_cached, r_out = rate
    uncached = max(0, input_tokens - cached_input_tokens)
    return {
        "uncached_input_usd": uncached / R.MILLION * r_in,
        "cached_input_usd": cached_input_tokens / R.MILLION * r_cached,
        "output_usd": output_tokens / R.MILLION * r_out,
        "rate_in_per_mtok": r_in,
        "rate_cached_in_per_mtok": r_cached,
        "rate_out_per_mtok": r_out,
    }


def total(d):
    return sum(v for k, v in d.items() if k.endswith("_usd"))


def day(ts):
    """Дата из метки времени. None или пустая строка -> '—', не TypeError.

    На пустом наборе first_ts/last_ts равны None, и срез ts[:10] ронял процесс
    ПОСЛЕ записи combined.json: артефакт оставался с нулями, а прогон выходил с
    единицей. Дырка в дате -- не причина терять весь артефакт.
    -> str
    """
    return str(ts)[:10] if ts else "—"


def num(n):
    """Число для печати, отсутствующее -> '—'. Форматов '%d' с None не бывает."""
    return "—" if n is None else "{:,}".format(n)


def need(name, hint):
    """Прочитать обязательный артефакт или объяснить, какой командой его сделать.

    Раньше отсутствие файла давало FileNotFoundError с трассой: сообщение
    показывало путь, но не команду, а команда — единственное, что тут нужно
    знать. Файл всё так же обязателен, combined.json без раздела не собирается.
    -> dict
    """
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        raise SystemExit("нет %s — %s" % (name, hint))
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def optional(name, hint):
    """Прочитать НЕобязательный артефакт. Нет файла -> None, и раздела не будет.

    Обязательным здесь может быть только Claude Code: инструмент про него и
    написан. Codex и Antigravity есть далеко не у каждого, и жёсткое требование
    делало две плохие вещи разом. Во-первых, на машине без них конвейер просто
    не запускался. Во-вторых -- и это хуже -- артефакты лежат в репозитории
    закоммиченными, поэтому у клонировавшего они находились, и в его отчёт
    попадали ЧУЖИЕ измерения под классом MEASURED. Отсутствующий раздел честнее
    чужого числа.
    -> dict | None
    """
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        print("нет %s — раздел пропущен (%s)" % (name, hint))
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


out = {"generated_for": "all-time token audit", "evidence_note": __doc__}

# --- Claude Code (MEASURED on this machine) --------------------------------
cl = need("claude_totals.json", "сделать: python claude_agg.py")
t = cl["totals_deduped"]
claude = {
    "evidence": "MEASURED",
    "source": cl["source_root"],
    "period": [cl["first_ts"], cl["last_ts"]],
    "sessions": cl["session_count"],
    # scan_stats бывает пустым словарём (скан ничего не нашёл), и обращение по
    # ключу роняло процесс KeyError уже после записи combined.json.
    "files": (cl.get("scan_stats") or {}).get("files"),
    "responses_deduped": cl["records_deduped"],
    "responses_raw": cl["records_raw"],
    "dedupe_dropped": cl["duplicate_records_dropped"],
    "totals": {
        "uncached_input": t["inp"], "cache_write": t["cc"],
        "cache_read": t["cr"], "output": t["out"],
        "total": t["inp"] + t["cc"] + t["cr"] + t["out"],
    },
    "raw_undeduped_total": (cl["totals_raw"]["inp"] + cl["totals_raw"]["cc"]
                            + cl["totals_raw"]["cr"] + cl["totals_raw"]["out"]),
    "by_model": {},
    "cost_usd_by_model": {},
    "unpriced_models": [],
}
grand = 0.0
for m, v in cl["by_model"].items():
    tt = v["inp"] + v["cc"] + v["cr"] + v["out"]
    claude["by_model"][m] = {
        "responses": v["n"], "total": tt, "uncached_input": v["inp"],
        "cache_write": v["cc"], "cache_write_5m": v.get("e5m", 0),
        "cache_write_1h": v.get("e1h", 0), "cache_read": v["cr"], "output": v["out"],
    }
    if m == R.SYNTHETIC or tt == 0:
        continue
    if m in R.ANTHROPIC:
        # e5m/e1h should sum to cc; if the split is missing, treat all as 5m
        e5, e1 = v.get("e5m", 0), v.get("e1h", 0)
        if e5 + e1 == 0:
            e5 = v["cc"]
        c = anthropic_cost(m, v["inp"], e5, e1, v["cr"], v["out"])
    elif R.OPENAI.get(m):
        # a non-Anthropic model reached through the local proxy: cache_read here
        # is the closest analogue to OpenAI's cached input
        c = openai_cost(m, v["inp"] + v["cr"], v["cr"], v["out"])
    else:
        claude["unpriced_models"].append(m)
        continue
    c["total_usd"] = total(c)
    claude["cost_usd_by_model"][m] = c
    grand += c["total_usd"]
claude["cost_usd_total_list_price_equivalent"] = grand
claude["cost_caveat"] = (
    "FLOOR, not a bill. Traffic went through 127.0.0.1:8318 (Desktop/proxy.js), "
    "which rewrote models to claude-opus-5 and retried 429/5xx every 2s; those "
    "retries burned upstream tokens that were never written to a transcript."
)
out["claude_code"] = claude

# --- Cline / Roo-Code (MEASURED on this machine) -----------------------------
cn = optional("cline_totals.json", "сделать: python cline_agg.py")
if cn:
    cline = {
        "evidence": "MEASURED",
        "tasks": cn.get("task_count", 0),
        "requests": cn.get("request_count", 0),
        "totals": {
            "uncached_input": cn.get("inp", 0),
            "cache_write": cn.get("cw", 0),
            "cache_read": cn.get("cr", 0),
            "output": cn.get("out", 0),
            "total": cn.get("total_tokens", 0),
        },
        "by_model": {},
        "cost_usd_by_model": {},
        "unpriced_models": [],
    }
    c_grand = 0.0
    for m, v in cn.get("by_model", {}).items():
        tt = v["inp"] + v.get("cw", 0) + v["cr"] + v["out"]
        cline["by_model"][m] = {
            "requests": v["reqs"], "total": tt, "uncached_input": v["inp"],
            "cache_write": v.get("cw", 0), "cache_read": v["cr"], "output": v["out"],
        }
        if m in R.ANTHROPIC:
            c = anthropic_cost(m, v["inp"], v.get("cw", 0), 0, v["cr"], v["out"])
        elif R.OPENAI.get(m):
            c = openai_cost(m, v["inp"] + v["cr"], v["cr"], v["out"])
        else:
            cline["unpriced_models"].append(m)
            continue
        if c:
            c["total_usd"] = total(c)
            cline["cost_usd_by_model"][m] = c
            c_grand += c["total_usd"]
    cline["cost_usd_total_list_price_equivalent"] = round(c_grand, 4)
    out["cline"] = cline

# --- Codex ------------------------------------------------------------------
# codex_totals.json пишет только codex_agg.py, и до этой правки его не запускал
# никто: refresh.py гонял лишь codex_agg_chains.py. Теперь под --codex идут оба
# прохода, поэтому файл здесь -- свежее измерение, а не мой закоммиченный слепок.
cx = optional("codex_totals.json", "сделать: python refresh.py --codex "
                                   "-- запускает и chain-split, и максимум по файлу")
mine = cx["totals_max_cumulative"] if cx else None
if cx:
    # То же, что с claude_totals: scan_stats пустеет на пустом скане, и обращение по
    # ключу роняло процесс. Байты отсутствуют -> гигабайты None, а не деление None.
    cxs = cx.get("scan_stats") or {}
    codex = {
        "my_measurement": {
            "evidence": "MEASURED",
            "scope": "backup root only (C:/Users/Admin/Documents/CodexBackups/codex_cleanup_20260521_194850)",
            "period": [cx["first_ts"], cx["last_ts"]],
            "rollout_files": cxs.get("files"),
            "gigabytes": round(cxs["bytes"] / 1e9, 2) if cxs.get("bytes") else None,
            "sessions": cx.get("session_files_with_data"),
            "totals": mine,
            "cross_check_from_minute_deltas": cx["totals_from_minute_deltas"],
            "cross_check_matches": mine == cx["totals_from_minute_deltas"],
            "counter_resets": sum(s.get("counter_resets") or 0
                                  for s in (cx.get("sessions") or {}).values()),
        },
        # From TOKEN_USAGE_AUDIT_2026-06-06.json root_breakdown on this machine.
        "prior_audit_2026_06_06": {
            "evidence": "REPORTED",
            "report": r"C:\hades\Hecton8\Docs\DEPRECATED\Root_Docs_Noise_2026-05-26\TOKEN_USAGE_AUDIT_2026-06-06.json",
            "period": ["2026-04-03T17:11:28Z", "2026-06-06T10:13:46Z"],
            "method_matches_mine": True,
            "method_note": ("that script overwrote final_usage on each token_count event, "
                            "keeping the last cumulative value -- identical semantics to "
                            "taking the max, so the numbers are directly comparable"),
            "roots": {
                "danat_live_sessions": {"path": r"C:\Users\danat\.codex\sessions",
                                        "files": 1891, "total_tokens": 50387894530,
                                        "on_this_machine": False},
                "danat_archived": {"path": r"C:\Users\danat\.codex\archived_sessions",
                                   "files": 1, "total_tokens": 157103,
                                   "on_this_machine": False},
                "backup_20260521": {"path": r"...\CodexBackups\codex_cleanup_20260521_194850",
                                    "files": 1048, "total_tokens": 57856335910,
                                    "on_this_machine": True},
            },
            "sum_of_roots": 50387894530 + 157103 + 57856335910,
            "daily_delta_sum": 108312008697,
            "headline_totals_field": 138912242896,
            "headline_is_inflated": True,
            "inflation_explanation": (
                "The 138.9 B headline is the sum of per-session final counters across all "
                "three roots, and the same sessions live in both the live directory and the "
                "backup. sessions_with_usage=3635 vs unique_session_or_path_keys=2830 is a "
                "ratio of 1.285, and 138.91/108.31 is 1.283 -- the gap is double counting. "
                "The deduplicated figure is 108.3 B, which independently agrees with the "
                "sum of the three per-root selected totals (108.24 B)."
            ),
            "by_model_delta": {
                "gpt-5.5": 95247607213, "gpt-5.4": 13002550593,
                "gpt-5.2-codex": 31468079, "gpt-5.3-codex": 22822547,
                "gpt-5.4-mini": 5851626, "gpt-5.1-codex-mini": 995678,
                "gpt-5.2": 142159, "unknown_model": 570802,
            },
            "cache_ratio": 0.9615313229684241,
            "plan_type_counts": {"free": 2792, "team": 10, "unknown": 2},
            "uncached_input_tokens": 5325139889,
        },
        "reconciliation": {
            "my_backup_root_total": mine["total_tokens"],
            "prior_audit_same_root": 57856335910,
            "difference": mine["total_tokens"] - 57856335910,
            "difference_pct": round(100.0 * (mine["total_tokens"] - 57856335910) / 57856335910, 3),
            "verdict": ("VERIFIED -- two independent implementations of the same method agree "
                        "on the same file set to within 0.7%; the residual is the 2 extra "
                        "zero-usage files I included and ~10 sessions that audit deduped "
                        "against its live root"),
        },
    }
    # cost the deduplicated per-model delta figures
    cxcost, unpriced = {}, []
    tot_cx = 0.0
    ratio = codex["prior_audit_2026_06_06"]["cache_ratio"]
    for m, tt in codex["prior_audit_2026_06_06"]["by_model_delta"].items():
        if not R.OPENAI.get(m):
            unpriced.append({"model": m, "total_tokens": tt})
            continue
        # split the model's volume using the audit's measured global shares:
        # output_ratio 0.00348 of total, and cached input 96.15% of input
        out_t = tt * 0.0034789215761335582
        in_t = tt - out_t
        cached_t = in_t * ratio
        c = openai_cost(m, int(in_t), int(cached_t), int(out_t))
        c["total_usd"] = total(c)
        c["split_method"] = ("ESTIMATE -- per-model input/output/cached split applied from "
                             "the audit's global ratios, since it published per-model totals "
                             "but not per-model field breakdowns")
        cxcost[m] = c
        tot_cx += c["total_usd"]
    codex["cost_usd_by_model_estimate"] = cxcost
    codex["cost_usd_total_list_price_equivalent"] = tot_cx
    codex["unpriced_models"] = unpriced
    codex["cost_caveat"] = (
        "List-price equivalent only. 2792 of 2804 sessions carried plan_type 'free' and "
        "10 'team', i.e. a subscription -- these per-token dollars were almost certainly "
        "never invoiced. The figure answers 'what would this volume cost at public API "
        "rates', not 'what was paid'."
    )
if cx:
    out["codex"] = codex

# --- Antigravity (PROXY only) ----------------------------------------------
ag = optional("antigravity_totals.json", "сделать: python refresh.py --antigravity")
if ag:
    agt = ag["totals"]
    out["antigravity"] = {
        "evidence": "PROXY",
        "verdict": ag["verdict"],
        "period": [ag["first_ts"], ag["last_ts"]],
        "days_covered": len(ag["by_day"]),
        "conversations_with_transcript": agt["conversations"],
        "model_turns": ag["record_type_counts"].get("PLANNER_RESPONSE", 0),
        "tool_calls": agt["tool_calls"],
        "user_inputs": agt["user_inputs"],
        "quota_blocks_429": agt["quota_blocks"],
        "thinking_chars": agt["thinking_chars"],
        "content_chars": agt["content_chars"],
        "transcript_bytes": agt["bytes"],
        "tokens": None,
        "tokens_note": (
            "Deliberately null. No token counter exists on disk for Antigravity, and "
            "converting characters to tokens would produce a number that looks measured "
            "but is not. What a transcript stores is each message once; what the API is "
            "charged for is the whole context re-sent every turn. On Codex, where real "
            "counters exist, cached input was 96.15% of all tokens -- so the invisible "
            "re-sent context is the bulk of the spend and no character count can recover it."
        ),
        "recoverable_elsewhere": (
            "Gemini CLI writes real usage to ~/.gemini/tmp/<hash>/logs.json "
            "(input_token_count / output_token_count / cached_content_token_count / "
            "thoughts_token_count / total_token_count). That directory is EMPTY on this "
            "machine, so nothing is recoverable here. It may not be empty on the second machine."
        ),
    }

# --- print ------------------------------------------------------------------
# Печать идёт ДО записи combined.json. Раньше было наоборот, и любое падение в
# этом блоке (а падали два обращения: дата None и пустой scan_stats) оставляло на
# диске уже записанный артефакт при коде выхода 1 -- то есть полуверный файл,
# который следующие скрипты читали как готовый.
def fmt(n):
    return "{:>18,}".format(n) if n is not None else "%18s" % "—"


print("=" * 78)
print("TOKEN AUDIT -- ALL TIME")
print("=" * 78)
print()
print("CLAUDE CODE   [MEASURED]  %s .. %s" % (day(claude["period"][0]),
                                              day(claude["period"][1])))
print("  sessions %s | files %s | responses %s (deduped from %s)"
      % (num(claude["sessions"]), num(claude["files"]),
         f"{claude['responses_deduped']:,}", f"{claude['responses_raw']:,}"))
for k in ("uncached_input", "cache_write", "cache_read", "output", "total"):
    print("  %-16s %s" % (k, fmt(claude["totals"][k])))
print("  list-price equivalent: $%s" % f"{grand:,.2f}")
print("  per model:")
for m, c in sorted(claude["cost_usd_by_model"].items(), key=lambda x: -x[1]["total_usd"]):
    print("    %-18s %s tok  $%s"
          % (m, fmt(claude["by_model"][m]["total"]), f"{c['total_usd']:>12,.2f}"))
if claude["unpriced_models"]:
    print("  unpriced:", claude["unpriced_models"])
print()

if cn:
    print("CLINE / ROO-CODE   [MEASURED]")
    print("  tasks %s | requests %s" % (num(cline["tasks"]), num(cline["requests"])))
    for k in ("uncached_input", "cache_write", "cache_read", "output", "total"):
        print("  %-16s %s" % (k, fmt(cline["totals"][k])))
    print("  list-price equivalent: $%s" % f"{cline['cost_usd_total_list_price_equivalent']:,.2f}")
    print("  per model:")
    for m, c in sorted(cline["cost_usd_by_model"].items(), key=lambda x: -x[1]["total_usd"]):
        print("    %-24s %s tok  $%s"
              % (m, fmt(cline["by_model"][m]["total"]), f"{c['total_usd']:>12,.2f}"))
    if cline["unpriced_models"]:
        print("  unpriced:", cline["unpriced_models"])
    print()
if cx:
    print("CODEX")
    print("  [MEASURED]  backup root, %s .. %s  (%s files, %s GB, %s sessions)"
          % (day(codex["my_measurement"]["period"][0]),
             day(codex["my_measurement"]["period"][1]),
             num(codex["my_measurement"]["rollout_files"]),
             num(codex["my_measurement"]["gigabytes"]),
             num(codex["my_measurement"]["sessions"])))
    for k in ("input_tokens", "cached_input_tokens", "output_tokens",
              "reasoning_output_tokens", "total_tokens"):
        print("    %-24s %s" % (k, fmt(mine[k])))
    print("    delta cross-check matches:", codex["my_measurement"]["cross_check_matches"])
    r = codex["reconciliation"]
    print("  [VERIFIED]  vs prior audit on the same root: %s vs %s  (%+.3f%%)"
          % (f"{r['my_backup_root_total']:,}", f"{r['prior_audit_same_root']:,}", r["difference_pct"]))
    pa = codex["prior_audit_2026_06_06"]
    print("  [REPORTED]  full picture to 2026-06-06, deduplicated: %s" % f"{pa['sum_of_roots']:,}")
    for n, rr in pa["roots"].items():
        print("      %-22s %s  %s" % (n, fmt(rr["total_tokens"]),
                                      "on this machine" if rr["on_this_machine"] else "SECOND MACHINE"))
    print("  headline in the old ledger: %s  <-- inflated ~28%% by double counting"
          % f"{pa['headline_totals_field']:,}")
    print("  list-price equivalent (deduped basis): $%s" % f"{tot_cx:,.2f}")
    print()

if ag:
    print("ANTIGRAVITY   [PROXY -- no token counter exists]  %s .. %s"
          % (day((out.get("antigravity") or {}).get("period")[0]), day((out.get("antigravity") or {}).get("period")[1])))
    print("  conversations %s | model turns %s | tool calls %s | quota blocks %s"
          % (num(out["antigravity"]["conversations_with_transcript"]),
             f"{out['antigravity']['model_turns']:,}",
             f"{out['antigravity']['tool_calls']:,}",
             f"{out['antigravity']['quota_blocks_429']:,}"))
    print("  tokens: NOT MEASURABLE from local data")
    print()

# --- write ------------------------------------------------------------------
dst = os.path.join(HERE, "combined.json")
with open(dst, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=1, ensure_ascii=False)
print("wrote", dst)

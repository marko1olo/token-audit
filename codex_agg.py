#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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

ЭТО ВТОРОЙ, НЕЗАВИСИМЫЙ ПРОХОД, А НЕ ОСНОВНОЕ ИЗМЕРЕНИЕ. Максимум по файлу
теряет расход всех цепочек, кроме самой большой (почему -- в codex_agg_chains.py),
и живёт ровно как сверка: cost_model.py печатает его как VERIFIED-границу рядом с
chain-split. Метод здесь намеренно НЕ меняется.

КОРНИ ИЗ tokenaudit_config, А НЕ ИЗ ЛИТЕРАЛА. Здесь стояли два зашитых пути;
на любой другой машине они дают пустой скан, а пустой скан, записанный в
артефакт, -- это ноль, выданный за измерение. codex_roots() заодно раскрывает
archived_sessions/ (куда `codex archive` уносит сессии) и уважает $CODEX_HOME.
Пустой результат -> EXIT_NO_ROOT ДО любой записи.

ОКНО СКАНИРОВАНИЯ ЗАГРУЖАЕТСЯ, А НЕ СНИМАЕТСЯ ЗАНОВО. refresh.py --codex
запускает сначала codex_agg_chains.py, потом этот скрипт: те же 10 ГБ читаются
дважды, и без общего окна два артефакта Codex описывали бы разный вход, потому
что файлы дописываются, пока их читают. Манифест -- scan_manifest_codex.json,
снимает его codex_agg_chains. Логика окна и дедупликации имён импортируется
оттуда же: копия этой логики разошлась бы с первым проходом ровно так, как
расходились сами артефакты.

ВЕСЬ КОНВЕЙЕР ЖИВЁТ В main(). `import codex_agg` не сканирует 10 ГБ и не
перезаписывает codex_totals.json.
"""
import argparse
import json
import os
import sys
from collections import defaultdict

import codex_agg_chains as chains
import tokenaudit_config as cfg

HERE = os.path.dirname(os.path.abspath(__file__))
DST = os.path.join(HERE, "codex_totals.json")
ARTIFACT = os.path.basename(DST)

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
          "reasoning_output_tokens", "total_tokens")


def resolve_roots(cli=None):
    """Корни rollout-файлов Codex. Пустой список -> RootError, а не ноль."""
    return cfg.require(cfg.codex_roots(cli), "Codex", "codex", artifact=ARTIFACT)


def scan(window):
    """Прочитать окно и посчитать максимум по файлу плюс минутные приросты.

    Ничего не печатает кроме прогресса и ничего не пишет на диск.
    -> dict для build()
    """
    stats = defaultdict(int)
    # Счётчики-доказательства обязаны быть в артефакте даже нулевыми.
    for k in ("files", "bytes", "lines", "bad_lines", "unreadable_files",
              "files_without_token_data", "files_without_session_id",
              "duplicate_rollout_files_skipped"):
        stats[k] = 0
    stats["files"] = len(window.sizes)
    stats["bytes"] = sum(window.sizes.values())

    sessions = {}          # key -> per-file session record
    minute = defaultdict(lambda: defaultdict(int))   # "YYYY-MM-DDTHH:MM" -> field -> delta
    by_model = defaultdict(lambda: defaultdict(int))         # model -> field -> delta
    by_model_day = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    plan_types = defaultdict(int)
    models = defaultdict(int)
    cwds = defaultdict(int)
    cli_versions = defaultdict(int)
    sid_files = defaultdict(list)

    files = window.files()
    for i, (path, rel, root) in enumerate(files):
        if i % 50 == 0:
            print("  [%d/%d] %s" % (i, len(files), os.path.basename(path)[:60]),
                  flush=True)
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
            # try вокруг ЦИКЛА, а не вокруг вызова: open() случается на первой
            # итерации генератора окна, поэтому try вокруг вызова не поймал бы
            # ничего, а недоступный файл ронял бы весь прогон.
            for line in window.lines(path, rel, root):
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
        except OSError:
            stats["unreadable_files"] += 1
            continue

        if events == 0:
            stats["files_without_token_data"] += 1
            continue
        # Идентификатор сессии СТРОГО из session_meta. Откат к имени файла
        # убран: он давал каждому файлу без метаданных уникальный ключ, поэтому
        # distinct_session_ids и session_ids_in_multiple_files выглядели
        # безупречно ровно там, где данных для такого вывода не было. Итог
        # файла при этом остаётся в измерении -- здесь ключ по ИМЕНИ ФАЙЛА и
        # метод «максимум по файлу», -- меняется только честность отчёта о
        # сессиях.
        sid = (meta or {}).get("id")
        if sid:
            sid_files[sid].append(os.path.basename(path))
        else:
            stats["files_without_session_id"] += 1
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
            "cwd": cfg.redact(cwd) if cwd else cwd,
            "cli_version": cliv,
            "model": session_model,
            "models_in_file": dict(file_models),
            "plan_type": session_plan,
            "start": (meta or {}).get("timestamp") or first_ts,
            "first_token_ts": first_ts,
            "last_token_ts": last_ts,
            "events": events,
            "counter_resets": resets,
            "bytes": window.limit(root, rel) or 0,
            **{f: fmax[f] for f in FIELDS},
        }

    return {"stats": stats, "sessions": sessions, "minute": minute,
            "by_model": by_model, "by_model_day": by_model_day,
            "plan_types": plan_types, "models": models, "cwds": cwds,
            "cli_versions": cli_versions, "sid_files": sid_files}


def build(data, window, roots):
    """Собрать содержимое codex_totals.json. Только арифметика, без записи.

    Пути публикуются через cfg.redact(): корни и cwd сессий уезжали в публичный
    репозиторий абсолютными, с именем пользователя внутри.
    -> dict
    """
    stats = data["stats"]
    sessions = data["sessions"]
    minute = data["minute"]
    sid_files = data["sid_files"]

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
    paths = [cfg.redact(r) for r in (window.roots or roots)]

    out = {
        "roots": paths,
        # Окно пишется в артефакт, чтобы проверка целостности могла сравнить его
        # с окном chain-split: два прохода обязаны описывать один вход.
        "scan_window": {"files": len(window.sizes),
                        "bytes": sum(window.sizes.values()),
                        "manifest": chains.MANIFEST_NAME,
                        "captured_by": window.captured_by,
                        "roots": paths},
        "scan_stats": dict(stats),
        "session_files_with_data": len(sessions),
        "distinct_session_ids": len(sid_files),
        "session_ids_in_multiple_files": dupes,
        "totals_max_cumulative": totals,
        "totals_from_minute_deltas": minute_totals,
        "plan_types": dict(data["plan_types"]),
        "models_seen": dict(data["models"]),
        "by_model": {k: dict(v) for k, v in sorted(data["by_model"].items())},
        "by_model_day": {d: {m: dict(f) for m, f in sorted(ms.items())}
                         for d, ms in sorted(data["by_model_day"].items())},
        "cli_versions": dict(data["cli_versions"]),
        # Ключи cwd -- рабочие каталоги, то есть тоже пути. Публикуются
        # редактированными: в артефакте лежал абсолютный домашний каталог.
        "cwd_counts": {cfg.redact(k): v for k, v in
                       sorted(data["cwds"].items(), key=lambda x: -x[1])},
        "by_day": {k: dict(v) for k, v in sorted(day.items())},
        "by_hour": {k: dict(v) for k, v in sorted(hour.items())},
        "by_minute": {k: dict(v) for k, v in sorted(minute.items())},
        "sessions": sessions,
    }
    ts_all = sorted(s["first_token_ts"] for s in sessions.values() if s["first_token_ts"])
    out["first_ts"] = ts_all[0] if ts_all else None
    ts_end = sorted(s["last_token_ts"] for s in sessions.values() if s["last_token_ts"])
    out["last_ts"] = ts_end[-1] if ts_end else None
    return out


def report(out, window, loaded):
    """Печать итога прогона. Сверка минутных приростов -- вычитанием, не делением."""
    stats = out["scan_stats"]
    totals = out["totals_max_cumulative"]
    minute_totals = out["totals_from_minute_deltas"]
    print()
    print("rollout files scanned : %d  (%.2f GB)"
          % (stats["files"], stats["bytes"] / 1e9))
    print("lines read            : {:,}".format(stats["lines"]))
    print("files w/o token data  :", stats["files_without_token_data"])
    print("bad json lines        :", stats["bad_lines"])
    print("session files w/ data :", len(out["sessions"]))
    print("distinct session ids  :", out["distinct_session_ids"],
          " (ids in >1 file: %d)" % len(out["session_ids_in_multiple_files"]))
    if stats["files_without_session_id"]:
        print("файлов без session_meta:", stats["files_without_session_id"],
              "— в отчёте о сессиях не учтены, ключ строго по session_id")
    print("counter resets seen   :", sum(s["counter_resets"] for s in out["sessions"].values()))
    print("date range            :", out["first_ts"], "->", out["last_ts"])
    print("plan types            :", out["plan_types"])
    print("scan window           : %s (%s)"
          % (window.describe(),
             "из манифеста %s" % chains.MANIFEST_NAME if loaded
             else "снято этим прогоном"))
    print()
    print("=== TOTALS (max cumulative per file) ===")
    for f in FIELDS:
        print("  {:<24}: {:>15,}".format(f, totals[f]))
    print()
    print("=== CROSS-CHECK (sum of minute deltas) ===")
    for f in FIELDS:
        d = minute_totals[f] - totals[f]
        print("  {:<24}: {:>15,}   delta vs above: {:+,}".format(f, minute_totals[f], d))


def main(argv=None):
    """Замер и запись codex_totals.json.

    -> код возврата: 0 успех, cfg.EXIT_NO_ROOT если корня нет или в окне нет ни
       одного rollout-файла
    """
    cfg.stdout_utf8()          # до любого print: иначе кириллица умирает в pipe
    ap = cfg.add_path_args(argparse.ArgumentParser(
        description="агрегат расхода токенов Codex методом «максимум по файлу»"))
    args = ap.parse_args(argv)
    cli = cfg.apply_args(args)
    if getattr(args, "print_roots", False):
        return cfg.print_roots(cli)

    try:
        roots = resolve_roots(cli.get("codex"))
    except cfg.RootError as e:
        sys.stderr.write("%s\n" % e)
        return cfg.EXIT_NO_ROOT

    window, dups, loaded = chains.load_window(roots, captured_by="codex_agg")
    print(cfg.describe("Codex", roots, len(window.sizes), cfg.source_of("codex")))
    if not loaded:
        print("окно: манифеста %s нет, снято своё" % chains.MANIFEST_NAME)
    elif [str(r) for r in (window.roots or [])] != [str(r) for r in roots]:
        # Окно от другого набора корней -- артефакты будут описывать разный вход.
        # Не роняем прогон: корни записаны в оба артефакта, и расхождение видно
        # по полю scan_window, а не по молчанию.
        sys.stderr.write("ВНИМАНИЕ: корни в %s не совпадают с текущими: %s против %s\n"
                         % (chains.MANIFEST_NAME,
                            "; ".join(cfg.redact(r) for r in (window.roots or [])),
                            "; ".join(cfg.redact(r) for r in roots)))
    if dups:
        print("дубли по имени файла: %d, оставлен первый корень по списку" % len(dups))

    # ПУСТОЙ СКАН -- ОТКАЗ ДО ЗАПИСИ, иначе артефакт заменяется нулями.
    if not window.sizes and not cfg.allow_empty():
        sys.stderr.write(chains.no_files_error(roots, ARTIFACT) + "\n")
        return cfg.EXIT_NO_ROOT

    data = scan(window)
    data["stats"]["duplicate_rollout_files_skipped"] = len(dups)
    out = build(data, window, roots)

    with open(DST, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)

    report(out, window, loaded)
    print()
    print("wrote", cfg.redact(DST))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

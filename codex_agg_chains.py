#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
total_tokens == model_context_window. That is not spend. Проверено по данным:
на всём корпусе таких записей 24, и во ВСЕХ 24 нулевые не только input и output,
но также cached_input_tokens и reasoning_output_tokens, а total_tokens равен либо
0, либо ровно model_context_window (258 400). То есть пропуск не выбрасывает ни
одного измеримого токена. Настоящий ход с нулевым накопительным вводом
невозможен: счётчик внутри цепочки монотонный, и после любого расхода input уже
не ноль. Счётчик placeholders_with_other_fields в артефакте -- сигнализация:
станет ненулевым, значит допущение сломалось на новых данных, и условие надо
пересматривать по реальному случаю, а не по догадке.

КОРНИ, ОКНО И ЗАЩИТА ОТ ЗАПИСИ НУЛЕЙ
------------------------------------
Здесь стояли ДВА ЗАШИТЫХ ЛИТЕРАЛА пути. На машине, где их нет, обход давал
files == [], и скрипт СНАЧАЛА писал codex_chains_totals.json из одних нулей, и
только потом падал на делении на ноль. Отслеживаемый артефакт в 6,5 МБ
превращался в 1,2 КБ нулей (проверено: sha256 менялся, размер падал с 6 525 505
до 1 190 байт), после чего КАЖДЫЙ следующий прогон -- даже без --codex -- умирал
в report_gen на `KeyError: 'total_tokens'`: `totals_naive_max_per_file`
становился пустым словарём `{}`, а проверка `if not c.ch` его не отсекала, потому
что словарь пустых словарей истинный. Репозиторий переставал работать до
`git checkout codex_chains_totals.json`.

Поэтому:
  * корни приходят из tokenaudit_config.codex_roots() + require() -- ноль никогда
    не становится измерением;
  * ПУСТОЙ СКАН -- ЭТО ВЫХОД EXIT_NO_ROOT ДО ЛЮБОЙ ЗАПИСИ. Артефакт не
    открывается на запись вообще, пока не известно, что найден хотя бы один
    rollout-файл. Осознанно записать нули можно только флагом --allow-empty;
  * все процентные сравнения идут через pct(), который на нулевом знаменателе
    отдаёт None, а не ZeroDivisionError.

archived_sessions СЧИТАЕТСЯ. `codex archive` уносит сессии в
archived_sessions/YYYY/MM/DD/, и до этой правки их не смотрел никто, хотя
cost_model.py на такой каталог ссылается. codex_roots() раскрывает и sessions/,
и archived_sessions/. Раз корни теперь могут ПЕРЕКРЫВАТЬСЯ, одно и то же имя
rollout-файла обязано попасть в измерение РОВНО ОДИН РАЗ: перекрытие корней уже
надувало итог Codex на 28%. Дедупликация по имени файла делается явно в
select_rollouts(), приоритет -- порядок корней, то есть sessions/ раньше
archived_sessions/.

ОКНО СКАНИРОВАНИЯ ОБЩЕЕ НА ДВА ПРОХОДА. refresh.py --codex запускает и этот
скрипт, и codex_agg.py, то есть те же 10 ГБ читаются дважды. Без общего окна два
артефакта Codex описывали бы РАЗНЫЙ вход (файлы дописываются, пока их читают) --
ровно тот дефект, против которого существует tokenaudit_scan. Этот проход
СНИМАЕТ окно в scan_manifest_codex.json, codex_agg.py его ЗАГРУЖАЕТ, и оба
пишут его размеры в свой артефакт, чтобы проверка целостности могла их сравнить.

ВЕСЬ КОНВЕЙЕР ЖИВЁТ В main(). `import codex_agg_chains` не сканирует 10 ГБ и не
трогает артефакт; codex_agg.py импортирует отсюда окно и манифест, а не копию их
логики.
"""
import argparse
import json
import os
import sys
from collections import defaultdict

import tokenaudit_config as cfg
import tokenaudit_scan

HERE = os.path.dirname(os.path.abspath(__file__))
DST = os.path.join(HERE, "codex_chains_totals.json")
ARTIFACT = os.path.basename(DST)

# Манифест окна у Codex СВОЙ: scan_manifest.json принадлежит проходам Claude
# (claude_agg -> claude_deep), и один файл на два инструмента означал бы, что
# каждый прогон затирает окно другого.
MANIFEST_NAME = "scan_manifest_codex.json"

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens",
          "reasoning_output_tokens", "total_tokens")

# Имя файла, а не путь: так Codex называет транскрипты рабочих сессий. Всё
# остальное в sessions/ (history.jsonl и прочее) расходом не является.
PREFIX = "rollout-"
SUFFIX = ".jsonl"


def resolve_roots(cli=None):
    """Корни rollout-файлов Codex. Пустой список -> RootError, а не ноль.

    cli -- значение --codex-root (список или None). codex_roots() сама
    раскрывает sessions/ и archived_sessions/ и уважает $CODEX_HOME.
    -> list[str] существующих каталогов
    """
    return cfg.require(cfg.codex_roots(cli), "Codex", "codex", artifact=ARTIFACT)


def select_rollouts(sizes, roots):
    """Оставить только rollout-*.jsonl и каждое ИМЯ ФАЙЛА ровно один раз.

    sizes -- словарь окна {"корень|относительный путь": байты}, roots -- корни в
    порядке приоритета. Победитель для повторяющегося имени -- первый корень по
    списку, то есть sessions/ раньше archived_sessions/.

    Дедупликация именно по имени файла, а не по относительному пути: в имени
    rollout-файла лежит полный uuid сессии, поэтому имя различает сессии само по
    себе, а вот каталоги отличаются -- `codex archive` кладёт файл в
    archived_sessions/YYYY/MM/DD, бэкапы вообще хранят своё дерево. Сравнение
    относительных путей пропустило бы такой дубль и посчитало бы сессию дважды.
    -> (kept, dups): kept -- отфильтрованный словарь размеров,
       dups -- список (проигравший ключ, победивший ключ)
    """
    kept, dups, seen = {}, [], {}
    for root in [str(r) for r in roots]:
        head = root + "|"
        for key in sorted(k for k in sizes if k.startswith(head)):
            rel = key[len(head):]
            name = os.path.basename(rel)
            if not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
                continue
            # На Windows регистр имени не различает файлы, на POSIX различает.
            ident = os.path.normcase(name)
            if ident in seen:
                dups.append((key, seen[ident]))
                continue
            seen[ident] = key
            kept[key] = sizes[key]
    return kept, dups


def capture_window(roots, captured_by):
    """Снять окно сканирования по rollout-файлам корней. Ничего не пишет на диск.

    Окно -- граница в БАЙТАХ: размер каждого файла на момент снятия. Второй
    проход читает те же файлы и те же префиксы, поэтому два артефакта Codex
    описывают один вход по построению, а не по удаче.
    -> (ScanWindow, dups) -- dups как в select_rollouts()
    """
    raw = tokenaudit_scan.ScanWindow.capture(roots, suffix=SUFFIX,
                                             captured_by=captured_by)
    kept, dups = select_rollouts(raw.sizes, roots)
    window = tokenaudit_scan.ScanWindow(sizes=kept,
                                        roots=[str(r) for r in roots],
                                        captured_by=captured_by)
    return window, dups


def load_window(roots, captured_by):
    """Окно от первого прохода, иначе своё. Второй проход обязан читать тот же вход.

    Манифеста нет -- снимаем своё окно и честно об этом печатаем: самостоятельный
    прогон не обязан совпадать с чужим артефактом.
    -> (ScanWindow, dups, loaded): loaded -- True, если окно пришло из манифеста
    """
    window = tokenaudit_scan.ScanWindow.load(
        tokenaudit_scan.manifest_path(MANIFEST_NAME))
    if window is None:
        window, dups = capture_window(roots, captured_by)
        return window, dups, False
    return window, [], True


def no_files_error(roots, artifact=ARTIFACT):
    """Текст отказа, когда корни есть, а rollout-файлов в них нет.

    Отдельно от cfg.require(): там случай «корня нет», здесь «корень есть, но
    пуст». Оба обязаны закончиться EXIT_NO_ROOT и НИ ОДНОЙ записи в артефакт.
    artifact -- имя артефакта, который остался нетронутым (у второго прохода своё).
    -> str
    """
    lines = ["Codex: rollout-файлов не найдено"]
    for i, r in enumerate(roots or []):
        head = "  искал   : " if i == 0 else " " * 12
        lines.append("%s%s (%s*%s)" % (head, cfg.redact(r), PREFIX, SUFFIX))
    if not roots:
        lines.append("  искал   : ни одного корня")
    lines.append("  задать  : --codex-root ПУТЬ")
    lines.append(" " * 12 + "%s=ПУТЬ (разделитель '%s')"
                 % (cfg.ENV_CODEX, os.pathsep))
    lines.append(" " * 12 + '%s: {"codex_roots": ["ПУТЬ"]}' % cfg.CONFIG_NAME)
    lines.append("  ноль не записан: %s не тронут" % artifact)
    lines.append("  осознанно пустой прогон: --allow-empty")
    return "\n".join(lines)


def pct(a, b):
    """Разница a против b в процентах. b == 0 -> None: делить не на что.

    Прямое деление стояло в печати итога и роняло прогон ZeroDivisionError уже
    ПОСЛЕ записи артефакта -- то есть нули оставались на диске, а причина
    выглядела как ошибка арифметики, а не как пустой скан.
    -> float | None
    """
    if not b:
        return None
    return 100.0 * (a - b) / b


def fpct(value):
    """Процент для печати. None -> '—' (знаменатель нулевой). -> str"""
    return "—" if value is None else "%+.3f%%" % value


def scan(window):
    """Прочитать окно и разложить расход по цепочкам. Ничего не печатает и не пишет.

    -> dict со всем, что нужно build(): sessions, minute, by_model, naive_max,
       naive_delta, chain_hist, stats
    """
    stats = defaultdict(int)
    # Ключи, которые обязаны быть в артефакте даже нулевыми: это счётчики-
    # доказательства, а появляющийся только при поломке ключ доказывает нулём
    # ровно ничего.
    for k in ("files", "bytes", "lines", "bad_json_lines", "unreadable",
              "files_without_token_data", "files_without_session_id",
              "dup_session_files_dropped", "duplicate_rollout_files_skipped",
              "placeholders_with_other_fields"):
        stats[k] = 0
    stats["files"] = len(window.sizes)
    stats["bytes"] = sum(window.sizes.values())

    minute = defaultdict(lambda: defaultdict(int))
    by_model = defaultdict(lambda: defaultdict(int))
    sessions = {}
    naive_max = defaultdict(int)
    naive_delta = defaultdict(int)
    chain_hist = defaultdict(int)

    files = window.files()
    for i, (path, rel, root) in enumerate(files):
        if i % 100 == 0:
            print("  [%d/%d]" % (i, len(files)), flush=True)
        meta = None
        cur_model = None
        chains = []          # list of dicts: {"head": int, "vals": {field: int}}
        events = 0
        placeholders = 0
        ph_other = 0         # плейсхолдеры с непустыми cached/reasoning: сигнализация
        ooo = 0              # out-of-order events (assigned to a non-last chain)
        # naive comparisons, computed on the same pass
        n_max = dict.fromkeys(FIELDS, 0)
        n_prev = None
        n_delta = dict.fromkeys(FIELDS, 0)
        first_ts = last_ts = None
        plan = None
        try:
            # Границу читает окно: window.lines() отдаёт строки в пределах
            # запомненного размера файла. try стоит вокруг ЦИКЛА, а не вокруг
            # вызова: open() происходит на первой итерации генератора, поэтому
            # try вокруг вызова не поймал бы ничего, а один недоступный файл
            # ронял бы весь прогон.
            for line in window.lines(path, rel, root):
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
                if not isinstance(d, dict):
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
                if not isinstance(info, dict):
                    continue
                cum = info.get("total_token_usage")
                if not isinstance(cum, dict):
                    continue
                v = {f: (cum.get(f) or 0) for f in FIELDS}
                # skip post-compaction placeholders
                if v["input_tokens"] == 0 and v["output_tokens"] == 0:
                    placeholders += 1
                    if v["cached_input_tokens"] or v["reasoning_output_tokens"]:
                        # Ни одного такого случая на 10 ГБ (24 плейсхолдера, все
                        # нулевые по всем полям). Появится -- значит пропуск
                        # начал терять расход, и это видно по счётчику.
                        ph_other += 1
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
        except OSError:
            # Файл заблокирован, исчез или недоступен: считаем непрочитанным и
            # идём дальше, а не роняем измерение целиком.
            stats["unreadable"] += 1
            continue

        stats["placeholders_with_other_fields"] += ph_other
        if events == 0:
            stats["files_without_token_data"] += 1
            continue
        sid = (meta or {}).get("id")
        if not sid:
            # Ключ СТРОГО по session_id, отката к имени файла НЕТ. Ранняя версия
            # ключевалась на `sid or имя файла`, и один и тот же сеанс, попавший
            # в два файла (в одном session_meta есть, в другом нет), проходил
            # мимо дедупликации: перебор 0.645% при честном на вид
            # "session_ids_in_multiple_files: 0". Файл без session_meta -- это
            # обрезанный или битый транскрипт, и он попадает в отдельный
            # счётчик, а не в итог под выдуманным ключом.
            stats["files_without_session_id"] += 1
            continue
        chain_hist[len(chains)] += 1
        tot = {f: sum(c["vals"][f] for c in chains) for f in FIELDS}
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
            "placeholders_with_other_fields": ph_other,
            "chains": len(chains),
            "out_of_order_events": ooo,
            "chains_total": tot,
            "naive_max": dict(n_max),
            "naive_delta": dict(n_delta),
        }
        # dedupe strictly by session_id: keep the record with the larger total
        if sid in sessions and sessions[sid]["chains_total"]["total_tokens"] >= tot["total_tokens"]:
            stats["dup_session_files_dropped"] += 1
            continue
        if sid in sessions:
            stats["dup_session_files_dropped"] += 1
        sessions[sid] = rec
        for f in FIELDS:
            naive_max[f] += n_max[f]
            naive_delta[f] += n_delta[f]

    return {"sessions": sessions, "minute": minute, "by_model": by_model,
            "naive_max": naive_max, "naive_delta": naive_delta,
            "chain_hist": chain_hist, "stats": stats}


def build(data, window, roots):
    """Собрать содержимое codex_chains_totals.json. Только арифметика, без записи.

    Корни публикуются через cfg.redact(): в артефакте вместо
    'C:\\Users\\Admin\\...' стоит '~/...'. Раньше в публичный репозиторий уезжали
    два абсолютных пути с именем пользователя внутри.
    -> dict
    """
    sessions = data["sessions"]
    minute = data["minute"]
    stats = data["stats"]
    naive_max = data["naive_max"]
    naive_delta = data["naive_delta"]

    chains_total = {f: sum(s["chains_total"][f] for s in sessions.values())
                    for f in FIELDS}
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
    paths = [cfg.redact(r) for r in (window.roots or roots)]

    return {
        "method": "CHAIN_SPLIT",
        "method_note": (
            "One rollout file can hold several interleaved cumulative counters "
            "(concurrent threads/subagents, no thread id in the record). Events are "
            "assigned to the chain they continue; the total is the sum of each "
            "chain's final value. Post-compaction placeholder records "
            "(input_tokens==0 and output_tokens==0) are skipped. Sessions are keyed "
            "strictly by session_id, never by filename; the same rollout filename "
            "seen under two roots (sessions/ and archived_sessions/) is counted once."
        ),
        "roots": paths,
        # Окно пишется в артефакт: без него нельзя проверить, что оба прохода
        # Codex описывают один и тот же вход.
        "scan_window": {"files": len(window.sizes),
                        "bytes": sum(window.sizes.values()),
                        "manifest": MANIFEST_NAME,
                        "captured_by": window.captured_by,
                        "roots": paths},
        "scan_stats": dict(stats),
        "sessions": len(sessions),
        "sessions_with_multiple_chains": multi,
        "chain_count_histogram": dict(sorted(data["chain_hist"].items())),
        "out_of_order_events_total": sum(s["out_of_order_events"] for s in sessions.values()),
        "placeholders_skipped_total": sum(s["placeholders_skipped"] for s in sessions.values()),
        "placeholders_with_other_fields_total": stats["placeholders_with_other_fields"],
        "first_ts": ts_all[0] if ts_all else None,
        "last_ts": ts_end[-1] if ts_end else None,
        "totals_chain_split": chains_total,
        "totals_from_minute_increments": minute_total,
        "totals_naive_max_per_file": {f: naive_max[f] for f in FIELDS},
        "totals_naive_delta_per_file": {f: naive_delta[f] for f in FIELDS},
        "by_model": {k: dict(v) for k, v in sorted(data["by_model"].items())},
        "by_day": roll(10),
        "by_hour": roll(13),
        "by_minute": {k: dict(v) for k, v in sorted(minute.items())},
        "sessions_detail": sessions,
    }


def report(out, window, dups):
    """Печать итога прогона. Все процентные сравнения через pct()."""
    stats = out["scan_stats"]
    chains_total = out["totals_chain_split"]
    naive_max = out["totals_naive_max_per_file"]
    naive_delta = out["totals_naive_delta_per_file"]
    print()
    print("files %d | sessions %d | multi-chain sessions %d"
          % (stats["files"], out["sessions"], out["sessions_with_multiple_chains"]))
    if dups:
        print("одинаковых имён rollout под разными корнями: %d — посчитаны один раз"
              % len(dups))
    if stats["files_without_session_id"]:
        print("файлов без session_meta: %d — в итог не вошли, ключ строго по session_id"
              % stats["files_without_session_id"])
    if stats["dup_session_files_dropped"]:
        print("файлов с уже виденным session_id: %d — оставлен больший итог"
              % stats["dup_session_files_dropped"])
    print("chain-count histogram:", out["chain_count_histogram"])
    print("out-of-order events:", out["out_of_order_events_total"])
    print("placeholders skipped:", out["placeholders_skipped_total"],
          "(с непустыми cached/reasoning: %d)"
          % out["placeholders_with_other_fields_total"])
    print("date range:", out["first_ts"], "->", out["last_ts"])
    print()
    w = 26
    print("%-*s %18s %18s %18s" % (w, "field", "CHAIN-SPLIT", "naive max", "naive delta"))
    for f in FIELDS:
        print("%-*s %18s %18s %18s" % (w, f, "{:,}".format(chains_total[f]),
                                       "{:,}".format(naive_max[f]),
                                       "{:,}".format(naive_delta[f])))
    print()
    print("chain-split vs naive max   : %s"
          % fpct(pct(chains_total["total_tokens"], naive_max["total_tokens"])))
    print("chain-split vs naive delta : %s"
          % fpct(pct(chains_total["total_tokens"], naive_delta["total_tokens"])))
    print("minute-increment cross-check matches chain-split:",
          out["totals_from_minute_increments"] == chains_total)
    print("scan window          :", window.describe())


def main(argv=None):
    """Замер, запись codex_chains_totals.json и scan_manifest_codex.json.

    -> код возврата: 0 успех, cfg.EXIT_NO_ROOT если корня нет или в корнях нет
       ни одного rollout-файла
    """
    cfg.stdout_utf8()          # до любого print: иначе кириллица умирает в pipe
    ap = cfg.add_path_args(argparse.ArgumentParser(
        description="агрегат расхода токенов Codex методом chain-split"))
    args = ap.parse_args(argv)
    cli = cfg.apply_args(args)
    if getattr(args, "print_roots", False):
        return cfg.print_roots(cli)

    # Корни -- ПЕРВЫМ делом и до любой записи на диск.
    try:
        roots = resolve_roots(cli.get("codex"))
    except cfg.RootError as e:
        sys.stderr.write("%s\n" % e)
        return cfg.EXIT_NO_ROOT

    window, dups = capture_window(roots, captured_by="codex_agg_chains")
    print(cfg.describe("Codex", roots, len(window.sizes), cfg.source_of("codex")))
    if dups:
        print("дубли по имени файла: %d, оставлен первый корень по списку"
              % len(dups))

    # ПУСТОЙ СКАН -- ОТКАЗ ДО ЗАПИСИ. Ни артефакт, ни манифест не открываются.
    if not window.sizes and not cfg.allow_empty():
        sys.stderr.write(no_files_error(roots) + "\n")
        return cfg.EXIT_NO_ROOT

    data = scan(window)
    data["stats"]["duplicate_rollout_files_skipped"] = len(dups)
    out = build(data, window, roots)

    # Манифест сохраняется вместе с артефактом: codex_agg.py обязан прочитать
    # ровно этот вход, иначе два артефакта Codex несопоставимы.
    window.save(tokenaudit_scan.manifest_path(MANIFEST_NAME))
    with open(DST, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)

    report(out, window, dups)
    print()
    print("wrote", cfg.redact(DST))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

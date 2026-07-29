#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Honest aggregation of Claude Code token usage from local JSONL transcripts.

Counts every assistant record that carries a `message.usage` block.
Dedupes by API message id, because `--resume` / compaction copy prior
assistant messages into new session files (double-count trap).

КОРНИ БЕРУТСЯ ИЗ tokenaudit_config, а не из литерала ~/.claude/projects.
$CLAUDE_CONFIG_DIR -- штатный способ держать несколько аккаунтов, то есть ровно
та ситуация, из-за которой аудит и заводят; зашитый путь считал один аккаунт и
молчал про остальные.

ОТСУТСТВИЕ КОРНЯ -- ЭТО ВЫХОД С КОДОМ EXIT_NO_ROOT, А НЕ АРТЕФАКТ ИЗ НУЛЕЙ.
Раньше пропавший корень давал claude_totals.json со всеми нулями, пустой
scan_stats и код возврата 0: следующий проход (claude_deep.py) падал на
IndexError без единого слова о причине, а склонировавший репозиторий человек
публиковал ноль как измерение.

ВЕСЬ КОНВЕЙЕР ЖИВЁТ В main(). При импорте модуль не сканирует домашний каталог
и не перезаписывает claude_totals.json -- иначе `import claude_agg` в тесте или
в claude_deep.py стирал бы измерение самим фактом импорта.
"""
import argparse
import datetime as _dt
import json
import os
import sys
from collections import defaultdict

import tokenaudit_config as cfg
import tokenaudit_scan

HERE = os.path.dirname(os.path.abspath(__file__))
DST = os.path.join(HERE, "claude_totals.json")
ARTIFACT = os.path.basename(DST)


def resolve_roots(cli=None):
    """Корни транскриптов Claude Code. Пустой список -> RootError, не ноль.

    cli -- значение --claude-root (список или None).
    -> list[str] существующих каталогов
    """
    return cfg.require(cfg.claude_roots(cli), "Claude Code", "claude",
                       artifact=ARTIFACT)


def root_labels(roots):
    """Короткая метка каждого корня. Нужна только когда корней больше одного.

    ОДИН корень -> метка пустая, и ключ проекта остаётся ровно тем же, что был
    (имя каталога внутри корня). Двусмысленности при одном корне не бывает, а
    менять ключи отчёта на пустом месте значит рвать сравнение с предыдущими
    прогонами.
    НЕСКОЛЬКО корней -> метка от каталога аккаунта: '~/.claude/projects' даёт
    'claude', '$CLAUDE_CONFIG_DIR=~/.claude-work' даёт 'claude-work'. Без неё
    ключ 'c--hades' у двух аккаунтов совпадает, и by_project складывает разные
    аккаунты в одну строку, не сообщая об этом.
    Совпавшие метки разводятся номером: две разные метки для двух разных корней
    обязательны, иначе разделения не происходит.
    -> dict {корень: метка}
    """
    items = [str(r) for r in (roots or [])]
    if len(items) < 2:
        return {r: "" for r in items}
    out, used = {}, {}
    for r in items:
        trimmed = r.rstrip("\\/")
        base = os.path.basename(trimmed)
        if base.lower() == "projects":
            # сам каталог projects одинаков у всех аккаунтов, различает родитель
            base = os.path.basename(os.path.dirname(trimmed))
        label = base.lstrip(".") or "root"
        n = used.get(label.lower(), 0) + 1
        used[label.lower()] = n
        out[r] = label if n == 1 else "%s#%d" % (label, n)
    return out


def project_key(rel, root, labels=None):
    """Ключ проекта: первый каталог внутри корня, при нескольких корнях с меткой.

    rel -- путь файла относительно своего корня, root -- сам корень,
    labels -- результат root_labels(). Формат при нескольких корнях
    'метка/проект', при одном -- 'проект'.
    -> str
    """
    project = str(rel).split(os.sep)[0]
    label = (labels or {}).get(str(root)) or ""
    return "%s/%s" % (label, project) if label else project


def collect(window, labels=None):
    """Прочитать окно сканирования и вернуть сырые записи расхода.

    Ничего не печатает и ничего не пишет на диск: это дело main(). Дедупликации
    здесь нет, она отдельным шагом в dedupe().
    -> (rows, meta), meta -- {'stats', 'bad_lines', 'assistant_without_usage',
       'models_seen', 'synthetic'}
    """
    rows = []
    stats = defaultdict(int)
    bad_lines = 0
    no_usage_assistant = 0
    models_seen = set()
    synthetic = 0

    for path, rel, root in window.files():
        project = project_key(rel, root, labels)
        stats["files"] += 1
        try:
            for line in window.lines(path, rel, root):
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
                        # Рабочий каталог сессии. Есть в КАЖДОЙ записи
                        # (проверено: 403 из 403), и это единственный
                        # источник разбивки по реальным проектам: ключ
                        # by_project -- верхний каталог под ~/.claude/projects,
                        # и для всей работы в одном дереве он один и тот же.
                        "cwd": r.get("cwd") or "",
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
        except OSError:
            # Файл заблокирован, исчез или недоступен. Считаем непрочитанным и
            # идём дальше: раньше try стоял вокруг ВЫЗОВА window.lines(), а
            # open() происходит на первой итерации генератора, поэтому счётчик
            # unreadable_files был недостижим, а один недоступный транскрипт
            # ронял весь прогон.
            stats["unreadable_files"] += 1
            continue

    return rows, {"stats": stats, "bad_lines": bad_lines,
                  "assistant_without_usage": no_usage_assistant,
                  "models_seen": models_seen, "synthetic": synthetic}


def dedupe(rows):
    """Одна строка на message.id, представитель -- запись с МАКСИМАЛЬНОЙ суммой.

    One API response is written as several JSONL records: one per content block,
    plus incremental streaming snapshots where output_tokens grows 1 -> final.
    Verified: 13959/13960 duplicated ids are duplicated inside a single file
    (so this is block/stream splitting, not resume-copying), and where copies
    differ, input is constant while output grows. Therefore the correct
    representative per message id is the record with the LARGEST token total,
    i.e. the final complete usage snapshot. Keeping the *first* record would
    undercount output on 2442 ids.
    -> (uniq, dupe_rows, records_missing_id)
    """
    best = {}
    order = []
    noid = 0
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
    return uniq, dupe_rows, noid


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


def build(rows, uniq, dupe_rows, noid, meta, window, roots):
    """Собрать содержимое claude_totals.json. Только арифметика, без записи.

    roots публикуются через cfg.redact(): в артефакт уходит '~/.claude/projects'.
    До этого там лежала строка со смешанными разделителями и именем пользователя
    внутри -- в публичном репозитории.
    -> dict
    """
    stats = meta["stats"]
    paths = [cfg.redact(r) for r in roots]
    out = {
        # строкой, потому что cost_model.py режет это поле как строку;
        # машинно-читаемый список рядом, в source_roots
        "source_root": "; ".join(paths),
        "source_roots": paths,
        "scan_stats": dict(stats),
        "bad_lines": meta["bad_lines"],
        "assistant_without_usage": meta["assistant_without_usage"],
        "synthetic_records": meta["synthetic"],
        "records_raw": len(rows),
        "records_deduped": len(uniq),
        "duplicate_records_dropped": len(dupe_rows),
        "records_missing_id": noid,
        "models_seen": sorted(meta["models_seen"]),
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

    # Разбивка по рабочему каталогу -- то, чем by_project быть не может.
    #
    # Регистр приводится к одному: в записях встречаются и `c:\hades`, и
    # `C:\hades`, это один каталог, а не два. Домашний путь обезличивается через
    # redact(), потому что артефакт публичный.
    g = defaultdict(list)
    for r in uniq:
        w = (r.get("cwd") or "").strip()
        if not w:
            g["не указан"].append(r)
            continue
        key = cfg.redact(os.path.normpath(w))
        if len(key) > 1 and key[1:2] == ":":
            key = key[0].upper() + key[1:]      # диск в верхнем регистре
        g[key].append(r)
    out["by_cwd"] = {k: tot(v) for k, v in
                     sorted(g.items(), key=lambda x: -sum(
                         y["inp"] + y["cc"] + y["cr"] + y["out"] for y in x[1]))}

    # Свёрнутая разбивка: подкаталоги сливаются в свой проект. Без свёртки в
    # by_cwd девяносто с лишним ключей, и почти все -- рабочие копии воркдри
    # вида gigahrush2/.claude/worktrees/..., в которых по десятку ответов.
    # Правило: диск плюс не более двух уровней. Каталог самого инструмента
    # сворачивается в один ключ, иначе он растекается по своим подпапкам.
    def _roll(path):
        if path.startswith("~"):
            return "~ (каталог инструмента)"
        parts = path.replace("/", os.sep).split(os.sep)
        return os.sep.join(parts[:3]) if len(parts) > 3 else path

    g2 = defaultdict(list)
    for r in uniq:
        w = (r.get("cwd") or "").strip()
        key = cfg.redact(os.path.normpath(w)) if w else "не указан"
        if len(key) > 1 and key[1:2] == ":":
            key = key[0].upper() + key[1:]
        g2[_roll(key)].append(r)
    out["by_cwd_rolled"] = {k: tot(v) for k, v in
                            sorted(g2.items(), key=lambda x: -sum(
                                y["inp"] + y["cc"] + y["cr"] + y["out"] for y in x[1]))}

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
    out["scan_window"] = {"files": len(window.sizes),
                          "bytes": sum(window.sizes.values()),
                          "manifest": tokenaudit_scan.MANIFEST_NAME}
    return out


def main(argv=None):
    """Замер, запись claude_totals.json и scan_manifest.json, печать итога.

    -> код возврата: 0 успех, cfg.EXIT_NO_ROOT если корня нет
    """
    cfg.stdout_utf8()          # до любого print: иначе кириллица умирает в pipe
    ap = cfg.add_path_args(argparse.ArgumentParser(
        description="агрегат расхода токенов Claude Code по локальным транскриптам"))
    args = ap.parse_args(argv)
    cli = cfg.apply_args(args)
    if getattr(args, "print_roots", False):
        return cfg.print_roots(cli)

    # Корни -- ПЕРВЫМ делом и до любой записи на диск. SystemExit(текст) дал бы
    # код 1, а это код проверки рендера, поэтому текст идёт в stderr, а наружу
    # уходит именно EXIT_NO_ROOT.
    try:
        roots = resolve_roots(cli.get("claude"))
    except cfg.RootError as e:
        sys.stderr.write("%s\n" % e)
        return cfg.EXIT_NO_ROOT

    # Окно сканирования снимается ДО чтения: размер каждого файла запоминается и
    # читается ровно столько. Второй проход (claude_deep.py) читает те же
    # префиксы тех же файлов, поэтому два артефакта описывают один и тот же вход
    # по построению. Без этого проходы расходились на 138 ответов и 18.6 млн
    # токенов -- транскрипты дописываются, пока их читают. Подробности в
    # tokenaudit_scan.py.
    #
    # Каталог самого инструмента исключается: репозиторий лежит внутри
    # ~/.claude/projects, и без этого он считает собственные выходные файлы.
    window = tokenaudit_scan.ScanWindow.capture(
        roots, suffix=".jsonl", skip_dirs=(HERE,), captured_by="claude_agg")
    # Печатать всегда, и при успехе: молчание про корни -- причина, по которой
    # никто не замечал, что считается один аккаунт из двух.
    print(cfg.describe("Claude Code", roots, len(window.sizes),
                       cfg.source_of("claude")))

    labels = root_labels(window.roots or roots)
    rows, meta = collect(window, labels)
    uniq, dupe_rows, noid = dedupe(rows)
    out = build(rows, uniq, dupe_rows, noid, meta, window, roots)

    window.save()
    print("scan window          :", window.describe())

    with open(DST, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)

    stats = meta["stats"]
    t = out["totals_deduped"]
    tr = out["totals_raw"]
    dup = tot(dupe_rows)
    print("files scanned        :", stats["files"])
    print("lines                :", stats["lines"])
    print("assistant records    :", stats["assistant_records"])
    print("usage records raw    :", len(rows))
    print("usage records uniq   :", len(uniq))
    print("dupes dropped        :", len(dupe_rows),
          "(", dup["inp"] + dup["cc"] + dup["cr"] + dup["out"], "tokens )")
    print("missing message.id   :", noid)
    print("bad json lines       :", meta["bad_lines"])
    print("models               :", sorted(meta["models_seen"]))
    print("date range           :", out["first_ts"], "->", out["last_ts"])
    print("sessions             :", out["session_count"])
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
    # путь через redact(): имя пользователя не должно уезжать даже в лог прогона
    print("wrote", cfg.redact(DST))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

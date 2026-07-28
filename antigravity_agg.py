#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Antigravity volume metrics from local brain transcripts.

VERDICT ESTABLISHED BEFORE WRITING THIS: Antigravity keeps NO token ledger on
disk. A sweep of all 687 transcripts (2.38 GB, run with `rg --hidden` because
they live under a dot-directory) found token-accounting identifiers in only 13
files, and every hit is captured console output of a key-validation script the
agent itself ran -- not per-generation accounting.

Пересверено позже и независимо: полей-счётчиков 0 в 693 транскриптах при рабочем
контрольном случае (PLANNER_RESPONSE нашёлся в 691 из 693, то есть искали там,
где данные есть). Слово "token" встречается в 413 файлах, но всегда как
захваченный чужой вывод: StopToken, PublicKeyToken, "Unexpected token",
"Access token is unavailable", "Need more tokens?". Вывод тот же, поэтому текст
verdict в артефакте оставлен без изменений: его дословно повторяет подпись на
дашборде, и расхождение двух формулировок читалось бы как расхождение данных.

So this script deliberately produces PROXY metrics, not tokens:
  * PLANNER_RESPONSE count = model turns (the primary volume signal)
  * character volume of thinking / content
  * step_index high-water mark per conversation
  * RESOURCE_EXHAUSTED events = times the account hit its quota
  * time series at day / hour / minute resolution from `created_at`

The proxy systematically UNDER-counts real token spend, because a transcript
stores each message once while the API is re-sent the whole context every turn.
On Codex -- where real counters exist -- cached input was 96% of all tokens, so
the re-sent context is the bulk of the spend and is entirely invisible here.

КОРНИ БЕРУТСЯ ИЗ tokenaudit_config, а не из трёх абсолютных путей с именем
пользователя внутри. Прежний жёсткий список знал antigravity, antigravity-ide и
geminiantigravity: первый из них пуст, а antigravity-cli в списке отсутствовал
вовсе, то есть literal-список одновременно и сканировал пустоту, и терял корень.
Автопоиск по ~/.gemini/antigravity*/brain находит все четыре.

ОТСУТСТВИЕ КОРНЯ -- ЭТО ВЫХОД С КОДОМ EXIT_NO_ROOT, А НЕ АРТЕФАКТ ИЗ НУЛЕЙ.
Раньше при пропавших каталогах скрипт писал antigravity_totals.json со всеми
нулями и null-метками времени и выходил с кодом 0: склонировавший репозиторий
публиковал ноль как измерение. Осознанно пустой прогон разрешает --allow-empty.

ВЕСЬ КОНВЕЙЕР ЖИВЁТ В main(). При импорте модуль не обходит 2.4 ГБ транскриптов
и не перезаписывает antigravity_totals.json.
"""
import argparse
import json
import os
import sys
from collections import defaultdict

import tokenaudit_config as cfg

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "antigravity_totals.json")
ARTIFACT = os.path.basename(OUT)

MODEL_TURN = "PLANNER_RESPONSE"
QUOTA_MARKERS = ("RESOURCE_EXHAUSTED", "Individual quota", "quota reached")
# типы записей, которые считаются вызовом инструмента
TOOL_TYPES = frozenset(("VIEW_FILE", "RUN_COMMAND", "GREP_SEARCH", "CODE_ACTION",
                        "LIST_DIRECTORY", "MCP_TOOL", "SEARCH_WEB",
                        "READ_RESOURCE", "INVOKE_SUBAGENT"))

VERDICT = (
    "Antigravity keeps no token ledger on disk. Token-accounting identifiers "
    "appear in only 13 of 687 transcripts and every hit is captured console "
    "output of a script the agent ran, not per-generation accounting. "
    "These numbers are volume proxies and systematically UNDER-count real "
    "token spend, because re-sent context (96% of tokens on Codex) is not "
    "recorded anywhere in a transcript."
)


def resolve_roots(cli=None):
    """Корни brain Antigravity вместе с метками. Пустой список -> RootError.

    cli -- значение --antigravity-root (список или None).
    -> list[(path, label)]
    """
    return cfg.require(cfg.antigravity_roots(cli), "Antigravity", "antigravity",
                       artifact=ARTIFACT)


def find_transcripts(roots):
    """Один транскрипт на беседу: полный лог, иначе короткий.

    roots -- пары (path, label) от cfg.antigravity_roots(). Метка идёт от
    родительского каталога корня, а не от литерала, поэтому новый корень
    (появился antigravity-cli) попадает в разрезы без правки кода.
    -> list[(label, conv, path, kind)]
    """
    targets = []
    for root, label in roots:
        if not os.path.isdir(root):
            continue
        for conv in sorted(os.listdir(root)):
            logs = os.path.join(root, conv, ".system_generated", "logs")
            full = os.path.join(logs, "transcript_full.jsonl")
            short = os.path.join(logs, "transcript.jsonl")
            if os.path.isfile(full):
                targets.append((label, conv, full, "full"))
            elif os.path.isfile(short):
                targets.append((label, conv, short, "short"))
    return targets


def scan(targets, progress=True):
    """Обойти транскрипты и собрать все накопители. Ничего не пишет на диск.

    -> dict с ключами stats, type_counts, chars_by_type, conversations,
       minute, quota_minute, by_label
    """
    stats = defaultdict(int)
    stats["conversations_with_transcript"] = len(targets)
    type_counts = defaultdict(int)
    chars_by_type = defaultdict(int)
    conversations = {}
    minute = defaultdict(lambda: defaultdict(int))   # slot -> metric -> value
    quota_minute = defaultdict(int)
    by_label = defaultdict(lambda: defaultdict(int))

    for i, (label, conv, path, kind) in enumerate(targets):
        if progress and i % 40 == 0:
            print("  [%d/%d] %s %s" % (i, len(targets), label, conv[:8]), flush=True)
        size = os.path.getsize(path)
        stats["bytes"] += size
        rec = {
            "root": label,
            "transcript_kind": kind,
            "bytes": size,
            "records": 0,
            "model_turns": 0,
            "user_inputs": 0,
            "tool_calls": 0,
            "quota_blocks": 0,
            "thinking_chars": 0,
            "content_chars": 0,
            "max_step_index": 0,
            "first_ts": None,
            "last_ts": None,
        }
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            stats["unreadable"] += 1
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                stats["lines"] += 1
                rec["records"] += 1
                try:
                    d = json.loads(line)
                except Exception:
                    stats["bad_json_lines"] += 1
                    continue
                if not isinstance(d, dict):
                    continue
                t = d.get("type") or "UNKNOWN"
                type_counts[t] += 1
                ts = d.get("created_at")
                if isinstance(ts, str) and len(ts) >= 16:
                    rec["first_ts"] = rec["first_ts"] or ts
                    rec["last_ts"] = ts
                    slot = ts[:16]
                else:
                    slot = None
                si = d.get("step_index")
                if isinstance(si, int) and si > rec["max_step_index"]:
                    rec["max_step_index"] = si

                think = d.get("thinking")
                nthink = len(think) if isinstance(think, str) else 0
                cont = d.get("content")
                ncont = len(cont) if isinstance(cont, str) else 0
                if nthink:
                    rec["thinking_chars"] += nthink
                    chars_by_type[t] += nthink
                if ncont:
                    rec["content_chars"] += ncont
                    chars_by_type[t] += ncont

                is_turn = t == MODEL_TURN
                if is_turn:
                    rec["model_turns"] += 1
                elif t == "USER_INPUT":
                    rec["user_inputs"] += 1
                elif t in TOOL_TYPES:
                    rec["tool_calls"] += 1

                quota = False
                if t == "ERROR_MESSAGE":
                    err = d.get("error")
                    if isinstance(err, str) and any(m in err for m in QUOTA_MARKERS):
                        quota = True
                        rec["quota_blocks"] += 1

                if slot:
                    m = minute[slot]
                    m["records"] += 1
                    if is_turn:
                        m["model_turns"] += 1
                    m["chars"] += nthink + ncont
                    if quota:
                        quota_minute[slot] += 1

                b = by_label[label]
                b["records"] += 1
                if is_turn:
                    b["model_turns"] += 1
                b["chars"] += nthink + ncont
                if quota:
                    b["quota_blocks"] += 1

        # key by (root, conv): the same conversation uuid can exist under more than
        # one brain root, and keying on the uuid alone silently drops one of them.
        conversations["%s/%s" % (label, conv)] = rec

    return {"stats": stats, "type_counts": type_counts,
            "chars_by_type": chars_by_type, "conversations": conversations,
            "minute": minute, "quota_minute": quota_minute, "by_label": by_label}


def build(acc, roots):
    """Собрать содержимое antigravity_totals.json. Только арифметика, без записи.

    Пути корней публикуются через cfg.redact(): в артефакт уходит
    '~/.gemini/antigravity/brain', а не абсолютный путь с именем пользователя
    внутри -- репозиторий публичный.
    -> dict
    """
    stats = acc["stats"]
    conversations = acc["conversations"]
    minute = acc["minute"]

    def roll(cut):
        g = defaultdict(lambda: defaultdict(int))
        for slot, mv in minute.items():
            k = slot[:cut]
            for metric, v in mv.items():
                g[k][metric] += v
        return {k: dict(v) for k, v in sorted(g.items())}

    totals = {
        "conversations": len(conversations),
        "records": sum(c["records"] for c in conversations.values()),
        "model_turns": sum(c["model_turns"] for c in conversations.values()),
        "user_inputs": sum(c["user_inputs"] for c in conversations.values()),
        "tool_calls": sum(c["tool_calls"] for c in conversations.values()),
        "quota_blocks": sum(c["quota_blocks"] for c in conversations.values()),
        "thinking_chars": sum(c["thinking_chars"] for c in conversations.values()),
        "content_chars": sum(c["content_chars"] for c in conversations.values()),
        "bytes": stats["bytes"],
    }

    ts_all = sorted(c["first_ts"] for c in conversations.values() if c["first_ts"])
    ts_end = sorted(c["last_ts"] for c in conversations.values() if c["last_ts"])

    out = {
        "metric_class": "PROXY_NOT_TOKENS",
        "verdict": VERDICT,
        "roots": [cfg.redact(p) for p, _ in roots],
        "scan_stats": dict(stats),
        "totals": totals,
        "by_root": {k: dict(v) for k, v in sorted(acc["by_label"].items())},
        "record_type_counts": dict(sorted(acc["type_counts"].items(),
                                          key=lambda x: -x[1])),
        "chars_by_record_type": dict(sorted(acc["chars_by_type"].items(),
                                            key=lambda x: -x[1])),
        "first_ts": ts_all[0] if ts_all else None,
        "last_ts": ts_end[-1] if ts_end else None,
        "by_day": roll(10),
        "by_hour": roll(13),
        "by_minute": {k: dict(v) for k, v in sorted(minute.items())},
        "quota_blocks_by_day": {},
        "conversations": conversations,
    }
    qd = defaultdict(int)
    for slot, n in acc["quota_minute"].items():
        qd[slot[:10]] += n
    out["quota_blocks_by_day"] = dict(sorted(qd.items()))
    return out


def report(out):
    """Печать сводки по посчитанному. -> None"""
    totals = out["totals"]
    print()
    print("conversations w/ transcript :", totals["conversations"])
    print("transcript bytes            : %.2f GB" % (totals["bytes"] / 1e9))
    print("records (lines)             : {:,}".format(totals["records"]))
    print("MODEL TURNS (PLANNER_RESP)  : {:,}".format(totals["model_turns"]))
    print("user inputs                 : {:,}".format(totals["user_inputs"]))
    print("tool calls                  : {:,}".format(totals["tool_calls"]))
    print("quota blocks (429)          : {:,}".format(totals["quota_blocks"]))
    print("thinking chars              : {:,}".format(totals["thinking_chars"]))
    print("content chars               : {:,}".format(totals["content_chars"]))
    print("date range                  :", out["first_ts"], "->", out["last_ts"])
    print("days covered                :", len(out["by_day"]))
    print()
    print("top record types:")
    for k, v in list(out["record_type_counts"].items())[:12]:
        # Было "   %-22s {:>10,}".format(v).format(v) % k -- две .format подряд
        # поверх %-шаблона. Работало случайно и читалось как ошибка.
        print("   %-22s %10s" % (k, "{:,}".format(v)))


def main(argv=None):
    """Замер, запись antigravity_totals.json, печать сводки.

    -> код возврата: 0 успех, cfg.EXIT_NO_ROOT если корней нет
    """
    cfg.stdout_utf8()          # до любого print: иначе кириллица умирает в pipe
    ap = cfg.add_path_args(argparse.ArgumentParser(
        description="прокси-метрики Antigravity по локальным транскриптам brain"))
    args = ap.parse_args(argv)
    cli = cfg.apply_args(args)
    if getattr(args, "print_roots", False):
        return cfg.print_roots(cli)

    # Корни -- ПЕРВЫМ делом и до любой записи на диск. Наружу уходит именно
    # EXIT_NO_ROOT: код 1 -- это провал проверки рендера, подмена стёрла бы
    # причину ровно там, где её читают.
    try:
        roots = resolve_roots(cli.get("antigravity"))
    except cfg.RootError as e:
        sys.stderr.write("%s\n" % e)
        return cfg.EXIT_NO_ROOT

    targets = find_transcripts(roots)
    # Печатать всегда, и при успехе: молчание про корни -- причина, по которой
    # три недели никто не замечал, что один корень пуст, а другой не в списке.
    print(cfg.describe("Antigravity", roots, len(targets),
                       cfg.source_of("antigravity")))

    acc = scan(targets)
    out = build(acc, roots)

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)

    report(out)
    print()
    # путь через redact(): имя пользователя не должно уезжать даже в лог прогона
    print("wrote", cfg.redact(OUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""One command that re-measures everything and rebuilds every artifact.

    python refresh.py              # fast: Claude only + dashboard + verify
    python refresh.py --all        # also Codex (10 GB) and Antigravity (1.4 GB)
    python refresh.py --codex      # add Codex chain-split
    python refresh.py --antigravity
    python refresh.py --no-verify  # skip the headless-Chrome render check

Why this exists: every number in this audit was previously updated by hand, which
is how the 138.9 B figure survived seven weeks of being 28% wrong. This script
appends each run to snapshots.jsonl, so deltas between runs are computed rather
than remembered, and regenerates CURRENT.md straight from the data.

The verifier deliberately compares EXTRACTED TEXT, not raw markup. Five separate
false negatives during this audit came from searching markup or mis-encoded
strings: ripgrep skipping dot-directories, a format with no account field,
cp1251 mangling the probe, NBSP vs space, and finally `&nbsp;` entities. A probe
that cannot possibly match is worse than no probe.
"""
import argparse
import html
import io
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots.jsonl")
PY = sys.executable
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

FIELDS = ("inp", "cc", "cr", "out")
NAMES = {"inp": "свежий ввод", "cc": "запись кэша", "cr": "чтение кэша", "out": "вывод"}

RATES = {"claude-opus-5": (5, 25), "claude-opus-4-8": (5, 25),
         "claude-fable-5": (10, 50), "claude-sonnet-5": (2, 10)}
OPENAI = {"gpt-5.5": (5, 0.5, 30)}
M = 1_000_000.0


def run(script, label):
    t0 = time.time()
    print("  ▸ %-26s " % label, end="", flush=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([PY, "-u", os.path.join(HERE, script)],
                       cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        print("ОШИБКА")
        print((r.stderr or "")[-1500:])
        raise SystemExit("прерван на %s" % script)
    print("%.1f с" % (time.time() - t0))
    return r.stdout


def L(name):
    with io.open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return json.load(fh)


def cost_claude(by_model):
    """Cache write 1.25x base, cache read 0.1x base. Per-model, from measured fields."""
    out, total = {}, 0.0
    for m, v in by_model.items():
        if v.get("total", 0) == 0:
            continue
        if m in RATES:
            ri, ro = RATES[m]
            c = {"unc": v["uncached_input"] / M * ri,
                 "cw": v["cache_write"] / M * ri * 1.25,
                 "cr": v["cache_read"] / M * ri * 0.1,
                 "out": v["output"] / M * ro}
        elif m in OPENAI:
            ri, rc, ro = OPENAI[m]
            c = {"unc": v["uncached_input"] / M * ri, "cw": 0.0,
                 "cr": v["cache_read"] / M * rc, "out": v["output"] / M * ro}
        else:
            continue
        c["total"] = sum(c.values())
        out[m] = c
        total += c["total"]
    return out, total


def fmt(n):
    return "{:,}".format(int(n)).replace(",", " ")


def usd(n):
    return "$" + "{:,.2f}".format(n).replace(",", " ").replace(".", ",")


def verify_dashboard(expect, target=None):
    """Render headless, then check EXTRACTED TEXT — never raw markup.

    `target` defaults to the local dashboard.html. Pass an http(s) URL to verify
    a *published* copy instead: the dashboard draws its SVG in JS at runtime, so
    fetching the bytes and grepping for <rect> proves nothing — only a real
    render does.
    """
    dom = os.path.join(HERE, "_verify_dom.html")
    if not target:
        dash = os.path.join(HERE, "dashboard.html")
        target = "file:///" + dash.replace("\\", "/")
    p = subprocess.run([CHROME, "--headless=new", "--disable-gpu",
                        "--virtual-time-budget=9000", "--dump-dom", target],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    raw = p.stdout or ""
    if len(raw) < 10000:
        return False, ["Chrome вернул %d байт — рендер не удался" % len(raw)], {}
    with io.open(dom, "w", encoding="utf-8") as fh:
        fh.write(raw)
    # 1) сущности  2) теги  3) любые пробелы в один
    #
    # html.unescape, а не свой список замен: рукописный список ловил &nbsp;
    # и пропускал числовые ссылки на тот же символ -- &#160;, &#xa0;, &#8239;
    # проходили мимо и давали ложный провал проверки на верном дашборде.
    # Это шестая ошибка одного класса в этой работе, поэтому здесь стоит
    # штатный разборщик, покрывающий все именованные и числовые сущности.
    #
    # \s в str-шаблоне уже включает U+00A0 и U+202F (проверено), так что
    # перечислять неразрывные пробелы в классе символов не нужно.
    txt = html.unescape(raw)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt)

    problems = []

    # 1) Ошибки JavaScript. Chrome при --dump-dom НИЧЕГО не пишет в stderr,
    #    когда скрипт падает: проверено внедрением обращения к переменной до
    #    её объявления -- stderr пуст, а панели пусты. Единственный способ
    #    увидеть падение снаружи -- перехватчик window.onerror, поставленный
    #    ДО остальных скриптов; он вписывает текст ошибки в DOM с меткой
    #    JSERR, и здесь мы её ищем. Так ловятся оба класса поломок, которые
    #    уже случались: столкновение имён на верхнем уровне и обращение к
    #    const до объявления.
    #    Ищем сам контейнер ошибки, а не слово в тексте: слово встречается и в
    #    комментарии внутри самого перехватчика, а атрибут id в такой форме
    #    появляется только в сериализованном DOM.
    #    Текст берётся ИЗ КОНТЕЙНЕРА, а не поиском слова по всему документу:
    #    слово встречается ещё и в комментарии внутри перехватчика, и поиск по
    #    тексту печатал комментарий вместо самой ошибки.
    jse = re.search(r'id="jserr"[^>]*>(.*?)</div>', raw, re.S)
    if jse:
        msg = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", jse.group(1)))).strip()
        problems.append("ошибка JavaScript в дашборде: %s" % (msg[:300] or "текст не извлёкся"))

    # 2) Пустое содержимое. Проверяется по ТИПУ содержимого, а не по окну
    #    фиксированной длины.
    #
    #    Было: срез raw[i:i+4000] от каждого id. Такое окно заглядывало в
    #    РАЗМЕТКУ СЛЕДУЮЩЕЙ панели, поэтому пустая панель объявлялась
    #    заполненной -- обнуление трёх необязательных ключей гасило четыре
    #    панели, а проверка возвращала «пустых 0».
    #
    #    Наивное сужение до следующего id тоже неверно: из 59 элементов с id
    #    только часть -- графики. Остальные это легенды, плитки и подписи, в
    #    которых SVG-марок не бывает по устройству, и требовать их значит
    #    получить 15 ложных срабатываний. Поэтому требование зависит от того,
    #    что внутри: график обязан содержать марки, таблица -- строки,
    #    текстовый контейнер -- непустой текст.
    MARKS = ("<rect", "<text", "<circle", "<path", "<line", "<polyline", "<polygon")
    pos = [(m.start(), m.group(1)) for m in re.finditer(r'id="([a-zA-Z0-9_]+)"', raw)]
    bounds = [p[0] for p in pos] + [len(raw)]
    ids, empty, kinds = [], [], {"график": 0, "таблица": 0, "текст": 0}
    for k, (start, pid) in enumerate(pos):
        if pid in ("mode", "tip", "jserr"):
            continue
        ids.append(pid)
        seg = raw[start:bounds[k + 1]]
        if "<svg" in seg:
            kinds["график"] += 1
            if not any(t in seg for t in MARKS):
                empty.append("%s (график без марок)" % pid)
        elif "<tr" in seg:
            # Признак таблицы -- строки, а не открывающий <table>. Тег <table>
            # протекает через границу: id стоит на <tbody>, поэтому <table>
            # попадает в срез ПРЕДЫДУЩЕГО элемента и прозаическая подпись
            # выглядит таблицей. Проверено на crshare и dpcap.
            #
            # Одной строки мало: таблица с одним только заголовком -- это
            # таблица без данных, ровно тот случай, который надо ловить.
            kinds["таблица"] += 1
            if seg.count("<tr") < 2:
                empty.append("%s (таблица только с заголовком)" % pid)
        else:
            kinds["текст"] += 1
            plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", seg)).strip()
            if not plain:
                empty.append("%s (пустой контейнер)" % pid)
    if empty:
        problems.append("без данных: %s" % ", ".join(empty))

    for probe in expect:
        if probe not in txt:
            problems.append("не найдено в тексте: %r" % probe)
    os.remove(dom)
    return (not problems), problems, {"panels": len(ids), "empty": len(empty),
                                      "dom_bytes": len(raw)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--codex", action="store_true")
    ap.add_argument("--antigravity", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--verify-url", metavar="URL",
                    help="проверять рендер по этому URL вместо локального файла — "
                         "так проверяется опубликованная копия")
    a = ap.parse_args()
    do_cx = a.all or a.codex
    do_ag = a.all or a.antigravity

    print("=" * 74)
    print("ПЕРЕСЧЁТ АУДИТА")
    print("=" * 74)

    prev = None
    if os.path.exists(SNAP):
        with io.open(SNAP, encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh if x.strip()]
        prev = rows[-1] if rows else None

    print("\n[1/5] измерение")
    run("claude_agg.py", "Claude Code, агрегат")
    run("claude_deep.py", "Claude Code, распределения")
    if do_cx:
        run("codex_agg_chains.py", "Codex, chain-split (10 ГБ)")
    if do_ag:
        run("antigravity_agg.py", "Antigravity, прокси (1.4 ГБ)")

    cl, dp = L("claude_totals.json"), L("claude_deep.json")
    t = cl["totals_deduped"]
    by_model, total_usd = cost_claude(dp["by_model"])
    snap = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "claude": {
            "total": sum(t[f] for f in FIELDS),
            **{f: t[f] for f in FIELDS},
            "sessions": cl["session_count"],
            "responses": cl["records_deduped"],
            "last_event": cl["last_ts"],
            "usd": round(total_usd, 2),
            "by_model": {m: dp["by_model"][m]["total"] for m in dp["by_model"]},
            "active_h": dp["active_time_hours_gap_le_300s"],
            "tph": dp["tokens_per_active_hour"],
            "subagent_share": dp["main_vs_subagent"]["subagent"]["share_pct"],
        },
        "ran": {"codex": do_cx, "antigravity": do_ag},
    }
    if do_cx:
        ch = L("codex_chains_totals.json")
        snap["codex"] = {"total": ch["totals_chain_split"]["total_tokens"],
                         "sessions": ch["sessions"], "files": ch["scan_stats"]["files"]}
    if do_ag:
        ag = L("antigravity_totals.json")
        snap["antigravity"] = {"model_turns": ag["record_type_counts"].get("PLANNER_RESPONSE", 0),
                               "conversations": ag["totals"]["conversations"],
                               "quota_blocks": ag["totals"]["quota_blocks"]}

    print("\n[2/5] стоимость и дельты")
    with io.open(os.path.join(HERE, "claude_cost_deep.json"), "w", encoding="utf-8") as fh:
        json.dump({"claude_cost_by_model": by_model,
                   "claude_total_usd": round(total_usd, 2),
                   "claude_total_tokens": snap["claude"]["total"],
                   "all_time_measurable_tokens":
                       snap["claude"]["total"] + 119058904842}, fh,
                  indent=1, ensure_ascii=False)

    d = {}
    if prev and "claude" in prev:
        p = prev["claude"]
        for k in ("total", "sessions", "responses", "usd", *FIELDS):
            d[k] = snap["claude"][k] - p.get(k, 0)
        d["pct"] = (100.0 * d["total"] / p["total"]) if p.get("total") else 0.0
        d["since"] = prev["ts"]
    snap["delta"] = d

    run("cost_model.py", "модель стоимости")

    print("\n[3/5] генерация отчётов")
    gen_out = run("report_gen.py", "AUTO-блоки + целостность")
    for ln in (gen_out or "").splitlines():
        ln = ln.strip()
        if ln.startswith(("SUMMARY", "DEEP_REPORT", "README", "CURRENT",
                          "целостность", "НЕИЗВЕСТНЫЕ")):
            print("      " + ln)

    print("\n[4/5] сборка дашборда")
    run("build_dashboard.py", "dashboard.html")

    with io.open(SNAP, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap, ensure_ascii=False) + "\n")

    # ---- CURRENT.md, целиком из данных ----
    c = snap["claude"]
    lines = [
        "# Актуальные цифры", "",
        "Сгенерировано автоматически `refresh.py` — **не править руками**.",
        "Дельты считаются от предыдущего запуска, а не по памяти.", "",
        "| | Значение |", "|---|---:|",
        "| срез | %s |" % snap["ts"],
        "| последнее событие | %s |" % (c["last_event"] or "—"),
        "| **Claude Code, всего** | **%s** |" % fmt(c["total"]),
        "| сессий | %s |" % fmt(c["sessions"]),
        "| ответов (дедуп.) | %s |" % fmt(c["responses"]),
        "| $ по прайсу | %s |" % usd(c["usd"]),
        "| активных часов | %s |" % c["active_h"],
        "| токенов в активный час | %s |" % fmt(c["tph"]),
        "| доля субагентов | %s%% |" % c["subagent_share"], "",
        "## По типу токена", "", "| тип | токенов | доля |", "|---|---:|---:|",
    ]
    for f in FIELDS:
        lines.append("| %s | %s | %.2f%% |" % (NAMES[f], fmt(c[f]),
                                               100.0 * c[f] / max(1, c["total"])))
    lines += ["", "## По моделям", "",
              "| модель | токенов | доля | $ |", "|---|---:|---:|---:|"]
    for m, v in sorted(c["by_model"].items(), key=lambda x: -x[1]):
        if v == 0:
            continue
        cu = by_model.get(m, {}).get("total")
        lines.append("| %s | %s | %.2f%% | %s |" % (
            m, fmt(v), 100.0 * v / max(1, c["total"]), usd(cu) if cu else "—"))
    if d:
        lines += ["", "## Изменение с предыдущего запуска (%s)" % d.get("since", "?"), "",
                  "| метрика | прирост |", "|---|---:|",
                  "| **всего токенов** | **+%s (+%.1f%%)** |" % (fmt(d["total"]), d["pct"]),
                  "| $ по прайсу | +%s |" % usd(d["usd"]),
                  "| сессий | +%s |" % fmt(d["sessions"]),
                  "| ответов | +%s |" % fmt(d["responses"])]
        tot = d["total"] or 1
        lines.append("")
        lines.append("Состав прироста: " + ", ".join(
            "%s %.1f%%" % (NAMES[f], 100.0 * d[f] / tot) for f in FIELDS))
    if "codex" in snap:
        lines += ["", "## Codex (chain-split)", "",
                  "| | |", "|---|---:|",
                  "| токенов | %s |" % fmt(snap["codex"]["total"]),
                  "| сессий | %s |" % fmt(snap["codex"]["sessions"])]
    if "antigravity" in snap:
        lines += ["", "## Antigravity (прокси, не токены)", "",
                  "| | |", "|---|---:|",
                  "| ходов модели | %s |" % fmt(snap["antigravity"]["model_turns"]),
                  "| беседы | %s |" % fmt(snap["antigravity"]["conversations"]),
                  "| упоров в квоту | %s |" % fmt(snap["antigravity"]["quota_blocks"])]
    lines += ["", "---", "",
              "Запусков в истории: %d (`snapshots.jsonl`)." % (
                  sum(1 for _ in io.open(SNAP, encoding="utf-8"))),
              "", "Полная аналитика: `SUMMARY.md`, `DEEP_REPORT.md`, `dashboard.html`."]
    with io.open(os.path.join(HERE, "CURRENT.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print("\n[5/5] проверка рендера")
    if a.no_verify:
        print("  ▸ пропущена (--no-verify)")
        ok, probs, info = True, [], {}
    else:
        expect = [fmt(c["total"]), fmt(c["sessions"]), "чтение кэша"]
        ok, probs, info = verify_dashboard(expect, a.verify_url)
        where = a.verify_url or "локальный файл"
        if ok:
            print("  ▸ OK: панелей %d, все с данными, DOM %.2f МБ (%s)"
                  % (info["panels"], info["dom_bytes"] / 1e6, where))
        else:
            print("  ▸ ПРОБЛЕМЫ:")
            for x in probs:
                print("      -", x)

    print("\n" + "=" * 74)
    print("ИТОГ  %s" % snap["ts"])
    print("=" * 74)
    print("  Claude Code : %s токенов | %s | %s сессий | %s ответов"
          % (fmt(c["total"]), usd(c["usd"]), fmt(c["sessions"]), fmt(c["responses"])))
    if d:
        print("  прирост     : +%s (+%.1f%%), +%s  с %s"
              % (fmt(d["total"]), d["pct"], usd(d["usd"]), d["since"]))
        tot = d["total"] or 1
        print("  состав      : " + ", ".join(
            "%s %.1f%%" % (NAMES[f], 100.0 * d[f] / tot) for f in FIELDS))
    else:
        print("  прирост     : первый запуск, сравнивать не с чем")
    if "codex" in snap:
        print("  Codex       : %s токенов" % fmt(snap["codex"]["total"]))
    if "antigravity" in snap:
        print("  Antigravity : %s ходов модели" % fmt(snap["antigravity"]["model_turns"]))
    print("\n  обновлено: CURRENT.md, dashboard.html, claude_totals.json,")
    print("             claude_deep.json, claude_cost_deep.json, combined.json,")
    print("             snapshots.jsonl (+1 запись)")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

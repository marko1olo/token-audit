#!/usr/bin/env python3
"""One command that re-measures everything and rebuilds every artifact.

    python refresh.py              # fast: Claude only + dashboard + verify
    python refresh.py --all        # also Codex (10 GB) and Antigravity (1.4 GB)
    python refresh.py --codex      # add Codex: chain-split plus max-per-file
    python refresh.py --antigravity
    python refresh.py --no-verify  # skip the headless-Chrome render check
    python refresh.py --chrome ПУТЬ      # свой браузер вместо найденного
    python refresh.py --claude-root ПУТЬ # свой корень транскриптов, флаг повторяем
    python refresh.py --allow-empty      # осознанно разрешить пустое измерение
    python refresh.py --print-roots      # показать найденные корни и выйти

Why this exists: every number in this audit was previously updated by hand, which
is how the 138.9 B figure survived seven weeks of being 28% wrong. This script
appends each run to snapshots.jsonl, so deltas between runs are computed rather
than remembered, and regenerates CURRENT.md straight from the data.

The verifier deliberately compares EXTRACTED TEXT, not raw markup. Five separate
false negatives during this audit came from searching markup or mis-encoded
strings: ripgrep skipping dot-directories, a format with no account field,
cp1251 mangling the probe, NBSP vs space, and finally `&nbsp;` entities. A probe
that cannot possibly match is worse than no probe.

ПУТИ, ЦЕНЫ И БРАУЗЕР ЗДЕСЬ НЕ ЖИВУТ. Корни данных и headless-браузер приходят из
tokenaudit_config, ставки и множители кэша — из tokenaudit_rates. Зашитый в этот
файл путь к chrome.exe ронял проверку на любой чужой машине уже ПОСЛЕ того, как
все артефакты были перезаписаны, а локальная копия таблицы ставок разошлась с
cost_model.py.

КОДЫ ВЫХОДА (словарь общий на весь аудит, tokenaudit_config.EXIT_*):
    0  всё сошлось
    1  проверка рендера не прошла: пустые панели, ошибка JS или проба не нашлась
    2  целостность не сошлась (это код report_gen.py, он проходит наружу как есть)
    3  корень данных не найден ИЛИ измерен ноль токенов
    4  headless-браузера нет, проверять рендер нечем
Ненулевой код дочернего скрипта проходит наружу как есть, а его stderr печатается
выше — номер никогда не остаётся единственной информацией о падении.
"""
import argparse
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

import tokenaudit_config as cfg
import tokenaudit_rates as rates

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots.jsonl")
PY = sys.executable

# Файл с цифрами, измеренными НЕ на этой машине. По умолчанию ОТСУТСТВУЕТ, и это
# нормальное состояние: см. load_external().
EXTERNAL_NAME = "external_measurements.json"

FIELDS = ("inp", "cc", "cr", "out")
NAMES = {"inp": "свежий ввод", "cc": "запись кэша", "cr": "чтение кэша", "out": "вывод"}

# Разделитель разрядов, который ставит fmt(). Нужен пробам: в извлечённом тексте
# DOM все неразрывные пробелы уже сведены к обычному, см. verify_dashboard().
SEP = " "


def run(script, label, env=None):
    t0 = time.time()
    print("  ▸ %-26s " % label, end="", flush=True)
    r = subprocess.run([PY, "-u", os.path.join(HERE, script)],
                       cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env=dict(os.environ, **(env or {})))
    if r.returncode != 0:
        print("ОШИБКА")
        print((r.stderr or "")[-1500:])
        # Код дочернего скрипта не подменяется: 2 от report_gen.py — это
        # целостность, 3 от агрегатора — отсутствующий корень. Подмена на
        # единицу стирала бы причину ровно в том месте, где её и читают.
        print("прерван на %s, код %d" % (script, r.returncode))
        raise SystemExit(r.returncode)
    print("%.1f с" % (time.time() - t0))
    return r.stdout


def L(name):
    with io.open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return json.load(fh)


def load_external(name=EXTERNAL_NAME):
    """Внешние измерения: цифры, снятые НЕ на этой машине. Файла нет — пустой dict.

    Зачем файл. Здесь стоял литерал `+ 119058904842` — итог Codex со второй
    машины, зашитый в код. Каждый, кто клонировал репозиторий, регенерировал эту
    чужую цифру в свой собственный README как измеренную у себя.

    Схема: объект «имя фигуры -> запись», и запись обязана нести происхождение,
    иначе число снова становится литералом без владельца:
        {"codex_second_machine_tokens": {
            "value": 119058904842,
            "source": "reconciliation.json, consistent_total_max_basis",
            "measured_at": "2026-06-06",
            "machine": "danat"}}
    Поля value/source/measured_at/machine обязательны все четыре. Запись без
    числового value игнорируется с предупреждением: половина записи хуже
    отсутствующей, потому что в отчёт она попадает как измеренная.
    -> dict[str, dict]
    """
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        return {}
    try:
        with io.open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as e:
        raise SystemExit("%s: битый JSON — %s" % (name, e))
    if not isinstance(data, dict):
        raise SystemExit("%s: на верхнем уровне ожидался объект JSON, получено %s"
                         % (name, type(data).__name__))
    out = {}
    for key, rec in data.items():
        if not isinstance(rec, dict) or not isinstance(rec.get("value"), (int, float)):
            sys.stderr.write("%s: запись %r без числового value — пропущена\n"
                             % (name, key))
            continue
        missing = [f for f in ("source", "measured_at", "machine") if not rec.get(f)]
        if missing:
            sys.stderr.write("%s: у записи %r нет полей %s — цифра без "
                             "происхождения, пропущена\n"
                             % (name, key, ", ".join(missing)))
            continue
        out[key] = rec
    return out


def token_total(v):
    """Сумма измеренных токенов записи by_model. Оба стиля ключей.

    Короткие ключи — claude_totals.json ('inp','cc','cr','out'), длинные —
    claude_deep.json, где сумма уже посчитана в 'total'.
    -> int
    """
    if v.get("total") is not None:
        return v["total"]
    keys = FIELDS if "inp" in v else ("uncached_input", "cache_write",
                                      "cache_read", "output")
    return sum(v.get(k) or 0 for k in keys)


def cost_claude(by_model):
    """Стоимость по моделям из измеренных полей. Ставки — только tokenaudit_rates.

    Локальных копий таблиц и множителей здесь больше нет: копия в этом файле
    знала четыре модели Anthropic и один gpt-5.5, то есть оценивала те же данные
    иначе, чем cost_model.py.

    Считать надо по claude_totals.json, а НЕ по claude_deep.json: только первый
    несёт разбивку записи кэша по TTL (e5m/e1h), а часовая запись стоит 2x
    базовой цены ввода против 1.25x пятиминутной. На текущих данных счёт по
    claude_deep.json давал 10 399.8846 против 10 439.5440 — на 39.66 доллара
    меньше и вразрез с combined.json, то есть дашборд показывал две разные
    итоговые суммы на одной странице.
    -> ({модель: разложение}, сумма, [модели без цены])
    """
    out, total, unpriced = {}, 0.0, []
    for m, v in by_model.items():
        if token_total(v) == 0:
            continue      # <synthetic> и модели без расхода: оценивать нечего
        c = rates.cost_breakdown(v, m)
        if c is None:
            unpriced.append(m)
            continue
        out[m] = c
        total += c["total"]
    return out, total, rates.unpriced_models(unpriced)


def fmt(n):
    return "{:,}".format(int(n)).replace(",", " ")


def usd(n):
    return "$" + "{:,.2f}".format(n).replace(",", " ").replace(".", ",")


def probes(total, sessions, cost):
    """Пробы для проверки рендера плюс счётчик тех, которые что-то доказывают.

    Числовая проба без разделителя разрядов не годится. На нулевом наборе fmt(0)
    — это один символ «0», а он встречается в dashboard.html 215 818 раз
    (измерено на текущем файле); вместе со статической подписью «чтение кэша»
    все три прежние пробы совпадали и на дашборде из одних нулей, и инструмент
    печатал «OK, все панели с данными». Поэтому число попадает в пробы только
    когда в нём есть разделитель, то есть от 1000 и выше; трёхзначное число
    (154 сессии) в документе с тысячами чисел не различает ничего.

    Проба по деньгам добавлена намеренно: она проверяет вторую независимую
    величину и ровно тот путь, где расходились артефакты. Формат совпадает с
    дашбордом побайтно — там toLocaleString('ru-RU'), то есть пробел между
    разрядами и запятая в дробной части.

    «чтение кэша» остаётся, но доказывает только кодировку и сам факт рендера,
    а не наличие данных: подпись есть в разметке всегда.
    -> (list[str], int)
    """
    out = []
    for value in (total, sessions):
        s = fmt(value)
        if SEP in s:
            out.append(s)
    d = usd(cost)
    if SEP in d:
        out.append(d)
    numeric = len(out)
    out.append("чтение кэша")
    return out, numeric

def extract_text(raw):
    r"""Извлечённый текст DOM: сущности разобраны, теги убраны, пробелы сведены.

    Вынесено отдельно, чтобы это проверялось тестом без Chrome. Шесть ложных
    отрицательных за эту работу пришли ровно отсюда: рукописный список замен
    ловил &nbsp; и пропускал числовые ссылки на тот же символ (&#160;, &#xa0;,
    &#8239;), а до него -- cp1251 в пробе и NBSP против обычного пробела.
    html.unescape покрывает и именованные, и числовые сущности, а \s в
    str-шаблоне уже включает U+00A0 и U+202F.
    -> str
    """
    txt = html.unescape(raw)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt)


def verify_dashboard(expect, numeric, chrome, target=None):
    """Render headless, then check EXTRACTED TEXT — never raw markup.

    `target` defaults to the local dashboard.html. Pass an http(s) URL to verify
    a *published* copy instead: the dashboard draws its SVG in JS at runtime, so
    fetching the bytes and grepping for <rect> proves nothing — only a real
    render does.

    `chrome` — путь от tokenaudit_config.find_chrome(), аргументы от
    chrome_args(): без --user-data-dir уже открытый Chrome с тем же профилем
    отдаёт пустой рендер, и проверка врёт про «0 байт».
    `numeric` — сколько проб в expect способны что-то различить, см. probes().
    """
    dom = os.path.join(HERE, "_verify_dom.html")
    if not target:
        dash = os.path.join(HERE, "dashboard.html")
        target = "file:///" + dash.replace("\\", "/")
    p = subprocess.run([chrome] + cfg.chrome_args(target, dom),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    shutil.rmtree(cfg.chrome_profile_dir(dom), ignore_errors=True)
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
    txt = extract_text(raw)

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

    # 3) Вырожденная проверка. Если ни одна проба не различает данные от их
    #    отсутствия, «OK» означает только то, что браузер отрисовал страницу.
    #    Молчать об этом нельзя: именно так проверка подтверждала дашборд из
    #    нулей. Осознанный пустой прогон разрешается через --allow-empty.
    if numeric == 0 and not cfg.allow_empty():
        problems.append("пробы вырождены: ни одно измеренное число не содержит "
                        "разделителя разрядов, доказать наличие данных нечем "
                        "(осознанно — --allow-empty)")

    os.remove(dom)
    return (not problems), problems, {"panels": len(ids), "empty": len(empty),
                                      "dom_bytes": len(raw)}


def guard_measured(total, allow_empty):
    """Ноль измеренных токенов -> выход 3 и НИ ОДНОГО перегенерированного отчёта.

    Стоит до стадии стоимости, а не после: иначе claude_cost_deep.json,
    combined.json, отчёты и дашборд перезаписывались нулями, а проверка рендера
    их же и подтверждала. README обещает, что так не бывает, — вот место, где
    обещание выполняется.
    -> None
    """
    if total or allow_empty:
        return
    print("\n  ▸ измерено 0 токенов — корень пуст или не найден; "
          "отчёты не перегенерированы")
    print("    где искали: python refresh.py --print-roots")
    print("    осознанно пустой прогон: --allow-empty")
    raise SystemExit(cfg.EXIT_NO_ROOT)


def main():
    # ПЕРВОЙ строкой, до любого вывода: PYTHONIOENCODING в env дочернего процесса
    # родителя не спасает. Под Git Bash, в любом pipe, любом редиректе и в CI
    # stdout не консоль Windows, Python берёт cp1251, и первый же символ '▸'
    # убивал процесс UnicodeEncodeError внутри стадии [1/5] — до всякого
    # измерения, с кодом 1 и трассой вместо отчёта.
    cfg.stdout_utf8()

    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--codex", action="store_true")
    ap.add_argument("--antigravity", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--verify-url", metavar="URL",
                    help="проверять рендер по этому URL вместо локального файла — "
                         "так проверяется опубликованная копия")
    # --chrome, --allow-empty, --claude-root, --codex-root, --antigravity-root,
    # --second-machine, --config, --print-roots приходят одним набором из
    # tokenaudit_config: имена флагов обязаны совпадать со всеми остальными
    # скриптами, иначе текст ошибки будет советовать флаг, которого здесь нет.
    cfg.add_path_args(ap)
    a = ap.parse_args()
    cli = cfg.apply_args(a)
    if a.print_roots:
        raise SystemExit(cfg.print_roots(cli))
    do_cx = a.all or a.codex
    do_ag = a.all or a.antigravity

    # Корни и браузер разрешаются ДО измерения. Прежде путь к chrome.exe был
    # литералом и проверялся только на стадии [5/5]: на чужой машине это
    # необработанный FileNotFoundError ПОСЛЕ того, как все артефакты уже
    # перезаписаны. Дешёвая проверка обязана идти раньше дорогой работы.
    chrome = None
    if not a.no_verify:
        try:
            chrome = cfg.find_chrome(a.chrome)
        except cfg.RootError as e:
            print(e)
            raise SystemExit(cfg.EXIT_NO_CHROME)

    # Дочерние скрипты получают уже разрешённые корни через переменные окружения
    # (и PYTHONIOENCODING=utf-8 в придачу), чтобы родитель не переписывал argv
    # каждому из них.
    env = cfg.env_for_children(dict(cli, chrome=chrome or cli.get("chrome")))

    print("=" * 74)
    print("ПЕРЕСЧЁТ АУДИТА")
    print("=" * 74)
    if chrome:
        print("chrome: %s (%s)" % (cfg.redact(chrome), cfg.source_of("chrome")))

    prev = None
    if os.path.exists(SNAP):
        with io.open(SNAP, encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh if x.strip()]
        prev = rows[-1] if rows else None

    print("\n[1/5] измерение")
    run("claude_agg.py", "Claude Code, агрегат", env)
    run("claude_deep.py", "Claude Code, распределения", env)
    if do_cx:
        run("codex_agg_chains.py", "Codex, chain-split (10 ГБ)", env)
        # codex_totals.json читают cost_model.py и build_dashboard.py, а пишет
        # его только codex_agg.py, которого не вызывал никто: у клонирующего
        # панель Codex собиралась из моего закоммиченного файла, уже устаревшего
        # на 18 часов. Второй проход стоит ещё один обход тех же 10 ГБ, зато
        # chain-split остаётся основным измерением, а максимум по файлу — той
        # самой независимой сверкой, которую cost_model.py печатает как VERIFIED.
        run("codex_agg.py", "Codex, максимум по файлу", env)
    if do_ag:
        run("antigravity_agg.py", "Antigravity, прокси (1.4 ГБ)", env)

    cl = L("claude_totals.json")
    t = cl.get("totals_deduped") or {}
    measured = sum(t.get(f) or 0 for f in FIELDS)
    guard_measured(measured, a.allow_empty)

    dp = L("claude_deep.json")
    by_model, total_usd, unpriced = cost_claude(cl["by_model"])
    if unpriced:
        print("  ▸ БЕЗ ЦЕНЫ: %s — токены посчитаны, деньги нет"
              % ", ".join(unpriced))
    snap = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "claude": {
            "total": measured,
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
    cost_deep = {"claude_cost_by_model": by_model,
                 "claude_total_usd": round(total_usd, 2),
                 "claude_total_tokens": snap["claude"]["total"]}
    # Сумма «всё измеримое» складывает измеренное здесь с измеренным на другой
    # машине. Пока внешней цифры нет, ключа НЕТ ВОВСЕ: ноль или литерал в этом
    # месте выглядят в отчёте как измерение, которого не было. Происхождение
    # едет вместе с числом, а не остаётся в истории коммитов.
    ext = load_external()
    second = ext.get("codex_second_machine_tokens")
    if second:
        cost_deep["all_time_measurable_tokens"] = (snap["claude"]["total"]
                                                   + int(second["value"]))
        cost_deep["all_time_measurable_addends"] = [
            {"value": snap["claude"]["total"], "source": "claude_totals.json",
             "machine": "эта машина", "measured_at": snap["ts"][:10]},
            dict(second, key="codex_second_machine_tokens")]
        print("  ▸ внешняя цифра: %s токенов (%s, %s, %s)"
              % (fmt(second["value"]), second["machine"],
                 second["measured_at"], second["source"]))
    else:
        print("  ▸ внешних измерений нет (%s отсутствует) — "
              "ключ all_time_measurable_tokens не пишется" % EXTERNAL_NAME)
    with io.open(os.path.join(HERE, "claude_cost_deep.json"), "w", encoding="utf-8") as fh:
        json.dump(cost_deep, fh, indent=1, ensure_ascii=False)

    d = {}
    if prev and "claude" in prev:
        p = prev["claude"]
        for k in ("total", "sessions", "responses", "usd", *FIELDS):
            d[k] = snap["claude"][k] - p.get(k, 0)
        d["pct"] = (100.0 * d["total"] / p["total"]) if p.get("total") else 0.0
        d["since"] = prev["ts"]
    snap["delta"] = d

    # reconciliation.json собирается ДО модели стоимости и отчётов, потому что
    # оба его читают. Раньше файл был рукописным и его не писал никто, из-за чего
    # заголовочная цифра Codex попадала в README у каждого, кто клонировал
    # репозиторий, оформленная так же, как измеренная рядом цифра Claude Code.
    # Условие на наличие артефакта Codex: без него собирать нечего.
    if os.path.isfile(os.path.join(HERE, "codex_chains_totals.json")):
        run("build_reconciliation.py", "сборка составного итога", env)
    else:
        print("  ▸ составной итог Codex не собирается: нет codex_chains_totals.json")

    run("cost_model.py", "модель стоимости", env)

    print("\n[3/5] генерация отчётов")
    gen_out = run("report_gen.py", "AUTO-блоки + целостность", env)
    for ln in (gen_out or "").splitlines():
        ln = ln.strip()
        if ln.startswith(("SUMMARY", "DEEP_REPORT", "README", "CURRENT",
                          "целостность", "НЕИЗВЕСТНЫЕ")):
            print("      " + ln)

    print("\n[4/5] сборка дашборда")
    run("build_dashboard.py", "dashboard.html", env)

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
        expect, numeric = probes(c["total"], c["sessions"], c["usd"])
        print("  ▸ пробы (%d различающих): %s"
              % (numeric, ", ".join(repr(x) for x in expect)))
        ok, probs, info = verify_dashboard(expect, numeric, chrome, a.verify_url)
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
    # Словарь кодов печатается рядом с итогом, а не только в docstring: CI и
    # человек читают именно этот блок, когда прогон закончился ненулём.
    print("\n  коды выхода: 0 всё сошлось | %d проверка рендера | %d целостность"
          % (cfg.EXIT_VERIFY, cfg.EXIT_INTEGRITY))
    print("               %d нет корня или измерен ноль | %d нет headless-браузера"
          % (cfg.EXIT_NO_ROOT, cfg.EXIT_NO_CHROME))
    if not ok:
        raise SystemExit(cfg.EXIT_VERIFY)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Extra AUTO blocks for the full report. Imported by report_gen.py.

Every generator takes the shared Ctx and returns a list of markdown lines.
Nothing here is hand-carried: if a source file is absent the block says so
instead of printing a stale number.
"""
import sys as _sys

# report_gen may be running as __main__, in which case a plain
# `from report_gen import ...` would import a SECOND copy of the module and the
# decorators would register into its empty registry instead of the live one.
_rg = _sys.modules.get("report_gen")
if _rg is None or not hasattr(_rg, "BLOCKS"):
    _m = _sys.modules.get("__main__")
    _rg = _m if (_m is not None and hasattr(_m, "BLOCKS")) else __import__("report_gen")

block, f, usd, pct = _rg.block, _rg.f, _rg.usd, _rg.pct
codex_total, NO_EXTERNAL = _rg.codex_total, _rg.NO_EXTERNAL
rates = _rg.rates
M, FIELDS, NAMES = _rg.M, _rg.FIELDS, _rg.NAMES


def _need(x, what):
    return ["*Нет данных: %s.*" % what] if not x else None


# Ставки только из tokenaudit_rates. Локальные копии _RA/_OA отсюда удалены:
# это были пятая и шестая копии в репозитории, и копии OPENAI уже разошлись --
# в cost_model.py семь моделей, здесь была одна, при 13 167 044 269 токенах
# gpt-5.4 в данных.
_LINES = ("свежий ввод", "запись кэша", "чтение кэша", "вывод")
_KEYS = ("inp", "cc", "cr", "out")   # ключи claude_totals.json


def _cost_lines(c):
    """Четыре долларовые строки и их объём в токенах, по всем моделям сразу.

    Возвращает (dollars, volume) -- два словаря с ключами _LINES. Одна
    реализация на все блоки, которые сравнивают деньги с объёмом: иначе два
    блока в одном документе начинают спорить друг с другом.
    """
    d = dict.fromkeys(_LINES, 0.0)
    v = dict.fromkeys(_LINES, 0)
    # Базис -- claude_totals.json: только там есть разбивка записи кэша по TTL
    # (e5m/e1h), а часовая запись стоит 2x против 1.25x пятиминутной. Счёт по
    # claude_deep.json давал на 39.66 доллара меньше, и отчёты публиковали одну
    # сумму там, где дашборд показывал другую.
    src = (c.cl or {}).get("by_model") or {}
    for m, x in src.items():
        b = rates.cost_breakdown(x, m)
        if b is None:
            continue          # без цены -- не ноль молча
        for k, key in zip(_LINES, ("unc", "cw", "cr", "out")):
            d[k] += b[key]
        for k, key in zip(_LINES, _KEYS):
            v[k] += x.get(key) or 0
    return d, v


@block("money_line")
def money_line(c):
    """Какая строка токенов реально создаёт деньги.

    Блок существует потому, что рукописный вывод в трёх документах утверждал
    обратное -- «деньги создаются только в строке некэшированного ввода» -- и
    противоречил таблице, стоявшей прямо над ним. Теперь вывод считается, а не
    формулируется по интуиции.
    """
    if not c.dp:
        return ["*Нет данных: claude_deep.json.*"]
    d, v = _cost_lines(c)
    td, tv = sum(d.values()), sum(v.values())
    if not td or not tv:
        return ["*Нет данных: стоимость не посчитана.*"]
    r = ["| строка | $ | доля денег | токенов | доля объёма | $ за млн |",
         "|---|---:|---:|---:|---:|---:|"]
    order = sorted(_LINES, key=lambda k: -d[k])
    for k in order:
        r.append("| %s | %s | %s | %s | %s | %s |" % (
            k, usd(d[k]), pct(d[k], td), f(v[k]), pct(v[k], tv),
            usd(d[k] / (v[k] / M)) if v[k] else "—"))
    top = order[0]
    cr, inp = v["чтение кэша"], v["свежий ввод"]
    ratio = (cr / inp) if inp else 0.0
    r += ["",
          "Крупнейшая статья — **%s**: %s всех денег. Свежий ввод на %d-м месте из четырёх."
          % (top, pct(d[top], td), order.index("свежий ввод") + 1)]
    if ratio:
        r.append("Скидка на чтение кэша десятикратная, но объём чтения больше свежего ввода "
                 "в **%.1f раза**, поэтому скидка перебита объёмом с запасом %.1fx. Отсюда "
                 "рычаг — не «поднять долю попаданий», а «пересылать меньше контекста за ход»: "
                 "чтение кэша растёт как контекст, умноженный на число ходов."
                 % (ratio, ratio / 10.0))
    if v["вывод"]:
        r.append("Дороже всего за токен — **вывод**, %s за млн, но его доля объёма %s, "
                 "поэтому на итог он влияет меньше всех."
                 % (usd(d["вывод"] / (v["вывод"] / M)), pct(v["вывод"], tv)))
    return r


@block("evidence")
def evidence(c):
    return [
        "| Класс | Что означает |", "|---|---|",
        "| **ИЗМЕРЕНО** | в локальном файле есть счётчик, и он пересчитан этим инструментом |",
        "| **СВЕРЕНО** | две или три независимые реализации дали одно и то же |",
        "| **ПО ОТЧЁТУ** | измерил только прежний аудит; исходных файлов больше нет |",
        "| **ПРОКСИ** | счётчика токенов не существует, вместо него метрика объёма |",
        "| **ОЦЕНКА** | арифметика поверх перечисленного, формула указана |",
        "",
        "Классы не смешиваются в одну цифру намеренно: смешивание — главный способ "
        "получить убедительно выглядящий неверный итог.",
    ]


@block("cost_breakdown")
def cost_breakdown(c):
    r = ["| модель | свежий ввод | запись кэша | чтение кэша | вывод | итого |",
         "|---|---:|---:|---:|---:|---:|"]
    # Седьмая копия таблиц ставок жила здесь, локальными переменными RA и OA --
    # поэтому её не видел даже разбор AST, искавший присваивания на уровне
    # модуля. Ставки берутся из tokenaudit_rates, базис -- claude_totals.json
    # с разбивкой записи кэша по TTL.
    tot = [0.0] * 5
    src = (c.cl or {}).get("by_model") or {}
    def _tok(x):
        return sum(x.get(k) or 0 for k in ("inp", "cc", "cr", "out"))
    for m, v in sorted(src.items(), key=lambda x: -_tok(x[1])):
        if _tok(v) == 0:
            continue
        b = rates.cost_breakdown(v, m)
        if b is None:
            continue
        a = [b["unc"], b["cw"], b["cr"], b["out"]]
        s = sum(a)
        for i in range(4):
            tot[i] += a[i]
        tot[4] += s
        r.append("| %s | %s | %s | %s | %s | **%s** |" % (
            m, usd(a[0]), usd(a[1]), usd(a[2]), usd(a[3]), usd(s)))
    r.append("| **итого** | %s | %s | %s | %s | **%s** |" % (
        usd(tot[0]), usd(tot[1]), usd(tot[2]), usd(tot[3]), usd(tot[4])))
    if tot[4]:
        r += ["", "Доли в деньгах: свежий ввод **%.1f%%**, запись кэша %.1f%%, "
              "чтение кэша **%.1f%%**, вывод %.1f%%." % tuple(
                  100.0 * x / tot[4] for x in tot[:4])]
    return r


@block("efficiency")
def efficiency(c):
    d, t = c.dp, c.total
    resp = c.cl["records_deduped"] or 1
    sess = c.cl["session_count"] or 1
    ah = d["active_time_hours_gap_le_300s"] or 0.1
    r = ["| производная метрика | значение |", "|---|---:|",
         "| токенов на ответ | %s |" % f(t / resp),
         "| токенов на сессию | %s |" % f(t / sess),
         "| токенов в активный час | %s |" % f(d["tokens_per_active_hour"]),
         "| $ на миллиард токенов | %s |" % usd(c.cost_total / (t / 1e9)),
         "| $ на ответ | %s |" % usd(c.cost_total / resp),
         "| $ на сессию | %s |" % usd(c.cost_total / sess),
         "| $ в активный час | %s |" % usd(c.cost_total / ah),
         "| ответов на сессию | %.1f |" % (resp / sess),
         "| контекста на токен вывода | %.0f : 1" % (
             (t - c.t["out"]) / max(1, c.t["out"])) + " |",
         "| доля вывода в объёме | %s |" % pct(c.t["out"], t),
         "| доля кэш-чтения в объёме | %s |" % pct(c.t["cr"], t)]
    ctx = c.t["inp"] + c.t["cc"] + c.t["cr"]
    r.append("| кэш-попадание по всему объёму | %s |" % pct(c.t["cr"], ctx))
    if c.t["cc"]:
        r.append("| чтений кэша на одну запись | %.1f |" % (c.t["cr"] / c.t["cc"]))
    return r


@block("hourly")
def hourly(c):
    b = c.cl.get("by_hour_of_day", {})
    if not b:
        return ["*Нет `by_hour_of_day`.*"]
    tot = sum(v["inp"] + v["cc"] + v["cr"] + v["out"] for v in b.values()) or 1
    r = ["| час UTC | токенов | доля | ответов |", "|---|---:|---:|---:|"]
    for k in sorted(b):
        v = b[k]
        s = v["inp"] + v["cc"] + v["cr"] + v["out"]
        r.append("| %s:00 | %s | %.2f%% | %s |" % (k, f(s), 100.0 * s / tot, f(v["n"])))
    peak = max(b, key=lambda k: sum(b[k][x] for x in FIELDS))
    r += ["", "Пик приходится на **%s:00 UTC**." % peak]
    return r


@block("weekday")
def weekday(c):
    b = c.cl.get("by_weekday", {})
    if not b:
        return ["*Нет `by_weekday`.*"]
    RU = {"Mon": "понедельник", "Tue": "вторник", "Wed": "среда", "Thu": "четверг",
          "Fri": "пятница", "Sat": "суббота", "Sun": "воскресенье"}
    tot = sum(sum(v[x] for x in FIELDS) for v in b.values()) or 1
    r = ["| день недели | токенов | доля | ответов |", "|---|---:|---:|---:|"]
    for k in sorted(b):
        v = b[k]
        s = sum(v[x] for x in FIELDS)
        r.append("| %s | %s | %.2f%% | %s |" % (
            RU.get(k[2:], k[2:]), f(s), 100.0 * s / tot, f(v["n"])))
    return r


@block("top_sessions")
def top_sessions(c):
    ts = c.dp.get("top_sessions", {})
    if not ts:
        return ["*Нет `top_sessions`.*"]
    r = ["| # | сессия | токенов | доля | ответов | длит. | т/час | модель | субаг. |",
         "|---:|---|---:|---:|---:|---:|---:|---|---:|"]
    for i, (sid, v) in enumerate(list(ts.items())[:25], 1):
        r.append("| %d | %s… | %s | %s | %s | %s | %s | %s | %s |" % (
            i, sid[:8], f(v["total"]), pct(v["total"], c.total), f(v["responses"]),
            ("%.1f ч" % (v["duration_s"] / 3600)) if v.get("duration_s") else "—",
            f(v["tokens_per_hour"]) if v.get("tokens_per_hour") else "—",
            (v.get("dominant_model") or "—").replace("claude-", ""),
            f(v.get("sidechain_responses", 0))))
    return r


@block("session_stats")
def session_stats(c):
    d = c.dp["session_size_distribution"]
    sl = c.dp.get("sessions_slim", [])
    durs = sorted(x["duration_s"] for x in sl if x.get("duration_s"))
    q = lambda a, p: a[min(len(a) - 1, int(p * (len(a) - 1)))] if a else 0
    r = ["| статистика по сессиям | значение |", "|---|---:|",
         "| сессий с расходом | %s |" % f(d["n"]),
         "| медиана объёма | %s |" % f(d["median"]),
         "| p90 объёма | %s |" % f(d["p90"]),
         "| p99 объёма | %s |" % f(d["p99"]),
         "| максимум объёма | %s |" % f(d["max"]),
         "| максимум / медиана | ×%.0f |" % (d["max"] / max(1, d["median"]))]
    if durs:
        r += ["| медиана длительности | %.1f ч |" % (q(durs, .5) / 3600),
              "| p90 длительности | %.1f ч |" % (q(durs, .9) / 3600),
              "| максимум длительности | %.1f ч |" % (durs[-1] / 3600),
              "| суммарная длительность сессий | %.1f ч |" % (sum(durs) / 3600)]
    h = c.dp.get("session_size_histogram", {})
    if h:
        r += ["", "| порядок величины | сессий |", "|---|---:|"]
        for k in sorted(h, key=lambda x: -1 if x == "0" else int(x.replace("1e", ""))):
            r.append("| %s | %s |" % ("0" if k == "0" else "10^" + k.replace("1e", ""), h[k]))
    return r


@block("output_dist")
def output_dist(c):
    o = c.dp.get("output_size_buckets", {})
    p = c.dp.get("output_per_response", {})
    r = []
    if p:
        r += ["| вывод на ответ | токенов |", "|---|---:|",
              "| медиана | %s |" % f(p["median"]), "| p90 | %s |" % f(p["p90"]),
              "| p99 | %s |" % f(p["p99"]), "| максимум | %s |" % f(p["max"]),
              "| среднее | %s |" % f(p["mean"]), ""]
    if o:
        tot = sum(o.values()) or 1
        r += ["| диапазон | ответов | доля |", "|---|---:|---:|"]
        for k in ("0", "1-99", "100-499", "500-1999", "2000-7999", "8000+"):
            if k in o:
                r.append("| %s | %s | %.1f%% |" % (k, f(o[k]), 100.0 * o[k] / tot))
    return r or ["*Нет распределения вывода.*"]


@block("gaps")
def gaps(c):
    g = c.dp.get("gap_between_responses_s", {})
    if not g:
        return ["*Нет статистики промежутков.*"]
    return ["| промежуток между ответами | секунд |", "|---|---:|",
            "| медиана | %s |" % f(g["median"]), "| p90 | %s |" % f(g["p90"]),
            "| p99 | %s |" % f(g["p99"]), "| максимум | %s |" % f(g["max"]),
            "| среднее | %.1f |" % g["mean"], "",
            "Активного времени (промежутки ≤300 с): **%s ч**." %
            c.dp["active_time_hours_gap_le_300s"]]


@block("projects")
def projects(c):
    b = c.cl.get("by_project", {})
    if not b:
        return ["*Нет `by_project`.*"]
    r = ["| каталог проекта | токенов | доля | ответов |", "|---|---:|---:|---:|"]
    for k, v in sorted(b.items(), key=lambda x: -sum(x[1][z] for z in FIELDS)):
        s = sum(v[z] for z in FIELDS)
        r.append("| `%s` | %s | %s | %s |" % (k, f(s), pct(s, c.total), f(v["n"])))
    return r


@block("versions")
def versions(c):
    r = []
    bv = c.cl.get("by_version", {})
    if bv:
        r += ["| версия Claude Code | токенов | ответов |", "|---|---:|---:|"]
        for k, v in sorted(bv.items(), key=lambda x: -sum(x[1][z] for z in FIELDS)):
            r.append("| %s | %s | %s |" % (k, f(sum(v[z] for z in FIELDS)), f(v["n"])))
        r.append("")
    bt = c.cl.get("by_tier", {})
    if bt:
        r += ["| service_tier | токенов | ответов |", "|---|---:|---:|"]
        for k, v in sorted(bt.items(), key=lambda x: -sum(x[1][z] for z in FIELDS)):
            r.append("| %s | %s | %s |" % (k, f(sum(v[z] for z in FIELDS)), f(v["n"])))
    return r or ["*Нет данных о версиях.*"]


@block("periods")
def periods(c):
    """Auto-segments days into cache regimes and prices each regime."""
    df = c.dp.get("by_day_full", {})
    if not df:
        return ["*Нет `by_day_full`.*"]
    def regime(p):
        return "хороший (≥85%)" if p >= 85 else ("средний (50–85%)" if p >= 50
                                                else "плохой (<50%)")
    agg = {}
    for k in sorted(df):
        v = df[k]
        g = regime(v["cache_pct"])
        a = agg.setdefault(g, {"days": 0, **{x: 0 for x in FIELDS}, "total": 0, "n": 0})
        a["days"] += 1
        a["total"] += v["total"]
        a["n"] += v["n"]
        for x in FIELDS:
            a[x] += v[x]
    r = ["| режим кэша | дней | токенов | ответов | $ | $ за млрд |",
         "|---|---:|---:|---:|---:|---:|"]
    for g in ("хороший (≥85%)", "средний (50–85%)", "плохой (<50%)"):
        if g not in agg:
            continue
        a = agg[g]
        cost = rates.day_cost(a)
        r.append("| %s | %s | %s | %s | %s | %s |" % (
            g, a["days"], f(a["total"]), f(a["n"]), usd(cost),
            usd(cost / max(1e-9, a["total"] / 1e9))))
    good = agg.get("хороший (≥85%)")
    bad = agg.get("плохой (<50%)")
    if good and bad:
        gc = rates.day_cost(good) / max(1e-9, good["total"] / 1e9)
        bc = rates.day_cost(bad) / max(1e-9, bad["total"] / 1e9)
        r += ["", "Токен в дни с плохим кэшем дороже в **%.1f раза** (%s против %s за "
              "миллиард). Это главный управляемый рычаг стоимости." % (
                  bc / max(1e-9, gc), usd(bc), usd(gc))]
    return r


@block("codex_models")
def codex_models(c):
    if not c.ch or "by_model" not in c.ch:
        return ["*Нет данных Codex — `refresh.py --codex`.*"]
    bm = c.ch["by_model"]
    tot = sum(v["total_tokens"] for v in bm.values()) or 1
    # Восьмая и последняя копия таблиц ставок жила здесь. Ставки OpenAI берутся
    # из tokenaudit_rates; модель без публичного тарифа остаётся без цены, а не
    # получает ноль.
    r = ["| модель | токенов | доля | кэшированный ввод | вывод | $ |",
         "|---|---:|---:|---:|---:|---:|"]
    ts = 0.0
    unpriced = []
    for m, v in sorted(bm.items(), key=lambda x: -x[1]["total_tokens"]):
        cost = "—"
        unc = max(0, v["input_tokens"] - v["cached_input_tokens"])
        x = rates.openai_cost(unc, v["cached_input_tokens"], v["output_tokens"], m)
        if x is None:
            unpriced.append(m)
        else:
            ts += x
            cost = usd(x)
        r.append("| %s | %s | %.2f%% | %s | %s | %s |" % (
            m, f(v["total_tokens"]), 100.0 * v["total_tokens"] / tot,
            f(v["cached_input_tokens"]), f(v["output_tokens"]), cost))
    r.append("| **итого** | **%s** | | | | **%s** |" % (f(tot), usd(ts)))
    note = ("*Тарифы OpenAI из `tokenaudit_rates` (каталог `developers.openai.com`, "
            "проверен 2026-06-06).*")
    if unpriced:
        note += (" *Без публичного тарифа и потому без цены: %s.*"
                 % ", ".join("`%s`" % x for x in sorted(unpriced)))
    r += ["", note]
    return r


@block("codex_days")
def codex_days(c):
    if not c.ch or "by_day" not in c.ch:
        return ["*Нет данных Codex — `refresh.py --codex`.*"]
    b = c.ch["by_day"]
    r = ["| день | всего | ввод | кэшированный | вывод | reasoning |",
         "|---|---:|---:|---:|---:|---:|"]
    for k in sorted(b):
        v = b[k]
        r.append("| %s | %s | %s | %s | %s | %s |" % (
            k, f(v.get("total_tokens", 0)), f(v.get("input_tokens", 0)),
            f(v.get("cached_input_tokens", 0)), f(v.get("output_tokens", 0)),
            f(v.get("reasoning_output_tokens", 0))))
    tot = sum(v.get("total_tokens", 0) for v in b.values())
    r += ["", "Дней с расходом **%d**, суммарно **%s** токенов, в среднем **%s** в день."
          % (len(b), f(tot), f(tot / max(1, len(b))))]
    return r


@block("antigravity_types")
def ag_types(c):
    if not c.ag:
        return ["*Нет данных Antigravity — `refresh.py --antigravity`.*"]
    t = c.ag["record_type_counts"]
    ch = c.ag.get("chars_by_record_type", {})
    tot = sum(t.values()) or 1
    r = ["| тип шага | записей | доля | символов |", "|---|---:|---:|---:|"]
    for k, v in list(t.items())[:16]:
        r.append("| `%s` | %s | %.2f%% | %s |" % (k, f(v), 100.0 * v / tot,
                                                  f(ch.get(k, 0))))
    return r


@block("antigravity_days")
def ag_days(c):
    if not c.ag:
        return ["*Нет данных Antigravity — `refresh.py --antigravity`.*"]
    b = c.ag.get("by_day", {})
    q = c.ag.get("quota_blocks_by_day", {})
    r = ["| день | ходов модели | записей | символов | упоров в квоту |",
         "|---|---:|---:|---:|---:|"]
    for k in sorted(b):
        v = b[k]
        r.append("| %s | %s | %s | %s | %s |" % (
            k, f(v.get("model_turns", 0)), f(v.get("records", 0)),
            f(v.get("chars", 0)), f(q.get(k, 0))))
    r += ["", "**Это прокси-метрика, не токены.** Транскрипт хранит каждое сообщение "
          "один раз, а API получает весь контекст заново на каждом ходу, поэтому "
          "прокси систематически занижает реальный расход."]
    return r


@block("scale_compare")
def scale_compare(c):
    # codex_total() возвращает None, когда внешних измерений нет. Здесь нужен
    # ноль для арифметики, но отличать «ноль» от «нет цифры» всё равно
    # обязательно, иначе блок напечатает 0 как измеренное значение.
    codex = codex_total(c)[0] or 0
    allt = codex + c.total
    G_MONTH = 3.2e15
    g_sec = G_MONTH / (30 * 86400)
    r = ["| ориентир | значение | сколько ему нужно на весь измеренный объём |",
         "|---|---|---|",
         "| Google, все поверхности | 3.2 квадриллиона токенов/месяц | **%.0f с** |"
         % (allt / g_sec),
         "| Google, только model API | 19 млрд токенов/минуту | %.1f мин |"
         % (allt / 19e9),
         "| OpenAI, платформа | 6 млрд токенов/минуту | %.1f мин |" % (allt / 6e9),
         "| OpenRouter | ~100 трлн/месяц | %.0f мин |" % (allt / (1e14 / 30 / 1440)),
         "", "| сравнение с разработчиком | значение |", "|---|---|"]
    days = 21.0
    per_day = c.cost_total / days
    r += ["| твой Claude Code | %s в сутки |" % usd(per_day),
          "| Anthropic: типичные $13 на разработчика в активный день | **×%.1f** |"
          % (per_day / 13),
          "| 90%% пользователей тратят <$30 в активный день | ×%.1f от порога |"
          % (per_day / 30),
          "| типичный месяц $150–250 | ×%.0f–%.0f |"
          % (per_day * 30 / 250, per_day * 30 / 150),
          "| «тяжёлая автоматизация» $500–2000/мес | ×%.1f–%.1f |"
          % (per_day * 30 / 2000, per_day * 30 / 500)]
    BOOK = 106657.0
    uniq = c.t["inp"] + 5325139889
    r += ["", "| в человеческих единицах | значение |", "|---|---|",
          "| весь измеренный объём | %s токенов |" % f(allt),
          "| книг по 80 тыс. слов | ~%s |" % f(allt / BOOK),
          "| лет непрерывного чтения на 250 сл/мин | ~%.0f |" % (allt * 5.708e-9),
          "| из этого уникального текста | %s (**%s**) |" % (f(uniq), pct(uniq, allt)),
          "| то есть уникальных книг | ~%s |" % f(uniq / BOOK),
          "", "Остальное — пересылка того же самого. Это не миллион разных книг, а "
          "несколько книг, перечитанных миллион раз.",
          "", "*Внешние цифры — самоотчёты вендоров, не аудированные. Сравнение "
          "показывает порядок величины.*"]
    return r


@block("scope")
def scope(c):
    return [
        "**Что входит в измеренное.** Только логи с машин владельца. Со слов владельца, "
        "в середине мая конфигурационный файл с токенами входа был роздан десяти людям, и "
        "суммарно они израсходовали «примерно столько же, может чуть больше». Их расход "
        "шёл через те же учётные данные, но с их дисков, и в локальных данных отсутствует "
        "физически — не «не найден», а не может здесь быть.",
        "",
        "| составляющая | токенов | класс |", "|---|---:|---|",
        "| измерено здесь | %s | ИЗМЕРЕНО / ПО ОТЧЁТУ |" % f(
            c.total + (codex_total(c)[0] or 0)),
        "| остальные десять человек | ~124–150 млрд | СЛОВА ВЛАДЕЛЬЦА |",
        "| **суммарно через эти учётки** | **~250–275 млрд** | ОЦЕНКА |",
        "",
        "**Фактические денежные траты за всё время — около 1000 рублей**, целиком на "
        "аккаунты и подписки для 19 Google-аккаунтов. По Codex и Claude Code — ноль. "
        "Все суммы в долларах в этом отчёте — **эквивалент по публичному прайсу, а не "
        "траты**.",
    ]


@block("key_findings")
def key_findings(c):
    codex, up = codex_total(c)
    ctx = c.t["inp"] + c.t["cc"] + c.t["cr"]
    df = c.dp.get("by_day_full", {})
    good = [v for v in df.values() if v["cache_pct"] >= 85]
    bad = [v for v in df.values() if v["cache_pct"] < 50]
    def price(days):
        t = sum(v["total"] for v in days) or 1
        cost = sum(rates.day_cost(v) for v in days)
        return cost / (t / 1e9)
    r = ["| | |", "|---|---:|",
         "| Codex, консервативно | %s |" % (
             ("**%s** токенов" % f(codex)) if codex else "*%s*" % NO_EXTERNAL),
         "| Codex, верхняя граница | %s |" % (f(up) if up else "—"),
         "| Claude Code, измерено | **%s** |" % f(c.total),
         "| Claude Code, эквивалент по прайсу | **%s** |" % usd(c.cost_total),
         "| Antigravity | счётчика не существует |",
         "| доля кэш-чтения в объёме Claude Code | **%s** |" % pct(c.t["cr"], c.total),
         "| доля вывода в объёме | %s |" % pct(c.t["out"], c.total),
         "| завышение при наивной сумме | **×%.2f** |" % (
             (c.cl["totals_raw"]["inp"] + c.cl["totals_raw"]["cc"]
              + c.cl["totals_raw"]["cr"] + c.cl["totals_raw"]["out"]) / max(1, c.total))]
    # Крупнейшая статья расхода считается, а не подставляется: рукописный вывод
    # в трёх документах называл свежий ввод, а он третий из четырёх.
    _d, _v = _cost_lines(c)
    if sum(_d.values()):
        _top = max(_d, key=lambda k: _d[k])
        r.append("| крупнейшая статья расхода | **%s, %s** |"
                 % (_top, pct(_d[_top], sum(_d.values()))))
    if good and bad:
        r.append("| токен дороже в дни со сломанным кэшем | **×%.1f** |"
                 % (price(bad) / max(1e-9, price(good))))
    return r

def _external():
    """Внешние ориентиры из external_measurements.json. Нет файла -- пусто.

    Читается тут, а не в Ctx, чтобы блок оставался единственным потребителем и
    не тянул зависимость в остальные.
    -> dict[str, dict]
    """
    import json as _j
    import os as _o
    path = _o.path.join(_rg.HERE, "external_measurements.json")
    if not _o.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = _j.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for k, v in (data or {}).items():
        if k.startswith("_") or not isinstance(v, dict):
            continue
        if isinstance(v.get("value"), (int, float)) and v.get("source"):
            out[k] = v
    return out


@block("benchmarks")
def benchmarks(c):
    """Расход против публичных ориентиров. Множители считаются, а не вписываются.

    Раньше эта таблица была рукописной и говорила «$4 873.87 за 20 дней =
    $243.69 в сутки» при фактических $21 269 и $967 -- то есть отставала в
    четыре раза. Ориентиры лежат в external_measurements.json с указанием
    источника, а множители пересчитываются от текущего расхода.
    """
    if not c.cl or not c.cost_total:
        return ["*Нет данных: стоимость не посчитана.*"]
    ext = _external()
    if not ext:
        return ["*Внешних ориентиров нет: external_measurements.json отсутствует "
                "или в нём не осталось записей с происхождением.*"]
    # Базис -- сутки ВСПЛЕСКА, а не весь период. Средний по периоду занижает
    # множители в 6.5 раза, потому что 19 тихих суток размывают трое рабочих.
    all_d, quiet, hot, cost = _spike(c)
    if hot:
        per_day = sum(cost[d] for d in hot) / len(hot)
        basis = "%d суток всплеска (%s .. %s)" % (len(hot), hot[0], hot[-1])
    else:
        per_day = c.cost_total / (len(all_d) or 1)
        basis = "%d суток, режим ровный -- всплеска не найдено" % len(all_d)
    per_month = per_day * 30
    r = ["| ориентир | значение | наш расход | множитель |",
         "|---|---:|---:|---:|"]

    def one(key, label, per):
        v = ext.get(key)
        if not v:
            return
        base = float(v["value"])
        got = per_day if per == "day" else per_month
        r.append("| %s | %s | %s | **%.0f×** |" % (
            label, usd(base) + ("/сут" if per == "day" else "/мес"),
            usd(got) + ("/сут" if per == "day" else "/мес"), got / base))

    def rng(lo_key, hi_key, label, per):
        lo, hi = ext.get(lo_key), ext.get(hi_key)
        if not (lo and hi):
            return
        a, b = float(lo["value"]), float(hi["value"])
        got = per_day if per == "day" else per_month
        r.append("| %s | %s–%s | %s | **%.0f–%.0f×** |" % (
            label, usd(a), usd(b) + ("/сут" if per == "day" else "/мес"),
            usd(got) + ("/сут" if per == "day" else "/мес"), got / b, got / a))

    one("bench_claude_per_dev_active_day",
        "Claude Code, медиана на разработчика в активный день", "day")
    one("bench_claude_p90_active_day",
        "Claude Code, порог 90% пользователей", "day")
    rng("bench_claude_typical_month_low", "bench_claude_typical_month_high",
        "Claude Code, типичный месяц", "month")
    rng("bench_claude_heavy_month_low", "bench_claude_heavy_month_high",
        "Claude Code, «тяжёлая автоматизация»", "month")
    rng("bench_codex_heavy_month_low", "bench_codex_heavy_month_high",
        "Codex, тяжёлый пользователь", "month")
    rng("bench_subscription_ide_low", "bench_subscription_ide_high",
        "подписка на ассистента в IDE", "month")
    r += ["",
          "Базис -- %s: **%s в сутки**, около **%s в месяц** в пересчёте. "
          "Все ориентиры внешние, источник у каждого указан в "
          "`external_measurements.json`." % (basis, usd(per_day), usd(per_month))]
    if hot and quiet:
        q_day = sum(cost[d] for d in quiet) / len(quiet)
        a_day = c.cost_total / (len(all_d) or 1)
        r.append("Почему именно этот базис. За весь период выходит %s в сутки, но "
                 "это среднее по %d суткам, из которых %d тихие (%s в сутки) и "
                 "только %d рабочие. Всплеск дороже тихих суток в **%.0f раза** и "
                 "дороже среднего по периоду в **%.1f раза**, поэтому множители, "
                 "посчитанные от среднего, занижены во столько же."
                 % (usd(a_day), len(all_d), len(quiet), usd(q_day), len(hot),
                    per_day / q_day if q_day else 0, per_day / a_day if a_day else 0))
    g = ext.get("bench_google_tokens_per_month")
    if g:
        share = 100.0 * c.total / float(g["value"])
        r.append("Для масштаба: у Google **%s** токенов в месяц на всех поверхностях "
                 "(%s). Наш измеренный объём -- **%.7f%%** от этого." %
                 (f(g["value"]), g["source"].split(":")[0], share))
    return r

@block("burn_forecast")
def burn_forecast(c):
    """Темп расхода и когда он доводит до порогов. Считается из снимков.

    Блок отвечает на вопрос «когда догоним Codex и сколько сожжём». Темп берётся
    из snapshots.jsonl на двух окнах, потому что одно окно вводит в заблуждение:
    свежий темп -- это темп работы с веером субагентов, средний по сессии втрое
    ниже. Прогноз с одним числом выглядел бы точнее, чем он есть.
    """
    if len(c.snaps) < 3 or not c.cost_total or not c.total:
        return ["*Истории меньше трёх прогонов — темп считать не на чем.*"]
    import datetime as _dt

    def _t(x):
        return _dt.datetime.strptime(x[:19], "%Y-%m-%dT%H:%M:%S")

    def _rate(n):
        a, b = c.snaps[-n], c.snaps[-1]
        h = (_t(b["ts"]) - _t(a["ts"])).total_seconds() / 3600.0
        return ((b["claude"]["total"] - a["claude"]["total"]) / h) if h > 0 else 0.0

    usd_per_tok = c.cost_total / c.total
    fast = _rate(min(5, len(c.snaps)))
    avg = _rate(len(c.snaps))
    if fast <= 0 or avg <= 0:
        return ["*Темп не посчитать: между снимками нет прироста.*"]
    r = ["| темп | токенов в час | $ в час |", "|---|---:|---:|",
         "| последние прогоны | %s | %s |" % (f(fast), usd(fast * usd_per_tok)),
         "| в среднем за всю историю | %s | %s |" % (f(avg), usd(avg * usd_per_tok)),
         "",
         "Цена факта: **%s за миллион токенов** — считается от измеренного объёма "
         "и измеренной стоимости, а не по прайсу одной модели."
         % usd(usd_per_tok * 1e6), ""]
    cons, up = codex_total(c)
    rows = []
    if cons:
        rows.append(("догнать Codex, консервативный итог", cons - c.total))
    if up:
        rows.append(("догнать Codex, верхняя граница", up - c.total))
    for th in (50000, 100000, 250000, 1000000):
        rows.append(("порог %s" % usd(th), th / usd_per_tok - c.total))
    r += ["| цель | осталось токенов | суток на свежем темпе | на среднем |",
          "|---|---:|---:|---:|"]
    for label, need in rows:
        if need <= 0:
            r.append("| %s | пройдено | — | — |" % label)
            continue
        r.append("| %s | %s | **%.1f** | %.1f |"
                 % (label, f(need), need / fast / 24.0, need / avg / 24.0))
    r += ["",
          "Оговорка, без которой прогноз бессмысленный: свежий темп -- это темп "
          "работы с веером субагентов, и держится он минутами, а не сутками. "
          "Средний по всей истории в **%.1f раза** ниже, и он ближе к правде на "
          "длинном горизонте." % (fast / avg if avg else 0)]
    r.append("При непрерывной работе на свежем темпе месяц дал бы **%s**, год — **%s**."
             % (usd(fast * 24 * 30 * usd_per_tok), usd(fast * 24 * 365 * usd_per_tok)))
    return r

def _spike(c):
    """Разделить сутки на тихие и всплеск. Перелом ищется, а не задаётся датой.

    ЗАЧЕМ. Средний расход по всему периоду вводит в заблуждение, когда работа
    была не ровной. Измерено: 22 суток дают $994 в сутки, но 19 из них тихие
    ($124), а трое суток всплеска -- $6 504, то есть в 52.6 раза дороже тихих и
    в 6.5 раза дороже среднего. Множители против индустрии, посчитанные от
    среднего, занижены в 6.5 раза.

    Правило: сутки, где объём больше десяти медиан по суткам. На текущих данных
    оно выделяет ровно 27-29 июля -- 94.94% всего объёма. Порог намеренно грубый:
    он должен ловить смену режима, а не колебания внутри режима.
    -> (все сутки, тихие, всплеск, {день: $})
    """
    import statistics as _st
    bd = (c.cl or {}).get("by_day") or {}
    days = sorted(bd)
    if len(days) < 4:
        return days, days, [], {d: rates.day_cost(bd[d]) for d in days}
    vol = {d: sum(bd[d].get(k, 0) for k in _KEYS) for d in days}
    cost = {d: rates.day_cost(bd[d]) for d in days}
    med = _st.median(vol.values()) or 1
    hot = [d for d in days if vol[d] > med * 10]
    quiet = [d for d in days if d not in hot]
    return days, quiet, hot, cost

@block("api_scale")
def api_scale(c):
    """Расход против платформы API, а не против подписок.

    Подписочные ориентиры отвечают на вопрос «сколько платит разработчик»,
    а этот -- «сколько прокачивает платформа и какая доля приходится на нас».
    Оба нужны: первый про деньги, второй про масштаб.

    Средний пользователь считается делением объёма платформы на число
    разработчиков и помечен как слабый ориентир. Это среднее по всем аккаунтам,
    включая спящие; распределение расхода по API сильно скошено, а перцентилей
    публично нет. Подставлять среднее вместо медианы и молчать об этом значит
    выдавать слабую оценку за сильную.
    """
    ext = _external()
    need = ("bench_openrouter_tokens_per_month", "bench_openrouter_developers")
    if not all(k in ext for k in need):
        return ["*Внешних данных по платформе API нет: нужны записи %s в "
                "`external_measurements.json`.*" % ", ".join("`%s`" % k for k in need)]
    if not c.cl:
        return ["*Нет данных: claude_totals.json.*"]
    plat_month = float(ext["bench_openrouter_tokens_per_month"]["value"])
    devs = float(ext["bench_openrouter_developers"]["value"]) or 1
    per_dev = plat_month / devs
    all_d, quiet, hot, _cost = _spike(c)
    bd = (c.cl or {}).get("by_day") or {}
    vol = {d: sum(bd[d].get(k, 0) for k in _KEYS) for d in bd}
    tot = sum(vol.values()) or 1
    avg_month = tot / (len(all_d) or 1) * 30
    hot_month = (sum(vol[d] for d in hot) / len(hot) * 30) if hot else avg_month
    r = ["| база | токенов в месяц | против среднего пользователя | доля платформы |",
         "|---|---:|---:|---:|",
         "| наш средний темп | %s | **%.0f×** | %.4f%% |"
         % (f(avg_month), avg_month / per_dev, 100.0 * avg_month / plat_month)]
    if hot:
        r.append("| наш темп в рабочие сутки | %s | **%.0f×** | %.4f%% |"
                 % (f(hot_month), hot_month / per_dev, 100.0 * hot_month / plat_month))
    r += ["| средний пользователь платформы | %s | 1× | — |" % f(per_dev),
          "| вся платформа | %s | %s пользователей | 100%% |" % (f(plat_month), f(devs)),
          "",
          "Источник: %s" % ext["bench_openrouter_tokens_per_month"]["source"], ""]
    r.append("Средний пользователь -- это объём платформы, делённый на число "
             "аккаунтов, то есть среднее вместе со спящими. Распределение расхода "
             "по API сильно скошено, а перцентилей публично нет, поэтому ориентир "
             "слабый и назван слабым. Сильный ориентир дал бы публичный рейтинг "
             "приложений OpenRouter по токенам, но для него нужен ключ доступа.")
    share = ext.get("bench_openrouter_share_of_google")
    yr = ext.get("bench_openrouter_tokens_per_year")
    if share and yr:
        g_hi = float(yr["value"]) / float(share["value"])
        our_year = hot_month / 30.0 * 365
        r.append("Сверху по масштабу: платформа это %.0f%% годового объёма Google, "
                 "то есть у Google порядка %s токенов в год. Наш годовой темп в "
                 "рабочем режиме -- %s, или **%.4f%%** от него."
                 % (100 * float(share["value"]), f(g_hi), f(our_year),
                    100.0 * our_year / g_hi))
    return r

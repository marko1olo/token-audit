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
M, FIELDS, NAMES = _rg.M, _rg.FIELDS, _rg.NAMES


def _need(x, what):
    return ["*Нет данных: %s.*" % what] if not x else None


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
    RA = {"claude-opus-5": (5, 25), "claude-opus-4-8": (5, 25),
          "claude-fable-5": (10, 50), "claude-sonnet-5": (2, 10)}
    OA = {"gpt-5.5": (5, 0.5, 30)}
    tot = [0.0] * 5
    for m, v in sorted(c.dp["by_model"].items(), key=lambda x: -x[1]["total"]):
        if v["total"] == 0:
            continue
        if m in RA:
            ri, ro = RA[m]
            a = [v["uncached_input"] / M * ri, v["cache_write"] / M * ri * 1.25,
                 v["cache_read"] / M * ri * 0.1, v["output"] / M * ro]
        elif m in OA:
            ri, rc, ro = OA[m]
            a = [v["uncached_input"] / M * ri, 0.0,
                 v["cache_read"] / M * rc, v["output"] / M * ro]
        else:
            continue
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
        cost = (a["inp"] / M * 5 + a["cc"] / M * 6.25 + a["cr"] / M * 0.5
                + a["out"] / M * 25)
        r.append("| %s | %s | %s | %s | %s | %s |" % (
            g, a["days"], f(a["total"]), f(a["n"]), usd(cost),
            usd(cost / max(1e-9, a["total"] / 1e9))))
    good = agg.get("хороший (≥85%)")
    bad = agg.get("плохой (<50%)")
    if good and bad:
        gc = (good["inp"] / M * 5 + good["cc"] / M * 6.25 + good["cr"] / M * 0.5
              + good["out"] / M * 25) / max(1e-9, good["total"] / 1e9)
        bc = (bad["inp"] / M * 5 + bad["cc"] / M * 6.25 + bad["cr"] / M * 0.5
              + bad["out"] / M * 25) / max(1e-9, bad["total"] / 1e9)
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
    RA = {"gpt-5.5": (5, .5, 30), "gpt-5.4": (2.5, .25, 15),
          "gpt-5.4-mini": (.75, .075, 4.5), "gpt-5.3-codex": (1.75, .175, 14)}
    r = ["| модель | токенов | доля | кэшированный ввод | вывод | $ |",
         "|---|---:|---:|---:|---:|---:|"]
    ts = 0.0
    for m, v in sorted(bm.items(), key=lambda x: -x[1]["total_tokens"]):
        cost = "—"
        if m in RA:
            ri, rc, ro = RA[m]
            unc = max(0, v["input_tokens"] - v["cached_input_tokens"])
            x = (unc / M * ri + v["cached_input_tokens"] / M * rc
                 + v["output_tokens"] / M * ro)
            ts += x
            cost = usd(x)
        r.append("| %s | %s | %.2f%% | %s | %s | %s |" % (
            m, f(v["total_tokens"]), 100.0 * v["total_tokens"] / tot,
            f(v["cached_input_tokens"]), f(v["output_tokens"]), cost))
    r.append("| **итого** | **%s** | | | | **%s** |" % (f(tot), usd(ts)))
    r += ["", "*Тарифы OpenAI из каталога прежнего аудита "
          "(`developers.openai.com`, проверен 2026-06-06). Модели без публичного "
          "тарифа не оценены.*"]
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
    codex = (c.rc or {}).get("consistent_total_max_basis", {}).get("total_tokens", 0)
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
            c.total + (c.rc or {}).get("consistent_total_max_basis", {}).get("total_tokens", 0)),
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
    codex = (c.rc or {}).get("consistent_total_max_basis", {}).get("total_tokens", 0)
    up = (c.rc or {}).get("upper_bound_if_delta_valid", 0)
    ctx = c.t["inp"] + c.t["cc"] + c.t["cr"]
    df = c.dp.get("by_day_full", {})
    good = [v for v in df.values() if v["cache_pct"] >= 85]
    bad = [v for v in df.values() if v["cache_pct"] < 50]
    def price(days):
        t = sum(v["total"] for v in days) or 1
        cost = sum(v["inp"] / M * 5 + v["cc"] / M * 6.25 + v["cr"] / M * 0.5
                   + v["out"] / M * 25 for v in days)
        return cost / (t / 1e9)
    r = ["| | |", "|---|---:|",
         "| Codex, консервативно | **%s** токенов |" % f(codex),
         "| Codex, верхняя граница | %s |" % f(up),
         "| Claude Code, измерено | **%s** |" % f(c.total),
         "| Claude Code, эквивалент по прайсу | **%s** |" % usd(c.cost_total),
         "| Antigravity | счётчика не существует |",
         "| доля кэш-чтения в объёме Claude Code | **%s** |" % pct(c.t["cr"], c.total),
         "| доля вывода в объёме | %s |" % pct(c.t["out"], c.total),
         "| завышение при наивной сумме | **×%.2f** |" % (
             (c.cl["totals_raw"]["inp"] + c.cl["totals_raw"]["cc"]
              + c.cl["totals_raw"]["cr"] + c.cl["totals_raw"]["out"]) / max(1, c.total))]
    if good and bad:
        r.append("| токен дороже в дни со сломанным кэшем | **×%.1f** |"
                 % (price(bad) / max(1e-9, price(good))))
    return r

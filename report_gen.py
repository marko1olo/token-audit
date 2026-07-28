#!/usr/bin/env python3
"""Fills AUTO blocks in the markdown reports from the measured JSON.

Contract:  in any .md file, a region shaped like

    <!-- AUTO:by_model -->
    ...anything here is replaced...
    <!-- /AUTO -->

is regenerated from data on every run. Prose outside the markers is never
touched. This exists because hand-carried numbers drift: the 138.9 B headline in
the old ledger was 28% wrong for seven weeks precisely because nothing
regenerated it.

Also runs cross-file integrity checks, because two aggregators measuring the same
transcripts must agree, and silent divergence is the failure mode that matters.
"""
import io
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FIELDS = ("inp", "cc", "cr", "out")
NAMES = {"inp": "свежий ввод", "cc": "запись кэша", "cr": "чтение кэша", "out": "вывод"}
RATES = {"claude-opus-5": (5, 25), "claude-opus-4-8": (5, 25),
         "claude-fable-5": (10, 50), "claude-sonnet-5": (2, 10)}
OPENAI = {"gpt-5.5": (5, 0.5, 30)}
M = 1_000_000.0


def L(name):
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return None
    with io.open(p, encoding="utf-8") as fh:
        return json.load(fh)


def f(n):
    return "{:,}".format(int(round(n))).replace(",", " ")


def usd(n):
    return "$" + "{:,.2f}".format(n).replace(",", " ").replace(".", ",")


def pct(a, b):
    return "%.2f%%" % (100.0 * a / b) if b else "—"


class Ctx:
    def __init__(self):
        self.cl = L("claude_totals.json")
        self.dp = L("claude_deep.json")
        self.ch = L("codex_chains_totals.json")
        self.cx = L("codex_totals.json")
        self.ag = L("antigravity_totals.json")
        self.rc = L("reconciliation.json")
        snap = os.path.join(HERE, "snapshots.jsonl")
        self.snaps = []
        if os.path.exists(snap):
            with io.open(snap, encoding="utf-8") as fh:
                self.snaps = [json.loads(x) for x in fh if x.strip()]
        self.t = self.cl["totals_deduped"] if self.cl else {}
        self.total = sum(self.t.get(k, 0) for k in FIELDS)
        self.cost, self.cost_total = self._cost()

    def _cost(self):
        out, tot = {}, 0.0
        if not self.dp:
            return out, tot
        for m, v in self.dp["by_model"].items():
            if v.get("total", 0) == 0:
                continue
            if m in RATES:
                ri, ro = RATES[m]
                c = (v["uncached_input"] / M * ri + v["cache_write"] / M * ri * 1.25
                     + v["cache_read"] / M * ri * 0.1 + v["output"] / M * ro)
            elif m in OPENAI:
                ri, rc, ro = OPENAI[m]
                c = (v["uncached_input"] / M * ri + v["cache_read"] / M * rc
                     + v["output"] / M * ro)
            else:
                continue
            out[m] = c
            tot += c
        return out, tot


# ----------------------------------------------------------------- blocks
BLOCKS = {}


def block(name):
    def deco(fn):
        BLOCKS[name] = fn
        return fn
    return deco


@block("stamp")
def _stamp(c):
    return ["*Числа в этом блоке сгенерированы `report_gen.py` %s. Не править руками.*"
            % time.strftime("%Y-%m-%d %H:%M")]


@block("headline")
def _headline(c):
    r = ["| Инструмент | Период | Токенов | Класс |", "|---|---|---:|---|"]
    if c.rc:
        cons = c.rc["consistent_total_max_basis"]["total_tokens"]
        up = c.rc["upper_bound_if_delta_valid"]
        r.append("| **OpenAI Codex** | 2026-04-03 → 06-13 | **%s** | составной |" % f(cons))
        r.append("| | | верхняя граница **%s** | |" % f(up))
    if c.cl:
        r.append("| **Claude Code** | %s → %s | **%s** | ИЗМЕРЕНО |" % (
            c.cl["first_ts"][:10], c.cl["last_ts"][:10], f(c.total)))
    if c.ag:
        r.append("| **Antigravity** | %s → %s | счётчика не существует | ПРОКСИ |" % (
            (c.ag["first_ts"] or "?")[:10], (c.ag["last_ts"] or "?")[:10]))
    return r


@block("claude_summary")
def _cs(c):
    d = c.dp
    r = ["| | Значение |", "|---|---:|",
         "| последнее событие | %s |" % c.cl["last_ts"],
         "| **всего токенов** | **%s** |" % f(c.total),
         "| $ по публичному прайсу | **%s** |" % usd(c.cost_total),
         "| сессий | %s |" % f(c.cl["session_count"]),
         "| ответов (дедуп.) | %s |" % f(c.cl["records_deduped"]),
         "| ответов до дедупликации | %s |" % f(c.cl["records_raw"]),
         "| активных часов | %s |" % d["active_time_hours_gap_le_300s"],
         "| токенов в активный час | %s |" % f(d["tokens_per_active_hour"]),
         "| доля субагентов | %s%% |" % d["main_vs_subagent"]["subagent"]["share_pct"],
         "| медиана промежутка между ответами | %s с |" % d["gap_between_responses_s"]["median"],
         "| крупнейшая сессия | %s |" % f(d["session_size_distribution"]["max"]),
         "| топ-10 сессий от объёма | %s%% |" % d["concentration"]["top_10_sessions_pct"]]
    raw = (c.cl["totals_raw"]["inp"] + c.cl["totals_raw"]["cc"]
           + c.cl["totals_raw"]["cr"] + c.cl["totals_raw"]["out"])
    r.append("| завышение при наивной сумме | **×%.2f** |" % (raw / max(1, c.total)))
    return r


@block("by_type")
def _bt(c):
    r = ["| тип токена | токенов | доля |", "|---|---:|---:|"]
    for k in ("cr", "inp", "cc", "out"):
        r.append("| %s | %s | %s |" % (NAMES[k], f(c.t[k]), pct(c.t[k], c.total)))
    r.append("| **всего** | **%s** | |" % f(c.total))
    return r


@block("by_model")
def _bm(c):
    r = ["| модель | ответов | сессий | токенов | доля | кэш-попадание | свежий ввод, медиана | $ |",
         "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for m, v in sorted(c.dp["by_model"].items(), key=lambda x: -x[1]["total"]):
        if v["total"] == 0:
            continue
        r.append("| %s | %s | %s | %s | %s | %.2f%% | %s | %s |" % (
            m, f(v["responses"]), v["sessions"], f(v["total"]),
            pct(v["total"], c.total), v["cache_hit_rate_pct"],
            f(v["uncached_per_call"].get("median", 0)),
            usd(c.cost[m]) if m in c.cost else "—"))
    r.append("| **итого** | **%s** | %s | **%s** | | | | **%s** |" % (
        f(c.cl["records_deduped"]), c.cl["session_count"], f(c.total), usd(c.cost_total)))
    return r


@block("by_day")
def _bd(c):
    r = ["| день | токенов | кэш | ответов | $ |", "|---|---:|---:|---:|---:|"]
    df = c.dp.get("by_day_full", {})
    for k in sorted(df):
        v = df[k]
        cost = (v["inp"] / M * 5 + v["cc"] / M * 6.25 + v["cr"] / M * 0.5
                + v["out"] / M * 25)
        r.append("| %s | %s | %.1f%% | %s | %s |" % (
            k, f(v["total"]), v["cache_pct"], f(v["n"]), usd(cost)))
    return r


@block("delta")
def _dl(c):
    if len(c.snaps) < 2:
        return ["*Истории меньше двух запусков — сравнивать не с чем.*"]
    a, b = c.snaps[-2]["claude"], c.snaps[-1]["claude"]
    r = ["| метрика | %s | %s | прирост |" % (c.snaps[-2]["ts"][5:16].replace("T", " "),
                                              c.snaps[-1]["ts"][5:16].replace("T", " ")),
         "|---|---:|---:|---:|"]
    for k in FIELDS:
        r.append("| %s | %s | %s | +%s |" % (NAMES[k], f(a[k]), f(b[k]), f(b[k] - a[k])))
    r.append("| **всего** | **%s** | **%s** | **+%s (+%.1f%%)** |" % (
        f(a["total"]), f(b["total"]), f(b["total"] - a["total"]),
        100.0 * (b["total"] - a["total"]) / max(1, a["total"])))
    r.append("| $ по прайсу | %s | %s | +%s |" % (usd(a["usd"]), usd(b["usd"]),
                                                  usd(b["usd"] - a["usd"])))
    r.append("| сессий | %s | %s | +%s |" % (a["sessions"], b["sessions"],
                                             b["sessions"] - a["sessions"]))
    d = b["total"] - a["total"]
    if d:
        r += ["", "Состав прироста: " + ", ".join(
            "%s **%.1f%%**" % (NAMES[k], 100.0 * (b[k] - a[k]) / d) for k in FIELDS)]
    return r


@block("growth")
def _gr(c):
    r = ["| номер вызова в сессии | ответов | контекст, медиана | p90 |",
         "|---|---:|---:|---:|"]
    g = c.dp["context_growth_by_call_index"]
    for k, v in g.items():
        lab = k.replace("calls_", "").replace("_plus", " и далее").replace("_", "–")
        r.append("| %s | %s | %s | %s |" % (lab, f(v["responses"]),
                                            f(v["median_context"]), f(v["p90_context"])))
    ks = list(g)
    if len(ks) > 1:
        a, b = g[ks[0]]["mean_context"], g[ks[-1]]["mean_context"]
        r += ["", "Средний контекст растёт от %s до %s — **в %.1f раза**." % (
            f(a), f(b), b / max(1, a))]
    return r


@block("concentration")
def _cc(c):
    d = c.dp
    r = ["| | доля всего объёма |", "|---|---:|"]
    for k, v in d["concentration"].items():
        r.append("| топ-%s сессий | %.2f%% |" % (re.search(r"\d+", k).group(), v))
    s = d["session_size_distribution"]
    r += ["", "| размер сессии | токенов |", "|---|---:|",
          "| медиана | %s |" % f(s["median"]), "| p90 | %s |" % f(s["p90"]),
          "| максимум | %s |" % f(s["max"]),
          "| отношение максимум / медиана | ×%.0f |" % (s["max"] / max(1, s["median"]))]
    return r


@block("main_vs_sub")
def _mvs(c):
    r = ["| | ответов | токенов | доля | среднее на ответ |", "|---|---:|---:|---:|---:|"]
    for k, lab in (("main", "основной поток"), ("subagent", "субагенты")):
        v = c.dp["main_vs_subagent"][k]
        r.append("| %s | %s | %s | %.2f%% | %s |" % (
            lab, f(v["responses"]), f(v["total"]), v["share_pct"],
            f(v["mean_tokens_per_response"])))
    return r


@block("codex_methods")
def _cm(c):
    if not c.ch:
        return ["*`codex_chains_totals.json` отсутствует — запусти `refresh.py --codex`.*"]
    a = c.ch["totals_chain_split"]
    b = c.ch["totals_naive_max_per_file"]
    d = c.ch["totals_naive_delta_per_file"]
    r = ["| поле | chain-split | максимум по файлу | сумма приростов |",
         "|---|---:|---:|---:|"]
    for k in ("input_tokens", "cached_input_tokens", "output_tokens",
              "reasoning_output_tokens", "total_tokens"):
        r.append("| %s | %s | %s | %s |" % (k, f(a[k]), f(b[k]), f(d[k])))
    r += ["", "Сессий %s, из них многоцепочечных **%s**. Внеочередных событий %s, "
          "плейсхолдеров отброшено %s." % (
              f(c.ch["sessions"]), c.ch["sessions_with_multiple_chains"],
              f(c.ch["out_of_order_events_total"]),
              f(c.ch["placeholders_skipped_total"])),
          "", "chain-split против максимума **%+.3f%%**, против приростов **%+.3f%%**." % (
              100.0 * (a["total_tokens"] - b["total_tokens"]) / b["total_tokens"],
              100.0 * (a["total_tokens"] - d["total_tokens"]) / d["total_tokens"])]
    return r


@block("codex_components")
def _cco(c):
    if not c.rc:
        return ["*`reconciliation.json` отсутствует.*"]
    r = ["| слагаемое | токенов | класс | источник |", "|---|---:|---|---|"]
    for x in c.rc["components"]:
        r.append("| %s | %s | %s | %s |" % (
            x["name"], f(x["total_tokens"]), x["class"], x["source"][:70]))
    r.append("| **итого консервативно** | **%s** | | |"
             % f(c.rc["consistent_total_max_basis"]["total_tokens"]))
    r.append("| верхняя граница | %s | | |" % f(c.rc["upper_bound_if_delta_valid"]))
    return r


@block("antigravity_proxy")
def _ap(c):
    if not c.ag:
        return ["*`antigravity_totals.json` отсутствует — `refresh.py --antigravity`.*"]
    t = c.ag["totals"]
    r = ["| метрика | значение |", "|---|---:|",
         "| беседы с транскриптом | %s |" % f(t["conversations"]),
         "| ходов модели (`PLANNER_RESPONSE`) | %s |"
         % f(c.ag["record_type_counts"].get("PLANNER_RESPONSE", 0)),
         "| вызовов инструментов | %s |" % f(t["tool_calls"]),
         "| запросов пользователя | %s |" % f(t["user_inputs"]),
         "| **упоров в квоту (429)** | **%s** |" % f(t["quota_blocks"]),
         "| символов `thinking` | %s |" % f(t["thinking_chars"]),
         "| символов `content` | %s |" % f(t["content_chars"]),
         "| объём транскриптов | %.2f ГБ |" % (t["bytes"] / 1e9),
         "| **токенов** | **не измеримо** |"]
    return r


@block("integrity")
def _ig(c):
    rows = ["| проверка | результат |", "|---|---|"]
    ok = True
    s = sum(v["total"] for v in c.dp["by_model"].values())
    diff = abs(s - c.total)
    good = diff <= max(1, c.total * 0.02)
    ok &= good
    rows.append("| сумма по моделям против итога | %s (расхождение %s) |" % (
        "СХОДИТСЯ" if good else "РАСХОДИТСЯ", f(diff)))
    mv = c.dp["main_vs_subagent"]
    s2 = mv["main"]["total"] + mv["subagent"]["total"]
    good = abs(s2 - s) <= max(1, s * 0.001)
    ok &= good
    rows.append("| основной + субагенты против суммы по моделям | %s |"
                % ("СХОДИТСЯ" if good else "РАСХОДИТСЯ"))
    sd = sum(v["total"] for v in c.dp.get("by_day_full", {}).values())
    good = abs(sd - s) <= max(1, s * 0.001)
    ok &= good
    rows.append("| сумма по дням против суммы по моделям | %s |"
                % ("СХОДИТСЯ" if good else "РАСХОДИТСЯ"))
    for lab, key in (("час", "by_hour"), ("10 минут", "by_10min"), ("минута", "by_minute")):
        b = c.cl.get(key, {})
        sb = sum(v["inp"] + v["cc"] + v["cr"] + v["out"] for v in b.values())
        good = abs(sb - c.total) <= max(1, c.total * 0.0001)
        ok &= good
        rows.append("| разрешение «%s»: %s интервалов, сумма | %s |" % (
            lab, f(len(b)), "СХОДИТСЯ" if good else "РАСХОДИТСЯ"))
    if c.ch:
        a = c.ch["totals_chain_split"]["total_tokens"]
        m = c.ch["totals_from_minute_increments"]["total_tokens"]
        good = abs(a - m) <= max(1, a * 0.01)
        ok &= good
        rows.append("| Codex: chain-split против минутных приростов | %s |"
                    % ("СХОДИТСЯ" if good else "РАСХОДИТСЯ"))
    rows += ["", "**Итог: %s**" % ("все проверки пройдены" if ok
                                   else "ЕСТЬ РАСХОЖДЕНИЯ — не доверять цифрам")]
    return rows


@block("history")
def _hi(c):
    if not c.snaps:
        return ["*История пуста.*"]
    r = ["| срез | токенов | $ | сессий | прирост |", "|---|---:|---:|---:|---:|"]
    for s in c.snaps[-12:]:
        cc = s["claude"]
        d = s.get("delta", {})
        r.append("| %s | %s | %s | %s | %s |" % (
            s["ts"].replace("T", " "), f(cc["total"]), usd(cc["usd"]),
            cc["sessions"], ("+" + f(d["total"])) if d.get("total") else "—"))
    r += ["", "Всего запусков в истории: **%d**." % len(c.snaps)]
    return r


@block("files")
def _fl(c):
    rows = ["| файл | размер | что |", "|---|---:|---|"]
    D = {
        "refresh.py": "точка входа: измеряет, считает, собирает, проверяет",
        "report_gen.py": "подстановка AUTO-блоков и проверки целостности",
        "claude_agg.py": "агрегат Claude Code, 4 разрешения по времени",
        "claude_deep.py": "распределения, сессии, перфокарта, рост контекста",
        "codex_agg_chains.py": "Codex методом chain-split, три метода сразу",
        "codex_agg.py": "Codex, первая версия (максимум по файлу)",
        "antigravity_agg.py": "прокси-метрики Antigravity",
        "cost_model.py": "модель стоимости и combined.json",
        "build_dashboard.py": "сборка dashboard.html",
        "dashboard.html": "интерактивный дашборд, самодостаточный",
        "CURRENT.md": "актуальные цифры, генерируется целиком",
        "SUMMARY.md": "сводка: проза авторская, числа в AUTO-блоках",
        "DEEP_REPORT.md": "детальный разбор",
        "README.md": "описание инструментария и методики",
        "snapshots.jsonl": "история запусков, источник дельт",
        "claude_totals.json": "данные Claude Code",
        "claude_deep.json": "распределения Claude Code",
        "codex_chains_totals.json": "данные Codex",
        "antigravity_totals.json": "данные Antigravity",
        "reconciliation.json": "сведение слагаемых Codex",
        "combined.json": "сводные данные для дашборда",
        "GEMINI_PROMPT_HARD.md": "задание для второй машины",
    }
    for n in sorted(D):
        p = os.path.join(HERE, n)
        if not os.path.exists(p):
            continue
        s = os.path.getsize(p)
        sz = "%.2f МБ" % (s / 1e6) if s > 1e6 else "%s КБ" % f(s / 1000)
        rows.append("| `%s` | %s | %s |" % (n, sz, D[n]))
    return rows


# ------------------------------------------------------------------ engine
RX = re.compile(r"(<!-- AUTO:([a-z_]+) -->)(.*?)(<!-- /AUTO -->)", re.S)


def fill(path, c):
    if not os.path.exists(path):
        return 0, []
    src = io.open(path, encoding="utf-8").read()
    miss, cnt = [], 0

    def rep(m):
        nonlocal cnt
        name = m.group(2)
        gen = BLOCKS.get(name)
        if not gen:
            miss.append(name)
            return m.group(0)
        cnt += 1
        body = "\n".join(gen(c))
        return "%s\n%s\n%s" % (m.group(1), body, m.group(4))

    out = RX.sub(rep, src)
    if out != src:
        io.open(path, "w", encoding="utf-8").write(out)
    return cnt, miss


def main():
    # extra blocks live in a separate module; importing registers them
    try:
        import report_blocks_ext  # noqa: F401
    except Exception as e:
        print("  ВНИМАНИЕ: report_blocks_ext не загружен:", e)
    c = Ctx()
    total, allmiss = 0, []
    for name in ("SUMMARY.md", "DEEP_REPORT.md", "README.md",
                 "CURRENT.md", "FULL_REPORT.md"):
        n, miss = fill(os.path.join(HERE, name), c)
        allmiss += miss
        total += n
        if n:
            print("  %-16s блоков заполнено: %d" % (name, n))
    if allmiss:
        print("  НЕИЗВЕСТНЫЕ БЛОКИ:", ", ".join(sorted(set(allmiss))))
    print("  доступные блоки: %s" % ", ".join(sorted(BLOCKS)))
    # integrity result to stdout so refresh.py can surface it
    txt = "\n".join(_ig(c))
    bad = "РАСХОЖДЕНИЯ" in txt
    print("  целостность: %s" % ("РАСХОЖДЕНИЯ" if bad else "OK"))
    if bad:
        print(txt)
        raise SystemExit(2)


if __name__ == "__main__":
    main()

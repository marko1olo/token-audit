#!/usr/bin/env python3
"""Emit a self-contained HTML dashboard from the aggregates.

Data is inlined rather than fetched, because a local file:// page cannot fetch a
sibling JSON without tripping CORS.

Palette: the dataviz reference instance, validated with scripts/validate_palette.js
  light #2a78d6,#1baf7a,#eda100,#008300,#4a3aa7 -> ALL PASS, contrast WARN on
      aqua (2.74) and yellow (2.11) -> relief shipped as direct labels + table view
  dark  #3987e5,#199e70,#c98500,#008300,#9085e9 -> ALL PASS, CVD WARN worst
      adjacent 10.3 (floor band) -> secondary encoding shipped as direct labels
      and 2px surface gaps between stacked segments
"""
import json
import os
import io
import tokenaudit_config as cfg
import tokenaudit_rates as rates
import re
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
def L(n, default=None):
    """Optional-tolerant loader: a missing side-file must not break the build."""
    p = os.path.join(HERE, n)
    if not os.path.exists(p):
        if default is None:
            raise SystemExit("обязательный файл отсутствует: %s" % n)
        return default
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)

cl = L("claude_totals.json")
cline_tot = L("cline_totals.json", {})
cline_dp = L("cline_deep.json", {})
# Codex необязателен: у клонировавшего его данных может не быть вовсе, а
# закоммиченный артефакт подставлял бы ЧУЖОЕ измерение под видом его
# собственного. Нет данных -- нет панелей, это честнее чужого числа.
cx = L("codex_totals.json", {})
ag = L("antigravity_totals.json", {})
cb = L("combined.json")
ch = L("codex_chains_totals.json", {})
dp = L("claude_deep.json")
cd_ = L("claude_cost_deep.json")
cn_ = L("claude_cost_night.json", {})
rc = L("reconciliation.json", {})
# Отчёт со ВТОРОЙ машины -- необязательный. Здесь стоял абсолютный путь в
# Downloads\Telegram Desktop, причём голым json.load(open(...)) в обход
# толерантного загрузчика L(), объявленного одиннадцатью строками выше. Файл не
# отслеживается git, поэтому в любом клоне его нет, и сборка падала на этапе 4 у
# каждого, кто скачал репозиторий. При отсутствии файла панели второй машины
# просто не рисуются -- это верное поведение, а не деградация.
_dn_path = cfg.second_machine_file()
dn = None
if _dn_path:
    try:
        with io.open(_dn_path, encoding="utf-8") as _fh:
            dn = json.load(_fh)
    except (OSError, ValueError) as e:
        print("отчёт второй машины не прочитан, панели не рисуются:", e)
if dn is None:
    print("отчёта второй машины нет -- панели второй машины не рисуются")

TOKEN_FIELDS = [("inp", "uncached input"), ("cc", "cache write"),
                ("cr", "cache read"), ("out", "output")]


# Предел числа точек в серии, уходящей в дашборд.
#
# Складчатый график шириной около тысячи пикселей физически показывает
# 350--500 столбиков, дальше браузер рисует их друг на друге. Измерено, что
# уходило до сокращения: codex.by_minute -- 28 561 точка, то есть 28.6 точки на
# пиксель, и весила серия 1357 КБ, 72.9% всей полезной нагрузки. Плюс
# claude.by_minute 6 632 точки и danat.by_minute 4 506. Вместе они составляли
# практически весь файл: полезная нагрузка 1863 КБ из 1941 КБ, то есть 96%.
#
# Полное поминутное разрешение НЕ теряется -- оно остаётся в JSON-выкладках,
# откуда его и надо брать для анализа. Сокращается только то, что встраивается
# в HTML: за пределом видимости данные превращаются из информации в вес.
MAX_POINTS = 720


def series(d, keys=("inp", "cc", "cr", "out"), cap=MAX_POINTS):
    """{bucket: {...}} -> {labels: [...], <key>: [...]}, не больше cap точек.

    При сокращении соседние интервалы складываются, подпись берётся от первого
    в группе. Сумма по каждому ключу сохраняется ТОЧНО -- иначе панель начнёт
    расходиться с итогами, а две разные цифры на одной странице в этом
    репозитории уже случались и стоили дорого. Проверяется тут же, на месте.
    """
    ks = sorted(d.keys())
    o = {"labels": ks}
    for k in keys:
        o[k] = [d[b].get(k, 0) for b in ks]
    if not cap or len(ks) <= cap:
        return o
    step = (len(ks) + cap - 1) // cap          # округление вверх
    out = {"labels": [ks[i] for i in range(0, len(ks), step)]}
    for k in keys:
        src = o[k]
        out[k] = [sum(src[i:i + step]) for i in range(0, len(src), step)]
        if sum(out[k]) != sum(src):
            raise SystemExit("сокращение серии потеряло сумму по ключу %r: "
                             "%d против %d" % (k, sum(out[k]), sum(src)))
    out["downsampled_from"] = len(ks)
    out["bucket_span"] = step
    return out


def day_compare(cl, dp):
    """Накопительные итоги на конец двух последних суток и прирост между ними.

    Заменяет таблицу, вписанную руками. На момент замены она утверждала
    «ВСЕГО 4 329 731 446 -> 7 769 375 039», тогда как из by_day накопительно
    выходило 6 271 135 564 -> 17 710 985 238: ошибка в 2.28 раза, и всё это было
    опубликовано. Числа, которые никто не пересчитывает, расходятся с данными
    не постепенно, а в разы.
    -> dict | None
    """
    bd = (cl or {}).get("by_day") or {}
    days = sorted(bd)
    if len(days) < 2:
        return None
    cum, snaps = {k: 0 for k in ("inp", "cc", "cr", "out")}, {}
    for d in days:
        for k in cum:
            cum[k] += bd[d].get(k, 0)
        snaps[d] = dict(cum)
    prev, last = days[-2], days[-1]
    a, b = snaps[prev], snaps[last]
    rows = []
    for key, name in (("inp", "свежий ввод"), ("cc", "запись кэша"),
                      ("cr", "чтение кэша"), ("out", "вывод")):
        rows.append({"name": name, "before": a[key], "after": b[key]})
    rows.append({"name": "ВСЕГО", "before": sum(a.values()), "after": sum(b.values())})
    # Стоимость на каждую дату -- по тем же ставкам, что и везде.
    cost_a = rates.day_cost(a)
    cost_b = rates.day_cost(b)
    out = {"prev": prev, "last": last, "rows": rows,
           "cost_before": cost_a, "cost_after": cost_b}
    # Последние часы: объём и доля попаданий в кэш. Раньше тринадцать значений
    # были вписаны руками и относились к одной конкретной ночи.
    bh = (cl or {}).get("by_hour") or {}
    hours = sorted(bh)[-13:]
    hr = []
    for h in hours:
        v = bh[h]
        ctx = (v.get("inp", 0) + v.get("cc", 0) + v.get("cr", 0)) or 1
        hr.append({"label": h[5:].replace("T", " "),
                   "total": sum(v.get(k, 0) for k in ("inp", "cc", "cr", "out")),
                   "cache_pct": round(100.0 * v.get("cr", 0) / ctx, 1)})
    out["hours"] = hr
    if hr:
        out["cache_min"] = min(x["cache_pct"] for x in hr)
    # Крупнейшие сутки и их доля -- тоже была вписана руками.
    tot = sum(sum(bd[d].get(k, 0) for k in ("inp", "cc", "cr", "out")) for d in days)
    top = max(days, key=lambda d: sum(bd[d].get(k, 0) for k in ("inp", "cc", "cr", "out")))
    tv = sum(bd[top].get(k, 0) for k in ("inp", "cc", "cr", "out"))
    out["top_day"] = {"date": top, "total": tv, "share_pct": round(100.0 * tv / (tot or 1), 1),
                      "days": len(days)}
    return out


CXF = ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"]

payload = {
    "claude": {
        "period": [cl["first_ts"], cl["last_ts"]],
        "sessions": cl["session_count"],
        "files": cl["scan_stats"]["files"],
        "resp_uniq": cl["records_deduped"],
        "resp_raw": cl["records_raw"],
        "totals": cl["totals_deduped"],
        "raw_totals": cl["totals_raw"],
        "by_model": cl["by_model"],
        "by_day": series(cl["by_day"]),
        "by_hour": series(cl["by_hour"]),
        "by_10min": series(cl["by_10min"]),
        "by_minute": series(cl["by_minute"]),
        "by_hour_of_day": series(cl["by_hour_of_day"]),
        "by_weekday": series(cl["by_weekday"]),
        "by_sidechain": cl["by_sidechain"],
        "by_day_model": cl["by_day_model"],
        "cost": cb["claude_code"]["cost_usd_by_model"],
        "cost_total": cb["claude_code"]["cost_usd_total_list_price_equivalent"],
    },
    "cline": None if not cline_tot else {
        "tasks": cline_tot.get("task_count", 0),
        "reqs": cline_tot.get("request_count", 0),
        "totals": {
            "inp": cline_tot.get("inp", 0),
            "cw": cline_tot.get("cw", 0),
            "cr": cline_tot.get("cr", 0),
            "out": cline_tot.get("out", 0),
            "total": cline_tot.get("total_tokens", 0),
        },
        "by_model": cline_tot.get("by_model", {}),
        "by_day": series(cline_tot.get("by_day", {})),
        "by_hour": series((cline_dp.get("by_hour") or {})),
        "by_minute": series((cline_dp.get("by_minute") or {})),
        "top_tasks": cline_dp.get("tasks", [])[:15],
        "cost": cb.get("cline", {}).get("cost_usd_by_model", {}),
        "cost_total": cb.get("cline", {}).get("cost_usd_total_list_price_equivalent", 0.0),
    },
    "daycmp": day_compare(cl, dp),
    # Разбивка по рабочему каталогу. Раньше эта панель была вписана руками и
    # показывала абсолютные значения в 4.3 раза ниже реальных при примерно
    # верных долях. Источник -- поле cwd, которое есть в каждой записи
    # транскрипта; by_project для этого не годится, там верхний каталог.
    "by_cwd": [{"name": k, "total": sum(v.get(x, 0) for x in ("inp", "cc", "cr", "out")),
                "responses": v.get("n", 0)}
               for k, v in list((cl.get("by_cwd_rolled") or {}).items())[:8]],
    "top_sessions": [{"id": k, "total": v["total"], "responses": v["responses"],
                      "start": v.get("start"), "model": v.get("dominant_model"),
                      "hours": round((v.get("duration_s") or 0) / 3600.0, 1),
                      "sub": v.get("sidechain_responses", 0)}
                     for k, v in sorted((dp.get("top_sessions") or {}).items(),
                                        key=lambda x: -x[1]["total"])[:15]],
    "codex": None if not (cx and cb.get("codex")) else {
        "period": [cx["first_ts"], cx["last_ts"]],
        "sessions": cx["session_files_with_data"],
        "files": cx["scan_stats"]["files"],
        "gb": round(cx["scan_stats"]["bytes"] / 1e9, 2),
        "totals": cx["totals_max_cumulative"],
        "by_day": series(cx["by_day"], CXF),
        "by_hour": series(cx["by_hour"], CXF),
        "by_minute": series(cx["by_minute"], CXF),
        "by_model": cx.get("by_model", {}),
        "plan_types": cx["plan_types"],
        "cwd_counts": dict(list(cx["cwd_counts"].items())[:8]),
        "prior": cb["codex"]["prior_audit_2026_06_06"],
        "recon": cb["codex"]["reconciliation"],
        "cost": cb["codex"]["cost_usd_by_model_estimate"],
        "cost_total": cb["codex"]["cost_usd_total_list_price_equivalent"],
    },
    # second machine -- genuinely new coverage for June. Ключ присутствует
    # только когда отчёт второй машины найден: JS-панели проверяют его
    # наличие и пропускают себя, а не рисуются пустыми.
    "danat": None if dn is None else {
        "root": dn["processed_root"],
        "period": [dn["first_ts"], dn["last_ts"]],
        "files": dn["files"],
        "files_in_old_audit": 1891,
        "sessions": dn["distinct_session_ids"],
        "gb": round(dn["bytes"] / 1e9, 2),
        "totals_max": dn["totals"],
        "totals_delta": dn["totals_from_deltas"],
        "after_jun06": dn["records_after_2026_06_06"],
        "by_day": series(dn["by_day"], CXF),
        "by_hour": series(dn["by_hour"], CXF),
        "by_minute": series(dn["by_minute"], CXF),
        "by_cwd": {k: v["total_tokens"] for k, v in dn["by_cwd"].items()},
        "resets": dn["counter_resets_seen"],
        "dedupe_reported": dn["tokens_dropped_by_dedupe"],
    },
    # three methods on my own raw files -- the control experiment
    "methods": {
        "chain_split": ch["totals_chain_split"],
        "naive_max": ch["totals_naive_max_per_file"],
        "naive_delta": ch["totals_naive_delta_per_file"],
        "sessions": ch["sessions"],
        "multi_chain_sessions": ch["sessions_with_multiple_chains"],
        "out_of_order": ch["out_of_order_events_total"],
        "placeholders": ch["placeholders_skipped_total"],
    },
    "deep": {
        "responses": dp["responses"],
        "by_model": dp["by_model"],
        "growth": dp["context_growth_by_call_index"],
        "concentration": dp["concentration"],
        "session_dist": dp["session_size_distribution"],
        "main_vs_sub": dp["main_vs_subagent"],
        "top_sessions": dp["top_sessions"],
        "out_buckets": dp["output_size_buckets"],
        "gaps": dp["gap_between_responses_s"],
        "punch": dp.get("day_hour_matrix", {}),
        "sessions_slim": dp.get("sessions_slim", []),
        "day_full": dp.get("by_day_full", {}),
        "sub_by_day": dp.get("subagent_share_by_day", {}),
        "size_hist": dp.get("session_size_histogram", {}),
        "active_h": dp["active_time_hours_gap_le_300s"],
        "tph": dp["tokens_per_active_hour"],
        "cost": cd_, "night": cn_,
    },
    "recon": rc,
    # Antigravity тоже необязателен, по той же причине: закоммиченный артефакт
    # подставлял бы чужое измерение как своё.
    "antigravity": None if not ag else {
        "period": [ag["first_ts"], ag["last_ts"]],
        "verdict": ag["verdict"],
        "totals": ag["totals"],
        "types": ag["record_type_counts"],
        "by_day": {"labels": sorted(ag["by_day"].keys()),
                   "model_turns": [ag["by_day"][k].get("model_turns", 0) for k in sorted(ag["by_day"])],
                   "records": [ag["by_day"][k].get("records", 0) for k in sorted(ag["by_day"])],
                   "chars": [ag["by_day"][k].get("chars", 0) for k in sorted(ag["by_day"])]},
        "by_hour": {"labels": sorted(ag["by_hour"].keys()),
                    "model_turns": [ag["by_hour"][k].get("model_turns", 0) for k in sorted(ag["by_hour"])],
                    "chars": [ag["by_hour"][k].get("chars", 0) for k in sorted(ag["by_hour"])]},
        "quota_by_day": ag["quota_blocks_by_day"],
    },
}

HTML = r"""<!DOCTYPE html>
<meta charset="utf-8">
<title>Token audit — all time</title>
<script>
/* Перехватчик ошибок. Стоит ПЕРВЫМ намеренно: Chrome при --dump-dom не пишет
   в stderr ничего, когда скрипт падает, поэтому упавший дашборд снаружи
   выглядит как пустой, но исправный. Обработчик вписывает текст ошибки в DOM
   с меткой JSERR, и проверка рендера в refresh.py её ищет. Так видны оба
   класса поломок, которые уже случались: столкновение имён на верхнем уровне
   и обращение к const до объявления. */
(function () {
  var E = [];
  window.__jserr = E;
  function flush() {
    if (!E.length || !document.body) return;
    var d = document.getElementById('jserr');
    if (!d) {
      d = document.createElement('div');
      d.id = 'jserr';
      d.style.cssText = 'background:#b3261e;color:#fff;padding:8px;font:12px monospace';
      document.body.insertBefore(d, document.body.firstChild);
    }
    d.textContent = 'JSERR ' + E.join(' | ');
  }
  window.onerror = function (m, u, l, c) {
    E.push(m + ' @' + l + ':' + c);
    flush();
    return false;
  };
  window.addEventListener('unhandledrejection', function (ev) {
    E.push('unhandledrejection: ' + ev.reason);
    flush();
  });
  document.addEventListener('DOMContentLoaded', flush);
  window.addEventListener('load', flush);
})();
</script>
<body data-palette="#2a78d6,#1baf7a,#eda100,#008300,#4a3aa7">
<style>
:root{
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#1baf7a; --s3:#eda100; --s4:#008300; --s5:#4a3aa7;
  --seq1:#cde2fb; --seq2:#9ec5f4; --seq3:#5598e7; --seq4:#2a78d6; --seq5:#256abf; --seq6:#184f95; --seq7:#0d366b;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b;
}
body.dark{
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#199e70; --s3:#c98500; --s4:#008300; --s5:#9085e9;
  /* Последовательная шкала для ТЁМНОЙ поверхности. Раньше её здесь не было, и
     тёмная тема брала светлую шкалу, из-за чего перфокарта часов читалась
     НАОБОРОТ: посчитано по WCAG на поверхности #1a1a19 -- самая низкая корзина
     давала контраст 13.16:1, самая высокая 1.46:1, то есть слабые значения были
     виднее сильных.

     Тот же оттенок, монотонная светлота, направление обратное светлой теме --
     на чёрном фоне рост значения должен идти к более светлому, а не к более
     тёмному. Проверено: 1.18 / 1.50 / 2.02 / 3.94 / 5.83 / 9.74 / 13.16 -- строго
     вверх. Это не автоматический разворот палитры, а отдельно подобранные шаги
     той же синей шкалы. */
  --seq1:#17293c; --seq2:#1d3a5c; --seq3:#244d7d; --seq4:#2a78d6; --seq5:#5598e7; --seq6:#9ec5f4; --seq7:#cde2fb;
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:17px;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--ink2);font-size:13px;margin:0 0 22px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
  padding:18px 20px;margin:0 0 18px}
.cap{color:var(--muted);font-size:12px;margin:2px 0 14px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:14px;margin:0 0 18px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:16px 18px}
.tile .k{color:var(--ink2);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.tile .v{font-size:30px;font-weight:640;letter-spacing:-.025em;margin:6px 0 2px}
.tile .n{color:var(--muted);font-size:12px}
.badge{display:inline-block;font-size:10px;font-weight:680;letter-spacing:.07em;
  padding:2px 7px;border-radius:4px;border:1px solid var(--ring);vertical-align:2px}
.b-meas{color:var(--good)} .b-ver{color:var(--s1)} .b-rep{color:var(--serious)}
.b-proxy{color:var(--muted)} .b-est{color:var(--warn)}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:0 0 10px;font-size:12px;color:var(--ink2)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.ctrl{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 14px}
button{font:inherit;font-size:12px;padding:5px 11px;border-radius:6px;cursor:pointer;
  border:1px solid var(--ring);background:transparent;color:var(--ink2)}
button[aria-pressed=true]{background:var(--s1);border-color:var(--s1);color:#fff;font-weight:600}
svg{display:block;width:100%;overflow:visible}
.gl{stroke:var(--grid);stroke-width:1}
.ax{stroke:var(--axis);stroke-width:1}
text{fill:var(--muted);font-size:11px}
text.lbl{fill:var(--ink);font-size:11px;font-weight:600}
table{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid var(--grid)}
th:first-child,td:first-child{text-align:left;font-variant-numeric:normal}
th{color:var(--ink2);font-weight:600}
.tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .08s;
  background:var(--surface);border:1px solid var(--ring);border-radius:7px;
  padding:8px 10px;font-size:12px;box-shadow:0 6px 22px rgba(0,0,0,.16);z-index:9;
  font-variant-numeric:tabular-nums;max-width:290px}
.tip b{display:block;margin-bottom:4px;font-variant-numeric:normal}
.tip .r{display:flex;justify-content:space-between;gap:14px}
.tip i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.note{font-size:12px;color:var(--ink2);border-left:2px solid var(--serious);
  padding:6px 0 6px 11px;margin:12px 0 0}
.note.crit{border-color:var(--crit)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.hide{display:none}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
</style>

<div class="wrap">
<div class="top">
  <div>
    <h1>Расход токенов — за всё время</h1>
    <p class="sub">Claude Code · OpenAI Codex · Google Antigravity. Собрано из локальных
      логов 2026-07-27. Класс доказательности указан у каждой цифры.</p>
  </div>
  <button id="mode">Тёмная / светлая</button>
</div>

<div class="tiles" id="tiles"></div>

<div class="card">
  <h2>Масштаб: три инструмента рядом</h2>
  <p class="cap">Только измеримые токены. Antigravity отсутствует на этой шкале
    намеренно — у него нет счётчика, и пририсовать ему столбик значило бы выдумать данные.</p>
  <div id="scale"></div>
  <div class="note">Итог по Codex складывается из четырёх слагаемых с разным классом
    доказательности, и смешивать их в одну «точную» цифру нельзя. Консервативно
    <b>119.06 млрд</b>, верхняя граница <b>152.40 млрд</b> — разница целиком в том, какой
    метод верен для данных второго компьютера (см. панель «Три метода подсчёта»).
    Против Claude Code это в <b>27–35 раз</b> больше по объёму; с поправкой на длину
    периода — примерно <b>7–9×</b> в сутки.</div>
</div>

<div class="card">
  <h2>Claude Code во времени <span class="badge b-meas">ИЗМЕРЕНО</span></h2>
  <p class="cap">Стек по типу токена. Переключатель меняет разрешение — от суток до минуты.</p>
  <div class="ctrl" id="res"></div><span class="note" id="resnote"></span>
  <div class="legend" id="lg1"></div>
  <div id="ts"></div>
  <div class="note">Кэш-чтение — <b id="crshare"></b> всего объёма. Именно поэтому
    «сколько токенов» и «сколько денег» — очень разные вопросы: чтение кэша стоит
    в 10 раз дешевле свежего ввода.</div>
  <div class="note crit">Один день — <b>27 июля: 3 042 436 363 токена, это 70.3%</b>
    всего расхода за 20 дней. Это сессия самого этого аудита. Механика та же, что
    и на Codex: в длинной сессии весь контекст пересылается заново на каждом ходу,
    и кэш-чтение растёт нелинейно. Столбик не выброс в данных — так выглядит
    один день глубокой работы против девятнадцати обычных.</div>
</div>

<div class="card">
  <!-- Заголовок, подпись и выводы этого раздела ЗАПОЛНЯЕТ JS из данных. Раньше
       здесь стояли числа, вписанные руками: прирост 3 439 643 593 не
       воспроизводился ни из одной пары снимков, а таблица ниже расходилась с
       данными в 2.28 раза. Утверждения, которые нельзя посчитать из
       артефактов -- про «размывание короткими вызовами» и сравнение с днями
       сломанного кэша -- удалены, а не переписаны: неверифицируемая цифра хуже
       отсутствующей. -->
  <h2 id="dchead">Последние сутки против предыдущих <span class="badge b-meas">ИЗМЕРЕНО</span></h2>
  <p class="cap" id="dccap">Накопительные итоги на конец каждой из двух дат и прирост между ними.</p>
  <table id="tnight"></table>
  <div id="nighthr" style="margin-top:14px"></div>
  <div class="note crit" id="dcnote"></div>
  <div class="note" id="dctop"></div>
</div>

<div class="grid2">
  <div class="card">
    <h2>Активность по часам суток</h2>
    <p class="cap">Один оттенок, светлее → темнее. Сумма за все 20 дней.</p>
    <div id="hod"></div>
  </div>
  <div class="card">
    <h2>По дням недели</h2>
    <p class="cap">Тот же период.</p>
    <div id="wd"></div>
  </div>
</div>

<div class="card">
  <h2>Claude Code по моделям <span class="badge b-meas">ИЗМЕРЕНО</span></h2>
  <p class="cap">Локальный прокси на 127.0.0.1:8318 переписывал модель в запросе,
    поэтому это метка <i>после</i> прокси, а не то, что выбирал пользователь.</p>
  <div class="legend" id="lg2"></div>
  <div id="models"></div>
  <table id="tmodels"></table>
</div>

<div class="card">
  <h2>Перфокарта: день × час <span class="badge b-meas">ИЗМЕРЕНО</span></h2>
  <p class="cap">Площадь точки пропорциональна объёму за этот час. Видно суточный ритм
    и то, какие ночи были рабочими.</p>
  <div id="punch"></div>
</div>

<div class="card">
  <h2>Все сессии на временной оси <span class="badge b-meas">ИЗМЕРЕНО</span></h2>
  <p class="cap">Каждая полоса — сессия: длина по реальному времени, толщина
    пропорциональна объёму, цвет — доминирующая модель. Сессии уложены в дорожки, поэтому
    видно параллельную работу.</p>
  <div class="legend" id="lgg"></div>
  <div id="gantt"></div>
  <div class="note">Дорожки — это не потоки, а укладка для читаемости: если полосы лежат
    на разных дорожках в одно время, значит сессии шли одновременно.</div>
</div>

<div class="card">
  <h2>Режимы стоимости: кэш против объёма <span class="badge b-meas">ИЗМЕРЕНО</span></h2>
  <p class="cap">Каждый пузырь — сутки. По горизонтали объём, по вертикали доля кэша,
    площадь — стоимость по прайсу. Чем выше и правее, тем эффективнее день.</p>
  <div id="regime"></div>
  <div class="note">Пузыри в левом низу — дорогой режим: мало токенов, плохой кэш, но
    заметная стоимость. Пузыри справа сверху — дешёвый: много токенов при почти полном
    попадании в кэш.</div>
</div>

<div class="grid2">
  <div class="card">
    <h2>Рост контекста внутри сессии</h2>
    <p class="cap">Линия — медиана, заливка — до p90. По номеру вызова в сессии.</p>
    <div id="growthband"></div>
  </div>
  <div class="card">
    <h2>Доля субагентов по дням</h2>
    <p class="cap">Процент объёма, пришедшийся на субагентов.</p>
    <div id="subday"></div>
  </div>
</div>

<div class="grid2">
  <div class="card">
    <h2>Размеры сессий, порядки величины</h2>
    <p class="cap">Сколько сессий попало в каждый десятичный порядок.</p>
    <div id="sizeh"></div>
  </div>
  <div class="card">
    <h2>Размеры ответов модели</h2>
    <p class="cap">Распределение вывода на один ответ, в токенах.</p>
    <div id="outh"></div>
  </div>
</div>

<div class="card">
  <h2>Мозаика: день × модель <span class="badge b-meas">ИЗМЕРЕНО</span></h2>
  <p class="cap">Ширина колонки пропорциональна объёму дня, высота сегмента — доле модели
    в этом дне. Две величины сразу: сколько и чем.</p>
  <div class="legend" id="lgm"></div>
  <div id="mosaic"></div>
</div>

<div class="card">
  <h2>Codex во времени <span class="badge b-meas">ИЗМЕРЕНО</span> <span class="badge b-ver">СВЕРЕНО</span></h2>
  <p class="cap">1050 файлов роллаутов, 10.1 ГБ, 1030 сессий. Накопительный счётчик
    <code>total_token_usage</code> монотонен, поэтому разница соседних значений даёт
    честный минутный ряд.</p>
  <div class="ctrl" id="res2"></div><span class="note" id="res2note"></span>
  <div class="legend" id="lg3"></div>
  <div id="ts2"></div>
  <div class="note crit">Это <b>только выживший бэкап-корень</b> и только апрель–май.
    Индекс сессий знает 2705 сессий, роллаутов на диске осталось 1030 —
    <b>1796 сессий удалены</b>. Остальное покрытие пришло из двух других источников:
    отчёта от 2026-06-06 по корню <code>danat</code> (файлы с тех пор тоже удалены,
    осталось 786 из 1891) и нового измерения второго компьютера за 4–13 июня.</div>
</div>

<div class="grid2">
  <div class="card">
    <h2>Codex: три корня <span class="badge b-rep">ПО ОТЧЁТУ</span></h2>
    <p class="cap">Дедуплицированные итоги по каждому корню на 2026-06-06.</p>
    <div id="roots"></div>
  </div>
  <div class="card">
    <h2>Codex по моделям <span class="badge b-rep">ПО ОТЧЁТУ</span></h2>
    <p class="cap">Метод приростов, дедуплицированный.</p>
    <div id="cxm"></div>
  </div>
</div>

<div class="card">
  <h2>Второй компьютер, профиль <code>danat</code> <span class="badge b-meas">ИЗМЕРЕНО</span> <span class="badge b-est">МЕТОД СПОРНЫЙ</span></h2>
  <p class="cap" id="dncap"></p>
  <div class="legend" id="lg5"></div>
  <div id="dnts"></div>
  <table id="tdn"></table>
  <div class="note">Из 1891 файла, который видел старый отчёт по этому же корню, осталось
    <b id="dnfiles"></b>. Уцелевшее покрывает 4–13 июня, то есть <b>почти целиком тот
    период, который не измерялся никогда</b>: прежний отчёт оборван 6 июня.</div>
  <div class="note crit">Период 4–6 июня есть и в старом отчёте, и в этом измерении.
    Поэтому в общий итог добавляется только часть <code>records_after_2026_06_06</code> —
    иначе 18.5 млрд были бы посчитаны дважды.</div>
</div>

<div class="card">
  <h2>Три метода подсчёта — контрольный опыт на моих сырых файлах</h2>
  <p class="cap">Один и тот же набор 1050 файлов, посчитанный тремя способами. Расхождение
    в пределах 0.04% — значит на чистых данных методы эквивалентны, и спор о методе
    решается не мнением, а измерением.</p>
  <div id="meth"></div>
  <table id="tmeth"></table>
  <div class="note">Почему методы вообще могут расходиться: в одном файле роллаута
    иногда идут <b>две накопительные цепочки сразу</b> — параллельные потоки пишут в один
    файл, а признака потока в записи нет. Реальный фрагмент:
    <code>3 065 004 → 1 326 125 → 3 225 875 → 1 480 311 → 3 396 892 → 1 635 308</code>.
    Метод максимума теряет вторую цепочку, метод приростов на каждом чередовании
    фабрикует ложный инкремент. Метод chain-split разносит события по цепочкам и
    суммирует их финалы.</div>
  <div class="note crit">На данных второго компьютера расхождение методов — <b>4.19×</b>
    (62.29 против 14.88 млрд). Это <b>не</b> объясняется чередованием: при сопоставимом
    числе сбросов (163 против моих 164) там на один сброс приходится
    <b>290 887 601</b> токена против моих <b>135 642</b> — разница в 2145 раз, при среднем
    размере сессии 19.4 млн. Плюс заявленные
    <code>tokens_dropped_by_dedupe = 165 124 336 663</code> при всего 3 дублирующихся
    сессиях внутренне невозможны. Поэтому в итог взята консервативная цифра по
    максимумам, а для точного ответа нужен пересчёт методом chain-split.</div>
</div>

<div class="card">
  <h2>Ловушка двойного счёта — почему 138.9 млрд неверны</h2>
  <p class="cap">Старый леджер вынес в заголовок 138.9 млрд. Это сумма «финалов по
    сессиям» по трём корням, а одни и те же сессии лежат и в живом каталоге, и в бэкапе.</p>
  <div id="dbl"></div>
  <table id="tdbl"></table>
</div>

<div class="card">
  <h2>Antigravity <span class="badge b-proxy">ПРОКСИ, НЕ ТОКЕНЫ</span></h2>
  <p class="cap" id="agv"></p>
  <div class="legend" id="lg4"></div>
  <div id="agts"></div>
  <div class="grid2" style="margin-top:16px">
    <div><h2 style="font-size:14px">Типы шагов</h2><div id="agt"></div></div>
    <div><h2 style="font-size:14px">Упоры в квоту (429) по дням</h2><div id="agq"></div></div>
  </div>
  <div class="note crit">Токены Antigravity <b>не восстановимы локально</b>. Прокси-метрика
    систематически <b>занижает</b> расход: транскрипт хранит каждое сообщение один раз,
    а API получает весь контекст заново на каждом ходу. На Codex, где счётчики есть,
    кэшированный ввод составил 96.15% — то есть основная масса расхода как раз в той
    части, которой в транскрипте нет.</div>
</div>

<div class="card">
  <h2>Четыре периода доступа — и кэш как множитель цены <span class="badge b-meas">ИЗМЕРЕНО</span></h2>
  <p class="cap">Доступ к Claude менялся несколько раз. Границы периодов видны в данных
    по эффективности кэша, набору моделей и версии CLI. Ниже — доля контекста, взятая
    из кэша, по дням.</p>
  <div id="cache"></div>
  <div class="note">8–13 июля кэш стабильно 91–95% и в наборе есть <b>gpt-5.5</b> —
    мультимодельный роутер. 14–25 июля кэш обваливается до <b>2.6–23%</b>. 26 июля
    впервые появляются <b>opus-5, fable-5, sonnet-5</b> и вторая версия CLI 2.1.219.
    27 июля кэш <b>99.9%</b> — это <code>proxy.js</code> с
    <code>UPSTREAM = api.anthropic.com</code>.</div>
  <table id="tper"></table>
  <div class="note crit">Период <b>14–25 июля</b> дал <b>7.5% объёма, но 25% денег</b>.
    Период <b>27 июля</b> — <b>75% объёма и 53% денег</b>. Вдесятеро больший объём стоил
    вдвое дороже, а не в десять раз — вся разница в кэше. Переплата из-за неработающего
    кэширования <b>$1 682, это 34% всей суммы</b>. Эффективность кэша важнее объёма.</div>
  <div class="note">До 8 июля транскриптов нет вовсе, хотя <code>firstStartTime</code> —
    2026-06-05. Около месяца работы не залогировано и не измеримо.</div>
</div>

<div class="card">
  <h2>Claude Code: разбор по моделям <span class="badge b-meas">ИЗМЕРЕНО</span></h2>
  <p class="cap" id="dpcap"></p>
  <table id="tdeep"></table>
  <div class="note">Главное в этой таблице — колонка «кэш-попадание». У
    <code>opus-5</code> медиана свежего ввода <b>2 токена</b>, у <code>opus-4-8</code> —
    <b>3 579</b>. Отсюда: <code>opus-4-8</code> дал втрое меньше токенов, но свежий ввод
    стоил <b>$1 426,91</b> против <b>$9,81</b> — в 145 раз дороже, и итог почти сравнялся.
    Причина — 111 коротких сессий против 26 длинных: короткая не успевает нагреть кэш.</div>
</div>

<div class="grid2">
  <div class="card">
    <h2>Рост контекста внутри сессии</h2>
    <p class="cap">Средний контекст на вызов в зависимости от номера вызова. Это и есть
      механизм расхода: платят не за то, что модель пишет, а за пересылку истории.</p>
    <div id="growth"></div>
  </div>
  <div class="card">
    <h2>Концентрация расхода</h2>
    <p class="cap">Доля всего объёма, приходящаяся на N крупнейших сессий из 148.</p>
    <div id="conc"></div>
  </div>
</div>

<div class="card">
  <h2>На что ушли токены <span class="badge b-meas">ИЗМЕРЕНО</span></h2>
  <p class="cap">Поимённо, по названиям сессий из записей <code>ai-title</code>. Токены
    дедуплицированы. Снимок на момент прогона анализатора — сессия аудита ещё дописывалась.</p>
  <table id="twent"></table>
  <div style="margin-top:14px" id="wproj"></div>
  <div class="note">По Codex — 2705 тредов, и слово <code>audit</code> в
    <b>1142 названиях, то есть в 42% всех тредов</b>. Плюс смежные: <code>inspect</code> 67,
    <code>gaps</code> 65, <code>locate</code> 45, <code>scan</code> 38. Это крупнейшая
    опознаваемая статья расхода — и она по определению перечитывает уже прочитанное.
    Codex почти целиком ушёл в HECTON-8: <code>c:\hades</code> 90.47 млрд,
    <code>c:\hades\Hecton8</code> 17.74 млрд.</div>
  <div class="note">Характер названий — «непрерывное улучшение», «автономная разработка»,
    «автономный рефакторинг», «продолжить operation immune system». Это не диалоги, а
    длительные самоуправляемые прогоны, что согласуется с профилем расхода: половина
    ответов приходится на вызовы после 225-го в сессии.</div>
</div>

<div class="card">
  <h2>Antigravity: что Google опубликовал сам <span class="badge b-rep">ВНЕШНИЕ ДАННЫЕ</span></h2>
  <p class="cap">Единственная официальная метрика — недельные активные пользователи.
    Токенов Google не публикует намеренно: квота считается в «выполненной работе»
    (compute effort), а не в токенах.</p>
  <table id="tgo"></table>
  <div class="note crit">Вердикт «счётчика нет» подтверждён независимо: на форуме Google AI
    Developers разработчик сообщил, что не может получить input / cached / output токены
    для чатов через Hooks — <b>данных нет в <code>transcript.jsonl</code></b>. То есть
    счётчика нет <b>by design</b>, а не потерян на этой машине.</div>
  <div class="note">История квот объясняет 6 134 упора: <b>250</b> запросов в день на
    запуске в ноябре 2025 → <b>20 в день</b> к декабрю (−92%) → в марте 2026 Pro переведён
    с 5-часового обновления на <b>недельное</b> с блокировками до 168 часов. Google дважды
    утроил лимиты после бэклэша. Ротация 19 аккаунтов была реакцией на это.</div>
</div>

<div class="card">
  <h2>На фоне индустрии <span class="badge b-rep">ВНЕШНИЕ ДАННЫЕ</span></h2>
  <p class="cap">Публичные объёмы — самоотчёты вендоров, не аудированные. Сравнение
    показывает порядок величины, а не точность.</p>
  <div id="ext"></div>
  <table id="text"></table>
</div>

<div class="card">
  <h2>Деньги — эквивалент по прайсу, <u>не счёт</u></h2>
  <p class="cap">Тарифы Anthropic сверены с живой документацией 2026-07-27;
    тарифы OpenAI взяты из каталога того же отчёта (источник developers.openai.com,
    проверен 2026-06-06).</p>
  <div id="cost"></div>
  <table id="tcost"></table>
  <div class="note crit">2792 из 2804 сессий Codex шли с <code>plan_type: "free"</code>,
    ещё 10 — <code>"team"</code>. Это подписка, а значит эти доллары почти наверняка
    <b>никогда не выставлялись счётом</b>. Цифра отвечает на вопрос «сколько бы стоил
    такой объём по публичному прайсу», а не «сколько заплачено».</div>
  <div class="note">По Claude Code сумма — <b>нижняя граница</b>. Прокси
    <code>Desktop\proxy.js</code> бесконечно повторял запросы при 429/5xx каждые 2 с;
    эти повторы жгли токены вверху, но в транскрипт не попали.</div>
</div>

<div class="card">
  <h2>Откуда взялся доступ: планы, аккаунты, ключи <span class="badge b-meas">ИЗМЕРЕНО</span></h2>
  <p class="cap">Ответ на вопрос «как такой объём был возможен»: за токены почти никогда
    не платили по счётчику. Только то, что зафиксировано в локальных файлах.</p>
  <div class="note crit"><b>Фактические траты за всё время — около 1000 рублей</b>
    (со слов владельца), целиком на аккаунты и подписки для 19 Google-аккаунтов.
    По Codex и Claude Code — ноль. Против этого стоит <b>$92 132</b> эквивалента по
    публичному прайсу. И отсюда инверсия: <b>единственный провайдер, которому заплатили,
    — единственный, который расход не показывает</b>. Два, которым не заплатили ничего,
    ведут учёт до токена. Он же самый хрупкий: 2 аккаунта из 19 уже expired,
    6 134 упора в квоту.</div>
  <div class="note">Почему бесплатный тариф Codex выдержал 119 млрд: с 29 апреля недельное
    окно лимита стало перезапускаться практически на каждом запросе — до
    <b>10 403 разных границ окна за одни сутки</b> 17 мая при неизменном лаге 6.99 суток,
    из-за чего <code>used_percent</code> залип на floor-значении <b>3.0%</b> на полмиллиона
    событий. До 29 апреля лимит был настоящим: 100% выбивались в 20 днях из 26. Конец
    резкий — 9 июня обвал в 94 раза, последнее событие 13 июня. Причину по локальным
    файлам установить нельзя. Число аккаунтов по Codex — <b>не наблюдаемая величина</b>:
    в формате роллаута поля аккаунта нет вообще.</div>
  <table id="tacc"></table>
  <div class="note">В <code>proxy.js</code> <b>один ключ, а не пул</b>, несмотря на
    комментарий «ENTERPRISE KEY POOL». Ротации в коде нет — есть 13 повторов и 4 обработки
    кода 429. Именно эти повторы жгут токены, которые в транскрипт не попадают, поэтому
    измеренное по Claude Code — нижняя граница.</div>
  <div class="note crit">Комплаенс-риск зафиксирован в данных, а не предполагается:
    <b>19 Google-аккаунтов</b>, сортировка по остатку квоты
    (<code>account_sort: quota-overall</code>), балансировка нагрузки и
    <code>device_profile_json</code> + <code>device_history_json</code> на каждом аккаунте.
    Это ротация аккаунтов с управлением отпечатком устройства. Схема нарушает условия
    провайдеров и приводит к блокировкам — <b>2 аккаунта уже в статусе expired</b>,
    и зафиксировано <b>6 134 упора в квоту</b>.</div>
  <div class="note crit"><b>Граница охвата.</b> Всё измеренное — расход
    <b>с машин владельца</b>. Со слов владельца, в середине мая конфигурационный файл с
    токенами входа был роздан <b>десяти людям</b>, и суммарно они израсходовали «примерно
    столько же, может чуть больше». Их расход шёл через те же учётные данные, но с их
    дисков, и в локальных данных отсутствует физически. Оценка суммарного расхода через
    эти учётки — <b>~250–275 млрд токенов</b>, но это слова владельца, а не измерение.
    Хронология важна: заморозка счётчика 29 апреля, раздача — середина мая, то есть
    <b>раздача не была её причиной</b>. Зато она правдоподобно объясняет, почему
    «чудо» закончилось 9 июня: одиннадцать человек под одними credentials заметнее
    одного.</div>
  <div class="note">Все суммы в долларах в этом отчёте — <b>эквивалент по публичному
    прайсу</b>, а не траты. Происхождение ключа Anthropic неизвестно: три из четырёх
    периодов доступа шли через посредников, поэтому перед ротацией стоит установить,
    чей он.</div>
</div>

<div class="card">
  <h2>Что осталось неизмеренным</h2>
  <div id="gaps"></div>
</div>
</div>
<div class="tip" id="tip"></div>

<script>
const D = __DATA__;
const $ = s => document.querySelector(s);
const SV = 'http://www.w3.org/2000/svg';
const el = (t,a={}) => { const e=document.createElementNS(SV,t);
  for(const k in a) e.setAttribute(k,a[k]); return e; };
const cv = n => getComputedStyle(document.body).getPropertyValue(n).trim();
const nf = n => n.toLocaleString('ru-RU');
function big(n){ const a=Math.abs(n);
  if(a>=1e9) return (n/1e9).toFixed(a>=1e10?1:2)+' млрд';
  if(a>=1e6) return (n/1e6).toFixed(a>=1e8?0:1)+' млн';
  if(a>=1e3) return (n/1e3).toFixed(0)+' тыс';
  return String(n); }
const usd = n => '$'+n.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});

/* ---- tooltip ---- */
const tip=$('#tip');
function showTip(ev, title, rows){
  tip.innerHTML='<b>'+title+'</b>'+rows.map(r=>
    '<div class="r"><span>'+(r[2]?'<i style="background:'+r[2]+'"></i>':'')+r[0]+
    '</span><span>'+r[1]+'</span></div>').join('');
  tip.style.opacity=1;
  const w=tip.offsetWidth, h=tip.offsetHeight;
  let x=ev.clientX+14, y=ev.clientY-h/2;
  if(x+w>innerWidth-8) x=ev.clientX-w-14;
  tip.style.left=Math.max(8,x)+'px';
  tip.style.top=Math.min(innerHeight-h-8,Math.max(8,y))+'px';
}
const hideTip=()=>tip.style.opacity=0;
addEventListener('scroll',hideTip,{passive:true});

/* ---- stacked time series with crosshair ---- */
function stack(host, s, keys, names, cols, opt={}){
  host.innerHTML='';
  const W=1200, H=opt.h||280, ml=64, mr=14, mt=10, mb=30;
  const n=s.labels.length; if(!n) return;
  const iw=W-ml-mr, ih=H-mt-mb;
  const tot=s.labels.map((_,i)=>keys.reduce((a,k)=>a+(s[k][i]||0),0));
  const max=Math.max(1,...tot);
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,preserveAspectRatio:'none',
    style:'height:'+H+'px'}); host.appendChild(svg);
  const y=v=>mt+ih-v/max*ih;
  const bw=iw/n, gap=n>160?0:Math.min(2,bw*0.22);
  /* gridlines + y ticks */
  for(let t=0;t<=4;t++){ const v=max*t/4, yy=y(v);
    svg.appendChild(el('line',{x1:ml,x2:W-mr,y1:yy,y2:yy,class:'gl'}));
    const tx=el('text',{x:ml-8,y:yy+4,'text-anchor':'end'}); tx.textContent=big(v);
    svg.appendChild(tx); }
  svg.appendChild(el('line',{x1:ml,x2:W-mr,y1:mt+ih,y2:mt+ih,class:'ax'}));
  /* bars, stacked, 2px surface gap between segments */
  for(let i=0;i<n;i++){
    let acc=0;
    for(let k=0;k<keys.length;k++){
      const v=s[keys[k]][i]||0; if(v<=0) continue;
      const y0=y(acc), y1=y(acc+v); let hh=y0-y1;
      const seg=keys.length>1&&k<keys.length-1?2:0;
      hh=Math.max(0.6,hh-(hh>3?seg:0));
      svg.appendChild(el('rect',{x:ml+i*bw+gap/2,y:y1,width:Math.max(0.6,bw-gap),
        height:hh,fill:cols[k],rx:hh>4&&k===keys.length-1?2:0}));
      acc+=v;
    }
  }
  /* x ticks */
  const step=Math.max(1,Math.ceil(n/9));
  for(let i=0;i<n;i+=step){
    const t=el('text',{x:ml+i*bw+bw/2,y:H-10,'text-anchor':'middle'});
    t.textContent=(opt.fmtX||(x=>x))(s.labels[i]); svg.appendChild(t); }
  /* crosshair */
  const ch=el('line',{x1:0,x2:0,y1:mt,y2:mt+ih,class:'ax',opacity:0});
  svg.appendChild(ch);
  const hit=el('rect',{x:ml,y:mt,width:iw,height:ih,fill:'transparent'});
  svg.appendChild(hit);
  hit.addEventListener('mousemove',e=>{
    const r=svg.getBoundingClientRect();
    const i=Math.min(n-1,Math.max(0,Math.floor((e.clientX-r.left)/r.width*W-ml)/bw|0));
    const px=ml+i*bw+bw/2; ch.setAttribute('x1',px); ch.setAttribute('x2',px);
    ch.setAttribute('opacity',.9);
    const rows=keys.map((k,j)=>[names[j],nf(s[k][i]||0),cols[j]]);
    rows.push(['всего',nf(tot[i])]);
    showTip(e,s.labels[i],rows);
  });
  hit.addEventListener('mouseleave',()=>{ch.setAttribute('opacity',0);hideTip();});
}

/* ---- horizontal labelled bars ---- */
function hbar(host, rows, opt={}){
  host.innerHTML='';
  const W=1200, rh=opt.rh||34, mt=6, ml=opt.ml||190, mr=150;
  const H=mt+rows.length*rh+6;
  const max=Math.max(1,...rows.map(r=>r.v));
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,preserveAspectRatio:'none',
    style:'height:'+H+'px'}); host.appendChild(svg);
  rows.forEach((r,i)=>{
    const y=mt+i*rh, bh=Math.min(18,rh-14);
    const w=Math.max(2,r.v/max*(W-ml-mr));
    const t=el('text',{x:ml-10,y:y+bh/2+4,'text-anchor':'end',class:'lbl'});
    t.textContent=r.k; svg.appendChild(t);
    const rect=el('rect',{x:ml,y:y,width:w,height:bh,fill:r.c,rx:4});
    svg.appendChild(rect);
    const vt=el('text',{x:ml+w+9,y:y+bh/2+4,class:'lbl'});
    vt.textContent=r.d||big(r.v); svg.appendChild(vt);
    if(r.n){ const nt=el('text',{x:ml+w+9,y:y+bh/2+18}); nt.textContent=r.n;
      svg.appendChild(nt); }
    rect.addEventListener('mousemove',e=>showTip(e,r.k,
      (r.rows||[['значение',nf(r.v),r.c]])));
    rect.addEventListener('mouseleave',hideTip);
  });
}

/* ---- sequential heatmap row ---- */
function heat(host, s, key){
  host.innerHTML='';
  const n=s.labels.length, W=1200, cw=W/n, H=86;
  const vals=s.labels.map((_,i)=>s[key][i]||0);
  const max=Math.max(1,...vals);
  const ramp=['--seq1','--seq2','--seq3','--seq4','--seq5','--seq6','--seq7'].map(cv);
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,preserveAspectRatio:'none',
    style:'height:'+H+'px'}); host.appendChild(svg);
  s.labels.forEach((lb,i)=>{
    const f=vals[i]/max;
    const c=ramp[Math.min(ramp.length-1,Math.round(f*(ramp.length-1)))];
    const r=el('rect',{x:i*cw+1,y:6,width:cw-2,height:44,fill:c,rx:3});
    svg.appendChild(r);
    const t=el('text',{x:i*cw+cw/2,y:68,'text-anchor':'middle'});
    t.textContent=lb.slice(-2); svg.appendChild(t);
    r.addEventListener('mousemove',e=>showTip(e,lb+':00',
      [['токенов',nf(vals[i]),c],['доля от пика',(f*100).toFixed(0)+'%']]));
    r.addEventListener('mouseleave',hideTip);
  });
}


/* ---- punchcard: day x hour, dot area proportional to value ---- */
function punch(host, mat){
  host.innerHTML='';
  const days=[...new Set(Object.keys(mat).map(k=>k.split('|')[0]))].sort();
  if(!days.length) return;
  const W=1200, ml=78, mr=20, mt=18, cell=(W-ml-mr)/24, rh=26;
  const H=mt+days.length*rh+24;
  const max=Math.max(...Object.values(mat));
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,preserveAspectRatio:'none',
    style:'height:'+H+'px'}); host.appendChild(svg);
  for(let h=0;h<24;h+=2){
    const t=el('text',{x:ml+h*cell+cell/2,y:mt-5,'text-anchor':'middle'});
    t.textContent=String(h).padStart(2,'0'); svg.appendChild(t);
  }
  days.forEach((d,i)=>{
    const y=mt+i*rh+rh/2;
    const lb=el('text',{x:ml-10,y:y+4,'text-anchor':'end'}); lb.textContent=d.slice(5);
    svg.appendChild(lb);
    for(let h=0;h<24;h++){
      const key=d+'|'+String(h).padStart(2,'0');
      const v=mat[key]||0, cx=ml+h*cell+cell/2;
      svg.appendChild(el('circle',{cx:cx,cy:y,r:2.2,fill:cv('--grid')}));
      if(v<=0) continue;
      const r=Math.max(2.6,Math.sqrt(v/max)*(rh/2-1.5));
      const c=el('circle',{cx:cx,cy:y,r:r,fill:cv('--s1'),
        stroke:cv('--surface'),'stroke-width':1.5});
      svg.appendChild(c);
      c.addEventListener('mousemove',e=>showTip(e,d+' '+String(h).padStart(2,'0')+':00',
        [['токенов',nf(v),cv('--s1')],['от пика',(100*v/max).toFixed(1)+'%']]));
      c.addEventListener('mouseleave',hideTip);
    }
  });
}

/* ---- gantt: sessions on a real time axis, packed into lanes ---- */
function gantt(host, sess, opt){
  opt=opt||{}; host.innerHTML='';
  const rows=sess.filter(x=>x.start&&x.end).slice(-(opt.limit||80));
  if(!rows.length) return;
  const ep=x=>Date.parse(x);
  const t0=Math.min(...rows.map(r=>ep(r.start))), t1=Math.max(...rows.map(r=>ep(r.end)));
  const span=Math.max(1,t1-t0);
  const W=1200, ml=10, mr=10, mt=24, lane=13, gp=2;
  const lanes=[];
  rows.sort((a,b)=>ep(a.start)-ep(b.start)).forEach(r=>{
    let li=lanes.findIndex(t=>t<=ep(r.start));
    if(li<0){lanes.push(0); li=lanes.length-1;}
    r._l=li; lanes[li]=ep(r.end)+span*0.004;
  });
  const H=mt+lanes.length*(lane+gp)+26;
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,preserveAspectRatio:'none',
    style:'height:'+H+'px'}); host.appendChild(svg);
  const X=t=>ml+(t-t0)/span*(W-ml-mr);
  const MC={'claude-opus-5':'--s1','claude-opus-4-8':'--s3','claude-fable-5':'--s5',
            'gpt-5.5':'--s2','claude-sonnet-5':'--s4'};
  const max=Math.max(...rows.map(r=>r.total));
  const day=86400000, start=Math.ceil(t0/day)*day;
  for(let t=start;t<=t1;t+=day){
    const px=X(t);
    svg.appendChild(el('line',{x1:px,x2:px,y1:mt-8,y2:H-22,class:'gl'}));
    const lb=el('text',{x:px+3,y:mt-10});
    lb.textContent=new Date(t).toISOString().slice(5,10); svg.appendChild(lb);
  }
  rows.forEach(r=>{
    const x0=X(ep(r.start)), x1=Math.max(x0+1.5,X(ep(r.end)));
    const y=mt+r._l*(lane+gp);
    const h=Math.max(3,lane*Math.min(1,0.34+0.66*Math.sqrt(r.total/max)));
    const b=el('rect',{x:x0,y:y+(lane-h)/2,width:x1-x0,height:h,rx:2,
      fill:cv(MC[r.model]||'--s1'),stroke:cv('--surface'),'stroke-width':0.7});
    svg.appendChild(b);
    b.addEventListener('mousemove',e=>showTip(e,
      r.id+'…  '+r.start.slice(5,16).replace('T',' '),
      [['токенов',nf(r.total),cv(MC[r.model]||'--s1')],['ответов',nf(r.responses)],
       ['субагентских',nf(r.sub)],['модель',r.model||'—'],
       ['длительность',r.duration_s?(r.duration_s/3600).toFixed(1)+' ч':'—']]));
    b.addEventListener('mouseleave',hideTip);
  });
}

/* ---- bubble scatter ---- */
function scatter(host, pts, opt){
  opt=opt||{}; host.innerHTML='';
  if(!pts.length) return;
  const W=1200, H=opt.h||330, ml=66, mr=120, mt=14, mb=44;
  const xmax=Math.max(...pts.map(p=>p.x))*1.08;
  const ymax=Math.max(100,Math.max(...pts.map(p=>p.y)));
  const rmax=Math.max(...pts.map(p=>p.r))||1;
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,preserveAspectRatio:'none',
    style:'height:'+H+'px'}); host.appendChild(svg);
  const X=v=>ml+v/xmax*(W-ml-mr), Y=v=>mt+(1-v/ymax)*(H-mt-mb);
  for(let i=0;i<=4;i++){
    const v=ymax*i/4, y=Y(v);
    svg.appendChild(el('line',{x1:ml,x2:W-mr,y1:y,y2:y,class:'gl'}));
    const t=el('text',{x:ml-8,y:y+4,'text-anchor':'end'});
    t.textContent=v.toFixed(0)+'%'; svg.appendChild(t);
  }
  for(let i=0;i<=4;i++){
    const v=xmax*i/4;
    const t=el('text',{x:X(v),y:H-18,'text-anchor':'middle'});
    t.textContent=big(v); svg.appendChild(t);
  }
  const ax=el('text',{x:(ml+W-mr)/2,y:H-3,'text-anchor':'middle'});
  ax.textContent=opt.xlab||''; svg.appendChild(ax);
  pts.forEach(p=>{
    const r=Math.max(4,Math.sqrt(p.r/rmax)*22);
    const c=el('circle',{cx:X(p.x),cy:Y(p.y),r:r,fill:p.c,'fill-opacity':0.6,
      stroke:p.c,'stroke-width':1.6});
    svg.appendChild(c);
    if(p.lab){
      const t=el('text',{x:X(p.x)+r+5,y:Y(p.y)+4,class:'lbl'});
      t.textContent=p.lab; svg.appendChild(t);
    }
    c.addEventListener('mousemove',e=>showTip(e,p.k,p.rows));
    c.addEventListener('mouseleave',hideTip);
  });
}

/* ---- line with percentile band ---- */
function lineband(host, items, opt){
  opt=opt||{}; host.innerHTML='';
  const nn=items.length; if(!nn) return;
  const W=1200, H=opt.h||300, ml=72, mr=72, mt=14, mb=42;
  const max=Math.max(...items.map(d=>d.hi))*1.05;
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,preserveAspectRatio:'none',
    style:'height:'+H+'px'}); host.appendChild(svg);
  const X=i=>ml+(nn===1?0.5:i/(nn-1))*(W-ml-mr), Y=v=>mt+(1-v/max)*(H-mt-mb);
  for(let i=0;i<=4;i++){
    const v=max*i/4, y=Y(v);
    svg.appendChild(el('line',{x1:ml,x2:W-mr,y1:y,y2:y,class:'gl'}));
    const t=el('text',{x:ml-8,y:y+4,'text-anchor':'end'});
    t.textContent=big(v); svg.appendChild(t);
  }
  let up='';
  items.forEach((d,i)=>{up+=(i?'L':'M')+X(i)+','+Y(d.hi);});
  for(let i=nn-1;i>=0;i--) up+='L'+X(i)+','+Y(d0(items[i]));
  svg.appendChild(el('path',{d:up+'Z',fill:cv('--s1'),'fill-opacity':0.16}));
  let mid='';
  items.forEach((d,i)=>{mid+=(i?'L':'M')+X(i)+','+Y(d.mid);});
  svg.appendChild(el('path',{d:mid,fill:'none',stroke:cv('--s1'),'stroke-width':2}));
  items.forEach((d,i)=>{
    const c=el('circle',{cx:X(i),cy:Y(d.mid),r:4.5,fill:cv('--s1'),
      stroke:cv('--surface'),'stroke-width':2});
    svg.appendChild(c);
    const t=el('text',{x:X(i),y:H-24,'text-anchor':'middle'});
    t.textContent=d.k; svg.appendChild(t);
    c.addEventListener('mousemove',e=>showTip(e,d.title||d.k,
      [['медиана',nf(d.mid),cv('--s1')],['p90',nf(d.hi)],['ответов',nf(d.n)]]));
    c.addEventListener('mouseleave',hideTip);
  });
  function d0(x){return x.lo||0;}
}

/* ---- vertical histogram ---- */
function histo(host, items, opt){
  opt=opt||{}; host.innerHTML='';
  const nn=items.length; if(!nn) return;
  const W=1200, H=opt.h||220, ml=62, mr=16, mt=18, mb=40;
  const max=Math.max(...items.map(d=>d.v))*1.08;
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,preserveAspectRatio:'none',
    style:'height:'+H+'px'}); host.appendChild(svg);
  const bw=(W-ml-mr)/nn, Y=v=>mt+(1-v/max)*(H-mt-mb);
  const F=opt.fmt||big;
  for(let i=0;i<=3;i++){
    const v=max*i/3, y=Y(v);
    svg.appendChild(el('line',{x1:ml,x2:W-mr,y1:y,y2:y,class:'gl'}));
    const t=el('text',{x:ml-8,y:y+4,'text-anchor':'end'});
    t.textContent=F(v); svg.appendChild(t);
  }
  items.forEach((d,i)=>{
    const y=Y(d.v), h=mt+(H-mt-mb)-y;
    const r=el('rect',{x:ml+i*bw+bw*0.14,y:y,width:bw*0.72,
      height:Math.max(1,h),rx:3,fill:d.c||cv('--s1')});
    svg.appendChild(r);
    const t=el('text',{x:ml+i*bw+bw/2,y:H-22,'text-anchor':'middle'});
    t.textContent=d.k; svg.appendChild(t);
    const vl=el('text',{x:ml+i*bw+bw/2,y:y-5,'text-anchor':'middle',class:'lbl'});
    vl.textContent=F(d.v); svg.appendChild(vl);
    r.addEventListener('mousemove',e=>showTip(e,d.title||d.k,
      [[opt.unit||'значение',F(d.v),d.c||cv('--s1')]]));
    r.addEventListener('mouseleave',hideTip);
  });
}

/* ---- marimekko: column width proportional to day total ---- */
function mosaic(host, days, keys, cols){
  host.innerHTML='';
  const W=1200, H=300, ml=56, mr=14, mt=14, mb=42;
  const tot=days.reduce((a,d)=>a+d.total,0); if(!tot) return;
  const svg=el('svg',{viewBox:'0 0 '+W+' '+H,preserveAspectRatio:'none',
    style:'height:'+H+'px'}); host.appendChild(svg);
  const iw=W-ml-mr, ih=H-mt-mb;
  let x=ml;
  days.forEach(d=>{
    const w=Math.max(1.2,d.total/tot*iw);
    let y=mt;
    keys.forEach((k,ki)=>{
      const v=d.v[k]||0; if(!v) return;
      const h=v/d.total*ih;
      const r=el('rect',{x:x,y:y,width:Math.max(0.8,w-1.2),
        height:Math.max(0.6,h-0.8),fill:cols[ki]});
      svg.appendChild(r);
      r.addEventListener('mousemove',e=>showTip(e,d.k+' · '+k,
        [['токенов',nf(v),cols[ki]],['доля дня',(100*v/d.total).toFixed(1)+'%'],
         ['день всего',nf(d.total)]]));
      r.addEventListener('mouseleave',hideTip);
      y+=h;
    });
    if(w>26){
      const t=el('text',{x:x+w/2,y:H-22,'text-anchor':'middle'});
      t.textContent=d.k.slice(5); svg.appendChild(t);
    }
    x+=w;
  });
  const note=el('text',{x:ml,y:H-6});
  note.textContent='ширина колонки пропорциональна объёму дня';
  svg.appendChild(note);
}

/* ---- legend ---- */
function legend(host, names, cols){
  host.innerHTML=names.map((n,i)=>
    '<span><i style="background:'+cols[i]+'"></i>'+n+'</span>').join('');
}

/* ================= render ================= */
let RES='by_day', RES2='by_day';
const KEYS=['inp','cc','cr','out'];
const NAMES=['свежий ввод','запись кэша','чтение кэша','вывод'];
const CXK=['input_tokens','output_tokens'];
const CXN=['ввод (вкл. кэш)','вывод'];

function cols(v){ return v.map(cv); }
const C4=()=>cols(['--s1','--s3','--s2','--s5']);
const C5=()=>cols(['--s1','--s2','--s3','--s4','--s5']);

function tot(o){ return o.inp+o.cc+o.cr+o.out; }

function render(){
  const c=D.claude;
  const hasAG=!!D.antigravity, a=D.antigravity||{by_day:{labels:[]},by_hour:{labels:[]},totals:{},types:{},quota_by_day:{}};
  /* Codex необязателен: у клонировавшего его данных может не быть. Флаг hasCX
     решает, рисовать ли его панели; заглушка нужна лишь чтобы обращения к
     полям не бросали до проверки. */
  const hasCX=!!D.codex, x=D.codex||{prior:{roots:{},by_model_delta:{}},cost:{},cost_total:0,
    by_day:{labels:[],series:{}},by_hour:{labels:[],series:{}},
    by_minute:{labels:[],series:{}},totals:{}};
  const cT=tot(c.totals);

  /* tiles */
  const RC=D.recon;
  const CXTOT=(RC.consistent_total_max_basis||{}).total_tokens||0;
  const CXMINE=(RC.components&&RC.components.length)?RC.components[0].total_tokens:0;
  $('#tiles').innerHTML=[
    /* Составной итог Codex может отсутствовать: он собирается из локально
       измеренного chain-split и внешних измерений, а внешних у клонировавшего
       нет. Тогда плитка честно говорит, что итог не собран, и показывает только
       ту часть, которая измерена здесь. Подставлять чужую цифру нельзя. */
    ...(CXTOT ? [['Codex', big(CXTOT),
        '<span class="badge b-meas">ИЗМЕРЕНО</span>+<span class="badge b-rep">ОТЧЁТ</span>'
        + '+<span class="badge b-est">ОЦЕНКА</span> 2026-04-03 → 06-13',
        'консервативно; верхняя граница ' + big(RC.upper_bound_if_delta_valid || 0)]]
      : [['Codex', 'итог не собран',
        '<span class="badge b-est">НЕТ ВНЕШНИХ ИЗМЕРЕНИЙ</span>',
        'локально измерено ' + big(CXMINE)]]),
    ['Claude Code',big(cT),'<span class="badge b-meas">ИЗМЕРЕНО</span> 2026-07-08 → 07-27, 20 дней',
     c.sessions+' сессий · '+nf(c.resp_uniq)+' ответов'],
    ...(hasAG?[['Antigravity','нет счётчика','<span class="badge b-proxy">ПРОКСИ</span> 2026-06-05 → 07-27, 52 дня',
     nf(a.types.PLANNER_RESPONSE)+' ходов модели · '+nf(a.totals.quota_blocks)+' упоров в квоту']]:[]),
    ['Проверено независимо','0.645%','<span class="badge b-ver">СВЕРЕНО</span> расхождение двух реализаций',
     'мои 58.23 млрд против 57.86 млрд в старом отчёте по тому же корню'],
  ].map(t=>'<div class="tile"><div class="k">'+t[0]+'</div><div class="v">'+t[1]+
    '</div><div class="n">'+t[2]+'<br>'+t[3]+'</div></div>').join('');

  /* scale */
  hbar($('#scale'),RC.components.map((cc,i)=>({
     k:{'backup_apr03_may21_mine_chainsplit':'бэкап, измерил я',
        'danat_apr03_jun06_old_audit':'danat до 6 июня (по отчёту)',
        'danat_archived':'danat архив',
        'danat_after_jun06_rescaled':'danat после 6 июня (оценка)'}[cc.name]||cc.name,
     v:cc.total_tokens,c:cv(['--s2','--s5','--s4','--s3'][i]||'--s1'),
     d:big(cc.total_tokens),n:cc.class+' · '+cc.source.slice(0,58)}))
   .concat([{k:'Claude Code',v:cT,c:cv('--s1'),d:big(cT),n:'ИЗМЕРЕНО · 20 дней'}]),
   {rh:50,ml:250});

  /* claude time series */
  const rb=(host,cur,set,list)=>{ host.innerHTML=''; list.forEach(([k,l])=>{
    const b=document.createElement('button'); b.textContent=l;
    b.setAttribute('aria-pressed',cur()===k);
    b.onclick=()=>{set(k);render()}; host.appendChild(b); }); };
  /* Честная подпись разрешения. Серия, встроенная в HTML, могла быть сокращена
     (см. MAX_POINTS в build_dashboard.py): подписывать корзину по 40 минут
     словом «минута» значит врать о том, что показано. Если сокращение было,
     говорим фактический шаг. */
  const spanNote=ser=>{
    if(!ser||!ser.bucket_span||ser.bucket_span<2) return '';
    const n=ser.bucket_span, from=ser.downsampled_from;
    return ' · корзины по '+n+' мин (сведено из '+nf(from)+' точек: столько'+
           ' на экран не помещается, суммы сохранены)';
  };
  rb($('#res'),()=>RES,v=>RES=v,[['by_day','сутки'],['by_hour','час'],
    ['by_10min','10 мин'],['by_minute','минута']]);
  legend($('#lg1'),NAMES,C4());
  stack($('#ts'),c[RES],KEYS,NAMES,C4(),{h:300,
    fmtX:s=>RES==='by_day'?s.slice(5):RES==='by_hour'?s.slice(5).replace('T',' ')+'ч':s.slice(5).replace('T',' ')});
  {const el=$('#resnote'); if(el) el.textContent=spanNote(c[RES]);}
  $('#crshare').textContent=(c.totals.cr/cT*100).toFixed(1)+'%';

  /* Сравнение двух последних суток. Раньше здесь стояла таблица, вписанная
     руками: она утверждала «ВСЕГО 4 329 731 446 -> 7 769 375 039», тогда как из
     by_day накопительно выходило 6 271 135 564 -> 17 710 985 238 -- ошибка в
     2.28 раза, и всё это было опубликовано. Числа, которые никто не
     пересчитывает, расходятся с данными не постепенно, а в разы.

     Теперь таблица и часовая полоса считаются из by_day и by_hour, обновляются
     сами и сравнивают две ПОСЛЕДНИЕ даты, а не одну зафиксированную ночь. */
  const DAYC=D.daycmp;
  if(DAYC){
    const dd=s=>s.slice(8)+'.'+s.slice(5,7);
    $('#tnight').innerHTML='<tr><th>метрика</th><th>на конец '+dd(DAYC.prev)+
      '</th><th>на конец '+dd(DAYC.last)+'</th><th>прирост</th></tr>'+
      DAYC.rows.map(r=>'<tr><td style="text-align:left">'+
        (r.name==='ВСЕГО'?'<b>ВСЕГО</b>':r.name)+'</td><td>'+nf(r.before)+
        '</td><td>'+nf(r.after)+'</td><td><b>+'+nf(r.after-r.before)+
        '</b></td></tr>').join('')+
      '<tr><td style="text-align:left"><b>$ по прайсу</b></td><td>'+usd(DAYC.cost_before)+
      '</td><td>'+usd(DAYC.cost_after)+'</td><td><b>+'+
      usd(DAYC.cost_after-DAYC.cost_before)+'</b></td></tr>';
    hbar($('#nighthr'),DAYC.hours.map(r=>({k:r.label,v:r.total,c:cv('--s2'),d:big(r.total),
      n:'кэш '+r.cache_pct.toFixed(1)+'%',rows:[['токенов',nf(r.total),cv('--s2')],
      ['кэш-попадание',r.cache_pct.toFixed(1)+'%']]})),{ml:110,rh:28});
    /* Состав прироста считается здесь же из тех же строк таблицы: две цифры на
       одной странице не должны спорить. */
    const tr=DAYC.rows[DAYC.rows.length-1], dTot=tr.after-tr.before;
    const part=n=>{const r=DAYC.rows.find(x=>x.name===n); return r?r.after-r.before:0;};
    const pc=v=>dTot?(100*v/dTot).toFixed(1)+'%':'—';
    $('#dchead').innerHTML=dd(DAYC.prev)+' → '+dd(DAYC.last)+
      ' <span class="badge b-meas">ИЗМЕРЕНО</span>';
    $('#dccap').textContent='Накопительные итоги на конец каждой даты и прирост между ними. '+
      'Часовая полоса — последние '+DAYC.hours.length+' часов.';
    $('#dcnote').innerHTML='Из прироста <b>'+nf(dTot)+'</b> токенов: чтение кэша <b>'+
      pc(part('чтение кэша'))+'</b>, запись кэша '+pc(part('запись кэша'))+
      ', вывод '+pc(part('вывод'))+', свежий ввод <b>'+pc(part('свежий ввод'))+
      '</b>. Впервые модель увидела <b>'+nf(part('свежий ввод'))+
      '</b> новых токенов'+(DAYC.cache_min!=null?'. Доля попаданий в кэш не опускалась ниже <b>'+
      DAYC.cache_min.toFixed(1)+'%</b> ни в одном из показанных часов':'')+'.';
    if(DAYC.top_day) $('#dctop').innerHTML='Крупнейшие сутки за весь период — <b>'+
      dd(DAYC.top_day.date)+'</b>: '+nf(DAYC.top_day.total)+' токенов, это <b>'+
      DAYC.top_day.share_pct.toFixed(1)+'%</b> всего расхода за '+DAYC.top_day.days+
      ' суток. Механика та же, что и на Codex: в длинной сессии весь контекст '+
      'пересылается заново на каждом ходу, поэтому объём растёт от самой длины, '+
      'а не от количества новой работы.';
  }

  /* hour of day + weekday */
  heat($('#hod'),c.by_hour_of_day,'cr');
  const wd=c.by_weekday;
  const RU={Mon:'пн',Tue:'вт',Wed:'ср',Thu:'чт',Fri:'пт',Sat:'сб',Sun:'вс'};
  hbar($('#wd'),wd.labels.map((l,i)=>({k:RU[l.slice(2)]||l.slice(2),
    v:wd.inp[i]+wd.cc[i]+wd.cr[i]+wd.out[i],c:cv('--s1')})),{ml:60,rh:30});

  /* claude per model */
  const mk=Object.keys(c.by_model).filter(m=>tot(c.by_model[m])>0)
    .sort((p,q)=>tot(c.by_model[q])-tot(c.by_model[p]));
  legend($('#lg2'),NAMES,C4());
  hbar($('#models'),mk.map(m=>({k:m,v:tot(c.by_model[m]),c:cv('--s1'),
    d:big(tot(c.by_model[m])),n:c.by_model[m].n+' ответов',
    rows:KEYS.map((k,j)=>[NAMES[j],nf(c.by_model[m][k]),C4()[j]])})),{rh:44});
  $('#tmodels').innerHTML='<tr><th>модель</th><th>ответов</th>'+
    NAMES.map(n=>'<th>'+n+'</th>').join('')+'<th>всего</th><th>$ по прайсу</th></tr>'+
    mk.map(m=>{const v=c.by_model[m];const co=c.cost[m];
      return '<tr><td>'+m+'</td><td>'+nf(v.n)+'</td>'+
      KEYS.map(k=>'<td>'+nf(v[k])+'</td>').join('')+
      '<td><b>'+nf(tot(v))+'</b></td><td>'+(co?usd(co.total_usd):'—')+'</td></tr>';}).join('')+
    '<tr><td><b>итого</b></td><td>'+nf(c.resp_uniq)+'</td>'+
    KEYS.map(k=>'<td><b>'+nf(c.totals[k])+'</b></td>').join('')+
    '<td><b>'+nf(cT)+'</b></td><td><b>'+usd(c.cost_total)+'</b></td></tr>';

  { const G=D.deep;
  /* ---- punchcard ---- */
  punch($('#punch'),G.punch||{});

  /* ---- gantt ---- */
  {const MC={'claude-opus-5':'--s1','claude-opus-4-8':'--s3','claude-fable-5':'--s5',
             'gpt-5.5':'--s2','claude-sonnet-5':'--s4'};
   const used=[...new Set((G.sessions_slim||[]).map(x=>x.model).filter(Boolean))];
   legend($('#lgg'),used,used.map(m=>cv(MC[m]||'--s1')));
   gantt($('#gantt'),G.sessions_slim||[],{limit:90});}

  /* ---- cost regime scatter ---- */
  {const dfull=G.day_full||{}, pts=[];
   for(const k of Object.keys(dfull).sort()){
     const v=dfull[k];
     const usd_=v.inp/1e6*5 + v.cc/1e6*6.25 + v.cr/1e6*0.5 + v.out/1e6*25;
     if(v.total<=0) continue;
     const col = v.cache_pct>=85?cv('--good'):v.cache_pct>=50?cv('--warn'):cv('--crit');
     pts.push({x:v.total,y:v.cache_pct,r:Math.max(0.5,usd_),c:col,
       lab:(v.total>4e8||usd_>600)?k.slice(5):'',k:k,
       rows:[['токенов',nf(v.total),col],['кэш-попадание',v.cache_pct.toFixed(1)+'%'],
             ['$ по прайсу',usd(usd_)],['ответов',nf(v.n)]]});
   }
   scatter($('#regime'),pts,{xlab:'токенов за сутки',h:340});}

  /* ---- context growth band ---- */
  {const g=G.growth||{}, items=Object.keys(g).map(k=>({
     k:k.replace('calls_','').replace('_plus','+').replace('_','–'),
     title:'вызовы '+k.replace('calls_','').replace('_plus',' и далее').replace('_','–'),
     mid:g[k].median_context,hi:g[k].p90_context,lo:0,n:g[k].responses}));
   lineband($('#growthband'),items,{h:300});}

  /* ---- subagent share per day ---- */
  {const sb=G.sub_by_day||{};
   histo($('#subday'),Object.keys(sb).sort().map(k=>({
     k:k.slice(5),v:sb[k].sub_pct,
     c:sb[k].sub_pct>=20?cv('--s5'):cv('--s2'),
     title:k+' — доля субагентов'})),
     {h:300,unit:'доля субагентов',fmt:v=>v.toFixed(0)+'%'});}

  /* ---- session size histogram ---- */
  {const sh=G.size_hist||{};
   const ord=Object.keys(sh).sort((a,b)=>{
     const f=x=>x==='0'?-1:parseInt(x.replace('1e',''),10); return f(a)-f(b);});
   histo($('#sizeh'),ord.map(k=>({k:k==='0'?'0':'10^'+k.replace('1e',''),
     v:sh[k],c:cv('--s1'),title:'сессий с объёмом порядка '+k})),
     {h:260,unit:'сессий',fmt:v=>String(Math.round(v))});}

  /* ---- output size histogram ---- */
  {const ob=G.out_buckets||{};
   const order=['0','1-99','100-499','500-1999','2000-7999','8000+'];
   histo($('#outh'),order.filter(k=>k in ob).map(k=>({
     k:k,v:ob[k],c:cv('--s3'),title:'ответов с выводом '+k+' токенов'})),
     {h:260,unit:'ответов',fmt:v=>nf(Math.round(v))});}

  /* ---- marimekko day x model ---- */
  {const bdm=c.by_day_model||{};
   const mk2=Object.keys(c.by_model).filter(m=>tot(c.by_model[m])>0)
     .sort((p,q)=>tot(c.by_model[q])-tot(c.by_model[p]));
   const colsM=mk2.map((_,i)=>cv(['--s1','--s3','--s5','--s2','--s4'][i%5]));
   legend($('#lgm'),mk2,colsM);
   const days=Object.keys(bdm).sort().map(d=>{
     const v={}; let t=0;
     for(const m of mk2){const x=bdm[d][m]; const q=x?tot(x):0; v[m]=q; t+=q;}
     return {k:d,v:v,total:t};
   }).filter(d=>d.total>0);
   mosaic($('#mosaic'),days,mk2,colsM);}

  }

  /* codex time series */
  rb($('#res2'),()=>RES2,v=>RES2=v,[['by_day','сутки'],['by_hour','час'],['by_minute','минута']]);
  legend($('#lg3'),CXN,cols(['--s1','--s5']));
  stack($('#ts2'),x[RES2],CXK,CXN,cols(['--s1','--s5']),{h:300,
    fmtX:s=>RES2==='by_day'?s.slice(5):s.slice(5).replace('T',' ')});
  {const el=$('#res2note'); if(el) el.textContent=spanNote(x[RES2]);}

  /* codex roots -- только если есть данные Codex */
  if(hasCX){
  const R=x.prior.roots;
  hbar($('#roots'),[
    {k:'второй комп (живой)',v:R.danat_live_sessions.total_tokens,c:cv('--s5'),
     d:big(R.danat_live_sessions.total_tokens),n:R.danat_live_sessions.files+' файлов · НЕТ ДОСТУПА'},
    {k:'бэкап (этот комп)',v:R.backup_20260521.total_tokens,c:cv('--s1'),
     d:big(R.backup_20260521.total_tokens),n:R.backup_20260521.files+' файлов · измерено мной'},
    {k:'архив (второй комп)',v:R.danat_archived.total_tokens,c:cv('--s2'),
     d:nf(R.danat_archived.total_tokens),n:'1 файл'},
  ],{ml:170,rh:48});

  /* codex per model -- тот же признак наличия данных */
  const cm=Object.entries(x.prior.by_model_delta).sort((p,q)=>q[1]-p[1]).slice(0,6);
  hbar($('#cxm'),cm.map(([m,v],i)=>({k:m,v:v,c:cv('--s'+((i%5)+1)),d:big(v)})),
    {ml:150,rh:34});
  }  /* конец панелей Codex */

  /* danat -- second machine. Данных может не быть: отчёт со второй машины
     необязателен, и панели тогда убираются целиком вместе со своими
     заголовками, а не остаются пустыми рамками. */
  const dnn=D.danat;
  if(!dnn){
    ['#dncap','#dnfiles','#dnday','#dnic','#dnsum'].forEach(function(sel){
      const el=$(sel); if(!el) return;
      const sec=el.closest('section')||el.parentElement;
      if(sec) sec.style.display='none'; else el.style.display='none';
    });
  } else {
  $('#dncap').textContent='Корень '+dnn.root+' · '+dnn.files+' файлов, '+dnn.gb+
    ' ГБ, '+dnn.sessions+' сессий · '+dnn.period[0].slice(0,10)+' → '+
    dnn.period[1].slice(0,10)+' · единственная модель gpt-5.5 · '+
    dnn.by_hour.labels.length+' часовых и '+dnn.by_minute.labels.length+
    ' минутных интервалов.';
  $('#dnfiles').textContent=dnn.files;
  legend($('#lg5'),CXN.map(t=>t+' — базис приростов, верхняя граница'),cols(['--s1','--s5']));
  stack($('#dnts'),dnn.by_day,CXK,CXN,cols(['--s1','--s5']),{h:230,fmtX:s=>s.slice(5)});
  const rr=RC.danat_by_day_both_bases;
  $('#tdn').innerHTML='<tr><th>день</th><th>метод приростов</th>'+
    '<th>базис максимумов</th><th></th></tr>'+
    Object.entries(rr).map(([k,v])=>'<tr><td>'+k+'</td><td>'+nf(v.delta)+'</td><td>'+
      nf(v.max_basis_estimate)+'</td><td style="text-align:left;color:var(--serious)">'+
      (k>'2026-06-06'?'ранее не измерялось':'')+'</td></tr>').join('')+
    '<tr><td><b>итого</b></td><td><b>'+nf(dnn.totals_delta.total_tokens)+
    '</b></td><td><b>'+nf(dnn.totals_max.total_tokens)+'</b></td><td></td></tr>'+
    '<tr><td>после 6 июня</td><td>'+nf(dnn.after_jun06.total_tokens)+'</td><td>'+
    nf(Math.round(dnn.after_jun06.total_tokens/RC.danat_internal_consistency.ratio_delta_over_max))+
    '</td><td style="text-align:left;color:var(--serious)">новое покрытие</td></tr>';

  /* three methods */
  const M=D.methods;
  hbar($('#meth'),[
    {k:'chain-split',v:M.chain_split.total_tokens,c:cv('--s2'),
     d:nf(M.chain_split.total_tokens),n:'разносит цепочки — принято'},
    {k:'максимум по файлу',v:M.naive_max.total_tokens,c:cv('--s1'),
     d:nf(M.naive_max.total_tokens),n:'теряет вторую цепочку'},
    {k:'сумма приростов',v:M.naive_delta.total_tokens,c:cv('--s3'),
     d:nf(M.naive_delta.total_tokens),n:'фабрикует инкремент при чередовании'},
  ],{ml:190,rh:46});
  const ic=RC.danat_internal_consistency;
  $('#tmeth').innerHTML='<tr><th>признак</th><th>мои 1050 файлов</th>'+
    '<th>второй комп, 786 файлов</th></tr>'+
    [['сессий',M.sessions,ic.sessions],
     ['сессий с несколькими цепочками',M.multi_chain_sessions,'—'],
     ['внеочередных событий / сбросов',M.out_of_order,ic.counter_resets],
     ['приросты минус максимум',M.naive_delta.total_tokens-M.naive_max.total_tokens,ic.delta_minus_max],
     ['то же на одно событие',ic.my_delta_minus_max_per_event,ic.delta_minus_max_per_reset],
     ['отношение приросты/максимум','1.00038',ic.ratio_delta_over_max],
     ['средний размер сессии',Math.round(M.chain_split.total_tokens/M.sessions),
      Math.round(ic.delta_minus_max/ic.counter_resets)&&Math.round(D.danat.totals_max.total_tokens/ic.sessions)]]
    .map(r=>'<tr><td>'+r[0]+'</td><td>'+(typeof r[1]==='number'?nf(r[1]):r[1])+
      '</td><td>'+(typeof r[2]==='number'?nf(r[2]):r[2])+'</td></tr>').join('');

  }  /* конец блока второй машины */

  /* double count */
  hbar($('#dbl'),[
    {k:'заголовок леджера',v:138912242896,c:cv('--crit'),d:'138.91 млрд',
     n:'сумма «финалов» — сессии посчитаны дважды'},
    {k:'сумма приростов',v:108312008697,c:cv('--s2'),d:'108.31 млрд',n:'из того же отчёта'},
    {k:'сумма трёх корней',v:108244387543,c:cv('--s1'),d:'108.24 млрд',
     n:'сходится с приростами — 0.06%'},
  ],{ml:190,rh:48});
  $('#tdbl').innerHTML='<tr><th>признак</th><th>значение</th><th>отношение</th></tr>'+
    [['sessions_with_usage',3635,''],['unique_session_or_path_keys',2830,'3635 / 2830 = 1.285'],
     ['заголовок / приросты','138.91 / 108.31','= 1.283']]
    .map(r=>'<tr><td>'+r[0]+'</td><td>'+(typeof r[1]==='number'?nf(r[1]):r[1])+
      '</td><td>'+r[2]+'</td></tr>').join('')+
    '<tr><td colspan=3 style="text-align:left;color:var(--ink2)">Два отношения совпали до третьего знака — это и есть доказательство двойного счёта, а не совпадение.</td></tr>';

  /* antigravity */
  $('#agv').textContent='Собственного учёта токенов у Antigravity на диске нет. '+
    'Токен-поля встречаются лишь в 13 из 687 транскриптов, и каждое из них — '+
    'захваченный вывод скрипта, который агент сам запускал, а не учёт генераций. '+
    'Gemini независимо это подтвердил на 505 транскриптах и дополнительно установил, '+
    'что поле size в gen_metadata — счётчик байтов protobuf (size == length(data)), '+
    'а ключ modelCredits пуст. Ниже — метрики объёма, не токены.';
  if(hasAG){
  legend($('#lg4'),['ходы модели'],[cv('--s2')]);
  stack($('#agts'),{labels:a.by_day.labels,model_turns:a.by_day.model_turns},
    ['model_turns'],['ходы модели'],[cv('--s2')],{h:220,fmtX:s=>s.slice(5)});
  const ty=Object.entries(a.types).slice(0,8);
  hbar($('#agt'),ty.map(([k,v],i)=>({k:k,v:v,c:cv('--s'+((i%5)+1)),d:nf(v)})),
    {ml:190,rh:30});
  const q=Object.entries(a.quota_by_day);
  hbar($('#agq'),q.sort((p,r)=>r[1]-p[1]).slice(0,8)
    .map(([k,v])=>({k:k,v:v,c:cv('--crit'),d:nf(v)})),{ml:110,rh:30});
  }  /* конец панелей Antigravity */

  /* cache effectiveness by day */
  {const bd=c.by_day, L=bd.labels;
   const pct=L.map((_,i)=>{const ctx=bd.inp[i]+bd.cc[i]+bd.cr[i];
     return ctx?100*bd.cr[i]/ctx:0;});
   hbar($('#cache'),L.map((d,i)=>({k:d.slice(5),v:pct[i],
     c:pct[i]>=85?cv('--good'):pct[i]>=50?cv('--warn'):cv('--crit'),
     d:pct[i].toFixed(1)+'%',
     n:big(bd.inp[i]+bd.cc[i]+bd.cr[i]+bd.out[i])+' токенов',
     rows:[['кэш-чтение',nf(bd.cr[i]),cv('--s2')],['свежий ввод',nf(bd.inp[i]),cv('--s1')],
           ['запись кэша',nf(bd.cc[i]),cv('--s3')],['вывод',nf(bd.out[i]),cv('--s5')]]})),
     {ml:70,rh:26});}
  $('#tper').innerHTML='<tr><th>период</th><th>токенов</th><th>кэш</th><th>$ факт</th>'+
    '<th>$ при 99% кэша</th><th>переплата</th></tr>'+
    [['8–13.07 роутер с gpt-5.5',772299132,'94.9%',690,549,141],
     ['14–25.07 кэш деградировал',367641808,'41.6%',1241,279,962],
     ['26.07 новые модели + CLI 2.1.219',147354143,'58.7%',401,125,276],
     ['27.07 proxy.js на api.anthropic.com',3667715343,'97.8%',2584,2282,303]]
    .map(r=>'<tr><td style="text-align:left">'+r[0]+'</td><td>'+nf(r[1])+'</td><td>'+r[2]+
      '</td><td>'+usd(r[3])+'</td><td>'+usd(r[4])+'</td><td><b>'+usd(r[5])+'</b></td></tr>').join('')+
    '<tr><td><b>итого</b></td><td></td><td></td><td><b>'+usd(4916)+'</b></td><td><b>'+
    usd(3234)+'</b></td><td><b>'+usd(1682)+'</b> = 34%</td></tr>';

  /* deep: per-model */
  const DP=D.deep, DC=DP.cost.claude_cost_by_model;
  $('#dpcap').textContent=nf(DP.responses)+' дедуплицированных ответов. «Контекст/вызов» — '+
    'сколько токенов отправляется в модель за один вызов; «кэш-поп.» — какая доля этого '+
    'взята из кэша; «конт:вывод» — сколько токенов контекста на один токен вывода.';
  $('#tdeep').innerHTML='<tr><th>модель</th><th>ответов</th><th>сессий</th><th>токенов</th>'+
    '<th>кэш-поп.</th><th>свежий ввод медиана</th><th>контекст медиана</th>'+
    '<th>конт:вывод</th><th>$ свежий</th><th>$ итого</th></tr>'+
    Object.entries(DP.by_model).filter(([m,v])=>v.total>0).map(([m,v])=>{
      const c=DC[m];
      return '<tr><td>'+m+'</td><td>'+nf(v.responses)+'</td><td>'+v.sessions+'</td><td>'+
      nf(v.total)+'</td><td>'+v.cache_hit_rate_pct.toFixed(2)+'%</td><td>'+
      nf(v.uncached_per_call.median||0)+'</td><td>'+nf(v.context_per_call.median||0)+'</td><td>'+
      v.context_to_output_ratio+'</td><td>'+(c?usd(c.unc):'—')+'</td><td><b>'+
      (c?usd(c.total):'—')+'</b></td></tr>';}).join('')+
    '<tr><td><b>итого</b></td><td>'+nf(DP.responses)+'</td><td>148</td><td><b>'+
    nf(DP.cost.claude_total_tokens)+'</b></td><td colspan=5></td><td><b>'+
    usd(DP.cost.claude_total_usd)+'</b></td></tr>';

  /* growth */
  hbar($('#growth'),Object.entries(DP.growth).map(([k,v])=>({
    k:k.replace('calls_','вызовы ').replace('_plus','+').replace(/_/g,'–'),
    v:v.mean_context,c:cv('--s1'),d:nf(v.mean_context),
    n:'n='+v.responses+' · медиана '+nf(v.median_context)})),{ml:150,rh:36});

  /* concentration */
  hbar($('#conc'),Object.entries(DP.concentration).map(([k,v])=>({
    k:'топ-'+k.match(/\d+/)[0]+' сессий',v:v,c:cv('--s5'),d:v.toFixed(2)+'%'})),
    {ml:130,rh:32});

  /* На что ушло. Раньше здесь были пятнадцать строк с рукописными названиями
     сессий и долями: названия брались из сводок, которых в артефактах нет, а
     доли разошлись -- первая была 8.6% против фактических 11.5%. Теперь таблица
     считается из top_sessions. Названий там нет, поэтому вместо выдуманных
     показываются настоящие признаки: дата, модель, длительность и доля
     субагентов. Придуманное имя хуже отсутствующего. */
  {const TS=D.top_sessions||[], tsum=TS.reduce((a,b)=>a+b.total,0)||1;
   $('#twent').innerHTML='<tr><th>доля</th><th>токенов</th><th>сессия</th>'+
     '<th>начало</th><th>ч</th><th>субагенты</th></tr>'+
     TS.map(r=>'<tr><td>'+(100*r.total/tsum).toFixed(1)+'%</td><td>'+nf(r.total)+
       '</td><td style="text-align:left"><code>'+String(r.id).slice(0,8)+
       '</code> '+(r.model||'')+'</td><td>'+(r.start||'').slice(5,16).replace('T',' ')+
       '</td><td>'+r.hours.toFixed(1)+'</td><td>'+nf(r.sub)+'</td></tr>').join('');
   const BC=D.by_cwd||[], bsum=BC.reduce((a,b)=>a+b.total,0)||1;
   hbar($('#wproj'),BC.map((r,i)=>({k:r.name,v:r.total,c:cv('--s'+((i%5)+1)),
     d:(100*r.total/bsum).toFixed(1)+'%',
     n:nf(r.total)+' токенов · '+nf(r.responses)+' ответов'})),{ml:210,rh:34});}

  /* google public stats */
  $('#tgo').innerHTML='<tr><th>метрика</th><th>значение</th><th>источник</th></tr>'+
    [['Antigravity, недельные активные пользователи','2 400 000','Alphabet Q2 2026, Пичаи, 22.07.2026'],
     ['разработчиков в месяц на моделях Google','9 000 000+','тот же звонок'],
     ['Gemini App, MAU','950 000 000','тот же звонок'],
     ['Fortune 100 на Gemini Enterprise','~90%','тот же звонок'],
     ['демо I/O 2026: сборка ОС с нуля','2 600 000 000 токенов, 15 000 запросов, 93 субагента, 12 ч, &lt;$1000','кейноут I/O 2026'],
     ['<b>эта машина, 27 июля</b>','<b>3 042 436 363 токена</b> — больше демо Google','измерено локально'],
     ['<b>ходов модели в Antigravity здесь</b>','<b>253 668</b> против 15 000 в демо','измерено локально'],
     ['что Google НЕ публикует','установки, MAU, платные/бесплатные, места, география','—']]
    .map(r=>'<tr><td style="text-align:left">'+r[0]+'</td><td>'+r[1]+
      '</td><td style="color:var(--ink2)">'+r[2]+'</td></tr>').join('');

  /* external */
  const ALL=DP.cost.all_time_measurable_tokens;
  hbar($('#ext'),[
    {k:'Google, все поверхности',v:100,c:cv('--s1'),d:'100 секунд',
     n:'3.2 квадриллиона токенов/месяц (I/O 2026)'},
    {k:'Google, только model API',v:390,c:cv('--s2'),d:'6.5 минут',
     n:'19 млрд токенов/минуту'},
    {k:'OpenAI, платформа',v:1242,c:cv('--s3'),d:'20.7 минут',
     n:'6 млрд токенов/минуту'},
    {k:'OpenRouter',v:3240,c:cv('--s5'),d:'54 минуты',n:'~100 трлн/месяц'},
  ],{ml:210,rh:44});
  $('#text').innerHTML='<tr><th>ориентир</th><th>значение</th><th>твой множитель</th></tr>'+
    [['Claude Code: $13 на разработчика в активный день (данные Anthropic)','$243,69/сутки','18.7×'],
     ['Claude Code: 90% пользователей <$30 в активный день','','8.1× порога'],
     ['Claude Code: типичный месяц $150–250','~$7 311/мес','29–49×'],
     ['Claude Code: «тяжёлая автоматизация» $500–2000/мес','','3.7–14.6×'],
     ['Codex: тяжёлый пользователь $100–200/мес','~$36 870/мес','184–369×'],
     ['Google Cloud: 375 клиентов прошли 1 трлн за 12 мес','124 млрд за ~4 мес','~37% рубежа в год'],
     ['активное время работы','92.3 ч за 20 дней','53 073 430 токенов/активный час'],
     ['в книгах по 80 тыс. слов','~1 163 000 книг','707 лет чтения на 250 сл/мин'],
     ['<b>из них уникального текста</b>','5 613 816 716 токенов','<b>4.53%</b> — ~52 600 книг'],
     ['то есть','не миллион разных книг','несколько книг, перечитанных миллион раз']]
    .map(r=>'<tr><td style="text-align:left">'+r[0]+'</td><td>'+r[1]+'</td><td><b>'+r[2]+
      '</b></td></tr>').join('');

  /* cost */
  const costRows=[];
  if(hasCX) costRows.push({k:'Codex (по прайсу)',v:x.cost_total,c:cv('--s1'),
    d:usd(x.cost_total),n:'подписка free/team — вероятно НЕ выставлялось'});
  costRows.push({k:'Claude Code (по прайсу)',v:c.cost_total,c:cv('--s3'),
    d:usd(c.cost_total),n:'нижняя граница: повторы прокси не учтены'});
  hbar($('#cost'),costRows,{ml:200,rh:50});
  $('#tcost').innerHTML='<tr><th>модель</th><th>тариф ввод $/млн</th>'+
    '<th>тариф вывод $/млн</th><th>$ итого</th></tr>'+
    Object.entries(c.cost).sort((p,q)=>q[1].total_usd-p[1].total_usd).map(([m,v])=>
      '<tr><td>'+m+'</td><td>'+v.rate_in_per_mtok+'</td><td>'+v.rate_out_per_mtok+
      '</td><td>'+usd(v.total_usd)+'</td></tr>').join('')+
    (hasCX?Object.entries(x.cost).sort((p,q)=>q[1].total_usd-p[1].total_usd).map(([m,v])=>
      '<tr><td>'+m+' <span class="badge b-est">ОЦЕНКА</span></td><td>'+v.rate_in_per_mtok+
      '</td><td>'+v.rate_out_per_mtok+'</td><td>'+usd(v.total_usd)+'</td></tr>').join(''):'');

  /* access paths & accounts */
  $('#tacc').innerHTML='<tr><th>провайдер</th><th>путь доступа</th><th>подтверждение в данных</th></tr>'+
    [['Codex','бесплатный и командный тариф',
      'plan_type free — 487 022 события, team — 6 321; на втором компе free 130 088, team 0'],
     ['Claude, июнь','китайский сервис (Opus 4.8/4.6)',
      'транскриптов нет: firstStartTime 2026-06-05 против первого лога 2026-07-08 — ~месяц не залогирован'],
     ['Claude, 08–25.07','AgentRouter',
      'gpt-5.5 внутри транскриптов Claude Code; реферальная ссылка в README GitHub — «Opus 4.8 или GPT-5.5»'],
     ['Claude, 26.07','Claude Max 20x','впервые opus-5, fable-5, sonnet-5 и версия CLI 2.1.219'],
     ['Claude, 26–27.07','свой ключ через proxy.js',
      'PROXY_PORT 8318, UPSTREAM api.anthropic.com; ключ ОДИН, пула нет'],
     ['Antigravity','19 Google-аккаунтов под менеджером',
      '16 active + 1 используемый + 2 expired; quota_json/device_profile/device_history 19 из 19; создано с 07.07']]
    .map(r=>'<tr><td style="text-align:left"><b>'+r[0]+'</b></td><td style="text-align:left">'+
      r[1]+'</td><td style="text-align:left;color:var(--ink2)">'+r[2]+'</td></tr>').join('');

  /* gaps */
  hbar($('#gaps'),[
    {k:'Codex: второй компьютер',v:50387894530,c:cv('--crit'),d:'50.4 млрд',
     n:'C:\\Users\\danat\\.codex\\sessions — 1891 файл, нет доступа'},
    {k:'Codex: удалённые сессии',v:1796,c:cv('--serious'),d:'1796 сессий',
     n:'известны индексу, роллаутов на диске нет'},
    {k:'Codex: после 2026-06-06',v:0,c:cv('--warn'),d:'не измерено',
     n:'последний отчёт датирован 6 июня'},
    {k:'Antigravity: все токены',v:0,c:cv('--crit'),d:'счётчика нет',
     n:'~/.gemini/tmp пуст; на втором компе может быть не пуст'},
    {k:'Antigravity: до 2026-06-05',v:0,c:cv('--warn'),d:'нет транскриптов',
     n:'установка от 2026-03-01, но логи начинаются с июня'},
    {k:'Claude Code: до 2026-07-08',v:0,c:cv('--warn'),d:'нет логов',
     n:'firstStartTime 2026-06-05, транскрипты только с 8 июля'},
  ],{ml:230,rh:46});
}

/* mode toggle: dark steps are selected, not an auto-flip */
const mq=matchMedia('(prefers-color-scheme: dark)');
if(mq.matches) document.body.classList.add('dark');
$('#mode').onclick=()=>{document.body.classList.toggle('dark');render();};
render();
</script>
"""

def check_js_syntax(js):
    """Синтаксическая проверка встроенного JavaScript чужим парсером.

    ЗАЧЕМ. Этот класс поломки уже проходил незамеченным трижды. Столкновение
    имени на верхнем уровне даёт SyntaxError, из-за чего НЕ рисуется НИ ОДНА
    панель, а снаружи это выглядит как пустой, но исправный дашборд: Chrome при
    --dump-dom в stderr не пишет ничего. Первый раз это был `const R`, второй --
    обращение к `DP` до объявления, третий -- `const DC` при уже объявленном в
    списке через запятую `const DP=D.deep, DC=...`, из-за чего поиск по строке
    `const DC=` его не находил.

    Своя проверка регулярками не годится: одно имя в РАЗНЫХ функциях на той же
    глубине скобок -- законно, а области видимости регуляркой не разобрать.
    Проверено: самодельный вариант дал пять ложных срабатываний на исправном
    файле. Поэтому здесь настоящий парсер, node --check.

    Нет node -- проверка честно говорит, что не выполнена, и НЕ выдаёт
    отсутствие ошибок за их отсутствие. Останется проверка рендера в refresh.py,
    которая ловит то же самое через перехватчик window.onerror.
    -> (ok, сообщение)
    """
    exe = shutil.which("node")
    if not exe:
        return True, "node не найден -- синтаксис JavaScript не проверен"
    tmp = os.path.join(tempfile.gettempdir(), "_ta_dash_check.js")
    try:
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(js)
        r = subprocess.run([exe, "--check", tmp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return True, "синтаксис JavaScript: ок (node --check)"
        return False, ((r.stderr or "") + (r.stdout or "")).strip()[:1200]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

dst = os.path.join(HERE, "dashboard.html")
html = HTML.replace("__DATA__", json.dumps(payload, ensure_ascii=False,
                                           separators=(",", ":")))
# Проверяем ДО записи: сломанный файл не должен попадать ни на диск, ни в
# публикацию. Проверяется КАЖДЫЙ встроенный скрипт по отдельности.
for _n, _js in enumerate(re.findall(r"<script[^>]*>(.*?)</script>", html, re.S), 1):
    _ok, _msg = check_js_syntax(_js)
    if not _ok:
        raise SystemExit("скрипт %d не проходит разбор -- файл не записан:" % _n
                         + chr(10) + _msg)
    if _n == 1:
        print(" ", _msg)

with open(dst, "w", encoding="utf-8") as fh:
    fh.write(html)
print("wrote", dst, "%.2f MB" % (os.path.getsize(dst) / 1e6))

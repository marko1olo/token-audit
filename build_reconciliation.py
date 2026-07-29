#!/usr/bin/env python3
"""Собирает reconciliation.json: как складывается составной итог по Codex.

    python build_reconciliation.py

ЗАЧЕМ ЭТО СУЩЕСТВУЕТ

reconciliation.json читали report_gen.py и build_dashboard.py, а писал его НИКТО:
файл был рукописным и лежал в репозитории константой. Через него заголовочная
цифра Codex попадала в первую таблицу README у каждого, кто клонировал
репозиторий, оформленная так же, как измеренная рядом цифра Claude Code. Это
ровно тот дефект, против которого весь инструмент: число, которое ничто не
пересчитывает.

Теперь итог складывается из двух источников, и они не смешиваются:
  * измеренное локально -- chain-split из codex_chains_totals.json. Меняется
    при каждом новом скане, потому что берётся из артефакта, а не из литерала.
  * измеренное не здесь -- из external_measurements.json, где у каждой цифры
    записаны источник, дата и машина. Файла нет -> итог не собирается, и в
    отчётах видно, что внешних измерений не хватает, вместо тихой константы.

ДВА БАЗИСА, И ПОЧЕМУ ИХ ДВА

Консервативный итог -- всё по методу максимума и chain-split. Верхняя граница --
то же, но июньский хвост danat взят методом приростов, как он был посчитан в
прежнем аудите. Разница между базисами на тех же данных 4.1875 раза, и метод
приростов почти наверняка завышает: при сопоставимом числе сбросов счётчика
расхождение на сброс отличается в 2145 раз. Поэтому заголовочной цифрой идёт
консервативная, а верхняя граница печатается рядом как граница, а не как оценка.
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "reconciliation.json")
EXTERNAL = "external_measurements.json"
CHAINS = "codex_chains_totals.json"

# Слагаемые внешней части. Порядок -- порядок вывода в отчёте.
FOREIGN = (
    ("codex_danat_apr03_jun06_old_audit", "danat_apr03_jun06_old_audit"),
    ("codex_danat_archived", "danat_archived"),
)
TAIL_MAX = "codex_danat_after_jun06_max_basis"
TAIL_DELTA = "codex_danat_after_jun06_delta_basis"


def load(name, required=False):
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        if required:
            raise SystemExit("нет %s -- сделать: python refresh.py --codex" % name)
        return None
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def external(data):
    """Проверенные записи внешних измерений. Без происхождения -- не берём.

    Требуются все четыре поля. Запись с числом, но без источника снова
    превращается в литерал без владельца, а именно от этого файл и заведён.
    -> dict[str, dict]
    """
    out = {}
    for key, rec in (data or {}).items():
        if key.startswith("_"):
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("value"), (int, float)):
            sys.stderr.write("%s: запись %r без числового value -- пропущена\n"
                             % (EXTERNAL, key))
            continue
        miss = [f for f in ("source", "measured_at", "machine") if not rec.get(f)]
        if miss:
            sys.stderr.write("%s: у записи %r нет полей %s -- цифра без "
                             "происхождения, пропущена\n" % (EXTERNAL, key, ", ".join(miss)))
            continue
        out[key] = rec
    return out


def f(n):
    return "{:,}".format(int(n)).replace(",", " ")


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    ch = load(CHAINS, required=True)
    ext = external(load(EXTERNAL))
    mine = ch["totals_chain_split"]["total_tokens"]

    components = [{
        "name": "backup_apr03_may21_mine_chainsplit",
        "total_tokens": mine,
        "class": "ИЗМЕРЕНО",
        "source": "codex_chains_totals.json, totals_chain_split",
        "note": "Пересчитывается локально методом chain-split при каждом скане.",
    }]
    missing = []
    for key, name in FOREIGN:
        rec = ext.get(key)
        if not rec:
            missing.append(key)
            continue
        components.append({
            "name": name, "total_tokens": int(rec["value"]),
            "class": rec.get("evidence") or "ПО ОТЧЁТУ",
            "source": "%s (%s, %s)" % (rec["source"], rec["machine"], rec["measured_at"]),
        })
    tail_max, tail_delta = ext.get(TAIL_MAX), ext.get(TAIL_DELTA)
    if tail_max:
        components.append({
            "name": "danat_after_jun06_rescaled",
            "total_tokens": int(tail_max["value"]),
            "class": tail_max.get("evidence") or "ОЦЕНКА",
            "source": "%s (%s, %s)" % (tail_max["source"], tail_max["machine"],
                                       tail_max["measured_at"]),
        })
    else:
        missing.append(TAIL_MAX)

    out = {
        "generated_by": "build_reconciliation.py",
        "method_note": (
            "Составной итог: локально измеренное chain-split плюс внешние измерения "
            "с указанным происхождением. Классы доказательности не смешиваются в одну "
            "цифру намеренно -- смешивание главный способ получить убедительно "
            "выглядящий неверный итог."),
        "components": components,
        "external_measurements_present": bool(ext),
        "external_missing": sorted(set(missing)),
    }

    complete = not missing
    total = sum(c["total_tokens"] for c in components)
    if complete:
        out["consistent_total_max_basis"] = {
            "total_tokens": total,
            "note": "Согласованный базис: все слагаемые по методу максимума или chain-split.",
        }
        if tail_delta:
            out["upper_bound_if_delta_valid"] = (
                total - int(tail_max["value"]) + int(tail_delta["value"]))
            out["upper_bound_note"] = (
                "То же, но июньский хвост danat по методу приростов, как в прежнем "
                "аудите. Метод приростов на тех данных завышает: при сопоставимом "
                "числе сбросов счётчика расхождение на сброс отличается в 2145 раз. "
                "Поэтому это граница, а не оценка.")
    else:
        out["consistent_total_max_basis"] = {
            "total_tokens": None,
            "note": ("Итог НЕ СОБРАН: нет внешних измерений %s. Локально измеренная "
                     "часть -- %d токенов. Чужую цифру взамен не подставляем."
                     % (", ".join(sorted(set(missing))), mine)),
        }

    # Исторические цифры прежнего леджера: хранятся как факт, в итоги не входят.
    prior = {}
    for key, name in (("codex_prior_ledger_headline", "ledger_headline_inflated"),
                      ("codex_prior_deduped_no_june_tail", "prior_deduped_no_june_tail")):
        if key in ext:
            prior[name] = int(ext[key]["value"])
    if prior:
        out["prior_figures"] = prior

    # Блоки из прежнего файла, которые не являются итогами: переносятся как есть,
    # если старый файл ещё лежит рядом. Пересчитать их без сырых данных нельзя.
    old = load("reconciliation.json")
    for k in ("danat_internal_consistency", "danat_by_day_both_bases", "danat_by_cwd",
              "danat_resolution_buckets", "overlap_warning"):
        if old and k in old:
            out[k] = old[k]

    with io.open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)

    print("=" * 70)
    print("СОСТАВНОЙ ИТОГ ПО CODEX")
    print("=" * 70)
    for c in components:
        print("  %-34s %18s  %s" % (c["name"], f(c["total_tokens"]), c["class"]))
    if complete:
        print("  %-34s %18s" % ("консервативный итог", f(total)))
        if "upper_bound_if_delta_valid" in out:
            print("  %-34s %18s" % ("верхняя граница", f(out["upper_bound_if_delta_valid"])))
    else:
        print("  ИТОГ НЕ СОБРАН: нет внешних измерений %s"
              % ", ".join(sorted(set(missing))))
        print("  локально измерено: %s" % f(mine))
    print("wrote", os.path.basename(OUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

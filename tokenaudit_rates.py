#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Единственный источник истины по ценам и множителям кэша.

ЦЕНА, ВСТРЕЧЕННАЯ В ЛЮБОМ ДРУГОМ ФАЙЛЕ РЕПОЗИТОРИЯ, — ЭТО БАГ. Не «дубликат»,
не «локальная копия», а баг: аудит существует ровно потому, что число, лежащее
в двух местах, расходится. Оно уже разошлось. RATES/OPENAI были скопированы
четыре раза, и три копии знают только gpt-5.5, тогда как в данных Codex лежит
13 167 044 269 токенов gpt-5.4, 49 908 576 gpt-5.4-mini и 22 822 547
gpt-5.3-codex — то есть две копии оценили бы одни и те же данные по-разному.

СЕМЬ МЕСТ, КОТОРЫЕ ОБЯЗАНЫ ПЕРЕЙТИ НА ЭТОТ МОДУЛЬ:
    1. refresh.py:38-41            RATES / OPENAI / M
    2. refresh.py:72-79            множители 1.25 и 0.1 внутри cost_claude()
    3. report_gen.py:27-30         вторая копия RATES / OPENAI
    4. report_gen.py:79-80         те же множители в Ctx._cost()
    5. report_gen.py:181           дневная цена литералами 5 / 6.25 / 0.5 / 25
    6. report_blocks_ext.py:45-55  третья копия таблиц (RA / OA)
    7. report_blocks_ext.py:265,273-275,438  и build_dashboard.py:1085, где
       формула стоимости продублирована ещё и в ГЕНЕРИРУЕМОМ JavaScript
Для восьмого места, cost_model.py:29-53, этот модуль и есть вынесенная наружу
копия: там таблицы были самыми полными, оттуда и взяты значения. После миграции
cost_model.py импортирует их отсюда, а не держит своими.

ЦЕНЫ. Anthropic — за 1 млн токенов, сверено 2026-07-27 по
platform.claude.com/docs/en/about-claude/models/overview.md. Множители кэша из
документации по prompt caching: чтение 0.1x базовой цены ввода, запись 1.25x при
TTL 5 минут и 2x при TTL 1 час. OpenAI — по developers.openai.com/api/docs/pricing
в состоянии на 2026-06-06 (источник — аудит той даты); cached_in здесь
АБСОЛЮТНАЯ цена, а не множитель. Модели без опубликованной цены остаются
None: неоценённое должно остаться неоценённым, а не превратиться в догадку.

Всякий доллар в этом аудите — ЭКВИВАЛЕНТ ПО ПРАЙСУ, а не счёт: Codex шёл по
подписке (plan_type free/team), а трафик Claude Code шёл через локальный прокси,
который переписывал модель и повторял 429/5xx, — те повторы сожгли токены,
которых нет ни в одном транскрипте.
"""

MILLION = 1_000_000.0

CACHE_READ_MULT = 0.1        # чтение кэша = 0.1x базовой цены ввода
CACHE_WRITE_5M_MULT = 1.25   # запись кэша, TTL 5 минут = 1.25x базовой цены ввода
CACHE_WRITE_1H_MULT = 2.0    # запись кэша, TTL 1 час = 2x базовой цены ввода

# модель -> (ввод за 1 млн, вывод за 1 млн)
ANTHROPIC = {
    "claude-opus-5":   (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5":  (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
}

# модель -> (ввод, кэшированный ввод, вывод) за 1 млн; None — цены нет
OPENAI = {
    "gpt-5.5":            (5.0, 0.5, 30.0),
    "gpt-5.4":            (2.5, 0.25, 15.0),
    "gpt-5.4-mini":       (0.75, 0.075, 4.5),
    "gpt-5.3-codex":      (1.75, 0.175, 14.0),
    "gpt-5.2-codex":      None,   # опубликованной цены аудит не нашёл
    "gpt-5.2":            None,
    "gpt-5.1-codex-mini": None,
}

# оговорки к отдельным ставкам: печатать рядом с цифрой, а не терять
NOTES = {
    "claude-sonnet-5": "вводная цена; прайс $3/$15, вводные $2/$10 действуют "
                       "до 2026-08-31, а все наблюдения — июль 2026",
}

# Claude Code пишет служебные записи ассистента с этой «моделью» и нулевым
# расходом: она не оценивается и не считается неоценённой моделью.
SYNTHETIC = "<synthetic>"

# Дневные и суточные разрезы не знают модели: в claude_deep.by_day_full лежат
# только суммы полей. Историческая формула 5 / 6.25 / 0.5 / 25 — это ставки
# claude-opus-5, доминирующей модели. Имя вынесено, чтобы литералы не вернулись.
DEFAULT_MODEL = "claude-opus-5"

# короткие ключи claude_agg / claude_totals.json и длинные claude_deep.json
SHORT_KEYS = ("inp", "cc", "cr", "out", "e5m", "e1h")
LONG_KEYS = ("uncached_input", "cache_write", "cache_read", "output",
             "cache_write_5m", "cache_write_1h")


def is_priced(model):
    """Есть ли у модели опубликованная цена. -> bool"""
    if not model or model == SYNTHETIC:
        return False
    if model in ANTHROPIC:
        return True
    return bool(OPENAI.get(model))


def unpriced_models(models):
    """Модели без цены. Принимает список, множество или dict (по ключам).

    SYNTHETIC исключён: это не модель, а служебная запись с нулевым расходом.
    -> list[str], отсортированный
    """
    out = set()
    for m in (models or []):
        if not m or m == SYNTHETIC:
            continue
        if not is_priced(m):
            out.add(m)
    return sorted(out)


def anthropic_cost(uncached_input, cache_write, cache_read, output, model,
                   cache_write_1h=0):
    """Стоимость расхода Anthropic в долларах по прайсу.

    cache_write трактуется как запись с TTL 5 минут (1.25x). Если известно
    разделение по TTL, часовую часть надо передать в cache_write_1h (2x) —
    без этого часовая запись занижается ровно на 0.75 базовой цены ввода.
    Модель без цены -> None (не 0.0: ноль сложился бы в итог как «бесплатно»).
    -> float | None
    """
    rate = ANTHROPIC.get(model)
    if not rate:
        return None
    r_in, r_out = rate
    return (uncached_input / MILLION * r_in
            + cache_write / MILLION * r_in * CACHE_WRITE_5M_MULT
            + cache_write_1h / MILLION * r_in * CACHE_WRITE_1H_MULT
            + cache_read / MILLION * r_in * CACHE_READ_MULT
            + output / MILLION * r_out)


def openai_cost(uncached_input, cached_input, output, model):
    """Стоимость расхода OpenAI в долларах по прайсу.

    ВНИМАНИЕ на семантику: uncached_input должен быть УЖЕ БЕЗ cached_input.
    В отчётности OpenAI cached_input_tokens — подмножество input_tokens, поэтому
    для сырых счётчиков есть openai_cost_from_total(); передать сюда полный
    input_tokens значит посчитать кэшированный ввод дважды.
    Модель без цены -> None.
    -> float | None
    """
    rate = OPENAI.get(model)
    if not rate:
        return None
    r_in, r_cached, r_out = rate
    return (uncached_input / MILLION * r_in
            + cached_input / MILLION * r_cached
            + output / MILLION * r_out)


def openai_cost_from_total(input_tokens, cached_input_tokens, output, model):
    """То же, но для сырых счётчиков Codex, где cached — подмножество input.

    Вычитание делается здесь, ровно как в cost_model.openai_cost().
    -> float | None
    """
    uncached = max(0, (input_tokens or 0) - (cached_input_tokens or 0))
    return openai_cost(uncached, cached_input_tokens or 0, output, model)


def _fields(data):
    """Распознать стиль ключей и вернуть (inp, cw5m, cw1h, cr, out).

    Короткие ключи — claude_agg / claude_totals.json ('inp','cc','cr','out',
    плюс разбивка 'e5m'/'e1h'). Длинные — claude_deep.json ('uncached_input',
    'cache_write','cache_read','output', плюс 'cache_write_5m'/'cache_write_1h').
    При наличии обоих стилей выигрывает короткий.
    Правило разбивки по TTL повторяет cost_model.py: если сумма 5m+1h равна
    нулю, вся запись кэша считается пятиминутной.
    """
    if any(k in data for k in ("inp", "cc", "cr")):
        inp = data.get("inp") or 0
        cw = data.get("cc") or 0
        cr = data.get("cr") or 0
        out = data.get("out") or 0
        w5 = data.get("e5m") or 0
        w1 = data.get("e1h") or 0
    else:
        inp = data.get("uncached_input") or 0
        cw = data.get("cache_write") or 0
        cr = data.get("cache_read") or 0
        out = data.get("output") or 0
        w5 = data.get("cache_write_5m") or 0
        w1 = data.get("cache_write_1h") or 0
    if w5 + w1 == 0:
        w5 = cw
    return inp, w5, w1, cr, out


def cost_of(fields_dict, model):
    """Стоимость по словарю измеренных полей. Стиль ключей определяется сам.

    Понимает и короткие ключи claude_agg ('inp','cc','cr','out'), и длинные
    claude_deep ('uncached_input','cache_write','cache_read','output'), и
    разбивку записи кэша по TTL ('e5m'/'e1h' либо 'cache_write_5m'/'..._1h').
    Для моделей OpenAI, попавших в транскрипт Claude Code через локальный прокси,
    чтение кэша трактуется как кэшированный ввод — так же, как в cost_model.py.
    Модель без цены -> None.
    -> float | None
    """
    if not isinstance(fields_dict, dict):
        return None
    inp, w5, w1, cr, out = _fields(fields_dict)
    if model in ANTHROPIC:
        return anthropic_cost(inp, w5, cr, out, model, cache_write_1h=w1)
    if OPENAI.get(model):
        return openai_cost(inp, cr, out, model)
    return None


def cost_breakdown(fields_dict, model):
    """Разложение стоимости по типам токенов, а не одна сумма.

    Ключи ответа те же, что писал refresh.py в claude_cost_deep.json:
    'unc' свежий ввод, 'cw' запись кэша, 'cr' чтение кэша, 'out' вывод,
    'total' сумма. Модель без цены -> None.
    -> dict[str, float] | None
    """
    if not isinstance(fields_dict, dict):
        return None
    inp, w5, w1, cr, out = _fields(fields_dict)
    if model in ANTHROPIC:
        r_in, r_out = ANTHROPIC[model]
        parts = {"unc": inp / MILLION * r_in,
                 "cw": (w5 / MILLION * r_in * CACHE_WRITE_5M_MULT
                        + w1 / MILLION * r_in * CACHE_WRITE_1H_MULT),
                 "cr": cr / MILLION * r_in * CACHE_READ_MULT,
                 "out": out / MILLION * r_out}
    elif OPENAI.get(model):
        r_in, r_cached, r_out = OPENAI[model]
        parts = {"unc": inp / MILLION * r_in, "cw": 0.0,
                 "cr": cr / MILLION * r_cached,
                 "out": out / MILLION * r_out}
    else:
        return None
    parts["total"] = sum(parts.values())
    return parts


def rates_for_js():
    """Структура для встраивания в генерируемый JavaScript дашборда.

    Полностью JSON-сериализуема. Множители включены нарочно: JS обязан выводить
    цену записи кэша как in * cache_write_5m_mult, а не носить 6.25 отдельным
    магическим числом — иначе четвёртая копия формулы стоимости заведётся снова.
    Ключ default_model нужен суточным разрезам, где модели нет.
    Форма:
        {"million": 1000000.0,
         "cache_read_mult": 0.1, "cache_write_5m_mult": 1.25,
         "cache_write_1h_mult": 2.0, "default_model": "claude-opus-5",
         "anthropic": {"claude-opus-5": {"in": 5.0, "out": 25.0}, ...},
         "openai": {"gpt-5.5": {"in": 5.0, "cached_in": 0.5, "out": 30.0}, ...,
                    "gpt-5.2": None},
         "notes": {"claude-sonnet-5": "..."}}
    -> dict
    """
    anthropic = {}
    for model, (r_in, r_out) in ANTHROPIC.items():
        anthropic[model] = {"in": r_in, "out": r_out}
    openai = {}
    for model, rate in OPENAI.items():
        if rate is None:
            openai[model] = None
        else:
            openai[model] = {"in": rate[0], "cached_in": rate[1], "out": rate[2]}
    return {"million": MILLION,
            "cache_read_mult": CACHE_READ_MULT,
            "cache_write_5m_mult": CACHE_WRITE_5M_MULT,
            "cache_write_1h_mult": CACHE_WRITE_1H_MULT,
            "default_model": DEFAULT_MODEL,
            "anthropic": anthropic,
            "openai": openai,
            "notes": dict(NOTES)}


if __name__ == "__main__":
    import json
    import sys

    for name in ("stdout", "stderr"):
        rec = getattr(getattr(sys, name, None), "reconfigure", None)
        if callable(rec):
            try:
                rec(encoding="utf-8", errors="replace")
            except Exception:
                pass
    print("Anthropic, $ за 1 млн токенов (ввод / вывод):")
    for m, (a, b) in sorted(ANTHROPIC.items()):
        note = NOTES.get(m)
        print("  %-18s %6.2f / %6.2f%s" % (m, a, b, "   " + note if note else ""))
    print("множители кэша: чтение %.2fx, запись 5м %.2fx, запись 1ч %.2fx"
          % (CACHE_READ_MULT, CACHE_WRITE_5M_MULT, CACHE_WRITE_1H_MULT))
    print("OpenAI, $ за 1 млн (ввод / кэш. ввод / вывод):")
    for m in sorted(OPENAI):
        r = OPENAI[m]
        print("  %-18s %s" % (m, "цены нет" if r is None
                              else "%6.2f / %6.3f / %6.2f" % r))
    print("для JS:", json.dumps(rates_for_js(), ensure_ascii=False, sort_keys=True))

def day_cost(fields_dict, model=None):
    """Стоимость среза без разбивки по моделям (день, час, минута).

    ПРИБЛИЖЕНИЕ, и это надо знать: в таких срезах модель не сохранена, поэтому
    всё считается по ставке доминирующей модели (DEFAULT_MODEL). Для дней, где
    работала другая модель, цифра смещена. Функция существует, чтобы литерал
    "5 / 6.25 / 0.5 / 25" -- та же ставка, вписанная руками -- не жил в шести
    местах репозитория: пока он там жил, копии успели разойтись.

    Разбивки по TTL в таких срезах тоже нет, поэтому вся запись кэша идёт по
    пятиминутной ставке. Точная сумма считается только по by_model из
    claude_totals.json, где есть e5m/e1h.
    -> float
    """
    b = cost_breakdown(fields_dict, model or DEFAULT_MODEL)
    return b["total"] if b else 0.0

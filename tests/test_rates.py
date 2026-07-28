# -*- coding: utf-8 -*-
"""Ставки и множители кэша. Единственный источник, восемь копий были удалены.

Копий таблиц ставок в репозитории было восемь: шесть на уровне модулей, седьмая
локальными переменными внутри функции (её не видел даже разбор AST), восьмая --
таблица OpenAI внутри блока отчёта. Копии OPENAI уже успели разойтись: в одной
семь моделей, в остальных одна, при 13 млрд токенов gpt-5.4 в данных. Эти тесты
существуют, чтобы девятая копия не появилась незамеченной.
"""
import unittest

from common import ROOT  # noqa: F401  -- добавляет корень репозитория в sys.path
import tokenaudit_rates as R

MIL = 1000000


class TestRates(unittest.TestCase):
    def test_uncached_input_price(self):
        # Миллион свежего ввода opus-5 по 5 долларов за миллион = ровно 5.0.
        self.assertAlmostEqual(R.cost_of({"inp": MIL, "cc": 0, "cr": 0, "out": 0},
                                         "claude-opus-5"), 5.0, places=9)

    def test_cache_read_is_ten_percent(self):
        # Чтение кэша -- 0.1x базовой цены ввода: 5.0 * 0.1 = 0.5.
        self.assertAlmostEqual(R.cost_of({"inp": 0, "cc": 0, "cr": MIL, "out": 0},
                                         "claude-opus-5"), 0.5, places=9)
        self.assertEqual(R.CACHE_READ_MULT, 0.1)

    def test_cache_write_ttl_multipliers(self):
        # Пятиминутная запись 1.25x -> 6.25, часовая 2.0x -> 10.0.
        five = R.cost_of({"inp": 0, "cc": MIL, "cr": 0, "out": 0, "e5m": MIL, "e1h": 0},
                         "claude-opus-5")
        hour = R.cost_of({"inp": 0, "cc": MIL, "cr": 0, "out": 0, "e5m": 0, "e1h": MIL},
                         "claude-opus-5")
        self.assertAlmostEqual(five, 6.25, places=9)
        self.assertAlmostEqual(hour, 10.0, places=9)
        self.assertEqual(R.CACHE_WRITE_5M_MULT, 1.25)
        self.assertEqual(R.CACHE_WRITE_1H_MULT, 2.0)
        self.assertGreater(hour, five, "часовая запись обязана быть дороже пятиминутной")

    def test_cache_write_without_ttl_falls_back_to_five_minutes(self):
        # Срезы без разбивки по TTL считаются по пятиминутной ставке.
        v = R.cost_of({"inp": 0, "cc": MIL, "cr": 0, "out": 0}, "claude-opus-5")
        self.assertAlmostEqual(v, 6.25, places=9)

    def test_day_cost_reproduces_the_old_literal(self):
        # Литерал "5 / 6.25 / 0.5 / 25" жил в четырёх местах. Сумма для
        # миллиона каждого поля = 36.75, и это фиксируется здесь.
        v = R.day_cost({"inp": MIL, "cc": MIL, "cr": MIL, "out": MIL})
        self.assertAlmostEqual(v, 36.75, places=9)

    def test_output_price(self):
        self.assertAlmostEqual(R.cost_of({"inp": 0, "cc": 0, "cr": 0, "out": MIL},
                                         "claude-opus-5"), 25.0, places=9)

    def test_long_and_short_keys_agree(self):
        short = {"inp": MIL, "cc": MIL, "cr": MIL, "out": MIL}
        long_ = {"uncached_input": MIL, "cache_write": MIL,
                 "cache_read": MIL, "output": MIL}
        self.assertAlmostEqual(R.cost_of(short, "claude-opus-5"),
                               R.cost_of(long_, "claude-opus-5"), places=9)

    def test_gpt54_is_priced(self):
        # Две удалённые копии таблицы знали только gpt-5.5, при том что в данных
        # 13 167 044 269 токенов gpt-5.4. Модель обязана иметь цену.
        for m in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex"):
            self.assertTrue(R.is_priced(m), "%s должна иметь цену" % m)
        self.assertIsNotNone(R.openai_cost(MIL, 0, 0, "gpt-5.4"))
        self.assertAlmostEqual(R.openai_cost(MIL, 0, 0, "gpt-5.4"), 2.5, places=9)

    def test_unknown_model_is_unpriced_not_zero(self):
        # Модель без цены не должна молча стоить ноль: ноль в сумме выглядит как
        # измерение, а отсутствие цены -- это отсутствие цены.
        self.assertIsNone(R.cost_of({"inp": MIL, "cc": 0, "cr": 0, "out": 0},
                                    "no-such-model-9000"))
        self.assertFalse(R.is_priced("no-such-model-9000"))
        self.assertIn("no-such-model-9000", R.unpriced_models(["no-such-model-9000"]))

    def test_explicitly_unpriced_openai_models_stay_unpriced(self):
        # В каталоге часть моделей помечена как без публичного тарифа. Догадка
        # вместо цены недопустима.
        for m in ("gpt-5.2-codex", "gpt-5.2", "gpt-5.1-codex-mini"):
            self.assertFalse(R.is_priced(m), "%s не должна получать выдуманную цену" % m)

    def test_rates_for_js_carries_multipliers(self):
        # Дашборд рисует стоимость в JS. Он обязан получать множители, а не
        # хранить 6.25 отдельной магической константой: это была девятая копия.
        js = R.rates_for_js()
        self.assertIsInstance(js, dict)
        blob = repr(js)
        self.assertIn("0.1", blob)
        self.assertIn("1.25", blob)


if __name__ == "__main__":
    unittest.main()

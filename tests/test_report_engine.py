# -*- coding: utf-8 -*-
"""Движок AUTO-блоков: защита от усушки и полнота реестра.

Обе проверки существуют из-за уже случившихся аварий.

Усушка. Шаблон таблицы, скомпилированный с re.S, заставил точку совпадать с
переводом строки и сожрал документ целиком: SUMMARY.md уменьшился с 50 814 до
7 697 байт. Отдельно от этого блок, вернувший пустой список, молча стирал таблицу
из 59 строк, файл усыхал на 93.1%, а код возврата оставался нулевым. При этом
таблица описаний файлов в самом report_gen.py уже рекламировала «защиту от
усушки», которой в коде не было.

Реестр. report_gen.py может выполняться как __main__, и тогда обычный импорт из
модуля дополнений создал бы ВТОРУЮ копию модуля с пустым реестром, из-за чего все
блоки молча разрегистрировались бы.
"""
import glob
import io
import os
import re
import shutil
import unittest

from common import ROOT, tmpdir
import report_gen as rg


class Dummy(object):
    """Пустой контекст: тестируемым блокам данные не нужны."""


class TestShrinkGuard(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.table = "\n".join("| строка %d | %d |" % (i, i * 1000) for i in range(1, 60))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _doc(self, block_name, body):
        p = os.path.join(self.d, "probe.md")
        text = ("# документ\n\nпроза до блока.\n\n"
                "<!-- AUTO:%s -->\n%s\n<!-- /AUTO -->\n\nпроза после блока.\n"
                % (block_name, body))
        with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return p

    def test_empty_block_refuses_to_write(self):
        rg.BLOCKS["probe_empty"] = lambda c: []
        p = self._doc("probe_empty", self.table)
        before = io.open(p, encoding="utf-8").read()
        with self.assertRaises(rg.BlockError):
            rg.fill(p, Dummy())
        self.assertEqual(io.open(p, encoding="utf-8").read(), before,
                         "файл обязан остаться неизменным")

    def test_whitespace_only_block_also_refuses(self):
        rg.BLOCKS["probe_blank"] = lambda c: ["", "   ", "\t"]
        p = self._doc("probe_blank", self.table)
        with self.assertRaises(rg.BlockError):
            rg.fill(p, Dummy())

    def test_oversized_shrink_refuses_to_write(self):
        rg.BLOCKS["probe_tiny"] = lambda c: ["| одна строка |"]
        p = self._doc("probe_tiny", self.table)
        before = io.open(p, encoding="utf-8").read()
        with self.assertRaises(rg.BlockError) as cm:
            rg.fill(p, Dummy())
        self.assertIn("усушка", str(cm.exception))
        self.assertEqual(io.open(p, encoding="utf-8").read(), before)

    def test_normal_regeneration_still_writes(self):
        # Защита не должна мешать обычной работе: замена таблицы на таблицу
        # близкого размера проходит.
        same = "\n".join("| строка %d | %d |" % (i, i * 1001) for i in range(1, 60))
        rg.BLOCKS["probe_ok"] = lambda c: same.split("\n")
        p = self._doc("probe_ok", self.table)
        cnt, miss = rg.fill(p, Dummy())
        self.assertEqual(cnt, 1)
        self.assertEqual(miss, [])
        txt = io.open(p, encoding="utf-8").read()
        self.assertIn("строка 59 | 59059", txt)
        self.assertIn("проза до блока.", txt)
        self.assertIn("проза после блока.", txt)

    def test_prose_outside_markers_is_never_touched(self):
        # Ровно это сломал re.S: шаблон вышел за границы блока и съел прозу.
        rg.BLOCKS["probe_keep"] = lambda c: ["| a | b |", "|---|---|", "| 1 | 2 |"]
        body = self.table
        p = self._doc("probe_keep", body)
        try:
            rg.fill(p, Dummy())
        except rg.BlockError:
            pass       # усушка возможна, важна сохранность прозы
        txt = io.open(p, encoding="utf-8").read()
        self.assertIn("# документ", txt)
        self.assertIn("проза до блока.", txt)
        self.assertIn("проза после блока.", txt)

    def test_shrink_threshold_is_a_real_number(self):
        self.assertTrue(0 < rg.MAX_SHRINK < 1)


class TestRegistry(unittest.TestCase):
    def test_every_marker_in_shipped_docs_has_a_block(self):
        # Импорт модуля дополнений регистрирует остальные блоки.
        import report_blocks_ext  # noqa: F401
        markers = set()
        for path in glob.glob(os.path.join(ROOT, "*.md")):
            txt = io.open(path, encoding="utf-8").read()
            markers |= set(re.findall(r"<!-- AUTO:([a-z_]+) -->", txt))
        self.assertTrue(markers, "в документах не нашлось ни одного маркера AUTO")
        missing = sorted(m for m in markers if m not in rg.BLOCKS)
        self.assertEqual(missing, [], "маркеры без генератора: %s" % missing)

    def test_registry_is_not_empty(self):
        # Пустой реестр -- это симптом второй копии модуля при запуске как
        # __main__, и он проявляется как молча незаполненные блоки.
        import report_blocks_ext  # noqa: F401
        self.assertGreater(len(rg.BLOCKS), 20,
                           "реестр подозрительно мал: %d" % len(rg.BLOCKS))

    def test_no_regex_in_the_engine_lets_dot_cross_lines_unintentionally(self):
        # Аудит шаблонов: re.S допустим только там, где он нужен -- в самом
        # маркере AUTO. Табличные шаблоны с re.S -- это та самая авария.
        src = io.open(os.path.join(ROOT, "report_gen.py"), encoding="utf-8").read()
        for m in re.finditer(r"re\.compile\((.{0,120}?)\)", src, re.S):
            frag = m.group(1)
            if "re.S" in frag or "DOTALL" in frag:
                self.assertIn("AUTO", frag,
                              "re.S вне маркера AUTO: %s" % frag.strip()[:80])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Дедупликация снимков потоковой записи. Инвариант номер один.

Claude Code пишет ОДИН ответ несколькими записями с одинаковым message.id: по
одной на блок содержимого плюс промежуточные снимки, где output_tokens растёт от
1 до финального. Наивная сумма таких записей завышает итог -- на живых данных в
2.16 раза. Правильный представитель -- запись с МАКСИМАЛЬНЫМ итогом, то есть
последний полный снимок.
"""
import os
import shutil
import unittest

from common import assistant_record, claude_root, tmpdir, write_jsonl


def measure(root):
    """Прочитать корень и вернуть (сырые записи, уникальные, суммы уникальных)."""
    import claude_agg
    import tokenaudit_scan
    w = tokenaudit_scan.ScanWindow.capture([root])
    rows, _meta = claude_agg.collect(w)
    uniq, _dupes, _noid = claude_agg.dedupe(rows)
    return rows, uniq, claude_agg.tot(uniq)


def total(t):
    return t["inp"] + t["cc"] + t["cr"] + t["out"]


class TestDedupe(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_streaming_snapshots_collapse_to_maximum(self):
        # Один ответ, три снимка. Ввод постоянен, вывод растёт 1 -> 50 -> 200.
        # Верный итог  = 100 + 1000 + 9000 + 200 = 10 300.
        # Наивная сумма = 10 101 + 10 150 + 10 300 = 30 551, в 2.97 раза больше.
        recs = [assistant_record("msg_A", inp=100, cc=1000, cr=9000, out=1),
                assistant_record("msg_A", inp=100, cc=1000, cr=9000, out=50),
                assistant_record("msg_A", inp=100, cc=1000, cr=9000, out=200)]
        rows, uniq, t = measure(claude_root(self.d, records=recs))
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(uniq), 1, "три снимка одного id -- одна запись")
        self.assertEqual(total(t), 10300, "остаётся максимальный снимок")
        self.assertEqual(t["out"], 200, "вывод максимальный, а не первый")

        import claude_agg
        raw = total(claude_agg.tot(rows))
        self.assertEqual(raw, 30551)
        # Страховка от вырождения фикстуры: если суммы совпали, тест перестал
        # что-либо проверять и должен об этом сказать.
        self.assertNotEqual(raw, total(t))

    def test_distinct_ids_are_kept(self):
        # Два разных ответа не склеиваются: 300 + 300 = 600.
        recs = [assistant_record("msg_A", inp=100, cc=100, cr=100),
                assistant_record("msg_B", inp=100, cc=100, cr=100)]
        rows, uniq, t = measure(claude_root(self.d, records=recs))
        self.assertEqual(len(uniq), 2)
        self.assertEqual(total(t), 600)

    def test_same_id_in_two_files_counted_once(self):
        # Тот же message.id в двух файлах -- это копирование при --resume или
        # компактификации, а не два ответа. Итог 300, не 600.
        rec = assistant_record("msg_A", inp=100, cc=100, cr=100)
        os.makedirs(os.path.join(self.d, "proj"), exist_ok=True)
        write_jsonl(os.path.join(self.d, "proj", "a.jsonl"), [rec])
        write_jsonl(os.path.join(self.d, "proj", "b.jsonl"), [rec])
        rows, uniq, t = measure(self.d)
        self.assertEqual(len(rows), 2, "прочитаны обе записи")
        self.assertEqual(len(uniq), 1, "одинаковый id в двух файлах -- один ответ")
        self.assertEqual(total(t), 300)

    def test_ttl_split_is_preserved(self):
        # Разбивка записи кэша по TTL обязана дойти до итогов: часовая запись
        # стоит 2x базовой цены ввода против 1.25x пятиминутной, и пока полей
        # не было в одном из артефактов, две части инструмента печатали разные
        # суммы на одной странице.
        recs = [assistant_record("msg_A", cc=1000, e5m=700, e1h=300)]
        _rows, _uniq, t = measure(claude_root(self.d, records=recs))
        self.assertEqual(t["cc"], 1000)
        self.assertEqual(t["e5m"], 700)
        self.assertEqual(t["e1h"], 300)
        self.assertEqual(t["e5m"] + t["e1h"], t["cc"],
                         "сумма по TTL обязана совпадать с общей записью кэша")


if __name__ == "__main__":
    unittest.main()

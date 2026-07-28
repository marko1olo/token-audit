# -*- coding: utf-8 -*-
"""Общее окно сканирования. Оно существует из-за реального расхождения.

Транскрипты дописываются, пока их читают -- в том числе той самой сессией, чей
расход считается. Два прохода подряд видели разный набор данных и расходились на
138 ответов и 18 615 694 токена, целиком в модели работающей сессии. Итог брался
из одного артефакта, таблица по моделям и деньги -- из другого, поэтому доли
суммировались в 100.19%, а дашборд показывал две разные суммы на одной странице.

Временная граница проблему только уменьшала: первый проход сам сканирует около
86 секунд, и файл, прочитанный в начале, теряет записи, дописанные к его концу.
Поэтому окно задаётся в БАЙТАХ.
"""
import io
import os
import shutil
import unittest

from common import tmpdir
import tokenaudit_scan as S


class TestScanWindow(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _file(self, name, text):
        p = os.path.join(self.d, name)
        with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return p

    def test_growing_file_contributes_only_captured_prefix(self):
        # Файл на три строки, окно снято. Потом дописали ещё две.
        # Второй проход обязан увидеть ровно три.
        p = self._file("a.jsonl", "one\ntwo\nthree\n")
        w = S.ScanWindow.capture([self.d])
        with io.open(p, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("four\nfive\n")
        got = [ln.strip() for ln in w.lines(p, "a.jsonl", self.d)]
        self.assertEqual(got, ["one", "two", "three"],
                         "дописанное после снятия окна не должно попадать в проход")

    def test_partial_trailing_line_is_dropped(self):
        # Граница режет по байтам, поэтому последняя строка может оказаться
        # неполной. Половина записи не разберётся как JSON, значит её надо
        # выбросить, а не пытаться читать.
        p = self._file("b.jsonl", "aaaa\nbbbb\n")
        w = S.ScanWindow.capture([self.d])
        # Урезаем окно так, чтобы оно закончилось посередине второй строки.
        key = S.ScanWindow.key(self.d, "b.jsonl")
        w.sizes[key] = 7           # "aaaa\n" = 5 байт, плюс "bb"
        got = [ln.strip() for ln in w.lines(p, "b.jsonl", self.d)]
        self.assertEqual(got, ["aaaa"], "неполная хвостовая строка отбрасывается")

    def test_byte_boundary_not_character_boundary(self):
        # В транскриптах кириллица: в utf-8 символ занимает два байта, поэтому
        # граница обязана считаться в байтах, иначе окно поедет.
        p = self._file("c.jsonl", "ключ\nзначение\n")
        size = os.path.getsize(p)
        self.assertEqual(size, len("ключ\nзначение\n".encode("utf-8")))
        w = S.ScanWindow.capture([self.d])
        self.assertEqual(w.sizes[S.ScanWindow.key(self.d, "c.jsonl")], size)
        got = [ln.strip() for ln in w.lines(p, "c.jsonl", self.d)]
        self.assertEqual(got, ["ключ", "значение"])

    def test_manifest_roundtrip_gives_identical_window(self):
        # Первый проход сохраняет манифест, второй загружает и обязан получить
        # тот же набор файлов и те же границы.
        self._file("d.jsonl", "x\ny\n")
        self._file("e.jsonl", "z\n")
        w = S.ScanWindow.capture([self.d], captured_by="test")
        path = os.path.join(self.d, "manifest.json")
        w.save(path)
        w2 = S.ScanWindow.load(path)
        self.assertIsNotNone(w2)
        self.assertEqual(w.sizes, w2.sizes)
        self.assertEqual(w2.captured_by, "test")
        self.assertEqual(len(w2.files()), 2)

    def test_missing_manifest_returns_none(self):
        # Отсутствие манифеста -- это не ошибка: самостоятельный прогон меряет
        # всё, что есть, и обязан честно об этом сообщить, а не притворяться.
        self.assertIsNone(S.ScanWindow.load(os.path.join(self.d, "nope.json")))

    def test_shrunk_file_is_reported(self):
        # Файл стал короче манифеста -- это надо заметить, а не молча прочитать
        # меньше, чем обещало окно.
        p = self._file("f.jsonl", "aaaa\nbbbb\ncccc\n")
        w = S.ScanWindow.capture([self.d])
        with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("aaaa\n")
        list(w.lines(p, "f.jsonl", self.d))
        self.assertTrue(w.shrunk, "усохший файл обязан попасть в список shrunk")
        self.assertIn("стало короче манифеста", w.describe())

    def test_read_lines_upto_none_reads_whole_file(self):
        p = self._file("g.jsonl", "1\n2\n3\n")
        self.assertEqual([x.strip() for x in S.read_lines_upto(p, None)], ["1", "2", "3"])

    def test_skip_dirs_excludes_the_tool_itself(self):
        # Репозиторий лежит внутри ~/.claude/projects, поэтому без исключения
        # своего каталога инструмент считает собственные выходные файлы.
        sub = os.path.join(self.d, "skipme")
        os.makedirs(sub, exist_ok=True)
        with io.open(os.path.join(sub, "h.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("q\n")
        self._file("keep.jsonl", "w\n")
        w = S.ScanWindow.capture([self.d], skip_dirs=(sub,))
        names = sorted(rel for _p, rel, _r in w.files())
        self.assertEqual(names, ["keep.jsonl"])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Извлечение текста из DOM и разрешение корней. Оба -- источники ложных выводов.

Извлечение текста. Шесть ложных отрицательных за одну работу пришли из одного
класса: ripgrep без --hidden не заходил в каталоги с точкой, у формата не было
поля аккаунта, cp1251 портил пробу, NBSP не равен пробелу, `&nbsp;` не равен
пробелу и, последнее, числовые ссылки на тот же символ (&#160;, &#xa0;, &#8239;)
проходили мимо рукописного списка замен. Отсюда правило: прежде чем писать «этого
нет», доказать контрольным случаем, что инструмент способен это увидеть.

Корни. Отсутствующий корень раньше давал нули и код возврата ноль, то есть
опубликованное измерение из ничего.
"""
import os
import shutil
import unittest

from common import tmpdir
import tokenaudit_config as cfg


class TestExtractText(unittest.TestCase):
    def setUp(self):
        import refresh
        self.extract = refresh.extract_text
        self.probe = "9 770 172 139"

    def test_every_form_of_nonbreaking_space_normalises(self):
        forms = {
            "обычные пробелы": "9 770 172 139",
            "&nbsp;": "9&nbsp;770&nbsp;172&nbsp;139",
            "&#160; десятичная ссылка": "9&#160;770&#160;172&#160;139",
            "&#xa0; шестнадцатеричная": "9&#xa0;770&#xa0;172&#xa0;139",
            "&#8239; узкий пробел": "9&#8239;770&#8239;172&#8239;139",
            "U+00A0 буквально": "9 770 172 139",
            "U+202F буквально": "9 770 172 139",
            "&thinsp;": "9&thinsp;770&thinsp;172&thinsp;139",
        }
        for name, raw in forms.items():
            self.assertIn(self.probe, self.extract(raw),
                          "форма %s не нормализовалась" % name)

    def test_tags_are_stripped(self):
        self.assertIn(self.probe, self.extract("<b>9</b>&nbsp;770 172 139"))

    def test_control_case_absent_number_is_really_absent(self):
        # Контрольный случай наоборот: проверка обязана НЕ находить то, чего нет.
        # Без этого тест на нормализацию доказывает только собственную мягкость.
        self.assertNotIn(self.probe, self.extract("9&nbsp;770&nbsp;172&nbsp;138"))

    def test_named_entities_decode(self):
        got = self.extract("&lt;тег&gt; &amp; &rarr; &times;")
        self.assertIn("&", got)
        self.assertIn("→", got)
        self.assertIn("×", got)

    def test_escaped_angle_brackets_are_lost_and_that_is_deliberate(self):
        # Порядок шагов имеет следствие: сущности разбираются ДО снятия тегов,
        # поэтому &lt;тег&gt; сначала становится текстом <тег>, а затем снимается
        # как тег. Для проб это безразлично -- в них числа и русские подписи, --
        # но зафиксировать поведение надо, иначе следующий читатель решит, что
        # это баг, и переставит шаги, сломав разбор настоящей разметки.
        self.assertNotIn("тег", self.extract("&lt;тег&gt;"))
        self.assertIn("9 770", self.extract("&lt;b&gt;9&lt;/b&gt;&nbsp;770"))


class TestRoots(unittest.TestCase):
    def setUp(self):
        self.d = tmpdir()
        self.saved = {k: os.environ.get(k) for k in
                      ("CLAUDE_CONFIG_DIR", "CODEX_HOME", cfg.ENV_CLAUDE, cfg.ENV_CODEX)}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.d, ignore_errors=True)

    def test_claude_root_honours_config_dir(self):
        # CLAUDE_CONFIG_DIR переносит всё дерево состояния, и его главное
        # применение -- переключение аккаунтов, то есть ровно те, кому нужен
        # аудит расхода. Игнорировать его значит измерять у них ноль.
        proj = os.path.join(self.d, "projects")
        os.makedirs(proj, exist_ok=True)
        os.environ["CLAUDE_CONFIG_DIR"] = self.d
        roots = cfg.claude_roots()
        self.assertTrue(any(os.path.normcase(proj) == os.path.normcase(str(r))
                            for r in roots),
                        "корень из CLAUDE_CONFIG_DIR не найден: %s" % roots)

    def test_codex_roots_expand_sessions_and_archived(self):
        # archived_sessions -- каталог, куда `codex archive` переносит сессии.
        # Пока его не сканировали, все, кто архивирует, недосчитывались молча.
        for sub in ("sessions", "archived_sessions"):
            os.makedirs(os.path.join(self.d, sub), exist_ok=True)
        os.environ["CODEX_HOME"] = self.d
        got = [os.path.basename(str(r).rstrip(os.sep)) for r in cfg.codex_roots()]
        self.assertIn("sessions", got)
        self.assertIn("archived_sessions", got)

    def test_require_raises_on_empty(self):
        # Пустой набор корней обязан быть ошибкой, а не нулём: ноль публикуется
        # как измерение.
        with self.assertRaises(cfg.RootError) as cm:
            cfg.require([], "Claude Code", "claude")
        msg = str(cm.exception)
        self.assertIn("корень не найден", msg)
        self.assertIn("задать", msg, "сообщение обязано подсказывать, что делать")

    def test_exit_code_vocabulary(self):
        self.assertEqual((cfg.EXIT_VERIFY, cfg.EXIT_INTEGRITY,
                          cfg.EXIT_NO_ROOT, cfg.EXIT_NO_CHROME), (1, 2, 3, 4))

    def test_redact_removes_the_username(self):
        # Артефакты публичные: домашний путь обязан уезжать как ~.
        home = os.path.expanduser("~")
        red = cfg.redact(os.path.join(home, ".claude", "projects"))
        self.assertTrue(red.startswith("~"), red)
        self.assertNotIn(os.path.basename(home), red)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Корни данных, окружение и headless-браузер. Единственное место в аудите,
где вообще разрешено писать путь или читать переменную окружения.

Модуль ничего не измеряет. Он отвечает на один вопрос: где лежат транскрипты
и чем их проверять. Всё остальное — агрегаторы, отчёты, дашборд — обязано
спрашивать здесь, потому что зашитый литерал вида
`C:\\Users\\Admin\\Documents\\CodexBackups\\...` работает ровно на одной машине,
а на любой другой молча превращается в ноль.

ПРИОРИТЕТ, одинаковый для каждого корня:
    1) явный аргумент (argv)
    2) переменная окружения TOKENAUDIT_*
    3) tokenaudit.config.json рядом с модулем (файл не обязателен)
    4) автопоиск по системным правилам инструмента

ПУСТОЙ РЕЗУЛЬТАТ НИКОГДА НЕ СТАНОВИТСЯ НУЛЁМ. Отсутствие корня — это RootError
с текстом, что именно искали и что задать. Причина жёсткая: пустое сканирование,
записанное в артефакт, уже один раз убило 6,5 МБ измеренных данных и уронило
все последующие запуски на KeyError вместо внятного сообщения.

ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ, которые читает модуль:
    TOKENAUDIT_CONFIG            путь к json-конфигу вместо соседнего файла
    TOKENAUDIT_CLAUDE_ROOTS      корни Claude Code, разделитель os.pathsep
    TOKENAUDIT_CODEX_ROOTS       корни Codex
    TOKENAUDIT_ANTIGRAVITY_ROOTS корни Antigravity (каталоги brain)
    TOKENAUDIT_SECOND_MACHINE    json со второй машины
    TOKENAUDIT_CHROME            путь к chrome/chromium/msedge
    TOKENAUDIT_ALLOW_EMPTY       "1" — осознанно разрешить пустой скан
Чужие переменные, которые модуль уважает, а не игнорирует:
    CLAUDE_CONFIG_DIR            штатный способ держать несколько аккаунтов
    CODEX_HOME                   штатный корень Codex
    CHROME, CHROME_PATH, PUPPETEER_EXECUTABLE_PATH

КЛЮЧИ tokenaudit.config.json (все опциональны):
    {"claude_roots": ["ПУТЬ", ...], "codex_roots": [...],
     "antigravity_roots": [...], "second_machine": "ФАЙЛ", "chrome": "ПУТЬ"}
"""
import argparse
import glob as _glob
import io
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "tokenaudit.config.json"

# --- словарь кодов выхода: один код — одна причина, чтобы вызывающий скрипт и
# --- CI различали их без разбора текста ------------------------------------
EXIT_VERIFY = 1      # проверка рендера дашборда не прошла: панели пустые или текст не найден
EXIT_INTEGRITY = 2   # цифры в артефактах не сходятся между собой
EXIT_NO_ROOT = 3     # корень данных не найден, ноль НЕ записан, артефакты не тронуты
EXIT_NO_CHROME = 4   # headless-браузера нет, проверить рендер нечем

ENV_CONFIG = "TOKENAUDIT_CONFIG"
ENV_CLAUDE = "TOKENAUDIT_CLAUDE_ROOTS"
ENV_CODEX = "TOKENAUDIT_CODEX_ROOTS"
ENV_ANTIGRAVITY = "TOKENAUDIT_ANTIGRAVITY_ROOTS"
ENV_SECOND = "TOKENAUDIT_SECOND_MACHINE"
ENV_CHROME = "TOKENAUDIT_CHROME"
ENV_ALLOW_EMPTY = "TOKENAUDIT_ALLOW_EMPTY"

# имя файла с выгрузкой второй машины: имя, а не путь — путь ищется
SECOND_MACHINE_NAME = "shinobu_danat_codex.json"

CHROME_REG_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
CHROME_ON_PATH = ("chrome", "google-chrome", "google-chrome-stable", "chromium",
                  "chromium-browser", "msedge", "microsoft-edge")
CHROME_ENV = (ENV_CHROME, "CHROME", "CHROME_PATH", "PUPPETEER_EXECUTABLE_PATH")

# метаданные инструментов: отсюда собирается текст ошибки, чтобы он не расходился
# с реальными именами флагов и переменных
_TOOLS = {
    "claude": {"what": "Claude Code", "flag": "--claude-root", "env": ENV_CLAUDE,
               "key": "claude_roots", "artifact": "claude_totals.json"},
    "codex": {"what": "Codex", "flag": "--codex-root", "env": ENV_CODEX,
              "key": "codex_roots", "artifact": "codex_chains_totals.json"},
    "antigravity": {"what": "Antigravity", "flag": "--antigravity-root",
                    "env": ENV_ANTIGRAVITY, "key": "antigravity_roots",
                    "artifact": "antigravity_totals.json"},
    "second_machine": {"what": "вторая машина", "flag": "--second-machine",
                       "env": ENV_SECOND, "key": "second_machine",
                       "artifact": "панель второй машины",
                       "single": True,
                       "tail": "панель второй машины не рисуется, "
                               "остальные цифры не затронуты"},
}
CONFIG_KEYS = ("claude_roots", "codex_roots", "antigravity_roots",
               "second_machine", "chrome")

# последний поиск по каждому инструменту: {tool: {"source": str, "trace": [...],
# "roots": [...]}}. Нужен, чтобы require() и print_roots() показывали, где именно
# смотрели, не повторяя поиск.
_LAST = {}
_CONFIG_OVERRIDE = None
_ALLOW_EMPTY = False
_PAD = 38            # ширина колонки пути в блоке "искал"
_IND = " " * 12      # отступ продолжения под "  искал   : "


class RootError(RuntimeError):
    """Корень не найден. Никогда не превращается в ноль и не пишется в артефакт.

    Текст исключения рассчитан на человека: что искали, где, и что задать.
    Вызывающему коду положено печатать его и выходить с EXIT_NO_ROOT
    (или EXIT_NO_CHROME, если это find_chrome).
    """


# --------------------------------------------------------------- кодировка
def stdout_utf8():
    """Перевести собственные stdout/stderr процесса в utf-8, errors='replace'.

    Вызывать ПЕРВОЙ строкой main(), до любого вывода. PYTHONIOENCODING в env
    дочернего процесса не спасает родителя: когда stdout не консоль Windows —
    Git Bash, любой pipe, любой редирект, CI — Python берёт локальную кодировку
    (здесь cp1251) и первый же символ '▸' убивает процесс UnicodeEncodeError.

    Безопасно вызывать дважды. Если поток подменён на объект без reconfigure
    (io.StringIO в тестах), функция молча ничего не делает.
    -> None
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        rec = getattr(stream, "reconfigure", None)
        if rec is None or not callable(rec):
            continue
        try:
            rec(encoding="utf-8", errors="replace")
        except Exception:
            # поток закрыт или не текстовый: вывод всё равно не наша задача ронять
            pass


# --------------------------------------------------------------- служебное
def _split(value):
    """Строка с os.pathsep, список или None -> список непустых строк."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = str(value).split(os.pathsep)
    out = []
    for it in items:
        s = str(it).strip().strip('"')
        if s:
            out.append(s)
    return out


def _norm(path):
    """Развернуть ~ и %VAR%, привести к абсолютному пути с нативными разделителями.

    Заодно лечит 'C:\\Users\\Admin/.claude/projects' из os.path.expanduser,
    который в таком виде уезжал прямо в опубликованный claude_totals.json.
    """
    p = os.path.expandvars(os.path.expanduser(str(path)))
    return os.path.normpath(os.path.abspath(p))


def _dedupe(paths):
    """Убрать повторы, сохранив порядок. На Windows сравнение без учёта регистра.

    Не косметика: два корня, указывающие на один каталог, — это ровно тот
    механизм, которым старый реестр надул итог Codex на 28%.
    """
    seen, out = set(), []
    for p in paths:
        k = os.path.normcase(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def _config_path(here=None):
    """Где лежит конфиг: --config / TOKENAUDIT_CONFIG / рядом с модулем."""
    if _CONFIG_OVERRIDE:
        return _norm(_CONFIG_OVERRIDE)
    env = os.environ.get(ENV_CONFIG)
    if env:
        return _norm(env)
    return os.path.join(_norm(here) if here else HERE, CONFIG_NAME)


def load_config(here=None):
    """Прочитать tokenaudit.config.json. Файла нет — пустой dict, это норма.

    Битый JSON или не-объект на верхнем уровне -> RootError: молча
    проигнорированный конфиг это та же болезнь, что молчаливый ноль.
    Незнакомый ключ — предупреждение в stderr (опечатка в имени ключа иначе
    выглядит как «конфиг не работает»).
    -> dict
    """
    p = _config_path(here)
    if not p or not os.path.isfile(p):
        return {}
    try:
        with io.open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as e:
        raise RootError("%s: битый JSON — %s\n  файл: %s" % (CONFIG_NAME, e, p))
    except OSError as e:
        raise RootError("%s: не читается — %s\n  файл: %s" % (CONFIG_NAME, e, p))
    if not isinstance(data, dict):
        raise RootError("%s: на верхнем уровне ожидался объект JSON, получено %s"
                        "\n  файл: %s" % (CONFIG_NAME, type(data).__name__, p))
    unknown = [k for k in data if k not in CONFIG_KEYS]
    if unknown:
        sys.stderr.write("%s: незнакомые ключи %s, известны: %s\n"
                         % (CONFIG_NAME, ", ".join(sorted(unknown)),
                            ", ".join(CONFIG_KEYS)))
    return data


def set_config_path(path):
    """Запомнить путь к конфигу из --config. path=None снимает переопределение."""
    global _CONFIG_OVERRIDE
    _CONFIG_OVERRIDE = path or None


def set_allow_empty(flag):
    """Включить/выключить режим --allow-empty для require()."""
    global _ALLOW_EMPTY
    _ALLOW_EMPTY = bool(flag)


def allow_empty():
    """Разрешён ли осознанно пустой скан: флаг --allow-empty или TOKENAUDIT_ALLOW_EMPTY.
    -> bool
    """
    if _ALLOW_EMPTY:
        return True
    return os.environ.get(ENV_ALLOW_EMPTY, "").strip().lower() in ("1", "true", "yes", "да")


def _ladder(tool, cli):
    """Первые три ступени приоритета. -> (items, source) или ([], None) для автопоиска."""
    meta = _TOOLS[tool]
    items = _split(cli)
    if items:
        return items, "аргумент " + meta["flag"]
    items = _split(os.environ.get(meta["env"]))
    if items:
        return items, "$" + meta["env"]
    items = _split(load_config().get(meta["key"]))
    if items:
        return items, CONFIG_NAME
    return [], None


def _finish(tool, cand, source, want="dir"):
    """Отфильтровать кандидатов по существованию, записать трассу поиска.

    cand: список (display, path|None). path=None — «переменная не задана».
    -> список существующих путей, без повторов
    """
    trace, roots = [], []
    for display, path in cand:
        if path is None:
            trace.append((display, "не задан"))
            continue
        ok = os.path.isdir(path) if want == "dir" else os.path.isfile(path)
        trace.append((display, "есть" if ok else "нет"))
        if ok:
            roots.append(path)
    roots = _dedupe(roots)
    _LAST[tool] = {"source": source or "автопоиск", "trace": trace, "roots": roots}
    return roots


def source_of(tool):
    """Откуда взялись корни этого инструмента при последнем вызове. -> str"""
    return _LAST.get(tool, {}).get("source", "не искали")


def trace_of(tool):
    """Полная трасса последнего поиска. -> список (что искали, 'есть'/'нет'/'не задан')"""
    return list(_LAST.get(tool, {}).get("trace", []))


# --------------------------------------------------------------------- корни
def claude_roots(cli=None):
    """Корни транскриптов Claude Code.

    Автопоиск: $CLAUDE_CONFIG_DIR/projects (штатный способ переключать
    аккаунты — как раз те люди, которым нужен аудит токенов), затем
    ~/.claude/projects. Если существуют оба и это разные каталоги — вернутся
    оба: дедупликация записей идёт по message.id, так что перекрытие не
    удваивает итог, а недосмотренный аккаунт занижает его навсегда.
    -> list[str] существующих каталогов
    """
    items, source = _ladder("claude", cli)
    if items:
        cand = [(p, _norm(p)) for p in items]
    else:
        source = "автопоиск"
        cand = []
        ccd = os.environ.get("CLAUDE_CONFIG_DIR")
        if ccd:
            for d in _split(ccd):
                p = os.path.join(_norm(d), "projects")
                cand.append((p, p))
        else:
            cand.append(("$CLAUDE_CONFIG_DIR", None))
        home = os.path.join(_norm("~"), ".claude", "projects")
        cand.append((home, home))
    return _finish("claude", cand, source)


def codex_roots(cli=None):
    """Корни rollout-файлов Codex.

    Каждый кандидат раскрывается одинаково: если внутри есть sessions/ — берём
    sessions/ и archived_sessions/ (куда `codex archive` уносит сессии; их не
    сканировал никто, хотя cost_model.py на них ссылается). Если sessions/ нет,
    кандидат берётся как есть — так задаются каталоги-бэкапы.
    Автопоиск: $CODEX_HOME (несколько записей через os.pathsep), иначе ~/.codex.
    -> list[str] существующих каталогов
    """
    items, source = _ladder("codex", cli)
    cand = []
    if not items:
        source = "автопоиск"
        home = os.environ.get("CODEX_HOME")
        if home:
            items = _split(home)
        else:
            cand.append(("$CODEX_HOME", None))
            items = ["~/.codex"]
    for it in items:
        base = _norm(it)
        sub = os.path.join(base, "sessions")
        if os.path.isdir(sub):
            cand.append((sub, sub))
            cand.append((os.path.join(base, "archived_sessions"),
                         os.path.join(base, "archived_sessions")))
        else:
            cand.append((it if it != base else base, base))
    return _finish("codex", cand, source)


def _label_for(path):
    """Метка корня Antigravity из имени РОДИТЕЛЬСКОГО каталога, не из литерала.

    .../.gemini/antigravity-ide/brain -> 'antigravity-ide'
    .../.geminiantigravity/brain      -> 'geminiantigravity' (ведущая точка снята)
    """
    p = str(path).rstrip("\\/")
    name = os.path.basename(p)
    if name.lower() == "brain":
        name = os.path.basename(os.path.dirname(p))
    name = name.lstrip(".")
    return name or "root"


def antigravity_roots(cli=None):
    """Корни brain Antigravity вместе с метками.

    Автопоиск: ~/.gemini/antigravity*/brain (на этой машине это antigravity,
    antigravity-cli, antigravity-ide — жёсткий список из трёх литералов терял
    antigravity-cli) плюс ~/.geminiantigravity/brain.
    -> list[(path, label)]
    """
    items, source = _ladder("antigravity", cli)
    if items:
        cand = [(p, _norm(p)) for p in items]
    else:
        source = "автопоиск"
        cand = []
        home = _norm("~")
        for pat in (os.path.join(home, ".gemini", "antigravity*", "brain"),
                    os.path.join(home, ".geminiantigravity", "brain")):
            hits = sorted(_glob.glob(pat))
            if hits:
                for h in hits:
                    p = _norm(h)
                    cand.append((p, p))
            else:
                cand.append((pat, None if "*" in pat else _norm(pat)))
    roots = _finish("antigravity", cand, source)
    return [(p, _label_for(p)) for p in roots]


def second_machine_file(cli=None):
    """json с выгрузкой второй машины. Не обязателен.

    Автопоиск: рядом с модулем, затем ~/Downloads/<имя> и ~/Downloads/*/<имя>
    (Telegram Desktop кладёт файл в подкаталог). Ничего не нашлось -> None,
    и панель второй машины просто не рисуется — это не ошибка.
    Если путь задан ЯВНО (argv/env/конфиг) и файла нет -> RootError: опечатка
    не должна тихо убирать панель.
    -> str | None
    """
    items, source = _ladder("second_machine", cli)
    explicit = bool(items)
    if items:
        cand = [(p, _norm(p)) for p in items]
    else:
        source = "автопоиск"
        cand = [(os.path.join(HERE, SECOND_MACHINE_NAME),
                 os.path.join(HERE, SECOND_MACHINE_NAME))]
        dl = os.path.join(_norm("~"), "Downloads")
        cand.append((os.path.join(dl, SECOND_MACHINE_NAME),
                     os.path.join(dl, SECOND_MACHINE_NAME)))
        for h in sorted(_glob.glob(os.path.join(dl, "*", SECOND_MACHINE_NAME))):
            cand.append((_norm(h), _norm(h)))
    found = _finish("second_machine", cand, source, want="file")
    if found:
        return found[0]
    if explicit:
        _fail("second_machine")
    return None


# ------------------------------------------------------------------ ошибки
def _fail(tool, what=None, artifact=None):
    """Собрать и бросить RootError по трассе последнего поиска. Никогда не возвращает."""
    meta = _TOOLS[tool]
    what = what or meta["what"]
    art = artifact or meta["artifact"]
    lines = ["%s: корень не найден" % what]
    trace = trace_of(tool)
    if not trace:
        trace = [("список кандидатов пуст", "нет")]
    for i, (display, status) in enumerate(trace):
        head = "  искал   : " if i == 0 else _IND
        lines.append(head + "%-*s (%s)" % (_PAD, display, status))
    single = meta.get("single", False)
    lines.append("  задать  : %s ПУТЬ" % meta["flag"])
    if single:
        lines.append(_IND + "%s=ПУТЬ" % meta["env"])
    else:
        lines.append(_IND + "%-*s (разделитель '%s')"
                     % (_PAD, meta["env"] + "=ПУТЬ", os.pathsep))
    lines.append(_IND + '%s: {"%s": %s}'
                 % (CONFIG_NAME, meta["key"], '"ПУТЬ"' if single else '["ПУТЬ"]'))
    lines.append("  " + (meta["tail"] if meta.get("tail")
                         else "ноль не записан: %s не тронут" % art))
    raise RootError("\n".join(lines))


def require(roots, what, tool, artifact=None, allow_empty_ok=None):
    """Пустой список корней -> RootError с текстом, что делать. Иначе тот же список.

    Единственный законный способ получить ноль — явный --allow-empty
    (или TOKENAUDIT_ALLOW_EMPTY=1); тогда в stderr уходит предупреждение, что
    артефакт будет перезаписан нулями.
    -> list (тот же объект)
    """
    if roots:
        return roots
    if allow_empty_ok is None:
        allow_empty_ok = allow_empty()
    if allow_empty_ok:
        sys.stderr.write("ВНИМАНИЕ: %s — корней нет, но задан --allow-empty: "
                         "в %s будут записаны нули\n"
                         % (what, (artifact or _TOOLS[tool]["artifact"])))
        return roots
    _fail(tool, what, artifact)


def describe(what, roots, files=None, source=None):
    """Одна строка лога о том, что просканировано. Печатать ВСЕГДА, и при успехе.

    Молчание при успехе — причина, по которой три недели никто не замечал, что
    один из корней Antigravity пуст, а другой вообще не в списке.
    files=None -> 'файлов —'.
    -> str
    """
    shown = []
    for r in (roots or []):
        if isinstance(r, (list, tuple)) and len(r) >= 2:
            shown.append("%s:%s" % (r[1], redact(r[0])))
        else:
            shown.append(redact(r))
    n = "—" if files is None else fmt_n(files)
    return "%s: корней %d, файлов %s, источник %s%s" % (
        what, len(shown), n, source or "не указан",
        (" — " + "; ".join(shown)) if shown else " — НИ ОДНОГО")


def fmt_n(n):
    """Число с узким пробелом между разрядами, как во всех отчётах аудита. -> str"""
    try:
        return "{:,}".format(int(n)).replace(",", " ")
    except (TypeError, ValueError):
        return str(n)


# ------------------------------------------------------------------ chrome
def chrome_from_registry():
    """Chrome из реестра Windows: HKLM, затем HKCU, ключ App Paths\\chrome.exe.

    Отдельная публичная функция, потому что это единственная ветка, которая
    реально работает на Windows: shutil.which не находит chrome ни под одним из
    семи имён — установщик Chrome не кладёт себя в PATH.
    winreg импортируется лениво, чтобы модуль спокойно грузился на POSIX.
    -> (path|None, [(что искали, статус), ...])
    """
    trace, found = [], None
    try:
        import winreg
    except ImportError:
        return None, [("реестр Windows", "не эта ОС")]
    for hive, name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"),
                       (winreg.HKEY_CURRENT_USER, "HKCU")):
        display = "реестр %s\\%s" % (name, CHROME_REG_KEY)
        try:
            with winreg.OpenKey(hive, CHROME_REG_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            trace.append((display, "нет ключа"))
            continue
        path = str(value).strip().strip('"')
        ok = bool(path) and os.path.isfile(path)
        trace.append((display, "есть" if ok else "ключ есть, файла нет"))
        if ok and found is None:
            found = path
    return found, trace


def _chrome_candidates():
    """Списки шагов 3-6: (display, path|None, kind) в порядке проверки."""
    out = []
    for name in CHROME_ON_PATH:
        out.append(("PATH: " + name, shutil.which(name), "file"))
    if os.name == "nt":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            for rel in (os.path.join("Google", "Chrome", "Application", "chrome.exe"),
                        os.path.join("Microsoft", "Edge", "Application", "msedge.exe")):
                if base:
                    out.append((os.path.join(base, rel),
                                os.path.join(base, rel), "file"))
                else:
                    out.append(("%%%s%%\\%s" % (var, rel), None, "file"))
    elif sys.platform == "darwin":
        home = _norm("~")
        for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  os.path.join(home, "Applications", "Google Chrome.app",
                               "Contents", "MacOS", "Google Chrome"),
                  "/Applications/Chromium.app/Contents/MacOS/Chromium",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
            out.append((p, p, "file"))
    else:
        for p in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                  "/usr/bin/chromium", "/usr/bin/chromium-browser",
                  "/snap/bin/chromium", "/opt/google/chrome/chrome"):
            out.append((p, p, "file"))
    return out


def find_chrome(cli=None):
    """Найти headless-браузер. Порядок из шести шагов, все обязательны.

    1) аргумент --chrome
    2) $TOKENAUDIT_CHROME, $CHROME, $CHROME_PATH, $PUPPETEER_EXECUTABLE_PATH
       (плюс ключ "chrome" в конфиге)
    3) shutil.which по семи именам
    4) Windows: реестр HKLM -> HKCU, затем %ProgramFiles%, %ProgramFiles(x86)%,
       %LOCALAPPDATA% с Google\\Chrome\\Application\\chrome.exe и теми же тремя
       с Microsoft\\Edge\\Application\\msedge.exe
    5) macOS: Google Chrome в /Applications и ~/Applications, Chromium, Edge
    6) Linux: /usr/bin/google-chrome(-stable), chromium, chromium-browser,
       /snap/bin/chromium, /opt/google/chrome/chrome
    Не нашлось -> RootError со полным списком того, где смотрели; вызывающему
    полагается выйти с EXIT_NO_CHROME (или пропустить проверку --no-verify).
    -> str путь к исполняемому файлу
    """
    trace = []
    items = _split(cli)
    if items:
        p = _norm(items[0])
        ok = os.path.isfile(p)
        trace.append(("аргумент --chrome: " + str(items[0]), "есть" if ok else "нет"))
        if ok:
            _LAST["chrome"] = {"source": "аргумент --chrome", "trace": trace, "roots": [p]}
            return p
    else:
        trace.append(("аргумент --chrome", "не задан"))
    for var in CHROME_ENV:
        v = os.environ.get(var)
        if not v:
            trace.append(("$" + var, "не задан"))
            continue
        p = _norm(v.strip().strip('"'))
        ok = os.path.isfile(p)
        trace.append(("$%s: %s" % (var, v), "есть" if ok else "нет"))
        if ok:
            _LAST["chrome"] = {"source": "$" + var, "trace": trace, "roots": [p]}
            return p
    cfg = load_config().get("chrome")
    if cfg:
        p = _norm(cfg)
        ok = os.path.isfile(p)
        trace.append(("%s: chrome=%s" % (CONFIG_NAME, cfg), "есть" if ok else "нет"))
        if ok:
            _LAST["chrome"] = {"source": CONFIG_NAME, "trace": trace, "roots": [p]}
            return p
    else:
        trace.append(("%s: ключ chrome" % CONFIG_NAME, "не задан"))

    steps = _chrome_candidates()
    if os.name == "nt":
        # реестр идёт перед Program Files: он единственный знает нестандартную установку
        reg_path, reg_trace = chrome_from_registry()
        path_steps = [s for s in steps if s[0].startswith("PATH: ")]
        rest = [s for s in steps if not s[0].startswith("PATH: ")]
        for display, path, _kind in path_steps:
            ok = bool(path) and os.path.isfile(path)
            trace.append((display, "есть" if ok else "нет"))
            if ok:
                _LAST["chrome"] = {"source": "PATH", "trace": trace, "roots": [path]}
                return _norm(path)
        trace.extend(reg_trace)
        if reg_path:
            _LAST["chrome"] = {"source": "реестр Windows", "trace": trace,
                               "roots": [_norm(reg_path)]}
            return _norm(reg_path)
        steps = rest
    for display, path, _kind in steps:
        ok = bool(path) and os.path.isfile(path)
        trace.append((display, "есть" if ok else "нет"))
        if ok:
            src = "PATH" if display.startswith("PATH: ") else "штатный каталог"
            _LAST["chrome"] = {"source": src, "trace": trace, "roots": [_norm(path)]}
            return _norm(path)

    _LAST["chrome"] = {"source": "не найден", "trace": trace, "roots": []}
    lines = ["headless-браузер не найден"]
    for i, (display, status) in enumerate(trace):
        head = "  искал   : " if i == 0 else _IND
        lines.append(head + "%-*s (%s)" % (_PAD, display, status))
    lines.append("  задать  : --chrome ПУТЬ")
    lines.append(_IND + "%-*s" % (_PAD, ENV_CHROME + "=ПУТЬ"))
    lines.append(_IND + '%s: {"chrome": "ПУТЬ"}' % CONFIG_NAME)
    lines.append("  без него: проверка рендера невозможна, "
                 "--no-verify пропускает её осознанно")
    raise RootError("\n".join(lines))


def _needs_no_sandbox():
    """Нужен ли --no-sandbox: root в Linux или контейнер (/.dockerenv). -> bool"""
    if os.path.exists("/.dockerenv"):
        return True
    geteuid = getattr(os, "geteuid", None)
    if sys.platform.startswith("linux") and callable(geteuid):
        try:
            return geteuid() == 0
        except OSError:
            return False
    return False


def chrome_profile_dir(dom_out):
    """Каталог одноразового профиля Chrome для этой проверки.

    Лежит в системном temp, а НЕ рядом с dom_out: каталог рядом с
    _verify_dom.html оказался бы внутри репозитория и попал бы в git status
    (в .gitignore закрыт сам файл, не профиль). Каталог можно спокойно удалять
    после проверки: shutil.rmtree(chrome_profile_dir(dom), ignore_errors=True).
    -> str
    """
    import tempfile
    name = os.path.basename(str(dom_out) or "verify") or "verify"
    return os.path.join(tempfile.gettempdir(), "tokenaudit-chrome-" + name)


def chrome_args(target, dom_out):
    """Аргументы headless-Chrome для снятия DOM. Исполняемый файл НЕ включён.

    Вызов целиком: subprocess.run([find_chrome()] + chrome_args(target, dom_out), ...)
    target — file:///... или http(s)://...; DOM Chrome отдаёт в stdout, dom_out
    нужен только как имя для отдельного профиля: без --user-data-dir уже
    открытый Chrome с тем же профилем отдаёт пустой рендер, и проверка врёт
    про «0 байт».
    --no-sandbox добавляется только когда он реально нужен: root в Linux или
    контейнер. Добавлять его всегда — снимать защиту на рабочей машине.
    -> list[str]
    """
    args = ["--headless=new", "--disable-gpu", "--virtual-time-budget=9000",
            "--dump-dom"]
    if _needs_no_sandbox():
        args.append("--no-sandbox")
    if dom_out:
        args.append("--user-data-dir=" + chrome_profile_dir(dom_out))
    args.append(str(target))
    return args


# --------------------------------------------------------------- публикация
def redact(path):
    """Заменить домашний каталог на '~'. Артефакты не должны светить имя пользователя.

    Заодно нормализует разделители в '/', так что вместо
    'C:\\Users\\Admin/.claude/projects' в json уходит '~/.claude/projects'.
    Путь вне домашнего каталога возвращается без изменений.
    -> str
    """
    s = str(path)
    if not s:
        return s
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    if not home:
        return s
    text = s.replace("\\", "/")
    hay = text.lower() if os.name == "nt" else text
    needle = home.lower() if os.name == "nt" else home
    if needle not in hay:
        return s
    out, i = [], 0
    while True:
        j = hay.find(needle, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        out.append("~")
        i = j + len(needle)
    return "".join(out)


def env_for_children(roots_by_tool):
    """Переменные, которые родитель подмешивает дочерним скриптам.

    Так дочерний скрипт наследует УЖЕ РАЗРЕШЁННЫЕ корни, и родителю не нужно
    переписывать argv каждого из них. Использование:
        env = dict(os.environ, **c.env_for_children({"claude": roots, ...}))
        subprocess.run([sys.executable, "-u", script], env=env, ...)

    Принимает dict с любыми из ключей:
        "claude" / "codex" / "antigravity"  список путей (для antigravity —
                                            список путей или пар (path,label))
        "second_machine", "chrome", "config" одиночные пути
        "allow_empty"                        bool
    Возвращаемые имена переменных, точно:
        PYTHONIOENCODING=utf-8       (иначе дочерний вывод умирает на cp1251)
        TOKENAUDIT_CLAUDE_ROOTS      пути через os.pathsep
        TOKENAUDIT_CODEX_ROOTS
        TOKENAUDIT_ANTIGRAVITY_ROOTS
        TOKENAUDIT_SECOND_MACHINE
        TOKENAUDIT_CHROME
        TOKENAUDIT_CONFIG
        TOKENAUDIT_ALLOW_EMPTY=1     только если разрешён пустой скан
    Ключи с пустым значением не попадают в результат вовсе.
    -> dict[str, str]
    """
    env = {"PYTHONIOENCODING": "utf-8"}
    for tool, var in (("claude", ENV_CLAUDE), ("codex", ENV_CODEX),
                      ("antigravity", ENV_ANTIGRAVITY)):
        value = roots_by_tool.get(tool)
        if not value:
            continue
        paths = []
        for item in value:
            if isinstance(item, (list, tuple)) and item:
                paths.append(str(item[0]))
            else:
                paths.append(str(item))
        if paths:
            env[var] = os.pathsep.join(paths)
    for key, var in (("second_machine", ENV_SECOND), ("chrome", ENV_CHROME),
                     ("config", ENV_CONFIG)):
        value = roots_by_tool.get(key)
        if value:
            env[var] = str(value)
    if roots_by_tool.get("allow_empty"):
        env[ENV_ALLOW_EMPTY] = "1"
    return env


# ------------------------------------------------------------------- argparse
def add_path_args(ap):
    """Добавить в парсер общие для всех скриптов аргументы путей. -> тот же парсер

    --claude-root / --codex-root / --antigravity-root можно повторять (append).
    """
    ap.add_argument("--claude-root", action="append", metavar="ПУТЬ",
                    help="корень транскриптов Claude Code, можно повторять")
    ap.add_argument("--codex-root", action="append", metavar="ПУТЬ",
                    help="корень rollout-файлов Codex, можно повторять")
    ap.add_argument("--antigravity-root", action="append", metavar="ПУТЬ",
                    help="каталог brain Antigravity, можно повторять")
    ap.add_argument("--second-machine", metavar="ФАЙЛ",
                    help="json с выгрузкой второй машины; без него панель не рисуется")
    ap.add_argument("--chrome", metavar="ПУТЬ",
                    help="путь к chrome/chromium/msedge для проверки рендера")
    ap.add_argument("--config", metavar="ФАЙЛ",
                    help="путь к %s вместо соседнего файла" % CONFIG_NAME)
    ap.add_argument("--allow-empty", action="store_true",
                    help="осознанно разрешить пустой скан и запись нулей "
                         "(по умолчанию это ошибка выхода %d)" % EXIT_NO_ROOT)
    ap.add_argument("--print-roots", action="store_true",
                    help="показать все найденные корни и выйти")
    return ap


def apply_args(args):
    """Применить разобранные аргументы: запомнить --config и --allow-empty.

    -> dict {"claude": [...]|None, "codex": ..., "antigravity": ...,
             "second_machine": str|None, "chrome": str|None,
             "config": str|None, "allow_empty": bool}
    Значения — ровно то, что пришло из argv (None, если не задано), готовые к
    передаче первым аргументом в *_roots() и в env_for_children().
    """
    cfg = getattr(args, "config", None)
    set_config_path(cfg)
    empty = bool(getattr(args, "allow_empty", False))
    set_allow_empty(empty)
    return {"claude": getattr(args, "claude_root", None),
            "codex": getattr(args, "codex_root", None),
            "antigravity": getattr(args, "antigravity_root", None),
            "second_machine": getattr(args, "second_machine", None),
            "chrome": getattr(args, "chrome", None),
            "config": cfg, "allow_empty": empty}


def print_roots(cli=None):
    """Напечатать всё, что модуль нашёл, вместе с источником и трассой поиска.

    Диагностика не имеет права падать: ненайденное показывается как «нет», а не
    как исключение. cli — dict из apply_args() либо None.
    -> int код выхода, всегда 0
    """
    stdout_utf8()
    cli = cli or {}
    print("корни аудита (приоритет: аргумент > env > %s > автопоиск)" % CONFIG_NAME)
    print("конфиг: %s (%s)" % (redact(_config_path()),
                               "есть" if os.path.isfile(_config_path()) else "нет"))
    for tool, getter in (("claude", claude_roots), ("codex", codex_roots),
                         ("antigravity", antigravity_roots)):
        what = _TOOLS[tool]["what"]
        try:
            roots = getter(cli.get(tool))
        except RootError as e:
            print("\n%s: ОШИБКА КОНФИГА\n%s" % (what, e))
            continue
        print("\n" + describe(what, roots, None, source_of(tool)))
        for display, status in trace_of(tool):
            print("    %-*s (%s)" % (_PAD, redact(display), status))
    try:
        sm = second_machine_file(cli.get("second_machine"))
    except RootError as e:
        sm, _ = None, print("\nвторая машина: ОШИБКА\n%s" % e)
    print("\nвторая машина: %s" % (redact(sm) if sm else "нет, панель не рисуется"))
    try:
        print("chrome: %s (%s)" % (redact(find_chrome(cli.get("chrome"))),
                                   source_of("chrome")))
    except RootError as e:
        print("chrome: не найден")
        for line in str(e).splitlines()[1:]:
            print("  " + line)
    return 0


if __name__ == "__main__":
    _ap = add_path_args(argparse.ArgumentParser(
        description="показать, какие корни данных видит аудит на этой машине"))
    raise SystemExit(print_roots(apply_args(_ap.parse_args())))

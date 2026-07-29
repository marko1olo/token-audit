#!/usr/bin/env python3
"""Автообновление аудита: измерить, зафиксировать, отправить в оба зеркала.

    python autoupdate.py                # один проход
    python autoupdate.py --no-push      # измерить и закоммитить, но не отправлять
    python autoupdate.py --dry-run      # только измерить, ничего не менять в git
    python autoupdate.py --install      # поставить задачу в планировщик Windows
    python autoupdate.py --uninstall    # снять задачу
    python autoupdate.py --status       # что стоит в планировщике

ЧТО ЭТО ДЕЛАЕТ, И ЧЕГО НЕ ДЕЛАЕТ

Один проход -- это `refresh.py` целиком: измерение, стоимость, генерация
AUTO-блоков в отчётах, сборка дашборда и проверка рендера в headless-браузере.
Затем коммит и отправка, если и только если есть что фиксировать.

Отказы намеренно тихие и безопасные:
  * ненулевой код `refresh.py` -> НИЧЕГО не коммитится. Код 2 это несошедшаяся
    целостность, код 3 -- отсутствующий корень или измеренный ноль. Фиксировать
    такое значит публиковать заведомо неверные цифры.
  * нет изменений в рабочем дереве -> нет коммита. Пустые коммиты каждый час
    превращают историю в шум.
  * push не удался -> коммит остаётся локальным, следующий проход отправит оба.
    Терять работу нельзя, поэтому порядок именно такой: сначала коммит, потом
    отправка.

БЕЗОПАСНОСТЬ. Токены доступа не печатаются, не логируются и не остаются на
диске. Они читаются из файла с кредами (путь задаётся --credentials или
переменной TOKENAUDIT_GIT_CREDENTIALS) в момент отправки, кладутся во временный
credential-store вне репозитория, а после отправки файл перезаписывается и
удаляется. Вывод git фильтруется от значений токенов на случай, если git решит
напечатать URL с встроенными кредами. Сам этот файл не содержит ни одного
секрета и потому лежит в публичном репозитории спокойно.

Формат файла с кредами -- строки вида `GitHub Token: ...`, ожидаются четыре:
GitHub Username, GitHub Token, GitLab Username, GitLab Token.

Лог пишется рядом со скриптом в autoupdate.log и в репозиторий не попадает:
это состояние машины, а не артефакт аудита.
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
LOG = os.path.join(HERE, "autoupdate.log")
TASK = "TokenAuditHourly"
CRED_ENV = "TOKENAUDIT_GIT_CREDENTIALS"
DEFAULT_CRED = os.path.join(os.path.expanduser("~"), "Documents", "gitenv.txt")

# Зеркала: имя удалённого репозитория -> хост, под который нужен credential.
MIRRORS = (("origin", "github.com", "github"), ("gitlab", "gitlab.com", "gitlab"))


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with io.open(LOG, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except OSError:
        pass          # лог -- удобство, а не условие работы


def git(*args, **kw):
    """Запустить git в каталоге репозитория. -> (код, вывод)

    scrub -- список значений, которые надо вычистить из вывода. Нужен на случай,
    если git напечатает URL со встроенными кредами.
    """
    scrub = kw.pop("scrub", ())
    p = subprocess.run(("git",) + args, cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    for s in scrub:
        if s:
            out = out.replace(s, "***")
    return p.returncode, out


def read_credentials(path):
    """Четыре значения из файла с кредами. Ничего не печатает. -> dict | None"""
    if not path or not os.path.isfile(path):
        return None
    got = {}
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"^\s*(Git(?:Hub|Lab))\s+(Username|Token)\s*[:=]\s*(\S+)\s*$", line)
            if m:
                got[(m.group(1) + "_" + m.group(2)).lower()] = m.group(3)
    need = ("github_username", "github_token", "gitlab_username", "gitlab_token")
    return got if all(k in got for k in need) else None


def measure(extra_args):
    """Полный прогон refresh.py. -> (код, последние строки вывода)"""
    cmd = [PY, "-u", os.path.join(HERE, "refresh.py")] + list(extra_args)
    p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return p.returncode, ((p.stdout or "") + (p.stderr or ""))


def figures():
    """Цифры для сообщения коммита из измеренных артефактов. -> dict"""
    def load(name):
        try:
            with io.open(os.path.join(HERE, name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}
    cl, cb = load("claude_totals.json"), load("combined.json")
    t = cl.get("totals_deduped") or {}
    total = sum(t.get(k, 0) for k in ("inp", "cc", "cr", "out"))
    snaps = []
    sp = os.path.join(HERE, "snapshots.jsonl")
    if os.path.isfile(sp):
        with io.open(sp, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        snaps.append(json.loads(line))
                    except ValueError:
                        pass
    delta = None
    if len(snaps) >= 2:
        delta = snaps[-1]["claude"]["total"] - snaps[-2]["claude"]["total"]
    return {"total": total, "sessions": cl.get("session_count"),
            "responses": cl.get("records_deduped"), "delta": delta,
            "cost": (cb.get("claude_code") or {}).get(
                "cost_usd_total_list_price_equivalent"),
            "window": cl.get("scan_window") or {}}


def fmt(n):
    return "—" if n is None else "{:,}".format(int(n)).replace(",", " ")


def commit_message(fig):
    head = "Автообновление: %s токенов" % fmt(fig["total"])
    body = ["", "Автоматический почасовой прогон. Числа подставлены генератором",
            "из измеренных JSON, целостность и рендер проверены до коммита.", ""]
    body.append("  токенов   %s" % fmt(fig["total"]))
    if fig["cost"] is not None:
        body.append("  по прайсу $%s"
                    % "{:,.2f}".format(fig["cost"]).replace(",", " "))
    body.append("  сессий %s, ответов %s" % (fig["sessions"], fmt(fig["responses"])))
    if fig["delta"] is not None:
        body.append("  прирост   %+s" % fmt(fig["delta"]) if fig["delta"] < 0
                    else "  прирост   +%s" % fmt(fig["delta"]))
    w = fig["window"]
    if w:
        body.append("  окно      %s файлов, %s байт"
                    % (fmt(w.get("files")), fmt(w.get("bytes"))))
    return head + "\n" + "\n".join(body) + "\n"


def push(cred):
    """Отправить в оба зеркала через одноразовый credential-store. -> [(имя, код)]"""
    store = os.path.join(os.environ.get("TEMP") or "/tmp", "_ta_cred.tmp")
    lines = ["https://%s:%s@%s" % (cred["%s_username" % key], cred["%s_token" % key], host)
             for _remote, host, key in MIRRORS]
    secrets = [cred["github_token"], cred["gitlab_token"]]
    results = []
    try:
        with io.open(store, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        helper = "store --file=%s" % store.replace("\\", "/")
        for remote, _host, _key in MIRRORS:
            rc, out = git("-c", "credential.helper=", "-c", "credential.helper=" + helper,
                          "push", remote, "main", scrub=secrets)
            results.append((remote, rc))
            log("push %s: %s" % (remote, "ок" if rc == 0 else "ОШИБКА " + out[-300:]))
    finally:
        try:
            with io.open(store, "w", encoding="utf-8") as fh:
                fh.write("x" * 512)      # перезаписать, потом удалить
            os.remove(store)
        except OSError as e:
            log("ВНИМАНИЕ: временный credential-файл не удалён: %s" % e)
    return results


def one_pass(a):
    log("=" * 62)
    extra = ["--no-verify"] if a.no_verify else []
    if a.codex:
        extra.append("--codex")
    if a.antigravity:
        extra.append("--antigravity")
    rc, out = measure(extra)
    tail = "\n".join(x for x in out.strip().split("\n")[-4:] if x.strip())
    if rc != 0:
        log("refresh.py вернул %d -- НИЧЕГО не коммитим" % rc)
        log(tail[-600:])
        return rc
    fig = figures()
    log("измерено: %s токенов, сессий %s, ответов %s"
        % (fmt(fig["total"]), fig["sessions"], fmt(fig["responses"])))
    if a.dry_run:
        log("--dry-run: git не трогаем")
        return 0

    rc, out = git("status", "--porcelain")
    if rc != 0:
        log("git status не сработал: %s" % out[-300:])
        return 1
    changed = [x for x in out.split("\n") if x.strip()]
    if changed:
        rc, out = git("add", "-A")
        if rc != 0:
            log("git add не сработал: %s" % out[-300:])
            return 1
        msg = os.path.join(os.environ.get("TEMP") or "/tmp", "_ta_msg.txt")
        with io.open(msg, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(commit_message(fig))
        rc, out = git("commit", "-q", "-F", msg)
        try:
            os.remove(msg)
        except OSError:
            pass
        if rc != 0:
            log("коммит не прошёл (возможно, хук): %s" % out[-400:])
            return 1
        _rc, head = git("log", "--oneline", "-1")
        log("коммит: %s (файлов изменено %d)" % (head, len(changed)))
    else:
        log("изменений нет -- пустой коммит не создаём")

    if a.no_push:
        log("--no-push: отправка пропущена")
        return 0
    rc, out = git("log", "origin/main..HEAD", "--oneline")
    if rc == 0 and not out.strip():
        log("отправлять нечего -- зеркала уже актуальны")
        return 0
    cred = read_credentials(a.credentials)
    if not cred:
        log("креды не найдены (%s) -- коммит остался локальным, следующий проход "
            "отправит оба" % (a.credentials or "путь не задан"))
        return 0
    bad = [r for r, code in push(cred) if code != 0]
    return 1 if bad else 0


# ------------------------------------------------------------ планировщик
def _console_encoding():
    """Кодировка вывода консоли Windows. -> str"""
    try:
        import ctypes
        return "cp%d" % ctypes.windll.kernel32.GetOEMCP()
    except Exception:
        return "cp866"


def schtasks(*args):
    """Вызвать schtasks и разобрать вывод в правильной кодировке.

    schtasks печатает не в utf-8, а в кодировке консоли -- на русской Windows
    это cp866. С encoding="utf-8" ответ приходил нечитаемой кашей, и разбор
    своего же вывода не работал: --status ничего не находил при существующей
    задаче. Кодировка спрашивается у системы, а не угадывается.
    """
    raw = subprocess.run(("schtasks",) + args, capture_output=True)
    data = (raw.stdout or b"") + (raw.stderr or b"")
    for enc in (_console_encoding(), "cp866", "cp1251", "utf-8"):
        try:
            return raw.returncode, data.decode(enc).strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.returncode, data.decode("utf-8", "replace").strip()


def install(a):
    """Почасовая задача в планировщике Windows.

    Минута берётся не нулевая намеренно: ноль -- это минута, на которую
    приходится всё остальное в системе, и попадать в неё незачем.
    """
    if os.name != "nt":
        line = "%d * * * * cd %s && %s autoupdate.py >> autoupdate.log 2>&1" % (
            a.minute, HERE, PY)
        print("на этой системе планировщика Windows нет. Строка для crontab:")
        print("  " + line)
        return 0
    cmd = '"%s" "%s"' % (PY, os.path.join(HERE, "autoupdate.py"))
    rc, out = schtasks("/Create", "/TN", TASK, "/TR", cmd, "/SC", "HOURLY",
                       "/ST", "00:%02d" % a.minute, "/F")
    print(out)
    if rc == 0:
        print("задача %s поставлена: каждый час на :%02d" % (TASK, a.minute))
        print("проверить:  python autoupdate.py --status")
        print("снять:      python autoupdate.py --uninstall")
    return rc


def uninstall(_a):
    if os.name != "nt":
        print("на этой системе задача ставилась через crontab -- снимать там же")
        return 0
    rc, out = schtasks("/Delete", "/TN", TASK, "/F")
    print(out)
    return rc


def status(_a):
    """Показать задачу. Ключевые строки находятся независимо от языка системы."""
    if os.name != "nt":
        print("crontab -l | grep autoupdate")
        return 0
    rc, out = schtasks("/Query", "/TN", TASK, "/V", "/FO", "LIST")
    if rc != 0:
        print("задача %s не найдена" % TASK)
        print(out[-400:])
        return 1
    # Имена полей зависят от локали, поэтому берём и русские, и английские
    # варианты; не совпало ничего -- печатаем вывод как есть.
    keys = ("TaskName", "Имя задачи", "Task To Run", "Задача для выполнения",
            "Schedule Type", "Тип расписания", "Start Time", "Время запуска",
            "Next Run Time", "Время следующего запуска", "Status", "Состояние")
    hit = [ln.strip() for ln in out.splitlines()
           if any(k.lower() in ln.lower() for k in keys)]
    print("\n".join(hit) if hit else out)
    return 0


def main():
    ap = argparse.ArgumentParser(description="почасовое автообновление аудита токенов")
    ap.add_argument("--no-push", action="store_true", help="коммитить, но не отправлять")
    ap.add_argument("--dry-run", action="store_true", help="только измерить")
    ap.add_argument("--no-verify", action="store_true",
                    help="без проверки рендера (быстрее, но слабее)")
    ap.add_argument("--codex", action="store_true", help="включить скан Codex (медленно)")
    ap.add_argument("--antigravity", action="store_true", help="включить скан Antigravity")
    ap.add_argument("--credentials", default=os.environ.get(CRED_ENV) or DEFAULT_CRED,
                    help="файл с кредами git (по умолчанию $%s или ~/Documents/gitenv.txt)"
                         % CRED_ENV)
    ap.add_argument("--minute", type=int, default=17,
                    help="минута часа для запуска (по умолчанию 17, не 0)")
    ap.add_argument("--install", action="store_true", help="поставить почасовую задачу")
    ap.add_argument("--uninstall", action="store_true", help="снять задачу")
    ap.add_argument("--status", action="store_true", help="показать задачу")
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    if a.install:
        return install(a)
    if a.uninstall:
        return uninstall(a)
    if a.status:
        return status(a)
    return one_pass(a)


if __name__ == "__main__":
    raise SystemExit(main())

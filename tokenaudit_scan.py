#!/usr/bin/env python3
"""Общее окно сканирования для проходов, которые читают одни и те же файлы.

ЗАЧЕМ ЭТО СУЩЕСТВУЕТ

Транскрипты дописываются, пока инструмент их читает -- в том числе той самой
сессией, чей расход он и считает. Два прохода подряд видят разный набор данных,
и артефакты перестают быть согласованными между собой.

Измерено на реальном прогоне: claude_agg насчитал 48 107 ответов, claude_deep
следом -- 48 245, разница 138 ответов и 18 615 694 токена, целиком в
claude-opus-5, то есть в модели работающей сессии. Итог публиковался из одного
файла, а таблица по моделям и стоимость -- из другого, поэтому таблица не
сходилась с итогом на 0.19% и расходилась в деньгах на 24.88 доллара.
Дедупликация тут не при чём: одним проходом обе логики дают ровно 61 267
уникальных ответов и одинаковую сумму.

Временнáя граница по last_ts проблему только уменьшает -- с 138 ответов до 21 --
потому что первый проход сам сканирует около 86 секунд: файл, прочитанный в
начале, теряет записи, дописанные к его концу, и их метки времени всё ещё
меньше глобального last_ts.

Поэтому окно задаётся в БАЙТАХ. Первый проход запоминает размер каждого файла на
момент чтения и читает ровно столько; манифест пишется рядом с артефактами.
Второй проход читает те же файлы и те же префиксы. Совпадение входа становится
свойством конструкции, а не удачей.

Манифест -- не кэш. Он не ускоряет работу и не позволяет пропустить чтение: оба
прохода читают всё заново. Он фиксирует только границу.
"""
import io
import json
import os

MANIFEST_NAME = "scan_manifest.json"
HERE = os.path.dirname(os.path.abspath(__file__))


def manifest_path(name=MANIFEST_NAME):
    return os.path.join(HERE, name)


def read_lines_upto(path, limit):
    """Строки файла в пределах первых limit байт.

    Читаем в двоичном режиме, чтобы граница считалась в байтах, а не в
    символах: в транскриптах есть кириллица, и в utf-8 символ занимает
    несколько байт. Неполная хвостовая строка отбрасывается -- файл мог
    вырасти между снятием размера и чтением, и половина записи не разберётся
    как JSON.

    limit is None -- читаем файл целиком (самостоятельный прогон без манифеста).
    """
    n = 0
    with open(path, "rb") as fh:
        for raw in fh:
            if limit is not None:
                n += len(raw)
                if n > limit:
                    return
            yield raw.decode("utf-8", "replace")


def walk_files(roots, suffix=".jsonl", skip_dirs=()):
    """Отсортированный список (абсолютный путь, путь относительно корня, корень).

    Порядок детерминированный: без него манифест первого прогона и обход
    второго могут разойтись на файлах с одинаковым именем в разных каталогах.
    """
    out = []
    for root in roots:
        root = str(root)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            ap = os.path.abspath(dirpath)
            if any(ap.startswith(str(s)) for s in skip_dirs):
                dirnames[:] = []
                continue
            for fn in filenames:
                if fn.endswith(suffix):
                    p = os.path.join(dirpath, fn)
                    out.append((p, os.path.relpath(p, root), root))
    out.sort(key=lambda x: (x[2], x[1]))
    return out


class ScanWindow(object):
    """Окно сканирования: какие файлы и сколько байт в каждом.

    Первый проход создаёт окно через `capture`, читает через `lines`, затем
    сохраняет через `save`. Второй проход берёт окно через `load` и читает
    те же префиксы тех же файлов.
    """

    def __init__(self, sizes=None, roots=None, captured_by=None):
        self.sizes = dict(sizes or {})       # ключ -- "корень|относительный путь"
        self.roots = list(roots or [])
        self.captured_by = captured_by
        self.missing = []                    # файлы из манифеста, которых уже нет
        self.shrunk = []                     # файлы, ставшие короче манифеста

    # ---------- ключи ----------
    @staticmethod
    def key(root, rel):
        return "%s|%s" % (root, rel)

    # ---------- первый проход ----------
    @classmethod
    def capture(cls, roots, suffix=".jsonl", skip_dirs=(), captured_by=None):
        w = cls(roots=[str(r) for r in roots], captured_by=captured_by)
        w._files = walk_files(roots, suffix, skip_dirs)
        for path, rel, root in w._files:
            try:
                w.sizes[cls.key(root, rel)] = os.path.getsize(path)
            except OSError:
                w.sizes[cls.key(root, rel)] = 0
        return w

    # ---------- второй проход ----------
    @classmethod
    def load(cls, path=None):
        p = path or manifest_path()
        if not os.path.exists(p):
            return None
        try:
            with io.open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            return None
        w = cls(sizes=d.get("sizes"), roots=d.get("roots"),
                captured_by=d.get("captured_by"))
        w._files = None
        return w

    # ---------- общий доступ ----------
    def files(self):
        """Файлы окна как (путь, относительный путь, корень).

        Если окно снято этим же прогоном -- отдаём то, что обошли. Если
        загружено из манифеста -- восстанавливаем пути из ключей, чтобы второй
        проход не зависел от результата собственного обхода каталогов.
        """
        if getattr(self, "_files", None):
            return self._files
        out = []
        for k in sorted(self.sizes):
            root, _, rel = k.partition("|")
            p = os.path.join(root, rel)
            if os.path.exists(p):
                out.append((p, rel, root))
            else:
                self.missing.append(k)
        return out

    def limit(self, root, rel):
        return self.sizes.get(self.key(root, rel))

    def lines(self, path, rel, root):
        lim = self.limit(root, rel)
        if lim is not None:
            try:
                if os.path.getsize(path) < lim:
                    self.shrunk.append(self.key(root, rel))
            except OSError:
                pass
        return read_lines_upto(path, lim)

    def save(self, path=None):
        p = path or manifest_path()
        with io.open(p, "w", encoding="utf-8") as fh:
            json.dump({"captured_by": self.captured_by,
                       "roots": self.roots,
                       "files": len(self.sizes),
                       "bytes": sum(self.sizes.values()),
                       "sizes": self.sizes}, fh, indent=1, ensure_ascii=False)
        return p

    def describe(self):
        s = "окно: файлов %d, байт %s" % (
            len(self.sizes), "{:,}".format(sum(self.sizes.values())).replace(",", " "))
        if self.missing:
            s += "; исчезло с момента снятия: %d" % len(self.missing)
        if self.shrunk:
            s += "; стало короче манифеста: %d" % len(self.shrunk)
        return s

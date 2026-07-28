# -*- coding: utf-8 -*-
"""Общее для тестов: путь к модулям и синтетические транскрипты.

Тесты НЕ трогают настоящие данные и не пишут в репозиторий. Всё, что нужно,
собирается на месте, а ожидаемое число выводится руками и записано в комментарии
рядом с фикстурой -- иначе тест проверяет не инвариант, а собственный вывод.
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def tmpdir():
    """Каталог под вывод теста. Никогда не внутри репозитория."""
    return tempfile.mkdtemp(prefix="tokenaudit_test_")


def assistant_record(mid, model="claude-opus-5", ts="2026-07-01T10:00:00.000Z",
                     inp=0, cc=0, cr=0, out=0, session="s1", e5m=None, e1h=None,
                     sidechain=False):
    """Одна запись транскрипта Claude Code в том же виде, в каком её пишет CLI."""
    usage = {"input_tokens": inp, "output_tokens": out,
             "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}
    if e5m is not None or e1h is not None:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": e5m or 0,
                                   "ephemeral_1h_input_tokens": e1h or 0}
    return {"type": "assistant", "timestamp": ts, "sessionId": session,
            "isSidechain": sidechain, "version": "2.1.204",
            "message": {"id": mid, "model": model, "usage": usage,
                         "stop_reason": "end_turn"}}


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def claude_root(base, project="proj", name="session.jsonl", records=()):
    """Корень транскриптов Claude Code: <base>/<project>/<name>."""
    d = os.path.join(base, project)
    os.makedirs(d, exist_ok=True)
    write_jsonl(os.path.join(d, name), records)
    return base


def rollout(path, events, model="gpt-5.5", session_id="019de027-c757-7450-87f7-e6386c25a1e3"):
    """Файл rollout Codex: session_meta плюс события token_count.

    events -- список накопительных снимков (input, cached, output, total).
    Счётчик у Codex НАКОПИТЕЛЬНЫЙ, поэтому итог сессии -- последнее значение
    цепочки, а не сумма событий.
    """
    recs = [{"type": "session_meta", "payload": {
        "id": session_id, "cwd": "/tmp", "model": model,
        "timestamp": "2026-04-10T02:40:41.000Z"}}]
    for i, (inp, cached, out, tot) in enumerate(events):
        recs.append({"type": "event_msg", "timestamp": "2026-04-10T02:%02d:00.000Z" % (i % 60),
                     "payload": {"type": "token_count", "info": {
                         "model_context_window": 400000,
                         "total_token_usage": {
                             "input_tokens": inp, "cached_input_tokens": cached,
                             "output_tokens": out, "reasoning_output_tokens": 0,
                             "total_tokens": tot}}}})
    return write_jsonl(path, recs)

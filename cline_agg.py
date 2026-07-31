#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль сканирования и агрегации токенов Cline / Roo-Code / Claude-Dev.

Собирает точные счетчики запросов, токенов и стоимости из логов и задач
Cline/Roo-Code в глобальном хранилище VS Code.

Генерирует файлы:
  - cline_totals.json — легкие агрегаты по моделям и дням
  - cline_deep.json — полная детализация по диалогам, часам и минутам
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import tokenaudit_config as cfg
import tokenaudit_rates as rates

# Временная зона по умолчанию: UTC+4 (Europe/Samara / MSK+1)
TZ_SAMARA = timezone(timedelta(hours=4))


def scan_cline_tasks(roots=None):
    """Сканирует все каталоги задач Cline / Roo-Code и извлекает точный расход.
    
    -> dict с подробными агрегатами
    """
    if roots is None:
        roots = [
            r"C:\Users\Admin\AppData\Roaming\Code\User\globalStorage\rooveterinaryinc.roo-cline\tasks",
            r"C:\Users\Admin\AppData\Roaming\Code\User\globalStorage\saoudrizwan.claude-dev\tasks",
        ]
        
    tasks = []
    by_model = {}
    by_day = {}
    by_hour = {}
    by_minute = {}
    
    total_reqs = 0
    total_in = 0
    total_out = 0
    total_cw = 0
    total_cr = 0
    
    for root in roots:
        if not os.path.exists(root):
            continue
        tool_name = "roo-cline" if "roo-cline" in root else "cline"
        
        for entry in os.scandir(root):
            if not entry.is_dir():
                continue
            tf = entry.path
            task_id = entry.name
            
            ui_file = os.path.join(tf, "ui_messages.json")
            meta_file = os.path.join(tf, "task_metadata.json")
            item_file = os.path.join(tf, "history_item.json")
            
            meta = {}
            for fpath in (item_file, meta_file):
                if os.path.exists(fpath):
                    try:
                        with open(fpath, encoding="utf-8", errors="replace") as f:
                            m = json.load(f)
                            meta.update(m)
                    except Exception:
                        pass
                        
            task_text = meta.get("task", "")
            ts_start = meta.get("ts")
            
            model_id = "grok-4.5"
            if "model_usage" in meta and meta["model_usage"]:
                model_id = meta["model_usage"][0].get("model_id", model_id)
            elif "apiModelId" in meta:
                model_id = meta["apiModelId"]
                
            task_in = 0
            task_out = 0
            task_cw = 0
            task_cr = 0
            task_reqs = 0
            api_calls = []
            
            if os.path.exists(ui_file):
                try:
                    with open(ui_file, encoding="utf-8", errors="replace") as uf:
                        uimsgs = json.load(uf)
                        
                    for uim in uimsgs:
                        if not task_text and uim.get("type") == "say" and uim.get("say") == "task":
                            task_text = uim.get("text", "")
                        if not ts_start and uim.get("ts"):
                            ts_start = uim.get("ts")
                            
                        if uim.get("say") == "api_req_started" and uim.get("text"):
                            try:
                                d = json.loads(uim.get("text"))
                                ts_call = uim.get("ts") or ts_start or 0
                                t_in = d.get("tokensIn", 0) or 0
                                t_out = d.get("tokensOut", 0) or 0
                                t_cw = d.get("cacheWrites", 0) or 0
                                t_cr = d.get("cacheReads", 0) or 0
                                
                                task_in += t_in
                                task_out += t_out
                                task_cw += t_cw
                                task_cr += t_cr
                                task_reqs += 1
                                
                                dt_call = datetime.fromtimestamp(ts_call / 1000.0, tz=TZ_SAMARA) if ts_call else datetime.now(TZ_SAMARA)
                                day_str = dt_call.strftime("%Y-%m-%d")
                                hour_str = dt_call.strftime("%Y-%m-%d %H:00")
                                min_str = dt_call.strftime("%Y-%m-%d %H:%M")
                                
                                # Агрегируем по моделям
                                if model_id not in by_model:
                                    by_model[model_id] = {"inp": 0, "out": 0, "cw": 0, "cr": 0, "reqs": 0}
                                by_model[model_id]["inp"] += t_in
                                by_model[model_id]["out"] += t_out
                                by_model[model_id]["cw"] += t_cw
                                by_model[model_id]["cr"] += t_cr
                                by_model[model_id]["reqs"] += 1
                                
                                # Агрегируем по дням
                                if day_str not in by_day:
                                    by_day[day_str] = {"inp": 0, "out": 0, "cw": 0, "cr": 0, "reqs": 0}
                                by_day[day_str]["inp"] += t_in
                                by_day[day_str]["out"] += t_out
                                by_day[day_str]["cw"] += t_cw
                                by_day[day_str]["cr"] += t_cr
                                by_day[day_str]["reqs"] += 1
                                
                                # Агрегируем по часам
                                if hour_str not in by_hour:
                                    by_hour[hour_str] = {"inp": 0, "out": 0, "cw": 0, "cr": 0, "reqs": 0}
                                by_hour[hour_str]["inp"] += t_in
                                by_hour[hour_str]["out"] += t_out
                                by_hour[hour_str]["cw"] += t_cw
                                by_hour[hour_str]["cr"] += t_cr
                                by_hour[hour_str]["reqs"] += 1
                                
                                # Агрегируем по минутам
                                if min_str not in by_minute:
                                    by_minute[min_str] = {"inp": 0, "out": 0, "cw": 0, "cr": 0, "reqs": 0}
                                by_minute[min_str]["inp"] += t_in
                                by_minute[min_str]["out"] += t_out
                                by_minute[min_str]["cw"] += t_cw
                                by_minute[min_str]["cr"] += t_cr
                                by_minute[min_str]["reqs"] += 1
                                
                                api_calls.append({
                                    "ts": ts_call,
                                    "inp": t_in,
                                    "out": t_out,
                                    "cw": t_cw,
                                    "cr": t_cr,
                                })
                            except Exception:
                                pass
                except Exception:
                    pass
                    
            if task_reqs > 0 or task_in > 0:
                total_reqs += task_reqs
                total_in += task_in
                total_out += task_out
                total_cw += task_cw
                total_cr += task_cr
                
                tasks.append({
                    "task_id": task_id,
                    "source": tool_name,
                    "model_id": model_id,
                    "task_text": task_text[:120],
                    "ts_start": ts_start,
                    "reqs": task_reqs,
                    "inp": task_in,
                    "out": task_out,
                    "cw": task_cw,
                    "cr": task_cr,
                    "total_tokens": task_in + task_out + task_cw + task_cr,
                })

    totals = {
        "tool": "cline",
        "scanned_at": datetime.now(TZ_SAMARA).isoformat(),
        "task_count": len(tasks),
        "request_count": total_reqs,
        "inp": total_in,
        "out": total_out,
        "cw": total_cw,
        "cr": total_cr,
        "total_tokens": total_in + total_out + total_cw + total_cr,
        "by_model": by_model,
        "by_day": dict(sorted(by_day.items())),
    }
    
    deep = {
        "totals": totals,
        "tasks": sorted(tasks, key=lambda x: x["total_tokens"], reverse=True),
        "by_hour": dict(sorted(by_hour.items())),
        "by_minute": dict(sorted(by_minute.items())),
    }
    
    return totals, deep


def run_and_save(out_dir=None):
    """Снимает данные и сохраняет cline_totals.json и cline_deep.json."""
    if out_dir is None:
        out_dir = os.path.dirname(os.path.abspath(__file__))
        
    totals, deep = scan_cline_tasks()
    
    totals_path = os.path.join(out_dir, "cline_totals.json")
    deep_path = os.path.join(out_dir, "cline_deep.json")
    
    with open(totals_path, "w", encoding="utf-8") as f:
        json.dump(totals, f, ensure_ascii=False, indent=2)
        
    with open(deep_path, "w", encoding="utf-8") as f:
        json.dump(deep, f, ensure_ascii=False, indent=2)
        
    print(f"[cline_agg] Saved totals to {totals_path} ({totals['total_tokens']:,} tokens across {totals['task_count']} tasks)")
    return totals, deep


if __name__ == "__main__":
    run_and_save()

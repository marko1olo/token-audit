# HANDOFF PROMPT FOR COLLEAGUE'S AI AGENT

> **Task**: Multi-User Anthropic API Token & Spend Forensic Audit
> **Objective**: Run `token-audit` on this PC, extract exact token spend ($USD) for specified 40-interval timestamp windows, and export `colleague_token_breakdown.json`.

---

## 🎯 INSTRUCTIONS FOR THE AGENT

You are participating in a multi-PC forensic audit to reconstruct total shared account spend and determine the fixed Auto-Recharge amount ($R$) and active user shares.

### Step 1: Clone or locate `token-audit` repository
Ensure `token-audit` is available locally or run scripts from `tokenaudit` directory.

### Step 2: Target Timestamp Windows (July 26 - July 29, 2026)
Below is the master list of active work intervals (UTC) bounded by global HTTP 400 credit freezes. For EACH interval, scan all local `.jsonl` session files in `~/.claude/projects/` and sum the exact token spend ($USD) using `tokenaudit_rates.py`.

```json
[
  { "id": 1,  "start": "2026-07-26T06:38:00Z", "end": "2026-07-26T06:58:00Z" },
  { "id": 2,  "start": "2026-07-26T06:59:00Z", "end": "2026-07-26T07:10:00Z" },
  { "id": 3,  "start": "2026-07-26T07:12:00Z", "end": "2026-07-26T07:40:00Z" },
  { "id": 5,  "start": "2026-07-26T08:06:00Z", "end": "2026-07-26T08:52:00Z" },
  { "id": 6,  "start": "2026-07-26T08:59:00Z", "end": "2026-07-26T09:13:00Z" },
  { "id": 15, "start": "2026-07-26T16:49:00Z", "end": "2026-07-26T17:05:00Z" },
  { "id": 16, "start": "2026-07-26T17:05:00Z", "end": "2026-07-26T17:31:00Z" },
  { "id": 21, "start": "2026-07-26T23:58:00Z", "end": "2026-07-27T00:28:00Z" },
  { "id": 22, "start": "2026-07-27T04:39:00Z", "end": "2026-07-27T05:26:00Z" },
  { "id": 23, "start": "2026-07-27T05:43:00Z", "end": "2026-07-27T06:25:00Z" },
  { "id": 24, "start": "2026-07-27T06:25:00Z", "end": "2026-07-27T06:55:00Z" },
  { "id": 27, "start": "2026-07-27T11:15:00Z", "end": "2026-07-27T11:53:00Z" },
  { "id": 28, "start": "2026-07-27T11:55:00Z", "end": "2026-07-27T12:47:00Z" },
  { "id": 29, "start": "2026-07-27T19:31:00Z", "end": "2026-07-27T19:44:00Z" },
  { "id": 30, "start": "2026-07-27T19:45:00Z", "end": "2026-07-27T20:16:00Z" },
  { "id": 31, "start": "2026-07-27T20:24:00Z", "end": "2026-07-27T20:59:00Z" },
  { "id": 32, "start": "2026-07-27T21:02:00Z", "end": "2026-07-27T21:26:00Z" },
  { "id": 33, "start": "2026-07-27T21:30:00Z", "end": "2026-07-27T22:03:00Z" },
  { "id": 34, "start": "2026-07-27T22:07:00Z", "end": "2026-07-27T22:43:00Z" },
  { "id": 35, "start": "2026-07-27T22:51:00Z", "end": "2026-07-27T23:38:00Z" },
  { "id": 38, "start": "2026-07-28T02:02:00Z", "end": "2026-07-28T02:46:00Z" },
  { "id": 39, "start": "2026-07-28T02:54:00Z", "end": "2026-07-28T03:40:00Z" }
]
```

### Step 3: Calculation Script to Run
Run this Python script on the Colleague's PC:

```python
import json, os, glob, sys
from datetime import datetime

# Load tokenaudit rates
sys.path.insert(0, ".")
import tokenaudit_rates as R

windows = [ ... paste windows above ... ]

jsonl_files = glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True)
turns = []

for fpath in jsonl_files:
    with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if 'usage' in line and 'input_tokens' in line:
                try:
                    obj = json.loads(line)
                    ts_str = obj.get("timestamp") or obj.get("message", {}).get("timestamp")
                    if not ts_str: continue
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() * 1000
                    msg = obj.get("message", {})
                    usage = msg.get("usage") if isinstance(msg, dict) else obj.get("usage")
                    if not usage: continue
                    model = msg.get("model", "claude-3-5-sonnet-20241022")
                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cw = usage.get("cache_creation_input_tokens", 0)
                    cr = usage.get("cache_read_input_tokens", 0)
                    rate = R.ANTHROPIC.get(model, (3.0, 15.0))
                    cost = (inp/1e6)*rate[0] + (cw/1e6)*rate[0]*1.25 + (cr/1e6)*rate[0]*0.1 + (out/1e6)*rate[1]
                    turns.append({"ts": ts, "cost": cost, "tokens": inp+cw+cr+out})
                except: pass

colleague_results = []
for w in windows:
    w_start = datetime.fromisoformat(w["start"].replace("Z", "+00:00")).timestamp() * 1000
    w_end = datetime.fromisoformat(w["end"].replace("Z", "+00:00")).timestamp() * 1000
    in_w = [t for t in turns if t["ts"] >= w_start and t["ts"] <= w_end]
    colleague_results.append({
        "id": w["id"],
        "start": w["start"],
        "end": w["end"],
        "colleague_turns": len(in_w),
        "colleague_tokens": sum(t["tokens"] for t in in_w),
        "colleague_cost_usd": round(sum(t["cost"] for t in in_w), 2)
    })

with open("colleague_token_breakdown.json", "w") as f:
    json.dump(colleague_results, f, indent=2)
print("Saved colleague_token_breakdown.json!")
```

### Step 4: Output Output File
Save the resulting `colleague_token_breakdown.json` and push it to Git.

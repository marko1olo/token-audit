# 🚀 AI Agent Proxies & Load Balancers

Пакет локальных прокси-серверов для непрерывной работы авто-агентов (Cline, Roo-Code, Claude Code) без падений по 429 Rate Limit и 5xx серверным ошибкам.

---

## 📌 1. `grok-proxy.js` (v2.3 — Grok 4.5 Smart Balancer)

Локальный прокси для Grok API (`grok-4.5` / `tunnel.rue.onl`).

### Ключевые возможности:
- **Умный балансировщик (Smart Balancer):** автоматически назначает новые диалоги на ключ с наименьшей нагрузкой (LRU idle selection).
- **Авто-перенос при коллизиях (Auto-Rebalance):** если 2 диалога оказались на одном ключе, а 3-й ключ пустует — прокси на лету перенесет сессию на свободный ключ.
- **Sticky Sessions:** диалог удерживает привязку к своему ключу (Prompt Cache не ломается).
- **Wait-and-Retry:** при ошибке 429 Rate Limit прокси сам делает паузу 20 секунд и повторяет запрос без падения Cline.

### Быстрый запуск:
1. Открой `grok-proxy.js` и заполни массив `GROK_KEYS` своими API-ключами (`pk_*`).
2. Запусти `Grok Proxy.cmd`.
3. В Cline/Roo-Code поставь:
   - **Provider:** `OpenAI Compatible`
   - **Base URL:** `http://127.0.0.1:8319/v1`
   - **API Key:** `any-key`
   - **Model:** `grok-4.5`

---

## 📌 2. `claude-proxy.js` (v3.2 — Enterprise Claude Proxy)

Локальный прокси для Claude Code / Anthropic API (`agentrouter.org`).

### Ключевые возможности:
- **Circuit Breaker (Credit-400 Gate):** при исчерпании кредитного баланса паркует запросы и делает пробник раз в 70-90с вместо сброса вызовов.
- **Forced Effort & Output Tokens:** форсирует `effort=max`, `thinking=adaptive` и `max_tokens >= 32768` для глубокого автономного кода.
- **Infinite Retry & Socket Defense:** бесконечные повторы при сетевых сбоях и мобильных обрывах связи.

---

## 📜 Custom Instructions для Cline / Roo-Code

Скопируй этот блок в **Settings -> Custom Instructions** в Cline для идеальной автономности:

```markdown
[SCREENSHOT COMPRESSION]
Raw PNG >500KB = HTTP 413 crash. ALWAYS compress before vision analysis:
use PIL -> resize to 1280x720, JPEG quality=80 (<150KB). If PIL missing: pip install Pillow.

[SUBAGENT CRASH RECOVERY]
On subagent error/timeout: check .agent_mem/<name>.mem.json or scratch/mem_<name>.json.
Read completed_steps + pending_steps + findings - continue the work yourself or re-spawn with only the remaining steps.
Every subagent you spawn MUST write a .mem.json (write_to_file) after EACH tool call that produces findings:
{status, completed_steps, pending_steps, findings, last_error}
On crash - set status="crashed", write last_error before dying. Never lose partial work.

[RATE LIMIT / 429]
On HTTP 429: STOP. Wait 20 seconds. Retry same request. Do NOT switch model or key.
Do NOT spam retries. One retry per 20s, infinite patience.

[NO DESKTOP DUMP]
NEVER create files on Desktop or its subdirectories.
Scratch scripts, logs, output .txt files, temp data - ALL go to project's .agent_mem/ folder or scratch/.
If you dumped files on Desktop - delete them with Remove-Item before finishing.
```

# 🚀 AI Agent Proxies & Load Balancers

Пакет локальных прокси-серверов для непрерывной работы авто-агентов (Cline, Roo-Code, Claude Code) без падений по 429 Rate Limit и 5xx серверным ошибкам.

---

## 📌 1. `grok-proxy.js` (v5.0 — Terminal Overseer & Smart Balancer)

Локальный прокси для Grok API (`grok-4.5` / `tunnel.rue.onl`) с оформлением в стиле Cyberpunk Terminal CLI, поддержкой интерактивных команд в консоли и динамическими инъекциями.

### Ключевые возможности:
- **100% Terminal-Native Interface:** таблицы нагрузок на ключи с полосами индикации `[████░░░░░░]`, цветовое выделение логов и ноль сторонних веб-зависимостей.
- **Интерактивные CLI Команды (STDIN):**
  - `inj <text>` — мгновенная подгрузка архитектурного приказа во входящий поток агента прямо из консоли.
  - `clear` (или `c`) — быстрая очистка действующих инъекций.
  - `status` (или `s`) — вывод наглядной таблицы распределения сессий и статуса API-ключей.
  - `help` (или `h`) — просмотр списка команд.
- **Классификатор Ролей (Subagent Role Classifier):** фильтрация сабагентов по первому системному сообщению (`You are subagent...`). Сабагенты получают поисковый ошейник (`SUBAGENT GUARD`), а Главные агенты — стратегические задачи.
- **Bulletproof Directive Injector:** безошибочное подмешивание директив в `user`-сообщения без нарушения правил ротации OpenAI/Grok.
- **Strict User-Protected 413 Trimmer:** реактивная обрезка длинного дамп-вывода консоли при `HTTP 413 Payload Too Large`.
- **Smart LRU & Dead Key Guard:** балансировка сессий, ротация при 401/403 и 20-секундная пауза при `429 Rate Limit`.

### Быстрый запуск:
1. Заполни `GROK_KEYS` в `grok-proxy.js`, используй `keys.json` или запусти `grok-proxy-EXPORT.js`.
2. Запусти `Grok Proxy.cmd` (или `node grok-proxy.js`).
3. Ввод команд доступен прямо в открытом окне консоли (`grok-proxy> inj <приказ>`).
4. В Cline / Roo-Code / Antigravity установи:
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

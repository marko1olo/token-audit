# Задание: сбор телеметрии Codex и Antigravity со второго компьютера

**Исполнитель:** Gemini (Antigravity / Gemini CLI) на машине `Shinobu` (профиль пользователя `danat`).
**Заказчик:** аудит расхода токенов за всё время. Составлено 2026-07-27.
**Режим:** только чтение. Ничего не удалять, не перемещать, не переписывать.

---

## Зачем это нужно

На машине `Admin` уже измерено:

- **Claude Code** — 4,040,919,209 токенов (08.07.2026 – 27.07.2026), 146 сессий.
- **Codex, бэкап-корень** — 58,229,730,730 токенов по 1048 файлам `rollout-*.jsonl`.

Из старого отчёта `TOKEN_USAGE_AUDIT_2026-06-06.json` известно, что полная картина Codex складывалась из трёх корней, и **два из трёх находятся на вашей машине**:

| Корень | Файлов | Токенов (по тому отчёту) | Где лежит |
|---|---:|---:|---|
| `C:\Users\danat\.codex\sessions` | 1891 | 50,387,894,530 | **у вас** |
| `C:\Users\danat\.codex\archived_sessions` | 1 | 157,103 | **у вас** |
| `C:\Users\danat\Documents\CodexBackups\codex_cleanup_20260521_194850` | 1048 | 57,856,335,910 | уже измерено на Admin |

Тот отчёт датирован 06.06.2026. Всё, что происходило **после** 6 июня, вообще нигде не посчитано. Нужны свежие данные с вашей машины.

Отдельно: по Antigravity на машине `Admin` **собственного учёта токенов нет** — проверено сквозным поиском по 687 транскриптам (2.38 ГБ): поля `usageMetadata` / `promptTokenCount` / `input_token_count` встречаются лишь в 13 файлах и во всех случаях это захваченный вывод чужого скрипта, а не учёт генераций. У вас может быть другая версия Antigravity — нужно проверить заново, а не поверить на слово.

---

## Правила безопасности (обязательно)

1. **Никогда не выводить и не сохранять** содержимое `auth.json`, `credentials.json`, `token_json`, `oauth*`, `*.key`, cookies, пароли, API-ключи. Если такое поле встретилось — писать `<REDACTED len=N>`.
2. SQLite открывать **только для чтения**: `sqlite3.connect('file:<путь>?mode=ro&immutable=1', uri=True)`.
3. Никаких `find /`, `du -s` по всему диску, рекурсивных grep по всему профилю — только с `-maxdepth` и по конкретным каталогам. Иначе всё повиснет.
4. Итоговые файлы не должны содержать текст переписок — только метрики и агрегаты.

---

## Часть 1 — Codex (главное, есть точный учёт токенов)

### Что искать

```
C:\Users\danat\.codex\sessions\**\rollout-*.jsonl
C:\Users\danat\.codex\archived_sessions\**\rollout-*.jsonl
C:\Users\danat\Documents\CodexBackups\**\rollout-*.jsonl
C:\Users\danat\.codex\logs_*.sqlite          (схема + диапазон дат, не содержимое)
C:\Users\danat\Documents\.codex\session_index.jsonl   (сколько сессий вообще знает индекс)
```

Также проверьте, нет ли `.codex` в других местах: `Documents\.codex`, `OneDrive`, второй профиль пользователя, другие диски (`D:`, `E:`).

### Как считать — это критично

В каждом файле `rollout-*.jsonl` есть записи вида:

```json
{"timestamp":"2026-05-18T13:15:08.607Z","type":"event_msg","payload":{"type":"token_count",
 "info":{"total_token_usage":{"input_tokens":82689,"cached_input_tokens":12672,
 "output_tokens":3023,"reasoning_output_tokens":1001,"total_tokens":85712},
 "last_token_usage":{...},"model_context_window":258400},
 "rate_limits":{"plan_type":"free",...}}}
```

**`total_token_usage` — накопительный счётчик и растёт монотонно внутри файла.** Отсюда:

- Итог по файлу = **максимум** `total_token_usage`, **никогда не сумма** (сумма завысит в десятки раз).
- Расход за интервал = **разница** соседних накопительных значений. Так получается временной ряд с минутным разрешением, а дубликаты событий дают разницу 0 и не мешают.
- Если счётчик **упал** относительно предыдущего значения — это сброс (форк/компакция). Тогда прибавляйте новое значение целиком как свежий расход.
- Модель берите из записей `turn_context` (`payload.model`), она может меняться посреди сессии — привязывайте разницу к модели, действующей на этот момент.
- `cached_input_tokens` — **подмножество** `input_tokens`, не слагаемое. `reasoning_output_tokens` — подмножество `output_tokens`.

### Ловушка двойного счёта — не повторяйте её

Старый отчёт выдал заголовочную цифру **138,912,242,896**, и она **завышена примерно на 28%**: одни и те же сессии попали и в живой каталог, и в бэкап, а сумма «финалов по сессиям» сложила их дважды. Сумма подневных приростов в том же отчёте — 108,312,008,697, и она сходится с суммой по трём корням (108,244,387,543).

Поэтому: **дедуплицируйте по `session_meta.payload.id`**. Если один и тот же `session_id` встретился в двух корнях — берите файл с большим итогом ровно один раз. В отчёте укажите: сколько `session_id` были в более чем одном файле и сколько токенов отброшено дедупликацией.

### Что прислать по Codex

JSON с такой структурой (имена полей соблюдайте — я сливаю это со своими данными автоматически):

```json
{
  "machine": "shinobu",
  "roots": [{"label":"...","path":"...","files":0,"files_with_token_data":0}],
  "scan_stats": {"files":0,"bytes":0,"lines":0,"bad_json_lines":0},
  "distinct_session_ids": 0,
  "session_ids_in_multiple_files": 0,
  "tokens_dropped_by_dedupe": 0,
  "counter_resets_seen": 0,
  "first_ts": "ISO", "last_ts": "ISO",
  "totals": {"input_tokens":0,"cached_input_tokens":0,"output_tokens":0,
             "reasoning_output_tokens":0,"total_tokens":0},
  "totals_cross_check_from_deltas": {"...то же самое, посчитанное через разницы..."},
  "by_model":   {"gpt-5.5": {"...пять полей..."}},
  "by_day":     {"2026-06-07": {"...пять полей..."}},
  "by_hour":    {"2026-06-07T14": {"...пять полей..."}},
  "by_minute":  {"2026-06-07T14:23": {"...пять полей..."}},
  "by_cwd":     {"c:\\hades": {"...пять полей..."}},
  "by_plan_type": {"free": 0, "team": 0},
  "by_cli_version": {},
  "sessions_summary": [{"session_id":"...","start":"ISO","end":"ISO","model":"...",
                        "cwd":"...","total_tokens":0,"events":0}]
}
```

**Обязательно приложите `totals_cross_check_from_deltas`.** Если две цифры не совпали — так и напишите, это диагностика, а не провал.

---

## Часть 2 — Antigravity (нужен вердикт, а не догадка)

### Где смотреть

```
C:\Users\danat\.gemini\antigravity\conversations\*.db            (SQLite, только чтение)
C:\Users\danat\.gemini\antigravity\brain\<uuid>\.system_generated\logs\transcript*.jsonl
C:\Users\danat\.gemini\antigravity-ide\...   (та же структура)
C:\Users\danat\.gemini\antigravity-cli\...   (cli.log, history.jsonl, log\, conversations\)
C:\Users\danat\.gemini\tmp\<hash>\logs.json                     ← см. ниже, самое ценное
C:\Users\danat\.antigravity-agent\app-*.log
C:\Users\danat\AppData\Roaming\Antigravity\logs\                (main.log, cloudcode.log, telemetry.log)
C:\Users\danat\AppData\Roaming\Antigravity\User\globalStorage\state.vscdb
```

> **Внимание:** транскрипты лежат в каталоге `.system_generated`, который начинается с точки. `ripgrep` по умолчанию скрытые каталоги пропускает — без флага `--hidden` вы получите ноль совпадений и решите, что данных нет. Это уже случилось на машине `Admin`. Всегда `rg --hidden` и `find` с явным путём.

### Самое ценное: `~/.gemini/tmp/<project_hash>/logs.json`

Gemini CLI пишет туда события телеметрии, и там **есть настоящие токены**. Формат подтверждён по коду в `@google/gemini-cli/bundle/chunk-ITC7TFJU.js`:

```js
this["event.name"] = "api_response";
this.model = model;
this.usage = {
  input_token_count:          usage_data?.promptTokenCount ?? 0,
  output_token_count:         usage_data?.candidatesTokenCount ?? 0,
  cached_content_token_count: usage_data?.cachedContentTokenCount ?? 0,
  thoughts_token_count:       usage_data?.thoughtsTokenCount ?? 0,
  tool_token_count:           usage_data?.toolUsePromptTokenCount ?? 0,
  total_token_count:          usage_data?.totalTokenCount ?? 0
};
```

На машине `Admin` каталог `~/.gemini/tmp/` **пустой**, поэтому там ничего не восстановить. **Проверьте, не пустой ли он у вас** — если нет, это единственный источник настоящих токенов Gemini, и он важнее всего остального в этой части. Просуммируйте события `api_response` по модели и по времени.

### Проверка гипотезы «Antigravity прячет токены»

Проверьте по порядку и по каждому пункту дайте ответ «есть / нет» с командой-доказательством:

1. Сквозной поиск по транскриптам (**с `--hidden`**) по: `usageMetadata`, `promptTokenCount`, `candidatesTokenCount`, `cachedContentTokenCount`, `thoughtsTokenCount`, `totalTokenCount`, `input_token_count`, `output_token_count`, `total_tokens_burned`, `prompt_tokens_burned`, `"credits"`, `"cost"`. Для каждого совпадения покажите ~300 байт контекста и классифицируйте: **(а)** собственный учёт Antigravity, **(б)** вывод постороннего скрипта, который агент сам запускал, **(в)** исходный код, который агент просто читал. На `Admin` всё оказалось классом (б).
2. Таблица `gen_metadata(idx, data, size)` в `conversations\*.db`: сравните `size` с `length(data)` на многих строках. Если равны — это счётчик байтов и для токенов бесполезен, так и напишите. Если расходятся — опишите зависимость.
3. Разберите несколько protobuf-блобов `gen_metadata.data` обобщённым проходом по varint/length-delimited и поищите целые в диапазоне 10²–10⁶, встречающиеся один раз на генерацию, особенно рядом со строкой-названием модели.
4. `state.vscdb` → ключ `antigravityUnifiedStateSync.modelCredits`. На `Admin` он пустая строка. Проверьте, не заполнен ли у вас — там могут быть остатки кредитов по моделям.
5. `AppData\Roaming\Antigravity\logs\*.log` и `.antigravity-agent\app-*.log`: есть ли записи расхода на запрос. На `Admin` там только `CloudMonitor: Polling quotas...` и `tokenRefreshMs` (это задержка обновления авторизации, не токены).

### Прокси-метрика, если настоящих токенов нет

Тогда считайте по транскриптам (в каждом каталоге беседы берите `transcript_full.jsonl`, а `transcript.jsonl` — только если полного нет, иначе двойной счёт):

- число беседы-каталогов и число транскриптов, суммарный объём в байтах;
- количество записей по типам: `PLANNER_RESPONSE`, `USER_INPUT`, `VIEW_FILE`, `RUN_COMMAND`, `GREP_SEARCH`, `CODE_ACTION`, `MCP_TOOL`, `INVOKE_SUBAGENT`, `SEARCH_WEB`, `CHECKPOINT`, `ERROR_MESSAGE`;
- **`PLANNER_RESPONSE` = ход модели**, это ключевая метрика объёма;
- суммарное число символов в полях `thinking` и `content` (отдельно по типам записей);
- максимальный `step_index` по каждой беседе;
- временные ряды по `created_at` (там ISO-время): по дням, часам и минутам — количество ходов модели и символов;
- количество событий `RESOURCE_EXHAUSTED` / `Individual quota reached` по времени (на `Admin` таких 1859 — это доказательство упора в квоту);
- диапазон дат: самая ранняя и самая поздняя `created_at`.

Прямо укажите в отчёте: **это прокси, а не токены.** И укажите, что он систематически **занижает** реальный расход, потому что не учитывает повторную отправку контекста на каждом ходу — а именно она и составляет основную массу токенов (на Codex доля кэшированного входа 96%).

---

## Часть 3 — что ещё проверить

1. Общий период активности машины: самые ранние и поздние следы Codex и Antigravity, с указанием файла-доказательства.
2. Работал ли на этой машине **Claude Code** (`%USERPROFILE%\.claude\projects\**\*.jsonl`). Если да — посчитайте по полю `message.usage` у записей `type == "assistant"`, **дедуплицируя по `message.id` и беря максимум по каждому id** (стриминговые снимки одного ответа пишутся несколько раз, и наивная сумма завышает почти вдвое).
3. Список установленных AI-инструментов и дат их последнего запуска — не пропущен ли ещё один расходовавший токены инструмент.

---

## Формат сдачи

Сохраните два файла в `C:\Users\danat\Documents\token_audit_shinobu\`:

- `shinobu_codex_totals.json` — структура из части 1;
- `shinobu_antigravity.json` — вердикт и метрики из части 2;
- `SUMMARY.md` — на одну страницу: главные цифры, что удалось измерить, что измерить **не** удалось и почему.

В `SUMMARY.md` обязательно разделите:

- **измерено** — есть точный счётчик в файлах;
- **оценка** — получено пересчётом или экстраполяцией, с указанием формулы;
- **неизвестно** — данных не сохранилось.

Не выдавайте оценку за измерение. Честное «данных нет» — это результат, а не провал. Если какая-то часть не получилась — напишите, какая команда и с какой ошибкой упала.

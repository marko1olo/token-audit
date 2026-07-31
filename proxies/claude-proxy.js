/**
 * claude-proxy v3.2 — BULLETPROOF ENTERPRISE EDITION
 *
 * ЕДИНАЯ ВЕРСИЯ. Этот файл побайтово идентичен C:\Users\Admin\claude-proxy\proxy.js.
 * "Claude Proxy.cmd" запускает тот, что лежит РЯДОМ с ним (Рабочий стол), и только
 * если его нет — падает на claude-proxy\. Держи оба одинаковыми (cmp -s), иначе
 * правки уходят в копию, которая не запускается.
 *
 *  1. Sticky Session Key Binding (100% Prompt Caching per dialog)
 *  2. Бесконечный ретрай на 429/529/5xx и обрывы, с нарастающей паузой
 *  3. v3.2: credit-400 circuit breaker — ждём 70-90с, один пробник на всех
 *  4. Full Stream Error Interception (защита от падения Node при разрывах)
 *  5. Forced model / adaptive thinking / effort=max / max_tokens >= 32k
 *  6. Direct SSE Piping & Gzip Error Decoding
 *
 * ═══════════════════════════════════════════════════════════════════════════
 * ЭДЖКЕЙСЫ CREDIT-GATE — что происходит и почему
 * ═══════════════════════════════════════════════════════════════════════════
 *
 *  1. Первый credit-400
 *     → гейт закрывается СРАЗУ ДЛЯ ВСЕХ, включая ещё не отправленные запросы.
 *       Проверка стоит на входе executeForward, до формирования запроса.
 *
 *  2. Запросы, уже улетевшие наверх в момент первого 400
 *     → отозвать нельзя. Получат свои 400 и встанут в парковку. Один
 *       потраченный вызов на каждый. При залповом старте N субагентов в одну
 *       миллисекунду первая волна успеет получить до N ошибок — дальше тишина.
 *       ЭТО ЕДИНСТВЕННЫЙ СЛУЧАЙ, КОГДА НАВЕРХ УХОДИТ БОЛЬШЕ ОДНОГО ЗАПРОСА.
 *
 *  3. Новый запрос при закрытом гейте
 *     → в парковку без отправки наверх. Вызов не тратится.
 *
 *  4. Пробник получил 200
 *     → гейт открыт, ВСЯ парковка уходит наверх разом (releaseCreditGate с
 *       creditsAlive=true). Это твоё правило "один 200 оживляет остальных".
 *
 *  5. Пробник исчерпал попытки или бюджет
 *     → его клиент получает 400, но ГЕЙТ ОСТАЁТСЯ ЗАКРЫТЫМ: кредитов нет,
 *       гнать парковку наверх бессмысленно (получит те же 400 и снова закроет
 *       гейт — дребезг). Вместо этого promoteProbe() передаёт пробу следующему
 *       из парковки, пульс сохраняется.
 *
 *  6. У пробника отвалился клиент во время ожидания
 *     → probeActive=false + promoteProbe(). Иначе гейт закрыт, пробника нет,
 *       и наверх не идёт вообще никто.
 *
 *  7. У припаркованного отвалился клиент
 *     → повтор отменяется молча. Отвечать в мёртвый сокет нечему.
 *
 *  8. У припаркованного истёк бюджет, гейт всё ещё закрыт
 *     → answerCreditExhausted(): честный 400 клиенту. НЕ паркуется заново по
 *       кругу (это была бы вечная петля и таймаут вместо ответа).
 *
 *  9. Бюджет истёк, но кредиты доказанно живы (пришёл 200)
 *     → пускаем наверх всё равно. Терять готовый запрос на исходе бюджета глупо.
 *
 * 10. Несколько credit-400 одновременно
 *     → blockCreditGate() идемпотентен, гейт закрывается один раз, простой
 *       считается от первого.
 *
 * 11. 200 на любом эндпоинте (в т.ч. count_tokens)
 *     → открывает гейт. Намеренно: 200 откуда угодно доказывает, что баланс жив.
 *
 * 12. Обычный 400 (кривой запрос, а не баланс)
 *     → отдаётся клиенту сразу. Ни ретраев, ни гейта: кривой запрос кривой на
 *       любом ключе и в любой момент.
 *
 * 13. Ошибка в gzip
 *     → тело разжимается ДО проверки на credit-текст, иначе регексп не сматчит.
 *
 * ── ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ (не закрыты намеренно) ──────────────────────────
 *
 *  A. Credit-ошибка ВНУТРИ стрима (HTTP 200, потом SSE event с ошибкой)
 *     гейтом не ловится: заголовки уже отданы клиенту, ретраить нечем.
 *     По наблюдениям ошибка приходит чистым 400 до стрима, поэтому не лечим.
 *
 *  B. Тело запроса держится в памяти ради ретраев. Много припаркованных
 *     запросов с большим контекстом = много RAM. Плата за возможность повтора.
 *
 *  C. Состояние гейта живёт в процессе. Перезапуск прокси его сбрасывает.
 *
 *  D. Бюджет считается от прихода запроса в прокси, а не от старта клиента.
 *     Если клиент уже потратил часть своего таймаута до нас — бюджет окажется
 *     оптимистичнее реального. Поэтому в CREDIT_BUDGET_MS оставлен запас.
 */

const http   = require('http');
const https  = require('https');
const zlib   = require('zlib');
const { URL } = require('url');

const PROXY_PORT = 8318;
const UPSTREAM   = 'https://agentrouter.org';

// ── AGENTROUTER KEY POOL ──────────────────────────────────────────────────
const ENTERPRISE_KEYS = [
  'sk-dERPWqmuaY3vbSPk3GUNusf8DEcwcVrwqIVBvmE5UD68f88b'
];

// ── CREDIT-BALANCE 400: ЖДЁМ, ПОТОМ ПОВТОРЯЕМ ────────────────────────────────
// "Your credit balance is too low" приходит как HTTP 400 / invalid_request_error.
// SDK НИКОГДА не ретраит 400 — его набор это connection errors, 408, 409, 429
// и >=500. Поэтому клиент умирает мгновенно: отсюда "Agent terminated early due
// to an API error" у субагентов. Ждать 70-90 секунд — единственное лекарство.
const CREDIT_DELAYS_MS = (process.env.CREDIT_DELAYS_MS
  ? process.env.CREDIT_DELAYS_MS.split(',').map(Number)
  : [80000, 90000, 90000]);   // полоса 70-90с, 3 попытки = 260с

const CREDIT_RE = /credit balance is too low|insufficient[_ ]credit/i;

// ── СОГЛАСОВАНИЕ С ТАЙМАУТАМИ ────────────────────────────────────────────────
// Клиент (Claude Code SDK) ждёт ответа 600с по умолчанию. Сокет наверх живёт
// 300с. Значит ожидание кредитов не может занимать всё окно: после освобождения
// запросу ещё нужно реально выполниться. Бюджет ожидания = 600 - 300 = 300с.
// Дефолтные задержки (260с) в него влезают; если переопределишь CREDIT_DELAYS_MS
// на большие значения, дедлайн ниже всё равно не даст просрочить клиента.
const CLIENT_TIMEOUT_MS = Number(process.env.CLIENT_TIMEOUT_MS || 600000);
// Раздельные таймауты. Прежний единый 300000 был выставлен как ОПЦИЯ, но обработчика
// 'timeout' в коде не было — а без него Node только испускает событие и НИЧЕГО не делает,
// поэтому мёртвый сокет висел вечно. Это и был симптом "запросы уходят, 200 не приходит"
// на телефонной раздаче: при переключении вышки мобильный NAT перепривязывается, keep-alive
// сокеты становятся чёрными дырами, агент их переиспользует, и запрос уходит в никуда.
const CONNECT_TIMEOUT_MS  = Number(process.env.CONNECT_TIMEOUT_MS  || 15000);   // установка TCP+TLS
const IDLE_TIMEOUT_MS     = Number(process.env.IDLE_TIMEOUT_MS     || 90000);   // тишина в сокете
const UPSTREAM_TIMEOUT_MS = IDLE_TIMEOUT_MS;   // совместимость с расчётом бюджета ниже
const CREDIT_BUDGET_MS = CLIENT_TIMEOUT_MS - UPSTREAM_TIMEOUT_MS;   // 300с

// Пауза для 429/529/5xx и обрывов: ретраи бесконечны, но пауза растёт, чтобы
// не долбить апстрим каждые 2 секунды при затяжной недоступности.
const BUSY_DELAYS_MS = [2000, 2000, 4000, 8000, 15000, 30000];

// ±25% случайного разброса. Без него запросы, упавшие в одну миллисекунду, ретраятся
// тоже в одну миллисекунду — и бьют апстрим стадом. А падают они именно синхронно:
// обрыв туннеля, смена вышки и httpsAgent.destroy() убивают ВСЕ сокеты пула разом.
// Измерено в логе 2026-07-29 10:05:57Z: шесть «RETRY #1» в окне 18 мс, потому что
// шесть запросов упали вместе на 2 с раньше и отсчитали одинаковые ровно 2000 мс.
// Дальше стадо шло бы в ногу вечно: 2с, 2с, 4с, 8с — все шесть одновременно.
const busyDelay = (n) => {
  const base = BUSY_DELAYS_MS[Math.min(n, BUSY_DELAYS_MS.length - 1)];
  return Math.round(base * (0.75 + Math.random() * 0.5));
};

// ── CIRCUIT BREAKER НА ВХОДЕ ─────────────────────────────────────────────────
// Ложных срабатываний нет: 400 с текстом про баланс = баланс кончился, точка.
// Поэтому ПЕРВЫЙ же такой 400 закрывает гейт СРАЗУ ДЛЯ ВСЕХ, включая запросы,
// которые ещё не отправлялись. Пока гейт закрыт, наверх не уходит НИЧЕГО кроме
// одного пробника — остальные ждут в парковке, не тратя вызовы.
//
// Как только ХОТЬ ОДИН запрос получает 200 — гейт открывается и вся парковка
// уходит наверх разом. Без этого N субагентов отспали бы независимо и ударили
// одновременно, снова уронив лимит.
const creditGate = { blocked: false, probeActive: false, waiters: [], blockedAt: 0 };

function blockCreditGate() {
  if (creditGate.blocked) return;
  creditGate.blocked = true;
  creditGate.blockedAt = Date.now();
  log(`🔒 ГЕЙТ ЗАКРЫТ: баланс пуст — наверх пропускаю только пробник, остальных паркую`);
}

// creditsAlive=true — открылись потому что пришёл 200, кредиты реально живы.
// creditsAlive=false — пробник сдался, кредитов всё ещё нет. Разница важна:
// в первом случае парковку надо отправить наверх, во втором — гнать её наверх
// бессмысленно, она только получит те же 400 и снова закроет гейт (дребезг).
function releaseCreditGate(reason, creditsAlive = false) {
  if (!creditGate.blocked && !creditGate.waiters.length) return;
  const outage = creditGate.blockedAt ? Math.round((Date.now() - creditGate.blockedAt) / 1000) : 0;
  creditGate.blocked = false;
  creditGate.probeActive = false;
  creditGate.blockedAt = 0;
  const woken = creditGate.waiters;
  creditGate.waiters = [];
  log(`🔓 ГЕЙТ ОТКРЫТ (${reason}, простой ${outage}с) — выпускаю ${woken.length} запрос(ов)`);
  for (const wake of woken) wake(reason, false, creditsAlive);
}

// Отдать клиенту честный 400, когда ждать больше нельзя.
function answerCreditExhausted(res, waitedMs) {
  if (res.writableEnded || res.destroyed || res.headersSent) return;
  log(`⏸→💥 бюджет ${CREDIT_BUDGET_MS / 1000}с исчерпан в парковке (ждали ${Math.round(waitedMs / 1000)}с) — отдаю 400`);
  const b = Buffer.from(JSON.stringify({
    type: 'error',
    error: { type: 'invalid_request_error', message: 'Your credit balance is too low to access the Anthropic API.' }
  }), 'utf8');
  res.writeHead(400, { 'content-type': 'application/json', 'content-length': String(b.length) });
  res.end(b);
}

// Поставить запрос в парковку, НЕ отправляя его наверх.
function parkRequest(req, res, body, cleanUrl, sessionId, retryCount, creditRetry, arrivedAt) {
  let fired = false;
  const go = (why = 'разбужен', asProbe = false, creditsAlive = false) => {
    if (fired) return;
    fired = true;
    if (res.writableEnded || res.destroyed) return;

    const waited = Date.now() - arrivedAt;

    // Бюджет вышел и кредиты не доказаны живыми — отвечаем клиенту 400 и
    // наверх не идём. Если пришёл 200 (creditsAlive), пускаем даже на исходе
    // бюджета: шанс успеть есть, а терять готовый запрос глупо.
    if (waited >= CREDIT_BUDGET_MS && !creditsAlive) {
      answerCreditExhausted(res, waited);
      return;
    }
    log(`⏸→→ выпущен из парковки (${why})`);
    executeForward(req, res, body, cleanUrl, sessionId, retryCount, creditRetry, asProbe, arrivedAt);
  };
  creditGate.waiters.push(go);
  log(`⏸  в парковке без отправки наверх (в очереди ${creditGate.waiters.length})`);
  const left = Math.max(1000, CREDIT_BUDGET_MS - (Date.now() - arrivedAt));
  setTimeout(() => go('таймаут парковки'), left);
}

// Пробник ушёл (клиент отвалился) — забрать из парковки следующего, иначе
// гейт останется закрытым, а наверх не пойдёт вообще никто.
function promoteProbe() {
  if (!creditGate.blocked || creditGate.probeActive) return;
  const next = creditGate.waiters.shift();
  if (!next) return;
  log(`🔍 пробника нет — повышаю запрос из парковки (осталось ${creditGate.waiters.length})`);
  next('повышен в пробники', true);
}

const sessionKeyMap = new Map();
let fallbackKeyIndex = 0;

function getKeyForSession(sessionId, offset = 0) {
  if (!sessionId) {
    const idx = (fallbackKeyIndex + offset) % ENTERPRISE_KEYS.length;
    fallbackKeyIndex++;
    return { key: ENTERPRISE_KEYS[idx], keyNum: idx + 1 };
  }

  if (!sessionKeyMap.has(sessionId)) {
    const assignedIndex = sessionKeyMap.size % ENTERPRISE_KEYS.length;
    sessionKeyMap.set(sessionId, assignedIndex);
    log(`🔑 New Session [${sessionId.slice(0, 8)}...] bound to Enterprise Key #${assignedIndex + 1}`);
  }

  const baseIdx = sessionKeyMap.get(sessionId);
  const finalIdx = (baseIdx + offset) % ENTERPRISE_KEYS.length;
  return { key: ENTERPRISE_KEYS[finalIdx], keyNum: finalIdx + 1 };
}

const httpsAgent = new https.Agent({
  keepAlive: true,
  maxSockets: 100,        // НЕ снижаем: при 30+ сессиях это душит параллельность на проводе
  maxFreeSockets: 4,      // а вот простаивающих держим мало — именно они умирают в мобильной сети
  keepAliveMsecs: 5000,   // чаще TCP-пробы = быстрее обнаружение мёртвого канала
  timeout: IDLE_TIMEOUT_MS
});

// Сброс пула после подряд идущих сетевых сбоев. При смене вышки/IP ВСЕ сокеты мёртвые
// разом, и поштучные ретраи будут по очереди натыкаться на каждый. Дешевле выбросить пул.
let netFailStreak = 0;
function noteNetFailure(reason) {
  netFailStreak++;
  if (netFailStreak >= 2) {
    log(`🔄 ${netFailStreak} сетевых сбоя подряд (${reason}) — выбрасываю пул сокетов целиком`);
    try { httpsAgent.destroy(); } catch {}
    netFailStreak = 0;
  }
}

function log(...args) {
  process.stdout.write(`[${new Date().toISOString()}] ${args.join(' ')}\n`);
}

function decodeGzip(buf, cb) {
  zlib.gunzip(buf, (err, result) => cb(err ? buf.toString('utf8') : result.toString('utf8')));
}

// ── FORWARD REQUEST WITH RETRY ───────────────────────────────────────────────

function executeForward(req, res, body, cleanUrl, extractedSessionId, retryCount = 0, creditRetry = 0, isProbe = false, arrivedAt = Date.now()) {
  // ── ПРОВЕРКА НА ВХОДЕ ──────────────────────────────────────────────────────
  // Гейт закрыт → наверх не идём вообще. Пропускаем только пробник.
  if (creditGate.blocked && !isProbe) {
    return parkRequest(req, res, body, cleanUrl, extractedSessionId, retryCount, creditRetry, arrivedAt);
  }

  const upHeaders = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (k === 'host' || k === 'content-length') continue;
    if (k === 'x-claude-code-session-id') continue;
    upHeaders[k] = v;
  }

  upHeaders['host']           = 'agentrouter.org';
  upHeaders['content-length'] = String(body.length);

  const { key: assignedKey, keyNum } = getKeyForSession(extractedSessionId, retryCount);
  upHeaders['x-api-key']      = assignedKey;
  upHeaders['authorization']  = `Bearer ${assignedKey}`;
  upHeaders['user-agent']     = 'codex_cli_rs/0.101.0';
  upHeaders['originator']     = 'codex_cli_rs';

  log(`→ ${req.method} ${cleanUrl} (Key #${keyNum}${retryCount > 0 ? ` RETRY #${retryCount}` : ''}${creditRetry > 0 ? ` CREDIT #${creditRetry}` : ''}) effort=max`);

  const upUrl = new URL(cleanUrl, UPSTREAM);
  const upReq = https.request({
    hostname: upUrl.hostname,
    port:     443,
    path:     upUrl.pathname + (upUrl.search || ''),
    method:   req.method,
    headers:  upHeaders,
    agent:    httpsAgent,
    timeout:  CONNECT_TIMEOUT_MS
  }, (upRes) => {
    log(`↑ HTTP ${upRes.statusCode}`);

    // Бесконечный ретрай на перегрузку апстрима. 400 здесь НЕТ намеренно:
    // кривой запрос кривой на любом ключе, а credit-400 лечится ожиданием ниже.
    if (upRes.statusCode === 429 || upRes.statusCode === 529 || upRes.statusCode >= 500) {
      const wait = busyDelay(retryCount);
      log(`⚠ Upstream HTTP ${upRes.statusCode} (Overloaded/Busy). Повтор через ${wait / 1000}с (попытка #${retryCount + 1})...`);
      upRes.resume();   // вычитать тело, иначе keep-alive сокет не освободится
      setTimeout(() => {
        if (res.writableEnded || res.destroyed) return;
        executeForward(req, res, body, cleanUrl, extractedSessionId, retryCount + 1, creditRetry, isProbe, arrivedAt);
      }, wait);
      return;
    }

    if (upRes.statusCode !== 200) {
      const parts = [];
      upRes.on('data', c => parts.push(c));
      upRes.on('end', () => {
        const buf = Buffer.concat(parts);
        const isGzip = upRes.headers['content-encoding'] === 'gzip';
        const decode = isGzip ? decodeGzip : (b, cb) => cb(b.toString('utf8'));
        decode(buf, text => {

          // 400 + "credit balance too low" — по факту ошибка временная, хотя
          // статус-код объявляет её постоянной. Держим запрос и повторяем.
          if (upRes.statusCode === 400 && CREDIT_RE.test(text)) {
            blockCreditGate();   // закрывает для ВСЕХ, в том числе ещё не отправленных

            const wait = CREDIT_DELAYS_MS[creditRetry];
            const waited = Date.now() - arrivedAt;

            // Дедлайн: не ждать дольше, чем клиент готов терпеть.
            if (creditRetry >= CREDIT_DELAYS_MS.length || waited + wait > CREDIT_BUDGET_MS) {
              const why = creditRetry >= CREDIT_DELAYS_MS.length
                ? `${CREDIT_DELAYS_MS.length} попыток исчерпано`
                : `бюджет ${CREDIT_BUDGET_MS / 1000}с исчерпан (ждём уже ${Math.round(waited / 1000)}с)`;
              log(`💳 сдаюсь: ${why} — отдаю 400 этому клиенту`);
              // Гейт НЕ открываем: кредитов по-прежнему нет, гнать парковку
              // наверх бессмысленно (получит те же 400 и закроет гейт снова).
              // Вместо этого передаём пробу следующему — пульс сохраняется,
              // а припаркованные ждут своих бюджетов или прихода 200.
              creditGate.probeActive = false;
              promoteProbe();
            } else if (!creditGate.probeActive) {
              // Этот запрос — пробник. Ждёт окно один за всех и идёт наверх
              // с isProbe=true, чтобы пройти проверку на входе.
              creditGate.probeActive = true;
              log(`🔍 ПРОБНИК: жду ${Math.round(wait / 1000)}с (попытка ${creditRetry + 1}/${CREDIT_DELAYS_MS.length}, прошло ${Math.round(waited / 1000)}с)`);
              setTimeout(() => {
                creditGate.probeActive = false;
                if (res.writableEnded || res.destroyed) {
                  log(`🔍 пробник: клиент отвалился — снимаю пробу`);
                  promoteProbe();   // эстафету надо передать, иначе никто не пойдёт
                  return;
                }
                executeForward(req, res, body, cleanUrl, extractedSessionId, retryCount, creditRetry + 1, true, arrivedAt);
              }, wait);
              return;
            } else {
              // Пробник уже есть — этот в парковку, наверх не пойдёт.
              parkRequest(req, res, body, cleanUrl, extractedSessionId, retryCount, creditRetry, arrivedAt);
              return;
            }
          }

          log(`💥 ERROR ${upRes.statusCode}: ${text.slice(0, 400)}`);
          if (!res.headersSent) {
            const outBuf = Buffer.from(text, 'utf8');
            const headers = { ...upRes.headers };
            delete headers['content-encoding'];
            headers['content-length'] = String(outBuf.length);
            res.writeHead(upRes.statusCode, headers);
            res.end(outBuf);
          }
        });
      });
    } else {
      // 200 доказывает, что кредиты живы — будим всё, что в парковке.
      releaseCreditGate('200 OK', true);
      res.writeHead(upRes.statusCode, upRes.headers);
      upRes.pipe(res);
      upRes.on('error', err => log(`⚠ upRes pipe error: ${err.message}`));
    }
  });

  // Пока сокет ещё не установлен — жёсткий connect-таймаут. Как только соединение есть,
  // переключаемся на таймаут неактивности: при стриминге байты идут непрерывно, поэтому
  // долгая тишина означает мёртвый канал, а не долгий ответ.
  upReq.on('socket', (sock) => {
    if (sock.connecting) {
      sock.setTimeout(CONNECT_TIMEOUT_MS);
      sock.once('connect', () => sock.setTimeout(IDLE_TIMEOUT_MS));
    } else {
      sock.setTimeout(IDLE_TIMEOUT_MS);   // переиспользованный keep-alive: события 'connect' не будет
    }
  });

  // БЕЗ ЭТОГО обработчика опция timeout бесполезна: Node испускает событие и ждёт дальше.
  // destroy(err) уходит в обработчик 'error' ниже, а тот уже умеет ретраить с backoff.
  upReq.on('timeout', () => {
    const phase = upReq.socket && upReq.socket.connecting ? 'установка соединения' : 'тишина в сокете';
    log(`⏱ таймаут (${phase}) — сокет мёртв, обрываю и повторяю`);
    // noteNetFailure здесь НЕ вызываем: destroy(err) уходит в обработчик 'error', и счёт
    // ведётся там. Иначе один сбой считался бы дважды и пул сбрасывался на каждом.
    upReq.destroy(new Error('socket timeout'));
  });

  upReq.on('error', err => {
    // Обрыв связи тоже ретраится бесконечно, как и 5xx. В v3.1 здесь стоял
    // лимит retryCount < 2, но 5xx-ветка крутит тот же счётчик без предела —
    // из-за чего после двух busy-повторов любой обрыв сразу отдавал 502.
    const wait = busyDelay(retryCount);
    if (/ECONNRESET|ETIMEDOUT|EPIPE|ENETUNREACH|EHOSTUNREACH|socket timeout|ECONNREFUSED/i.test(err.message || '')) {
      noteNetFailure(err.message);
    }
    log(`✗ upstream error (Key #${keyNum}): ${err.message} → повтор через ${wait / 1000}с`);
    setTimeout(() => {
      if (res.writableEnded || res.destroyed) return;
      executeForward(req, res, body, cleanUrl, extractedSessionId, retryCount + 1, creditRetry, isProbe, arrivedAt);
    }, wait);
  });

  upReq.write(body);
  upReq.end();
}

// ── SERVER ───────────────────────────────────────────────────────────────────

const server = http.createServer((req, res) => {
  res.on('error', err => log(`⚠ client res error: ${err.message}`));

  // Healthcheck for extension
  if ((req.method === 'HEAD' || req.method === 'GET') && (req.url === '/' || req.url === '')) {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end();
    return;
  }

  const chunks = [];
  req.on('data', c => chunks.push(c));
  req.on('end', () => {
    const rawBody = Buffer.concat(chunks);
    const isMessages = req.url === '/v1/messages' || req.url.startsWith('/v1/messages?');
    const cleanUrl = req.url.replace(/^\/v1\/messages\?beta=true(&.*)?$/, '/v1/messages');

    let body = rawBody;
    let extractedSessionId = null;

    if (isMessages && rawBody.length > 0) {
      try {
        const obj = JSON.parse(rawBody.toString('utf8'));

        let totalChars = 0;
        if (Array.isArray(obj.messages)) {
          for (const m of obj.messages) {
            if (typeof m.content === 'string') totalChars += m.content.length;
            else if (Array.isArray(m.content)) {
              for (const p of m.content) {
                if (p.text) totalChars += p.text.length;
              }
            }
          }
        }
        const approxTokens = Math.round(totalChars / 4);
        log(`📦 req: model=${obj.model} stream=${obj.stream} | msgs=${obj.messages?.length || 0} context=~${approxTokens.toLocaleString()} tokens`);

        const incomingModel = obj.model;
        obj.model = 'claude-opus-5';
        if (incomingModel !== 'claude-opus-5') {
          log(`⚙  model forced: "${incomingModel}" → "claude-opus-5"`);
        }

        obj.thinking = { type: 'adaptive' };

        if (!obj.output_config) obj.output_config = { effort: 'max' };
        else obj.output_config.effort = 'max';

        // Форсируем достаточный запас токенов вывода для глубокого рассуждения (Ultracode - 32k)
        obj.max_tokens = Math.max(obj.max_tokens || 0, 32768);

        log(`✓ adaptive thinking & effort=max forced (max_tokens=${obj.max_tokens})`);

        if (obj.metadata) {
          if (obj.metadata.user_id) {
            try {
              const uid = JSON.parse(obj.metadata.user_id);
              if (uid.session_id) extractedSessionId = uid.session_id;
            } catch {}
          }
          log(`⚙  metadata stripped unconditionally (${JSON.stringify(obj.metadata)})`);
          delete obj.metadata;
        }

        body = Buffer.from(JSON.stringify(obj), 'utf8');
      } catch (e) {
        log(`⚠  json parse err: ${e.message}`);
      }
    }

    if (!extractedSessionId && req.headers['x-claude-code-session-id']) {
      extractedSessionId = req.headers['x-claude-code-session-id'];
    }

    executeForward(req, res, body, cleanUrl, extractedSessionId, 0);
  });
});

server.listen(PROXY_PORT, '127.0.0.1', () => {
  log(`=======================================================`);
  log(`🚀 claude-proxy v3.2 BULLETPROOF on http://127.0.0.1:${PROXY_PORT}`);
  log(`   Enterprise Keys Pool : ${ENTERPRISE_KEYS.length} key(s) active`);
  log(`   Sticky Session Cache : Enabled (100% Hit Rate)`);
  log(`   Busy Retry 429/5xx   : Infinite, backoff ${BUSY_DELAYS_MS.map(d => d / 1000).join('/')}с`);
  log(`   Credit-400 Gate      : ${CREDIT_DELAYS_MS.map(d => Math.round(d / 1000) + 'с').join(' → ')}  (circuit breaker на входе)`);
  log(`   Wait Budget          : ${CREDIT_BUDGET_MS / 1000}с из ${CLIENT_TIMEOUT_MS / 1000}с клиентского таймаута`);
  log(`   Timeouts             : connect ${CONNECT_TIMEOUT_MS / 1000}с / idle ${IDLE_TIMEOUT_MS / 1000}с  (mobile-safe)`);
  log(`   Socket Pool          : maxSockets 100, maxFreeSockets 4, keepAlive 5с`);
  log(`   Effort Level         : max (Forced)`);
  log(`   Output Tokens        : >= 32768 (Forced)`);
  log(`=======================================================\n`);
});

process.on('uncaughtException', err => log(`🛡 Uncaught Exception intercepted: ${err.message}`));
process.on('unhandledRejection', err => log(`🛡 Unhandled Rejection intercepted: ${err.message}`));

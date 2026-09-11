# ai-news-bot — личный новостной бот по ИИ и разработке

Присылает в Telegram актуальные новости, анонсы и события из мира технологий и
искусственного интеллекта. Каждую новость можно оценить кнопками — бот
подстраивает отбор под тебя.

Только стандартная библиотека Python (3.9+). Никаких `pip install`, никаких
подписок и платных API: единственная необязательная платная часть — DeepSeek,
и она укладывается в бесплатный стартовый грант.

---

## 1. Быстрый старт

```bash
cd ~/ai-news-bot

# 1) токен бота
cp .env.example .env
#   впиши TELEGRAM_BOT_TOKEN=... (получить: @BotFather -> /newbot)
chmod 600 .env

# 2) узнай chat_id: открой бота в Telegram и нажми Start, затем:
python3 bot.py whoami

# 3) проверь, что источники живы
python3 bot.py check

# 4) запусти
python3 bot.py run          # или ./run.sh
```

При первом запуске бот не вываливает бэклог: он помечает всё старое как
прочитанное и пишет «Бот подключён». Дальше присылает только новое.

---

## 2. Команды

| Команда | Что делает |
|---|---|
| `/status` | состояние, лимиты, источники, которые молчат |
| `/sources` | список источников и их здоровье |
| `/last` | последние 10 отправленных новостей |
| `/digest` | собрать и прислать прямо сейчас |
| `/pause`, `/resume` | пауза и возобновление |
| `/mute 2` | тишина 2 часа |
| `/help` | справка |

Кнопки под новостью: **👍 Полезно**, **👎 Не то**, **🔗 Источник**.

---

## 3. Как это не спамит

Три уровня:

1. **Часовой бюджет.** `max_per_hour` (по умолчанию 2) и `min_gap_minutes`
   (по умолчанию 20) — между сообщениями есть пауза, даже если новостей много.
2. **Порог балла.** Всё, что ниже `min_score`, не отправляется вообще.
3. **Breaking-перебивка.** Если новость набрала `breaking_score` и содержит
   событийное слово (`released`, `announces`, `open-source`, `представил`…),
   она пробивает часовой бюджет — но в своём лимите `breaking_max_per_hour`,
   чтобы один релиз не превратился в поток.

Очередь живёт `queue_ttl_hours` часов: устаревшее не приходит «пачкой задним
числом», новость должна быть новостью.

---

## 4. Как отбираются новости

Двухступенчатый отбор — это главное решение архитектуры:

```
сбор (30+ источников)
  → дедуп (по URL и по нормализованному заголовку)
  → дешёвый эвристический скоринг  ← бесплатно, по всем записям
  → шортлист (llm_min_heuristic и llm_max_per_cycle)
  → DeepSeek судит и пишет по-русски ← только по шортлисту
  → бюджет / breaking
  → доставка + кнопки оценки
```

Почему так: эвристика бесплатна и работает по всем записям, а модель вызывается
только там, где эвристика уже видит потенциал. На типичном объёме это
10–20 вызовов в сутки.

Скоринг учитывает: вес источника, свежесть, слова-сигналы (релиз, прорыв,
покупка), сущности (OpenAI, Anthropic, DeepSeek, Qwen…), вовлечённость
(лайки, очки HN, апвоты HF) и штрафы за рекламу, вакансии, вебинары, мемы.

**Обучение на оценках.** Каждое 👍/👎 сдвигает вес источника и вес ключевых
терминов этой новости. Через пару недель отбор заметно смещается в твою сторону.
Посмотреть накопленное: `python3 bot.py stats`.

---

## 5. X / Twitter: три бесплатных канала вместо одного

X — самый дорогой источник: официальный API с февраля 2026 полностью
pay-per-use, бесплатного тира для новых разработчиков нет. Поэтому работают
несколько независимых каналов, и ни один из них не требует аккаунта, cookies
или сессионных токенов.

**Канал 1 — майнер ссылок из Telegram (`x_miner`).** Самый дешёвый: ссылки на
посты X лежат в уже скачанных страницах Telegram-каналов, то есть запросов к X
на обнаружение — ноль. Пост гидратируется через слой контента и попадает в
общий отбор. Проверено: 5 постов из 4 каналов за один проход.

**Канал 2 — Bluesky (`bsky_*`).** Не обход X, а легальная замена для тех, кто
там дублируется: `karpathy.bsky.social`, `emollick.bsky.social`,
`natolambert.bsky.social`, `arankomatsuzaki.bsky.social`, а также официальные
аккаунты theverge.com, techcrunch.com, microsoft.com, github.com. Публичный API
AT Protocol бесплатный, без ключей и лимитов.

**Канал 3 — зеркала (`x_user`).** `twiiit.com` работает, но режет по IP, поэтому
между запросами держится пауза `x_politeness_delay_seconds`, а маршрут на
30 минут уходит в cooldown после 403/429. Важное: его успешный вызов отдаёт
реальную ленту, поэтому он остаётся в схеме как резерв.

**Слой контента (по ID поста) — это решённая задача:**
`cdn.syndication.twimg.com` (собственный embed-бэкенд X) → `api.fxtwitter.com`
→ `api.vxtwitter.com`. Работает независимо от того, каким каналом найден ID.

Что осталось заблокированным и как это включить: `xcancel` отдаёт валидный RSS,
но требует вайтлисты ридера — одно бесплатное письмо на `rss@xcancel.com` с ID,
который показывает `python3 bot.py xdiag`. После этого X получает четвёртый,
самый стабильный канал. Все мёртвые зеркала Nitter (privacyredirect, tiekoetter,
nitter.net и другие) оставлены в списке маршрутов: они бесплатны и иногда
оживают, а cooldown не даёт им тормозить цикл.

Никаких аккаунтов, cookies, прокси и пулов сессий — принципиально: именно за это
X судится с зеркалами, и именно на этом банят аккаунты.

---

## 6. DeepSeek

```bash
python3 bot.py models    # список id моделей для твоего ключа
python3 bot.py stats     # расход токенов за сегодня
```

Модель и базовая URL берутся из `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL`.
`thinking` отключён — для классификации это лишние деньги.

Экономика: на новом аккаунте DeepSeek даёт бесплатный грант токенов; при
шортлисте 10–20 вызовов в сутки он расходуется медленно, а дальше это центы в
месяц. Без ключа бот работает на эвристике — просто без русских пересказов и
без LLM-вердикта.

---

## 7. Постоянная работа

### macOS (launchd)

```bash
cat > ~/Library/LaunchAgents/ai.newsbot.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.newsbot</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>/Users/CHANGE_ME/ai-news-bot/bot.py</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/CHANGE_ME/ai-news-bot</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/newsbot.out</string>
  <key>StandardErrorPath</key><string>/tmp/newsbot.err</string>
</dict></plist>
PLIST
launchctl load ~/Library/LaunchAgents/ai.newsbot.plist
```

### Только cron (режим без кнопок)

Кнопки оценки требуют живого процесса (long polling). Если нужен именно cron,
используй `once` — доставка будет работать, оценки придётся отключить:

```
*/20 * * * * cd /PATH/ai-news-bot && ./run-once.sh >> /tmp/newsbot.log 2>&1
```

---

## 8. Управление источниками

`sources.json` — просто JSON. Поля: `name`, `type`, `group`, `weight`,
`enabled`, плюс параметры типа (`url`, `channel`, `handle`, `limit`).

Типы: `rss`, `hn_front_page`, `hn_show`, `hf_daily_papers`,
`hf_trending_models`, `telegram_web`, `x_user`.

Добавил источник — проверь: `python3 bot.py check --source ИМЯ`.
Отключить: `"enabled": false`.

Почему Rust-ленты нет в списке по умолчанию: Reddit `.rss` в этом окружении
недоступен (пустой ответ). Проверь у себя: если открывается, добавь
`"type": "rss"`, `"url": "https://www.reddit.com/r/LocalLLaMA/.rss"`.

---

## 9. Диагностика

```bash
python3 bot.py check                    # что отвечает, что молчит
python3 bot.py stats                    # состояние, веса, расход LLM
python3 bot.py xdiag                    # маршруты обнаружения X + ID для вайтлисты
python3 bot.py explain "OpenAI released a new model" --source openai_news
python3 bot.py once --dry-run --top 15  # что БЫ отправилось (ничего не шлёт)
python3 bot.py test-send                # проверка доставки
```

Частые проблемы:

- **Ничего не приходит.** Норма на старте: порог `min_score` может быть высоким
  для узкого набора источников. Посмотри `once --dry-run --top 15` и снизь
  `min_score` в `config.json`.
- **`chat_id is unknown`.** Не нажал Start в боте; сделай `python3 bot.py whoami`.
- **Источник в «молчат».** Смотри `/status`. RSS-ленты ломаются редко, зеркала X —
  регулярно; это ожидаемо.
- **Токен утёк.** `@BotFather` → `/revoke` → новый токен в `.env`.

---

## 10. Файлы

```
bot.py                 CLI: run / once / check / whoami / stats / models / explain
config.json            лимиты, пороги, интервалы
sources.json           реестр источников
newsbot/http.py        HTTP с ретраями, gzip, ETag
newsbot/feeds.py       RSS/Atom, HN, Hugging Face, Telegram-превью
newsbot/x_sources.py   X: каскад обнаружения и контента
newsbot/store.py       SQLite: записи, здоровье источников, веса обучения
newsbot/score.py       эвристический скоринг и детект «большой новости»
newsbot/llm.py         DeepSeek: судья + русский пересказ, кэш и учёт токенов
newsbot/render.py      карточка новости, кнопки, страницы статуса
newsbot/telegram.py    Bot API
newsbot/pipeline.py    сбор → дедуп → скоринг → суд → бюджет → доставка
newsbot/runner.py      живой цикл: опрос источников + кнопки + команды
state/news.db          база состояния (не коммитить)
```

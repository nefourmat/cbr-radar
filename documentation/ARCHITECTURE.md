# ЦБ-Радар · Архитектура системы

## Обзор

```
Источники данных           Парсеры              API              Клиенты
──────────────             ────────             ───              ────────
ЦБ РФ (cbr.ru)     ──►   gcurve.py    ──►
Минфин (minfin.ru)  ──►   minfin.py    ──►    main.py   ──►    Telegram Mini App
ЦБ РФ Form 101      ──►   form101.py   ──►    FastAPI         Telegram Bot
MOEX ISS API        ──►   (inline)     ──►                    Внешние клиенты
```

---

## Парсеры

### 1. `parsers/gcurve.py` — G-кривая ЦБ

**Источник:** `https://www.cbr.ru/hd_base/zcyc_params/zcyc/`  
**Метод:** POST-запрос с датой  
**Формат ответа:** JSON с массивом `{term, value}`

**Что парсит:**
- Доходности ОФЗ по срокам (0.25, 0.5, 0.75, 1, 2, 3, 5, 7, 10, 15, 20, 30 лет)
- Ключевую ставку ЦБ (отдельный запрос к `/hd_base/KeyRate/`)

**Ключевые функции:**
```
get_key_rate()          → float | None      КС в % (14.5)
get_last_gcurve()       → (DataFrame, str)  последняя кривая + дата
get_gcurve(date_str)    → DataFrame | None  кривая на конкретную дату
get_history()           → DataFrame         история (из файла)
```

**Вычисляемые сигналы:**
- `exp_cut = key_rate - min(yield)` — ожидаемое снижение КС
- `slope_2_10 = y10 - y2` — наклон кривой

**Fallback:** При сетевой ошибке возвращает `None` (не бросает исключение)

---

### 2. `parsers/minfin.py` — Аукционы Минфина

**Источник:** `https://minfin.gov.ru/.../ofz/auction/`  
**Метод:** Скачиваем XLSX файл, парсим pandas

**Два формата заголовков:**
| Год | Колонка даты |
|-----|-------------|
| 2024+ | `"Дата"` |
| 2021–2023 | `"Дата аукциона"` |

**Ключевые функции:**
```
get_latest_file_url()   → str       URL последнего годового файла
download_xlsx(url)      → BytesIO   скачиваем в память
parse_auctions(file)    → DataFrame парсим любой формат
analyze_auctions(df)    → None      печать сигнала (Минто)
```

**Вычисляемые поля:**
- `bid_to_cover = спрос_млн / предложение_млн`
- `лет_до_погашения = дней_до_погашения / 365`

**Логика парсинга:**
1. `pd.ExcelFile` → берём первый лист (не зависим от имени)
2. `header=5` — первые 5 строк заголовок/шапка файла Минфина
3. `COLUMNS_MAP` — переименовываем в унифицированные имена
4. Фильтруем: только строки с датой > 2020 и спросом > 0

---

### 3. `parsers/form101.py` — Форма 101 банков

**Источник:** `https://www.cbr.ru/vfs/credit/forms/101-YYYYMMDD.rar`  
**Формат:** RAR-архив с DBF файлами (Положение 809-П)

**Зависимости:**
- `7-Zip` — распаковка RAR (Linux: `p7zip-full`, Windows: `C:\Program Files\7-Zip\7z.exe`)
- `dbfread` — чтение DBF файлов
- `rarfile` — работа с RAR архивом

**Файлы внутри архива:**
| Файл | Содержимое |
|------|-----------|
| `MMYYYYB1.dbf` | Балансы по счетам (основной) |
| `N1.dbf` | Справочник банков (REGN → NAME_B) |

**Счета долговых ЦБ (Положение 809-П):**
| Счёт | Категория |
|------|-----------|
| 501 | FVPL — по справедливой стоимости через прибыль/убыток |
| 502 | FVOCI — по справедливой стоимости через прочий доход |
| 504 | AC — амортизированная стоимость (длинная позиция) |

**URL формат:**
```
Данные за апрель 2026 → файл 101-20260501.rar
(первое число следующего месяца)
```

**Ключевые функции:**
```
download_and_extract(url, sevenzip) → tmp_dir  скачать и распаковать
parse_b1(b1_path)                   → DataFrame позиции по счетам
read_bank_names(n1_path)            → dict      REGN → name
```

**BANKS_FALLBACK:** хардкод имён банков которых нет в `N1.dbf`:
```python
{354: "ГАЗПРОМБАНК", 436: "БАНК СПб", 121: "ПРОМСВЯЗЬБАНК", ...}
```

---

### 4. `parsers/inflation.py` — Инфляция и реальная ставка

**Источник:** `https://www.cbr.ru/hd_base/infl/`
**Метод:** GET HTML-таблицы (кодировка `windows-1251`)
**Колонки:** `Дата (MM.YYYY) | Ключевая ставка | Инфляция % г/г | Цель`

**Ключевые функции:**
```
get_inflation_data()            → list[dict] | None   строки (новые сверху)
build_inflation_signal(rows)    → dict                сигнал для главного экрана
```

**Вычисляемые сигналы:**
- `real_rate = key_rate − infl_yoy` — реальная ставка (жёсткость ДКП)
- `gap_vs_target = infl_yoy − target` — отклонение от цели 4%
- `trend_3m = инфляция_тек − 3мес назад` — < 0: дезинфляция
- `status`: `bull` (дезинфляция + жёсткая ДКП), `warn` (ускорение), `neu`

**Fallback:** При сетевой ошибке → `None` (не бросает исключение).
Кэш: `data/inflation_latest.json`.

---

### 5. `parsers/inflation_expectations.py` — Наблюдаемая инфляция (инФОМ)

**Источник:** `https://www.cbr.ru/analytics/dkp/inflationary_expectations/`
**Файлы:** `Infl_exp_YY-MM.xlsx` (ежемесячный опрос инФОМ, новый сверху)
**Лист:** `Данные за все годы` — медианные оценки по месяцам.

«Реальная» (ощущаемая населением) инфляция — обычно в 2–3× выше официальной.

**Ключевые функции:**
```
get_latest_xlsx_url()         → str | None
parse_expectations(xlsx)      → dict | None   {observed, expected, ...histories}
get_inflation_expectations()  → dict | None   полный цикл
```

**Поля сигнала (мерджатся в inflation):**
- `observed` — наблюдаемая инфляция (что ощущает население)
- `expected` — ожидаемая инфляция (на год вперёд)
- `gap_vs_official = observed − infl_yoy` — разрыв доверия Росстат vs восприятие

**Fallback:** опционально; при ошибке инфляционный сигнал отдаётся без инФОМ.

---

## Аналитические скрипты

### `scripts/cbr_probabilities.py` — Вероятности заседаний ЦБ

**Методология:** G-кривая → implied forward rates → логистическая функция

```
spot_rate(T) = G-кривая с якорем в КС при T=0
forward_rate(t1,t2) = ((1+r2)^t2 / (1+r1)^t1)^(1/(t2-t1)) - 1
prob_cut = 0.15 + sigmoid(k*(implied_cut - x0)) * 0.80
```

**Калибровка логистической функции:**
- 0 бп → 20% (базовая вероятность)
- 50 бп → 60%
- 100 бп → 82%
- 150+ бп → 95%

**Выходной файл:** `data/cbr_probabilities.json`

---

### `scripts/bond_screener.py` — Скринер ОФЗ

**Источник данных:** MOEX ISS API (бесплатно, без авторизации)  
**URL:** `https://iss.moex.com/iss/engines/stock/markets/bonds/boards/TQOB/securities.json`

**Фильтры:**
- SECID начинается с `SU26` (ОФЗ)
- Срок до погашения > 3 лет
- Фиксированный купон > 0%
- Есть рыночная цена (LAST > 0)

**Вычисления:**
```
YTM:      метод Ньютона-Рафсона (100 итераций, точность 0.0001)
Duration: дюрация Маколея (полугодовые купоны)
P&L:      -Duration × Δyield × price + coupon_income
```

**Supply Overhang поправка:**
```
supply_pressure = 1 - (BTC_current / BTC_normal)   # BTC_normal = 1.5×
pass_through    = 1 - supply_pressure × 0.5
adj_yield_cut   = cut_bps × pass_through
```

**Выходной файл:** `data/bond_screener.json`

---

### `scripts/refresh_data.py` — Автообновление данных

**Запуск:** ежедневно в 08:00 (Railway APScheduler) или вручную

**Шаги:**
1. G-кривая + КС → `data/gcurve_latest.json`
2. Аукционы → `data/auctions_latest.json`
3. Вероятности ЦБ → `data/cbr_probabilities.json`
4. Скринер ОФЗ → `data/bond_screener.json`
5. Form 101 — только если кэш старше 25 дней
6. Дайджест → `data/digest_latest.txt`
7. Инвалидация кэша `/api/overview`

**Логирование:** `logging` + уровни INFO/ERROR. При ошибках: `exit(1)`.

---

## Telegram-бот (`bot.py`)

PTB 21 (async). Получает данные через HTTP `GET {API_BASE}/api/overview`
(не дёргает пайплайн напрямую — нет гонок с инвалидацией кэша).

**Команды** (видны в меню бота через `set_my_commands`):
`/start` `/pulse` `/calendar` `/signal` `/digest` `/settings`.

**Подписки на уведомления** (`core/subscriptions.py`):
хранилище `data/subscribers.json` (`{chat_id: {subs: {key: bool}}}`, атомарная запись).
Типы: ежедневный пульс, заседания ЦБ, сигнал входа, аукционы, инфляция, недельный дайджест.
`/settings` — inline-клавиатура с тумблерами (CallbackQuery, ✅/⬜️, правка сообщения на месте).

**Рассылки** (`broadcast()`): троттлинг ~20 msg/s; при `Forbidden` (бот заблокирован)
подписчик удаляется. Канал (`CHANNEL_ID`) — опционально, дублирует рассылку.

**Планировщик (APScheduler, МСК):**
| Job | Когда | Кому |
|-----|-------|------|
| `daily_pulse` | 09:00 ежедневно | подписка `daily_pulse` |
| `meeting_reminders` | 09:05 ежедневно (за 3/1/0 дн) | `meetings` |
| `inflation_check` | 12:00 ежедневно (при новом периоде) | `inflation` |
| `weekly_digest` | пт 09:00 | `weekly_digest` + канал |
| `auction_alert` | ср 13:10 | `entry_signal`/`auctions` + канал |

`data/notify_state.json` — дедуп (например, период инфляции, о котором уже уведомили).

**Календарь событий** (`core/events.py`): заседания ЦБ (точные даты из
`cbr_probabilities.CBR_MEETINGS`) + резюме обсуждения + еженедельные аукционы/инфляция
+ ежемесячные публикации (ИПЦ, инФОМ, Форма 101). Точные события — `confirmed=True`,
периодические — «(ожидается)».

**Ежедневный пульс** (`core/pulse.py`): компактная утренняя сводка
(режим, КС, 4 сигнала, идея, ближайшее событие, вердикт).

**Переменные окружения бота:** `BOT_TOKEN`, `WEBAPP_URL`, `API_BASE` (по умолчанию = WEBAPP_URL),
`CHANNEL_ID` (опц.).

**Настройка в @BotFather (вручную):** задать описание/about, картинку, и
кнопку Menu → Web App на `WEBAPP_URL` (Bot Settings → Menu Button).

> ⚠️ **Важно:** `data/subscribers.json` лежит на файловой системе сервиса бота.
> На Railway ФС эфемерна — при редеплое подписки сбрасываются. Для прод-надёжности
> нужен персистентный том или БД (Supabase) — см. «Известные ограничения».

---

## API (FastAPI)

**Base URL:** `https://cbr-radar-production.up.railway.app`

| Endpoint | Кэш | Описание |
|----------|-----|----------|
| `GET /` | — | Mini App HTML |
| `GET /health` | — | Healthcheck `{"status":"ok"}` |
| `GET /api/overview` | 1 час | Все сигналы для главного экрана |
| `GET /api/meetings` | 6 часов | Вероятности заседаний ЦБ |
| `GET /api/screener` | 6 часов | Скринер 24 ОФЗ |
| `GET /api/banks` | 24 часа | Умные деньги (Form 101) |
| `GET /api/inflation` | 24 часа | Инфляция + реальная ставка (КС − ИПЦ) |
| `GET /api/calendar?days=N` | — | Календарь событий ДКП (N≤120) |
| `GET /api/digest` | — | Текстовый дайджест |

**Кэширование:** JSON файлы в `data/`. `read_cache(filename, max_age_hours)` проверяет mtime файла.

---

## Структура файлов

```
cbr_radar/
├── parsers/
│   ├── gcurve.py          G-кривая ЦБ
│   ├── minfin.py          Аукционы Минфина
│   └── form101.py         Форма 101 банков
├── scripts/
│   ├── refresh_data.py    Автообновление (Railway scheduler)
│   ├── cbr_probabilities.py Вероятности заседаний
│   ├── bond_screener.py   Скринер ОФЗ
│   ├── build_form101_history.py История Form 101
│   ├── build_history.py   История G-кривой и аукционов
│   ├── backtest_form101.py Бэктест гипотезы
│   └── pattern_engine.py  Поиск исторических паттернов
├── tests/
│   ├── conftest.py
│   ├── test_gcurve.py     26 тестов → 26/26 ✓
│   ├── test_minfin.py
│   └── test_api.py
├── data/
│   ├── gcurve_latest.json      Последняя G-кривая
│   ├── auctions_latest.json    Последние аукционы
│   ├── cbr_probabilities.json  Вероятности заседаний
│   ├── bond_screener.json      Скринер ОФЗ
│   ├── form101_latest.csv      Последняя Form 101
│   ├── form101_history.csv     История 35 мес
│   ├── form101_signal.json     Стрик накопления
│   ├── hypotheses.json         Открытые гипотезы
│   ├── backtest_results.json   Результаты бэктеста
│   ├── api_overview.json       Кэш /api/overview
│   └── digest_latest.txt       Последний дайджест
├── static/
│   └── index.html         Telegram Mini App
├── main.py                FastAPI бэкенд
├── bot.py                 Telegram Bot
├── digest.py              Генератор дайджеста
├── railway.toml           Deploy config (FastAPI)
├── nixpacks.toml          Linux build (7-zip)
├── requirements.txt       Зависимости
└── pytest.ini             Конфиг тестов
```

---

## Известные ограничения

| Ограничение | Причина | Статус |
|-------------|---------|--------|
| Form 101 только с мая 2023 | ЦБ не хранит старые RAR | Принято |
| 7-Zip требует установки | RAR формат без OSS-декодера | nixpacks.toml |
| Pass-through коэффициент 0.5 | Оценочный, не калиброванный | TODO |
| Нет PostgreSQL | Всё в CSV/JSON файлах | TODO (Supabase) |
| Нарратив хардкод | Нет LLM генерации | TODO (Anthropic API) |

---

## Тесты

```bash
pytest                          # все тесты
pytest tests/test_api.py        # только API
pytest -m "not integration"     # без сетевых тестов
pytest -v --tb=short            # подробный вывод
```

**Покрытие:** 26/26 тестов (100%)
- `test_gcurve.py`: 7 тестов — парсер G-кривой и расчёт сигнала
- `test_minfin.py`: 6 тестов — парсер аукционов, оба формата
- `test_api.py`: 13 тестов — все эндпоинты, структура ответов

---

*Обновлено: 04.06.2026*

# 🤖 Lead Vitrina Bot

Telegram-бот секретарь «Никита» — ведёт диалоги с риэлторами от твоего имени,  
продаёт подключение к «Лид-витрине», ведёт CRM в Google Sheets и уведомляет  
об истекающих триалах и подписках.

---

## Архитектура

```
bot.py           ← точка входа, polling
handlers.py      ← логика обработки сообщений
gemini_client.py ← Gemini AI + промпт продаж
sheets_client.py ← Google Sheets CRM
scheduler.py     ← ежедневные уведомления
config.py        ← переменные окружения
```

**Стек:** python-telegram-bot 21 · Gemini 1.5 Flash · gspread · APScheduler · Railway

---

## Быстрый старт

### 1. Создать бота в BotFather

1. Открой [@BotFather](https://t.me/BotFather) → `/newbot`
2. Получи `BOT_TOKEN`
3. Настрой бота как секретаря: Settings → Chat Automation → вставь `@имя_бота`
4. Выбери чаты, которые бот будет обрабатывать (нужные диалоги с риэлторами)

### 2. Google Sheets — Service Account

1. Открой [Google Cloud Console](https://console.cloud.google.com/)
2. Создай проект → включи **Google Sheets API** и **Google Drive API**
3. IAM → Service Accounts → создай аккаунт → скачай JSON ключ
4. Открой свою таблицу → поделись ею с email сервис-аккаунта (права Редактор)
5. Скопируй весь JSON ключа — он пойдёт в `GOOGLE_CREDENTIALS_JSON` (одной строкой)

**Получить GOOGLE_SHEET_ID:** из URL таблицы:
```
https://docs.google.com/spreadsheets/d/ВОТ_ЭТО_И_ЕСТЬ_ID/edit
```

> ⚠️ Таблица должна содержать листы с точными именами:
> - `👥 CRM клиентов` — данные клиентов (уже есть в твоей таблице)

### 3. Gemini API Key

1. Открой [Google AI Studio](https://aistudio.google.com/)
2. Создай API Key → скопируй в `GEMINI_API_KEY`

### 4. Узнать свой OWNER_CHAT_ID

Напиши боту [@userinfobot](https://t.me/userinfobot) — он пришлёт твой числовой ID.

---

## Деплой на Railway

### Через GitHub (рекомендуется)

```bash
git init
git add .
git commit -m "init"
git remote add origin https://github.com/ВАШ_НИК/lead-vitrina-bot.git
git push -u origin main
```

1. Открой [railway.app](https://railway.app) → New Project → Deploy from GitHub
2. Выбери репозиторий
3. Variables → добавь все переменные из `.env.example`:

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | токен от BotFather |
| `OWNER_CHAT_ID` | твой числовой Telegram ID |
| `GEMINI_API_KEY` | ключ из AI Studio |
| `GOOGLE_SHEET_ID` | ID таблицы из URL |
| `GOOGLE_CREDENTIALS_JSON` | весь JSON одной строкой |

4. Railway сам задеплоит и запустит `python bot.py`

---

## Локальный запуск (для разработки)

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Заполни .env своими данными

python bot.py
```

---

## Как это работает

### Воронка продаж (автоматически)
```
Новый диалог
    ↓ Gemini отвечает как «Никита» по промпту
    ↓ Upserting CRM: первый контакт
    ↓ Когда упоминается Лид-витрина → CRM: offer=Да
    ↓ Когда предлагается триал → CRM: trial=Да, expires_at=+3 дня
    ↓ Когда клиент спрашивает цену → бот пишет «уточню» + тебе уведомление 🔔
```

### CRM (Google Sheets → лист «👥 CRM клиентов»)
Бот автоматически заполняет:
- Username клиента
- Дата первого контакта
- Этапы воронки (Диалог / Предложение / Триал)
- Статус, дата подключения, дата окончания

### Уведомления (ежедневно в 10:00 UTC = 14:00 по Батуми)
- **Триал истекает завтра** → бот пишет тебе в личку
- **Подписка истекает через 3 дня** → бот пишет тебе в личку

### Сигнал для ручного подключения
Когда клиент спрашивает о цене/деталях — Gemini отвечает «уточню и напишу»  
и бот **немедленно уведомляет тебя** с текстом последнего сообщения клиента.

---

## Ручное управление CRM

Всё что бот не может сделать сам — заполняй вручную в таблице:
- `Оплата (лари)` / `Оплата (руб)` — после получения оплаты
- `Статус` → `✅ Активен` — после подключения к боту
- `Тариф (дней)` + `Дата окончания` — при продлении
- `Имя`, `Комментарий` — по желанию

---

## Структура переменных окружения

```env
BOT_TOKEN=                  # обязательно
OWNER_CHAT_ID=              # обязательно — числовой ID
GEMINI_API_KEY=             # обязательно
GOOGLE_SHEET_ID=            # обязательно
GOOGLE_CREDENTIALS_JSON=    # обязательно — весь JSON одной строкой

# Опционально:
GEMINI_MODEL=gemini-1.5-flash
TRIAL_WARN_DAYS=1
SUBSCRIPTION_WARN_DAYS=3
SCHEDULER_HOUR=10
```

# 🚨 Telegram бот повітряної тривоги — Берестинський район

Бот моніторить повітряну тривогу по **Берестинському району** (Харківська область) через API [alerts.in.ua](https://alerts.in.ua) і відправляє повідомлення в Telegram чат.

## Що бот робить

- 🚨 Повідомляє про **початок тривоги** (з деталями: тип, загрози, рівень)
- ✅ Повідомляє про **відбій тривоги** (з тривалістю)
- ⚠️ Повідомляє про **нові загрози** під час активної тривоги (ракети, дрони, авіація тощо)

## Передумови

1. **Python 3.11+**
2. **API токен alerts.in.ua** — отримати на [alerts.in.ua/api-request](https://alerts.in.ua/api-request)
3. **Telegram бот** — створити через [@BotFather](https://t.me/BotFather)
4. **Chat ID** — ID чату, куди бот буде писати

## Встановлення

```bash
# Клонуйте або скопіюйте проєкт
cd air-raid-bot

# Встановіть залежності
pip install -r requirements.txt
```

## Налаштування

### 1. Отримайте API токен alerts.in.ua

Заповніть [форму на сайті](https://alerts.in.ua/api-request) — вам надішлють токен на email.

### 2. Створіть Telegram бота

1. Напишіть [@BotFather](https://t.me/BotFather) в Telegram
2. Відправте `/newbot`
3. Дайте ім'я боту
4. Збережіть **токен бота**

### 3. Отримайте Chat ID

**Для особистого чату:**
1. Напишіть боту будь-яке повідомлення
2. Відкрийте `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Знайдіть `"chat":{"id": <ЧИСЛО>}` — це ваш Chat ID

**Для групи/каналу:**
1. Додайте бота в групу або канал (як адміністратора)
2. Напишіть будь-яке повідомлення в групу
3. Відкрийте `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
4. Chat ID групи зазвичай починається з `-` (наприклад `-1001234567890`)

### 4. Встановіть змінні оточення

```bash
export ALERTS_API_TOKEN='ваш_токен_alerts_in_ua'
export TELEGRAM_BOT_TOKEN='ваш_токен_telegram_бота'
export TELEGRAM_CHAT_ID='ваш_chat_id'
```

Або скопіюйте `.env.example` в `.env` і заповніть:

```bash
cp .env.example .env
# Відредагуйте .env
```

## Запуск

```bash
python bot.py
```

### Запуск з .env файлом

```bash
# Якщо використовуєте .env файл
export $(cat .env | xargs) && python bot.py
```

### Запуск через systemd (для сервера)

Створіть файл `/etc/systemd/system/air-raid-bot.service`:

```ini
[Unit]
Description=Air Raid Alert Telegram Bot
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/air-raid-bot
EnvironmentFile=/path/to/air-raid-bot/.env
ExecStart=/usr/bin/python3 /path/to/air-raid-bot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable air-raid-bot
sudo systemctl start air-raid-bot
sudo systemctl status air-raid-bot
```

### Запуск через Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
CMD ["python", "bot.py"]
```

```bash
docker build -t air-raid-bot .
docker run -d --name air-raid-bot \
  -e ALERTS_API_TOKEN='...' \
  -e TELEGRAM_BOT_TOKEN='...' \
  -e TELEGRAM_CHAT_ID='...' \
  air-raid-bot
```

## Приклади повідомлень

### Початок тривоги
```
🔴 🚨 Повітряна тривога

📍 Берестинський район
🕐 Початок: 15.09.2026 14:30:00

⚠️ Загрози:
  • 🟡 🛩 Дрони (БПЛА) — Дронова загроза (жовтий рівень)

🔗 alerts.in.ua
```

### Відбій
```
✅ Відбій тривоги

📍 Берестинський район
🕐 Початок: 15.09.2026 14:30:00
🕐 Кінець: 15.09.2026 15:45:00
⏱ Тривалість: 1 год 15 хв

🔗 alerts.in.ua
```

## Обмеження API

- **8-10 запитів/хв** (soft limit)
- **12 запитів/хв** (hard limit)
- За замовчуванням бот опитує кожні 10 секунд (6/хв) — це в межах ліміту
- Бот використовує заголовок `If-Modified-Since` для оптимізації запитів

## Ліцензія

MIT

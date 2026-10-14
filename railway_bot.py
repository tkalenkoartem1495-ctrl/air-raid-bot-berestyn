import asyncio
import logging
import os
import re

from telethon import TelegramClient, events
from telegram import Bot

logger = logging.getLogger(__name__)

RAILWAY_BOT_TOKEN = os.environ.get("RAILWAY_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Канали УЗ для моніторингу (username без @)
MONITORED_CHANNELS = ["UZprymisky"]

# Числові ID тих самих каналів (для резервної перевірки)
# @UZprymisky — Укрзалізниця: приміські поїзди
MONITORED_CHANNEL_IDS = [
    -1001110859951, 1110859951,   # UZprymisky (резервний)
]

class RailwayMonitor:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot = Bot(token=RAILWAY_BOT_TOKEN) if RAILWAY_BOT_TOKEN else None

        # Реєструємо обробник з явним фільтром по каналах.
        # Передаємо chats= щоб Telethon слухав ТІЛЬКИ ці канали
        # (навіть якщо userbot не є підписником — Telethon підпишеться автоматично)
        self.client.on(events.NewMessage(chats=MONITORED_CHANNELS))(self._on_new_message)

    def _replace_city_name(self, text: str) -> str:
        """Замінює Красноград на Берестин, зберігаючи регістр першої літери."""
        def replacer(match):
            word = match.group(0)
            if word.istitle():
                return "Берестин"
            elif word.isupper():
                return "БЕРЕСТИН"
            else:
                return "берестин"
        return re.sub(r'Красноград', replacer, text, flags=re.IGNORECASE)

    async def _on_new_message(self, event):
        """Обробник нових повідомлень від УЗ."""
        if not self.bot or not event.raw_text:
            return

        text = event.raw_text

        # Шукаємо згадку Берестин або Красноград
        if re.search(r'(Берестин|Красноград)', text, re.IGNORECASE):
            # Витягуємо лише корисне навантаження (заголовок + поїзд)
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            header = ""
            trains = []

            for i, line in enumerate(lines):
                if re.search(r'(Берестин|Красноград)', line, re.IGNORECASE):
                    trains.append(line)
                    if not header and i > 0:
                        # Шукаємо найближчий заголовок вище
                        for j in range(i - 1, -1, -1):
                            prev = lines[j]
                            if prev.endswith(':') or '❕' in prev or any(w in prev.lower() for w in ['затримується', 'змінено', 'скасовано', 'маршрут']):
                                header = prev
                                break

            if trains:
                if header:
                    text = header + " " + " ".join(trains)
                else:
                    text = " ".join(trains)

            clean_text = self._replace_city_name(text)

            logger.info(f"🚂 Знайдено повідомлення від Укрзалізниці! Відправляємо...")
            try:
                await self.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=clean_text)
            except Exception as e:
                logger.error(f"Помилка відправки в Railway bot: {e}")

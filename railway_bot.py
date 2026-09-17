import asyncio
import logging
import os
import re

from telethon import TelegramClient, events
from telegram import Bot

logger = logging.getLogger(__name__)

RAILWAY_BOT_TOKEN = os.environ.get("RAILWAY_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MONITORED_CHANNELS = ["UZprymisky"]

class RailwayMonitor:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot = Bot(token=RAILWAY_BOT_TOKEN) if RAILWAY_BOT_TOKEN else None

        # Реєструємо обробник нових повідомлень
        self.client.on(events.NewMessage)(self._on_new_message)

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
            
        chat = await event.get_chat()
        chat_username = getattr(chat, "username", "")
        
        if chat_username and chat_username.lower() in [c.lower() for c in MONITORED_CHANNELS]:
            text = event.raw_text
            
            # Шукаємо згадку Берестин або Красноград
            if re.search(r'(Берестин|Красноград)', text, re.IGNORECASE):
                clean_text = self._replace_city_name(text)
                clean_text = f"🚂 **Укрзалізниця повідомляє:**\n\n{clean_text}"
                
                logger.info(f"🚂 Знайдено повідомлення від Укрзалізниці! Відправляємо...")
                try:
                    await self.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=clean_text)
                except Exception as e:
                    logger.error(f"Помилка відправки в Railway bot: {e}")

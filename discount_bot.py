import asyncio
import logging
import os
import re
import io

from telethon import TelegramClient, events
from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

DISCOUNT_BOT_TOKEN = os.environ.get("DISCOUNT_BOT_TOKEN", "8441604612:AAFl9F0bkxWygvOHOCZ0nGjrTfeVPGXYZH8")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1001110859952")
ATB_CHANNEL_ID = -1002353861460

class DiscountMonitor:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot = Bot(token=DISCOUNT_BOT_TOKEN) if DISCOUNT_BOT_TOKEN else None

        # Реєструємо обробник нових повідомлень
        self.client.on(events.NewMessage)(self._on_new_message)

    def _process_text(self, text: str) -> str:
        if "#АТБ" not in text:
            return ""
            
        # Відсікаємо все, що нижче #АТБ
        parts = text.split("#АТБ")
        clean_text = parts[0] + "#АТБ"
        
        # Видаляємо конкретні фрази (з урахуванням можливих відмінностей у пробілах)
        clean_text = clean_text.replace("(ВСІ СТОРІНКИ ГАЗЕТИ В КОМЕНТАРЯХ ⬇)", "")
        
        # Можливий варіант без пробілу або інший символ стрілки, але почнемо з точного збігу
        
        # Забираємо зайві порожні рядки
        clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)
        
        return clean_text.strip()

    async def _on_new_message(self, event):
        """Обробник нових повідомлень з каналу знижок."""
        if not self.bot:
            return
            
        if getattr(event, "chat_id", 0) != ATB_CHANNEL_ID:
            return
            
        text = event.raw_text
        if not text:
            return
            
        clean_text = self._process_text(text)
        if not clean_text:
            return
            
        logger.info(f"🛒 Знайдено знижки АТБ! Відправляємо...")
        
        try:
            # Перевіряємо, чи є медіа (картинка)
            if event.message.media:
                # Завантажуємо медіа в пам'ять
                buffer = io.BytesIO()
                await self.client.download_media(event.message.media, file=buffer)
                buffer.seek(0)
                
                await self.bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID, 
                    photo=buffer, 
                    caption=clean_text,
                    parse_mode=ParseMode.HTML
                )
            else:
                # Якщо раптом просто текст
                await self.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, 
                    text=clean_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
        except Exception as e:
            logger.error(f"Помилка відправки в Discount bot: {e}")

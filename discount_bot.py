import asyncio
import logging
import os
import re
import io

from telethon import TelegramClient, events
from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

DISCOUNT_BOT_TOKEN = os.environ.get("DISCOUNT_BOT_TOKEN", "8441604612:AAHC1brGLaXAKxd8cPozBCCYTMR0njV8few")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1001110859952")
ATB_CHANNEL_ID = -1002353861460

class DiscountMonitor:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot = Bot(token=DISCOUNT_BOT_TOKEN) if DISCOUNT_BOT_TOKEN else None
        self.processed_groups = set()

        # Реєструємо обробник нових повідомлень
        self.client.on(events.NewMessage)(self._on_new_message)

    def _process_text(self, text: str) -> str:
        if not text or "#АТБ" not in text:
            return ""
            
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        term_line = ""
        
        # Шукаємо рядок з датою/терміном дії за допомогою регулярного виразу
        # Розпізнає: "термін дії", "діє до", "до 04.08", "16.09-22.09", "з 10.05 по 15.05" і т.д.
        pattern = re.compile(r'(?i)(термін дії|діє до|\bдо\s+\d{1,2}\.\d{2}|\b\d{1,2}\.\d{2}\s*(?:-|по|до)\s*\d{1,2}\.\d{2}|\bз\s+\d{1,2}\.\d{2})')
        
        for line in lines:
            if pattern.search(line):
                term_line = line
                break
                
        if term_line:
            return f"{term_line}\n#АТБ"
        else:
            return "#АТБ"

    async def _on_new_message(self, event):
        """Обробник нових повідомлень з каналу знижок."""
        from telegram import InputMediaPhoto
        
        if not self.bot:
            return
            
        if getattr(event, "chat_id", 0) != ATB_CHANNEL_ID:
            return
            
        # Якщо це альбом
        if event.message.grouped_id:
            if event.message.grouped_id in self.processed_groups:
                return
            self.processed_groups.add(event.message.grouped_id)
            
            # Чекаємо 3 секунди, щоб усі частини альбому встигли надійти на сервер
            await asyncio.sleep(3)
            
            # Завантажуємо останні 20 повідомлень, щоб зібрати весь альбом
            all_msgs = await self.client.get_messages(ATB_CHANNEL_ID, limit=20)
            album_msgs = [m for m in all_msgs if m.grouped_id == event.message.grouped_id]
            album_msgs.sort(key=lambda x: x.id)
            
            # Шукаємо текст з #АТБ серед частин альбому
            target_text = ""
            for m in album_msgs:
                if m.raw_text and "#АТБ" in m.raw_text:
                    target_text = m.raw_text
                    break
                    
            if not target_text:
                return
                
            clean_text = self._process_text(target_text)
            if not clean_text:
                return
                
            logger.info(f"🛒 Знайдено альбом АТБ ({len(album_msgs)} фото)! Відправляємо...")
            
            try:
                media_group = []
                for i, m in enumerate(album_msgs):
                    if m.media:
                        buffer = io.BytesIO()
                        await self.client.download_media(m.media, file=buffer)
                        buffer.seek(0)
                        
                        # Додаємо текст тільки до першого фото в альбомі
                        if i == 0:
                            media_group.append(InputMediaPhoto(media=buffer, caption=clean_text, parse_mode=ParseMode.HTML))
                        else:
                            media_group.append(InputMediaPhoto(media=buffer))
                
                if media_group:
                    await self.bot.send_media_group(chat_id=TELEGRAM_CHAT_ID, media=media_group)
            except Exception as e:
                logger.error(f"Помилка відправки альбому Discount bot: {e}")
            
            return
            
        # Одиночне повідомлення
        text = event.raw_text
        if not text:
            return
            
        clean_text = self._process_text(text)
        if not clean_text:
            return
            
        logger.info(f"🛒 Знайдено знижки АТБ (1 фото/текст)! Відправляємо...")
        
        try:
            if event.message.media:
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
                await self.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, 
                    text=clean_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
        except Exception as e:
            logger.error(f"Помилка відправки в Discount bot: {e}")

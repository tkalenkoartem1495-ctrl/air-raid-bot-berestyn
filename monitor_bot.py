#!/usr/bin/env python3
"""
Монітор Telegram-каналів.
Слухає @tlknewsua та @radar_kharkov, фільтрує за ключовими словами
(Красноград, Берестин), перекладає на українську і постить у цільовий чат.
"""

import asyncio
import html
import logging
import os
import re

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram import Bot
from telegram.constants import ParseMode
from deep_translator import GoogleTranslator

logger = logging.getLogger(__name__)

# ─── Конфігурація ────────────────────────────────────────────────
TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TELETHON_SESSION = os.environ.get("TELETHON_SESSION", "")
MONITOR_BOT_TOKEN = os.environ.get("MONITOR_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Канали для моніторингу та їхні ID для надійності
MONITORED_CHANNELS = ["tlknewsua", "radar_kharkov", "nochnojdozorkh", "monitor1654"]
MONITORED_CHANNEL_IDS = [
    -1001673474387,  # tlknewsua
    -1001850203289,  # radar_kharkov
    -1001667056986,  # NochnojDozorKh
    -1001104455802,  # monitor1654
]

# Ключові слова для фільтрації (регістронезалежно)
FILTER_KEYWORDS = ["красноград", "берестин"]


class ChannelMonitor:
    """Моніторинг Telegram-каналів через userbot (Telethon)."""

    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot = Bot(token=MONITOR_BOT_TOKEN)
        self.translator = GoogleTranslator(source="auto", target="uk")

        # Реєструємо обробники для нових та відредагованих повідомлень
        self.client.on(events.NewMessage)(self._on_new_message)
        self.client.on(events.MessageEdited)(self._on_new_message)

    def _matches_filter(self, text: str) -> bool:
        """Перевіряє, чи містить текст ключові слова."""
        text_lower = text.lower()
        return any(kw in text_lower for kw in FILTER_KEYWORDS)

    async def _translate(self, text: str) -> str:
        """Перекладає текст на українську через Gemini."""
        try:
            import google.generativeai as genai
            GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
            if not GEMINI_API_KEY:
                return text
                
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel("gemini-flash-lite-latest")
            
            prompt = (
                "Переклади наступний текст на чисту українську мову. "
                "Збережи всі емодзі та оригінальне форматування. "
                "Якщо текст вже українською, просто поверни його без змін. "
                "Відповідай ТІЛЬКИ перекладеним текстом:\n\n"
                f"{text}"
            )
            
            # Gemini block
            response = await asyncio.to_thread(model.generate_content, prompt)
            return response.text.strip() if response and response.text else text
        except Exception as e:
            logger.warning(f"Помилка перекладу Gemini: {e}")
            return text

    async def _on_new_message(self, event):
        """Обробник нових повідомлень у каналах."""
        text = event.raw_text
        if not text:
            return
            
        chat = await event.get_chat()
        channel_username = getattr(chat, "username", "")
        chat_id = getattr(event, "chat_id", 0)
        
        # Перевірка, чи це потрібний канал (по username або по ID)
        is_monitored = False
        if channel_username and channel_username.lower() in [c.lower() for c in MONITORED_CHANNELS]:
            is_monitored = True
        elif chat_id in MONITORED_CHANNEL_IDS:
            is_monitored = True
            
        if not is_monitored:
            return

        if not self._matches_filter(text):
            return
            
        # Ігноруємо повідомлення про початок/відбій тривоги тільки для NochnojDozorKh
        if chat_id == -1001667056986 or (channel_username and channel_username.lower() == "nochnojdozorkh"):
            text_lower = text.lower()
            alert_keywords = ["відбій", "отбой", "тривог", "тревог"]
            if any(kw in text_lower for kw in alert_keywords):
                logger.info("🚫 Ігноруємо повідомлення про тривогу/відбій з NochnojDozorKh")
                return

        # Отримуємо інфо про канал (для логів та підпису)
        channel_title = getattr(chat, "title", "Невідомий канал")

        logger.info(
            f"📰 Знайдено збіг у @{channel_username or channel_title}: "
            f"{text[:80]}..."
        )

        # Перекладаємо
        translated = await self._translate(text)
        
        # Замінюємо стару назву на нову (зберігаючи базовий регістр)
        def replacer(match):
            word = match.group(0)
            if word.istitle(): return "Берестин"
            if word.isupper(): return "БЕРЕСТИН"
            return "берестин"
            
        translated = re.sub(r'Красноград', replacer, translated, flags=re.IGNORECASE)

        translated_escaped = html.escape(translated)

        # Формуємо повідомлення
        source = f"@{channel_username}" if channel_username else channel_title
        msg = (
            f"⚠️ <b>Увага!</b>\n\n"
            f"{translated_escaped}\n\n"
            f"📌 Джерело: {source}"
        )

        try:
            await self.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            logger.info("📨 Повідомлення з каналу переслано в чат")
        except Exception as e:
            logger.error(f"Помилка відправки: {e}")

    async def start(self):
        """Запускає моніторинг каналів."""
        logger.info("=" * 50)
        logger.info("📡 Монітор новинних каналів запущено!")
        logger.info(
            f"📺 Канали: {', '.join('@' + c for c in MONITORED_CHANNELS)}"
        )
        logger.info(f"🔍 Ключові слова: {', '.join(FILTER_KEYWORDS)}")
        logger.info("=" * 50)

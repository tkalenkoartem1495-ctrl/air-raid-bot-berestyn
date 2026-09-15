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

# Канали для моніторингу
MONITORED_CHANNELS = ["tlknewsua", "radar_kharkov"]

# Ключові слова для фільтрації (регістронезалежно)
FILTER_KEYWORDS = ["красноград", "берестин"]


class ChannelMonitor:
    """Моніторинг Telegram-каналів через userbot (Telethon)."""

    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot = Bot(token=MONITOR_BOT_TOKEN)
        self.translator = GoogleTranslator(source="auto", target="uk")

        # Реєструємо обробник нових повідомлень
        self.client.on(events.NewMessage(chats=MONITORED_CHANNELS))(
            self._on_new_message
        )

    def _matches_filter(self, text: str) -> bool:
        """Перевіряє, чи містить текст ключові слова."""
        text_lower = text.lower()
        return any(kw in text_lower for kw in FILTER_KEYWORDS)

    async def _translate(self, text: str) -> str:
        """Перекладає текст на українську через Google Translate."""
        try:
            translated = await asyncio.to_thread(self.translator.translate, text)
            return translated or text
        except Exception as e:
            logger.warning(f"Помилка перекладу: {e}")
            return text

    async def _on_new_message(self, event):
        """Обробник нових повідомлень у каналах."""
        text = event.raw_text
        if not text:
            return

        if not self._matches_filter(text):
            return

        # Отримуємо інфо про канал
        chat = await event.get_chat()
        channel_username = getattr(chat, "username", "")
        channel_title = getattr(chat, "title", "Невідомий канал")

        logger.info(
            f"📰 Знайдено збіг у @{channel_username or channel_title}: "
            f"{text[:80]}..."
        )

        # Перекладаємо
        translated = await self._translate(text)
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

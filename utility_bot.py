#!/usr/bin/env python3
"""
Монітор комунальних послуг (світло та вода).
Слухає @krasnogradbezp та @krasnograd3serzem, шукає скарги на світло/воду,
передає їх пачкою в Gemini і результати відправляє в цільовий чат
від імені відповідних ботів (світло або вода).
"""

import asyncio
import logging
import os
import re

from telethon import TelegramClient, events
from telegram import Bot
from telegram.constants import ParseMode
import google.generativeai as genai

logger = logging.getLogger(__name__)

# ─── Конфігурація ────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
LIGHT_BOT_TOKEN = os.environ.get("LIGHT_BOT_TOKEN", "")
WATER_BOT_TOKEN = os.environ.get("WATER_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MONITORED_CHATS = ["krasnogradbezp", "krasnograd3serzem"]

# Регулярки для базового фільтрування
LIGHT_PATTERN = re.compile(r'(світл|свет|електроенерг)', re.IGNORECASE)
WATER_PATTERN = re.compile(r'(вод|водокачк)', re.IGNORECASE)
ACTION_PATTERN = re.compile(r'(пропал|блимнув|появи|з\'яви|зник|\+|-)', re.IGNORECASE)

PROMPT = """Прочитай цей батч повідомлень з місцевого чату. Твоя задача — знайти скарги на відключення світла або води і переформулювати їх у готові повідомлення. 
Якщо є інформація про світло, напиши: '🔴 Відключення світла: [локація/деталі з повідомлень]'. 
Якщо є інформація про воду, напиши: '🔵 Відключення води: [локація/деталі з повідомлень]'. 
Якщо скарг немає взагалі, поверни лише слово 'NONE'.
Якщо є інформація і про світло, і про воду, напиши обидва рядки."""

class UtilityMonitor:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.light_bot = Bot(token=LIGHT_BOT_TOKEN) if LIGHT_BOT_TOKEN else None
        self.water_bot = Bot(token=WATER_BOT_TOKEN) if WATER_BOT_TOKEN else None
        
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            self.model = genai.GenerativeModel("gemini-1.5-flash")
        else:
            self.model = None

        self.batch = []
        self.lock = asyncio.Lock()

        # Реєструємо обробник нових повідомлень
        self.client.on(events.NewMessage(chats=MONITORED_CHATS))(
            self._on_new_message
        )

    def _matches_filter(self, text: str) -> bool:
        """Перевіряє, чи містить текст згадки про світло/воду ТА дії."""
        has_light = bool(LIGHT_PATTERN.search(text))
        has_water = bool(WATER_PATTERN.search(text))
        has_action = bool(ACTION_PATTERN.search(text))
        
        # Щоб не реагувати на "води немає" без дії, хоча "немає" це не в списку, 
        # користувач просив action words, або просто наявність світла/води.
        # Для надійності: вимагаємо (світло або вода) + дія (хоча б мінус/плюс).
        return (has_light or has_water) and has_action

    async def _on_new_message(self, event):
        """Обробник нових повідомлень у комунальних чатах."""
        text = event.raw_text
        if not text:
            return

        if not self._matches_filter(text):
            return

        chat = await event.get_chat()
        chat_title = getattr(chat, "title", "Чат")

        logger.info(f"💧/💡 Знайдено скаргу в {chat_title}: {text[:50]}...")
        
        async with self.lock:
            self.batch.append(f"[{chat_title}] {text}")

    async def _process_batch_loop(self):
        """Фонова задача, яка кожні 60 секунд відправляє батч в Gemini."""
        while True:
            await asyncio.sleep(60)
            
            async with self.lock:
                if not self.batch:
                    continue
                messages_to_process = list(self.batch)
                self.batch.clear()

            if not self.model:
                logger.error("GEMINI_API_KEY не задано! Пропускаю батч.")
                continue

            batch_text = "\n---\n".join(messages_to_process)
            full_prompt = f"{PROMPT}\n\nПовідомлення:\n{batch_text}"

            try:
                # Виконуємо запит до Gemini
                response = await asyncio.to_thread(self.model.generate_content, full_prompt)
                result = response.text.strip()
                
                if result == "NONE" or not result:
                    continue
                    
                # Розбираємо відповідь і відправляємо потрібним ботом
                lines = result.split('\n')
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                        
                    if "🔴" in line and self.light_bot:
                        await self.light_bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=line
                        )
                        logger.info(f"💡 Відправлено статус світла: {line}")
                        
                    elif "🔵" in line and self.water_bot:
                        await self.water_bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=line
                        )
                        logger.info(f"💧 Відправлено статус води: {line}")
                        
            except Exception as e:
                logger.error(f"Помилка обробки Gemini або відправки: {e}")

    async def start(self):
        """Запускає фонову обробку батчів."""
        logger.info("=" * 50)
        logger.info("💧/💡 Монітор комуналки (світло/вода) запущено!")
        logger.info(f"📺 Канали: {', '.join('@' + c for c in MONITORED_CHATS)}")
        logger.info("=" * 50)
        
        # Запускаємо безкінечний цикл батчингу як фонову таску
        asyncio.create_task(self._process_batch_loop())


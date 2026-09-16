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
import re

logger = logging.getLogger(__name__)

# ─── Конфігурація ────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
LIGHT_BOT_TOKEN = os.environ.get("LIGHT_BOT_TOKEN", "")
WATER_BOT_TOKEN = os.environ.get("WATER_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MONITORED_CHATS = ["krasnogradbezp", "krasnograd3serzem"]

# Регулярки для ШИРОКОГО базового фільтрування (щоб не витрачати ліміти Gemini на пусті розмови)
BROAD_PATTERN = re.compile(r'(світл|свет|електроенерг|вод|водокачк)', re.IGNORECASE)

PROMPT = """Ти моніториш скарги мешканців на відключення світла та води у місцевих чатах.
Прочитай цей батч повідомлень (зібраний за останню хвилину). Знайди дійсні скарги або питання про відключення світла чи води і сформуй готові попередження.

ПРАВИЛА:
1. ВІДПОВІДАЙ ВИКЛЮЧНО УКРАЇНСЬКОЮ МОВОЮ (навіть якщо оригінали російською).
2. Якщо повідомлень багато — узагальнюй їх. Об'єднуй різні вулиці та райони в одне загальне попередження.
3. Якщо є скарги на світло, створи ОДНЕ зведене повідомлення і почни його з тегу [LIGHT].
4. Якщо є скарги на воду, створи ОДНЕ зведене повідомлення і почни його з тегу [WATER].
5. Питання типу "Що з водою?", "Коли дадуть світло?" ВВАЖАЙ скаргою на відключення. Сприймай це як факт відключення.
6. Якщо скарг взагалі немає, поверни слово NONE.

Приклад ідеальної відповіді:
[LIGHT] 🔴 Відключення світла: район центру та вул. Миру (немає світла); 3-й мікрорайон (люди питають коли дадуть).
[WATER] 🔵 Відключення води: 3-й мікрорайон (немає води вже годину)."""

class UtilityMonitor:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.light_bot = Bot(token=LIGHT_BOT_TOKEN) if LIGHT_BOT_TOKEN else None
        self.water_bot = Bot(token=WATER_BOT_TOKEN) if WATER_BOT_TOKEN else None
        
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            self.model = genai.GenerativeModel("gemini-flash-latest")
        else:
            self.model = None

        self.batch = []
        self.lock = asyncio.Lock()

        # Реєструємо обробник нових повідомлень (перевірка каналів буде всередині)
        self.client.on(events.NewMessage)(
            self._on_new_message
        )

    def _matches_filter(self, text: str) -> bool:
        """Дуже широкий фільтр, пропускає всі повідомлення де є бодай згадка води/світла."""
        return bool(BROAD_PATTERN.search(text))

    async def _on_new_message(self, event):
        """Обробник нових повідомлень у комунальних чатах."""
        text = event.raw_text
        if not text:
            return
            
        chat = await event.get_chat()
        chat_username = getattr(chat, "username", "")
        
        if chat_username not in MONITORED_CHATS:
            return

        if not self._matches_filter(text):
            return

        chat_title = getattr(chat, "title", "Чат")

        logger.info(f"💧/💡 Знайдено можливу скаргу в {chat_title}: {text[:50]}...")
        
        async with self.lock:
            self.batch.append(f"[{chat_title}] {text}")

    async def _process_batch_loop(self):
        """Фонова задача, яка щохвилини відправляє батч в Gemini."""
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
                response = await asyncio.to_thread(self.model.generate_content, full_prompt)
                result = response.text.strip()
                
                if result == "NONE" or not result:
                    continue
                    
                lines = result.split('\n')
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                        
                    if "[LIGHT]" in line and self.light_bot:
                        clean_text = line.replace("[LIGHT]", "").strip()
                        await self.light_bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=clean_text
                        )
                        logger.info(f"💡 Відправлено статус світла: {clean_text}")
                        
                    elif "[WATER]" in line and self.water_bot:
                        clean_text = line.replace("[WATER]", "").strip()
                        await self.water_bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=clean_text
                        )
                        logger.info(f"💧 Відправлено статус води: {clean_text}")
                        
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


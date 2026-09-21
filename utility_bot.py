#!/usr/bin/env python3
"""
Монітор комунальних послуг (світло та вода).
Слухає @krasnogradbezp та @krasnograd3serzem, шукає скарги або питання про світло/воду,
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



PROMPT = """Ти моніториш повідомлення мешканців щодо світла та води у місцевих чатах.
Прочитай цей батч повідомлень (зібраний за останні кілька хвилин). Твоє завдання — публікувати ТІЛЬКИ інформацію про фактичні відключення або ЗАПИТАННЯ мешканців щодо наявності послуг.

ПРАВИЛА:
1. ВІДПОВІДАЙ ВИКЛЮЧНО УКРАЇНСЬКОЮ МОВОЮ (навіть якщо оригінали російською).
2. Завжди використовуй назву міста Берестин (замість Красноград).
3. ІГНОРУЙ загальні обговорення графіків на майбутнє або новини.
4. Якщо люди СТВЕРДЖУЮТЬ, що послуги немає (напр. "немає світла", "зникла вода") - це ФАКТИЧНЕ відключення. Використовуй 🔴 (світло) або 🔵 (вода) та текст "Відключення...".
5. Якщо люди лише ПИТАЮТЬ про наявність послуги (напр. "На Піщанці є світло?", "Що з водою на мікрорайоні?"), це НЕВИЗНАЧЕНІСТЬ. Використовуй жовтий кружечок 🟡 і пиши "Питання щодо наявності...". НЕ ПИШИ "Відключення", якщо це просто питання.
6. Якщо повідомлень багато — узагальнюй їх. Розмежовуй статуси (ствердження vs питання).
7. Якщо дійсних скарг чи питань немає, поверни слово NONE.

Приклади ідеальної відповіді:
[LIGHT] 🔴 Відключення світла: район центру та вул. Миру.
[LIGHT] 🟡 Питання щодо наявності світла: 3-й мікрорайон (мешканці питають про наявність світла).
[WATER] 🔵 Відключення води: 3-й мікрорайон.
[WATER] 🟡 Питання щодо наявності води: вул. Київська.
"""

import time

class UtilityMonitor:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.light_bot = Bot(token=LIGHT_BOT_TOKEN) if LIGHT_BOT_TOKEN else None
        self.water_bot = Bot(token=WATER_BOT_TOKEN) if WATER_BOT_TOKEN else None
        
        # Таймери блокування (cooldown) у секундах (30 хвилин = 1800 сек)
        self.light_cooldown_until = 0
        self.water_cooldown_until = 0
        
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            self.model = genai.GenerativeModel("gemini-flash-lite-latest")
        else:
            self.model = None

        self.batch = []
        self.lock = asyncio.Lock()

        # Реєструємо обробник нових повідомлень (перевірка каналів буде всередині)
        self.client.on(events.NewMessage)(
            self._on_new_message
        )

    async def _on_new_message(self, event):
        """Обробник нових повідомлень у комунальних чатах."""
        text = event.raw_text
        if not text:
            return
            
        chat = await event.get_chat()
        chat_username = getattr(chat, "username", "")
        
        if chat_username not in MONITORED_CHATS:
            return

        chat_title = getattr(chat, "title", "Чат")

        logger.info(f"💧/💡 Знайдено нове повідомлення в {chat_title}: {text[:50]}...")
        
        async with self.lock:
            self.batch.append(f"[{chat_title}] {text}")

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

    async def _process_batch_loop(self):
        """Фонова задача, яка кожні 2 хвилини відправляє батч в Gemini."""
        while True:
            await asyncio.sleep(120)
            
            now = time.time()
            light_active = now >= self.light_cooldown_until
            water_active = now >= self.water_cooldown_until
            
            async with self.lock:
                if not self.batch:
                    continue
                    
                # Якщо обидва боти на кулдауні, просто викидаємо повідомлення
                if not light_active and not water_active:
                    self.batch.clear()
                    continue
                    
                messages_to_process = list(self.batch)
                self.batch.clear()

            if not self.model:
                logger.error("GEMINI_API_KEY не задано! Пропускаю батч.")
                continue

            dynamic_prompt = PROMPT + "\n\nДИНАМІЧНІ ПРАВИЛА (ВАЖЛИВО!):\n"
            if light_active:
                dynamic_prompt += "- Якщо є скарги або питання про світло, створи ОДНЕ зведене повідомлення і почни його з тегу [LIGHT].\n"
            else:
                dynamic_prompt += "- ІГНОРУЙ БУДЬ-ЯКІ СКАРГИ НА СВІТЛО. Інформація вже опублікована. Не генеруй [LIGHT].\n"
                
            if water_active:
                dynamic_prompt += "- Якщо є скарги або питання про воду, створи ОДНЕ зведене повідомлення і почни його з тегу [WATER].\n"
            else:
                dynamic_prompt += "- ІГНОРУЙ БУДЬ-ЯКІ СКАРГИ НА ВОДУ. Інформація вже опублікована. Не генеруй [WATER].\n"

            batch_text = "\n---\n".join(messages_to_process)
            full_prompt = f"{dynamic_prompt}\n\nПовідомлення:\n{batch_text}"

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
                        
                    if "[LIGHT]" in line and self.light_bot and light_active:
                        clean_text = line.replace("[LIGHT]", "").strip()
                        clean_text = self._replace_city_name(clean_text)
                        
                        # STATELESS DEDUPLICATION
                        is_duplicate = False
                        try:
                            now_ts = time.time()
                            clean_test = re.sub(r'[^\w\s]', '', clean_text.lower())
                            async for past_msg in self.client.iter_messages(int(TELEGRAM_CHAT_ID), limit=15):
                                if past_msg.date and (now_ts - past_msg.date.timestamp()) < 7200:
                                    if past_msg.text:
                                        past_clean = re.sub(r'[^\w\s]', '', past_msg.text.lower())
                                        if clean_test in past_clean or past_clean in clean_test:
                                            is_duplicate = True
                                            break
                        except Exception as e:
                            logger.error(f"Stateless dedup error (light): {e}")
                            
                        if not is_duplicate:
                            await self.light_bot.send_message(
                                chat_id=TELEGRAM_CHAT_ID,
                                text=clean_text
                            )
                            logger.info(f"💡 Відправлено статус світла: {clean_text}")
                            self.light_cooldown_until = time.time() + 1800
                        else:
                            logger.info("Повідомлення про світло вже було опубліковано недавно. Пропускаємо.")
                        
                    elif "[WATER]" in line and self.water_bot and water_active:
                        clean_text = line.replace("[WATER]", "").strip()
                        clean_text = self._replace_city_name(clean_text)
                        
                        # STATELESS DEDUPLICATION
                        is_duplicate = False
                        try:
                            now_ts = time.time()
                            clean_test = re.sub(r'[^\w\s]', '', clean_text.lower())
                            async for past_msg in self.client.iter_messages(int(TELEGRAM_CHAT_ID), limit=15):
                                if past_msg.date and (now_ts - past_msg.date.timestamp()) < 7200:
                                    if past_msg.text:
                                        past_clean = re.sub(r'[^\w\s]', '', past_msg.text.lower())
                                        if clean_test in past_clean or past_clean in clean_test:
                                            is_duplicate = True
                                            break
                        except Exception as e:
                            logger.error(f"Stateless dedup error (water): {e}")
                            
                        if not is_duplicate:
                            await self.water_bot.send_message(
                                chat_id=TELEGRAM_CHAT_ID,
                                text=clean_text
                            )
                            logger.info(f"💧 Відправлено статус води: {clean_text}")
                            self.water_cooldown_until = time.time() + 1800
                        else:
                            logger.info("Повідомлення про воду вже було опубліковано недавно. Пропускаємо.")
                        
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


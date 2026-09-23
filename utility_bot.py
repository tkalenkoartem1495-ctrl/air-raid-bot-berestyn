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
import time

from telethon import TelegramClient, events
from telegram import Bot
import google.generativeai as genai

logger = logging.getLogger(__name__)

# ─── Конфігурація ────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
LIGHT_BOT_TOKEN = os.environ.get("LIGHT_BOT_TOKEN", "")
WATER_BOT_TOKEN = os.environ.get("WATER_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MONITORED_CHATS = ["krasnogradbezp", "krasnograd3serzem"]

PROMPT = """Ти моніториш повідомлення мешканців щодо світла та води у місцевих чатах міста Берестин.
Прочитай цей батч повідомлень. Твоє завдання — публікувати ТІЛЬКИ інформацію про фактичні відключення або ЗАПИТАННЯ щодо наявності послуг.

МІСЦЕВА КОНВЕНЦІЯ (ДУЖЕ ВАЖЛИВО!):
Мешканці часто пишуть дуже коротко: "<назва вулиці/мікрорайону> <знак>".
- Знак "-" або "нема" або "відключили" = ВІДКЛЮЧЕННЯ (немає світла/води).
- Знак "+" або "дали" або "є" = ВІДНОВЛЕННЯ (послугу дали, це НЕ треба публікувати).
Приклади скарг на відключення: "Піщанка -", "Высокое -", "Центр нема", "Мікрорайон відключили"
Приклади відновлення (ігноруй): "Піщанка +", "Высокое+", "Центр дали", "Є!"

ПРАВИЛА:
1. ВІДПОВІДАЙ ВИКЛЮЧНО УКРАЇНСЬКОЮ МОВОЮ (навіть якщо оригінали російською).
2. Завжди використовуй назву міста Берестин (замість Красноград). Назви районів перекладай: "Высокое" → "Високе", "Песчаная/Піщана/Piщанка" → "Піщанка". ВАЖЛИВО: Піщанка — це не мікрорайон і не вулиця, пиши просто "Піщанка".
3. ІГНОРУЙ загальні обговорення, графіки на майбутнє, рекламу, оголошення.
4. ІГНОРУЙ повідомлення про відновлення послуги (знак "+", слова "дали", "є", "з'явилось").
5. Якщо люди СТВЕРДЖУЮТЬ про відсутність (знак "-", "нема", "відключили", "зникло") — це ФАКТИЧНЕ відключення. Використовуй 🔴 (світло) або 🔵 (вода): "Відключення...".
6. Якщо люди лише ПИТАЮТЬ ("є світло?", "що з водою?") — НЕВИЗНАЧЕНІСТЬ. Використовуй 🟡: "Питання щодо наявності...". НЕ ПИШИ "Відключення" для питань.
7. Якщо є кілька схожих повідомлень — узагальнюй їх в одне.
8. Якщо нічого релевантного немає — поверни слово NONE.
9. ВАЖЛИВО: назвою вулиці/мікрорайону може бути ТІЛЬКИ реальна географічна назва (Шевченко, Піщанка, Короленко, Центр, тощо). НЕ вважай звичайні слова назвами районів: "погода", "дирка", "капут", "все", "нема", "відсутність" — це НЕ назви місць. Якщо в повідомленні немає конкретної адреси/вулиці — просто пиши "Берестин" без вигадування назви.

Приклади ідеальної відповіді:
[LIGHT] 🔴 Відключення світла: мікрорайон Високе, Піщанка (мешканці повідомляють про відсутність світла).
[LIGHT] 🟡 Питання щодо наявності світла: район Центр (мешканці питають про наявність).
[WATER] 🔵 Відключення води: мікрорайон Центральний.
"""


class UtilityMonitor:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.light_bot = Bot(token=LIGHT_BOT_TOKEN) if LIGHT_BOT_TOKEN else None
        self.water_bot = Bot(token=WATER_BOT_TOKEN) if WATER_BOT_TOKEN else None
        
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
        _STOP = {"Відключення", "Берестин", "Питання", "Наявності", "Мешканці", "Повідомляють", "Відсутність"}
        DEDUP_WINDOW = 1800  # 30 хвилин для дедуплікатора

        while True:
            await asyncio.sleep(120)
            
            async with self.lock:
                if not self.batch:
                    continue
                    
                messages_to_process = list(self.batch)
                self.batch.clear()

            if not self.model:
                logger.error("GEMINI_API_KEY не задано! Пропускаю батч.")
                continue

            dynamic_prompt = PROMPT + "\n\nДИНАМІЧНІ ПРАВИЛА (ВАЖЛИВО!):\n"
            dynamic_prompt += "- Якщо є скарги або питання про світло, створи ОДНЕ зведене повідомлення і почни його з тегу [LIGHT].\n"
            dynamic_prompt += "- Якщо є скарги або питання про воду, створи ОДНЕ зведене повідомлення і почни його з тегу [WATER].\n"

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
                        
                    if "[LIGHT]" in line and self.light_bot:
                        clean_text = line.replace("[LIGHT]", "").strip()
                        clean_text = self._replace_city_name(clean_text)
                        
                        # Витягуємо назви районів/вулиць (виключаємо шаблонні слова)
                        new_locations = set(re.findall(r'\b[А-ЯІЇЄ][а-яіїє\']+\b', clean_text)) - _STOP
                        
                        # STATELESS DEDUPLICATION (по адресах, вікно 30 хв)
                        is_duplicate = False
                        try:
                            now_ts = time.time()
                            # Шукаємо найсвіжіший наш пост про світло у каналі
                            async for past_msg in self.client.iter_messages(int(TELEGRAM_CHAT_ID), limit=10):
                                if past_msg.date and (now_ts - past_msg.date.timestamp()) < DEDUP_WINDOW:
                                    if past_msg.text and ("Відключення світла" in past_msg.text or "наявності світла" in past_msg.text):
                                        # Порівнюємо адреси: якщо ВСІ нові адреси вже є в тому пості — дублікат
                                        past_locs = set(re.findall(r'\b[А-ЯІЇЄ][а-яіїє\']+\b', past_msg.text)) - _STOP
                                        if new_locations and new_locations.issubset(past_locs):
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
                        else:
                            logger.info("💡 Дублікат (ті самі адреси за 30 хв). Пропускаємо.")
                        
                    elif "[WATER]" in line and self.water_bot:
                        clean_text = line.replace("[WATER]", "").strip()
                        clean_text = self._replace_city_name(clean_text)
                        
                        new_locations = set(re.findall(r'\b[А-ЯІЇЄ][а-яіїє\']+\b', clean_text)) - _STOP
                        
                        # STATELESS DEDUPLICATION (по адресах, вікно 30 хв)
                        is_duplicate = False
                        try:
                            now_ts = time.time()
                            async for past_msg in self.client.iter_messages(int(TELEGRAM_CHAT_ID), limit=10):
                                if past_msg.date and (now_ts - past_msg.date.timestamp()) < DEDUP_WINDOW:
                                    if past_msg.text and ("Відключення води" in past_msg.text or "наявності води" in past_msg.text):
                                        past_locs = set(re.findall(r'\b[А-ЯІЇЄ][а-яіїє\']+\b', past_msg.text)) - _STOP
                                        if new_locations and new_locations.issubset(past_locs):
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
                        else:
                            logger.info("💧 Дублікат (ті самі адреси за 30 хв). Пропускаємо.")
                        
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

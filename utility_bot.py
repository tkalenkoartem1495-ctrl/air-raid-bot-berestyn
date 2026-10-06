#!/usr/bin/env python3
"""
Монітор комунальних послуг (світло та вода).
Слухає @krasnogradbezp та @krasnograd3serzem, шукає скарги або питання про світло/воду,
передає їх пачкою в Gemini і результати відправляє в цільовий чат
від імені відповідних ботів (світло або вода).
"""

import asyncio
from datetime import datetime, timezone, timedelta
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
MONITORED_CHAT_IDS = [-1003258624007, -1004456930190, 3258624007, 4456930190]
ENERGY_CHANNEL = "kharkivenergy"
ENERGY_CHANNEL_IDS = [-1002009071745, 2009071745]

POLL_INTERVAL = int(os.environ.get("UTILITY_POLL_INTERVAL", os.environ.get("POLL_INTERVAL", "120")))


def extract_energy_schedule(text: str) -> str | None:
    """Витягує графік відключень для черги 1.1 (Берестин) з офіційних повідомлень Харківобленерго."""
    if not text:
        return None
    pattern = r'(?:^|[^\d])1\.1\s*[:\-–—]?\s*(.*?)(?=(?:\s+[1-6]\.[12]\b|\s*Перелік|\s*ДЛЯ ПРОМИСЛОВОСТІ|\n\s*\n|$))'
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    raw_val = m.group(1).strip()
    raw_val = re.sub(r'[\s;,.]+$', '', raw_val).strip()
    # Якщо немає вказаного часу або якщо зазначено, що не вимикається - ігноруємо
    if not re.search(r'\d{1,2}:\d{2}', raw_val):
        return None
    if 'не вимикається' in raw_val.lower():
        return None
    return raw_val


def format_energy_message(schedule_time: str) -> str:
    """Форматує офіційне повідомлення про відключення світла для Берестина."""
    return f"Згідно інформації Харківобленерго, у Берестині планується відключення світла: {schedule_time}"


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
        
        # Спам-контроль для світла (накопичення при >3 скаргах за 15 хв)
        self.light_outage_timestamps = []
        self.light_accumulating_until = 0
        self.light_accumulated_locations = set()

        # Вода: кулдаун 30 хв
        self.water_cooldown_until = 0
        self.water_accumulated_locations = set()
        self.water_has_yellow = False

        # Відстеження повідомлень Харківобленерго (дедуплікація та оновлення): {msg_id: {"sent_msg_id": int, "schedule": str, "timestamp": float}}
        self.energy_posts = {}

        # Реєструємо обробник нових повідомлень
        self.client.on(events.NewMessage)(self._on_new_message)
        self.client.on(events.MessageEdited)(self._on_new_message)

    async def _handle_energy_message(self, event_or_msg):
        """Обробляє повідомлення з офіційного каналу Харківобленерго (@kharkivenergy)."""
        text = getattr(event_or_msg, "raw_text", None) or getattr(event_or_msg, "text", "")
        if not text:
            return
            
        schedule = extract_energy_schedule(text)
        msg_id = getattr(event_or_msg, "id", None)
        
        if not schedule:
            logger.debug(f"⚡ [Харківобленерго] Повідомлення {msg_id} не містить відключення черги 1.1")
            return
            
        outage_text = format_energy_message(schedule)
        now_ts = time.time()
        
        # 1. Перевірка: чи цей пост уже оброблявся в пам'яті
        existing = self.energy_posts.get(msg_id)
        if existing:
            if existing.get("schedule") == schedule:
                logger.info(f"⚡ [Харківобленерго] Дублікат для {msg_id} (графік без змін: {schedule})")
                return
            else:
                # Графік оновився в каналі Харківобленерго! Редагуємо повідомлення в каналі
                target_chat_id = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")
                sent_msg_id = existing.get("sent_msg_id")
                if sent_msg_id and self.light_bot and target_chat_id:
                    try:
                        await self.light_bot.edit_message_text(
                            chat_id=target_chat_id,
                            message_id=sent_msg_id,
                            text=outage_text
                        )
                        existing["schedule"] = schedule
                        existing["timestamp"] = now_ts
                        logger.info(f"⚡ [Харківобленерго] Відредаговано повідомлення в каналі (пост {msg_id}): {outage_text}")
                        return
                    except Exception as e:
                        logger.warning(f"Не вдалося відредагувати повідомлення {sent_msg_id}: {e}")
        
        # 2. Безстанова перевірка (Stateless Deduplication):
        # Якщо бот перезапустився, перевіримо останні повідомлення в каналі
        target_chat_id = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")
        try:
            if target_chat_id:
                async for past_msg in self.client.iter_messages(int(target_chat_id), limit=20):
                    if past_msg.text and outage_text.strip() in past_msg.text.strip():
                        logger.info(f"⚡ [Харківобленерго] Повідомлення вже є в каналі: {outage_text}")
                        self.energy_posts[msg_id] = {
                            "sent_msg_id": past_msg.id,
                            "schedule": schedule,
                            "timestamp": now_ts
                        }
                        return
        except Exception as e:
            logger.debug(f"Stateless dedup error (energy): {e}")
            
        # 3. Публікація нового повідомлення в цільовий канал
        if self.light_bot and target_chat_id:
            try:
                sent = await self.light_bot.send_message(
                    chat_id=target_chat_id,
                    text=outage_text
                )
                self.energy_posts[msg_id] = {
                    "sent_msg_id": getattr(sent, "message_id", None),
                    "schedule": schedule,
                    "timestamp": now_ts
                }
                logger.info(f"💡 [Харківобленерго] Опубліковано графік світла для Берестина: {outage_text}")
            except Exception as e:
                logger.error(f"Помилка відправки повідомлення Харківобленерго: {e}")

    def _format_status_message(self, clean_text: str) -> str:
        """Перетворює текст в список з булітами."""
        match = re.match(r"(🔴 Відключення світла:|🔵 Відключення води:|🟡 Питання щодо наявності світла:|🟡 Питання щодо наявності води:)\s*(.*)", clean_text)
        if match:
            header = match.group(1)
            rest = match.group(2)
            rest = re.sub(r'\(мешканці.*?\)', '', rest).strip()
            locs = [l.strip().strip('.').strip() for l in rest.split(',') if l.strip().strip('.').strip()]
            if "Берестин" in locs and len(locs) > 1:
                locs.remove("Берестин")
            
            formatted = header + "\n\n"
            for loc in locs:
                if loc:
                    formatted += f"- {loc}\n"
            return formatted.strip()
        return clean_text

    async def _on_new_message(self, event):
        """Обробник нових повідомлень у комунальних чатах та каналі Харківобленерго."""
        text = event.raw_text
        if not text:
            return
            
        chat = await event.get_chat()
        chat_username = getattr(chat, "username", "")
        chat_id = getattr(event, "chat_id", 0)
        
        # 1. Перевірка на канал Харківобленерго
        is_energy = False
        if chat_username and chat_username.lower() == ENERGY_CHANNEL.lower():
            is_energy = True
        elif chat_id in ENERGY_CHANNEL_IDS:
            is_energy = True
            
        if is_energy:
            await self._handle_energy_message(event)
            return

        # 2. Перевірка на чати скарг мешканців
        is_monitored = False
        if chat_username and chat_username.lower() in [c.lower() for c in MONITORED_CHATS]:
            is_monitored = True
        elif chat_id in MONITORED_CHAT_IDS:
            is_monitored = True
            
        if not is_monitored:
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

        energy_poll_counter = 0
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            
            # Періодичний фолбек-опит каналу Харківобленерго (кожні ~6 хв)
            energy_poll_counter += 1
            if energy_poll_counter >= 3:
                energy_poll_counter = 0
                try:
                    msgs = await self.client.get_messages(ENERGY_CHANNEL, limit=3)
                    for m in reversed(msgs):
                        await self._handle_energy_message(m)
                except Exception as pe:
                    logger.debug(f"Energy periodic check: {pe}")
            
            now_ts = time.time()
            
            # --- ПЕРЕВІРКА КУЛДАУНУ ВОДИ ---
            if now_ts >= self.water_cooldown_until and (self.water_accumulated_locations or self.water_has_yellow):
                locs = list(self.water_accumulated_locations)
                if "Берестин" in locs and len(locs) > 1:
                    locs.remove("Берестин")
                    
                if self.water_accumulated_locations:
                    header = "🔵 Відключення води:\n\n"
                    for loc in locs:
                        header += f"- {loc}\n"
                    header += "- мешканці повідомляють про відсутність води"
                    text_to_send = header
                else:
                    text_to_send = "🟡 Питання щодо наявності води:\n\n- Берестин\n- мешканці цікавляться станом водопостачання"
                    
                if self.water_bot:
                    try:
                        await self.water_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text_to_send)
                        logger.info(f"💧 Відправлено зведення води (після сну): {text_to_send}")
                    except Exception as e:
                        logger.error(f"Water summary send error: {e}")
                
                self.water_accumulated_locations.clear()
                self.water_has_yellow = False
                self.water_cooldown_until = now_ts + 1800  # Спимо ще 30 хв

            # 1. ПЕРЕВІРКА ПАЧКИ (Публікація зібраних адрес світла, якщо минув час)
            if self.light_accumulated_locations and now_ts >= self.light_accumulating_until:
                locs = list(self.light_accumulated_locations)
                if "Берестин" in locs and len(locs) > 1:
                    locs.remove("Берестин")
                
                header = "🔴 Відключення світла:\n\n"
                for loc in locs:
                    header += f"- {loc}\n"
                
                try:
                    await self.light_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=header.strip())
                    logger.info(f"🕒 ПАЧКА ОПУБЛІКОВАНА: {locs}")
                except Exception as e:
                    logger.error(f"Error sending accumulated batch: {e}")
                
                self.light_accumulated_locations.clear()
                self.light_outage_timestamps.clear()
                self.light_accumulating_until = 0

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
                        
                        is_yellow = "🟡" in clean_text or "Питання щодо наявності" in clean_text
                        
                        # STATELESS DEDUPLICATION (вікно 30 хв)
                        is_duplicate = False
                        try:
                            now_ts = time.time()
                            # Шукаємо найсвіжіший наш пост про світло у каналі
                            async for past_msg in self.client.iter_messages(int(TELEGRAM_CHAT_ID), limit=10):
                                if past_msg.date and (now_ts - past_msg.date.timestamp()) < DEDUP_WINDOW:
                                    if not past_msg.text:
                                        continue
                                        
                                    # Якщо це ЖОВТИЙ СТАТУС (питання): перевіряємо, чи були будь-які питання за останні 30 хв
                                    if is_yellow and "Питання щодо наявності світла" in past_msg.text:
                                        is_duplicate = True
                                        logger.info("💡 Жовтий статус на кулдауні (вже питали про світло за останні 30 хв).")
                                        break
                                        
                                    # Якщо це ЧЕРВОНИЙ СТАТУС (відключення): порівнюємо по адресах тільки з червоними постами
                                    elif not is_yellow and "Відключення світла" in past_msg.text:
                                        past_locs = set(re.findall(r'\b[А-ЯІЇЄ][а-яіїє\']+\b', past_msg.text)) - _STOP
                                        if new_locations and new_locations.issubset(past_locs):
                                            is_duplicate = True
                                        break
                        except Exception as e:
                            logger.error(f"Stateless dedup error (light): {e}")
                            
                        if not is_duplicate:
                            formatted_text = self._format_status_message(clean_text)
                            now_ts = time.time()
                            
                            if not is_yellow:
                                actual_locs = new_locations if new_locations else {"Берестин"}
                                is_accumulating = now_ts < self.light_accumulating_until
                                
                                if is_accumulating:
                                    self.light_accumulated_locations.update(actual_locs)
                                    logger.info(f"💡 📦 Режим збору. Додано до пачки: {actual_locs}")
                                else:
                                    # Очищаємо старі таймстемпи (старші 15 хв = 900 сек)
                                    self.light_outage_timestamps = [ts for ts in self.light_outage_timestamps if now_ts - ts < 900]
                                    self.light_outage_timestamps.append(now_ts)
                                    
                                    if len(self.light_outage_timestamps) >= 3:
                                        logger.info("💡 🚨 СПАМ-КОНТРОЛЬ! 3 повідомлення за 15 хв. Вмикаємо режим збору на 30 хв.")
                                        self.light_accumulating_until = now_ts + 1800
                                        self.light_accumulated_locations.update(actual_locs)
                                    else:
                                        try:
                                            await self.light_bot.send_message(
                                                chat_id=TELEGRAM_CHAT_ID,
                                                text=formatted_text
                                            )
                                            logger.info(f"💡 Відправлено статус світла: {formatted_text}")
                                        except Exception as e:
                                            logger.error(f"Light send error: {e}")
                            else:
                                # Жовті повідомлення просто публікуємо відформатованими
                                try:
                                    await self.light_bot.send_message(
                                        chat_id=TELEGRAM_CHAT_ID,
                                        text=formatted_text
                                    )
                                    logger.info(f"💡 Відправлено статус світла: {formatted_text}")
                                except Exception as e:
                                    logger.error(f"Light yellow send error: {e}")
                        else:
                            logger.info("💡 Дублікат (ті самі адреси за 30 хв). Пропускаємо.")
                        
                    elif "[WATER]" in line and self.water_bot:
                        clean_text = line.replace("[WATER]", "").strip()
                        clean_text = self._replace_city_name(clean_text)
                        
                        new_locations = set(re.findall(r'[А-ЯІЇЄ][а-яіїє\']+', clean_text)) - _STOP
                        is_yellow = "🟡" in clean_text or "Питання щодо наявності" in clean_text
                        actual_locs = new_locations if new_locations else {"Берестин"}
                        
                        now_ts = time.time()
                        
                        # Якщо на старті не ініціалізували, перевіримо безстаново
                        if self.water_cooldown_until == 0:
                            try:
                                async for past_msg in self.client.iter_messages(int(TELEGRAM_CHAT_ID), limit=10):
                                    if past_msg.date and (now_ts - past_msg.date.timestamp()) < DEDUP_WINDOW:
                                        if past_msg.text and ("Відключення води" in past_msg.text or "Питання щодо наявності води" in past_msg.text):
                                            self.water_cooldown_until = past_msg.date.timestamp() + 1800
                                            break
                            except Exception as e:
                                pass
                                
                        if now_ts < self.water_cooldown_until:
                            # Бот спить - акумулюємо
                            if is_yellow:
                                self.water_has_yellow = True
                            else:
                                self.water_accumulated_locations.update(actual_locs)
                            logger.info(f"💧 Режим сну. Додано до зведення води: {actual_locs}")
                        else:
                            # Бот не спить - відправляємо і засинаємо
                            formatted_text = self._format_status_message(clean_text)
                            try:
                                await self.water_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=formatted_text)
                                logger.info(f"💧 Відправлено статус води: {formatted_text}")
                            except Exception as e:
                                logger.error(f"Water send error: {e}")
                                
                            self.water_cooldown_until = now_ts + 1800
                        
            except Exception as e:
                logger.error(f"Помилка обробки Gemini або відправки: {e}")

    async def start(self):
        """Запускає фонову обробку батчів та ініціалізує моніторинг каналу Харківобленерго."""
        logger.info("=" * 50)
        logger.info("💧/💡 Монітор комуналки (світло/вода) запущено!")
        logger.info(f"📺 Чати мешканців: {', '.join('@' + c for c in MONITORED_CHATS)}")
        logger.info(f"⚡ Офіційний канал енерго: @{ENERGY_CHANNEL}")
        logger.info("=" * 50)
        
        # Підписка та підтягування свіжих повідомлень Харківобленерго за останні 24 години
        try:
            from telethon.tl.functions.channels import JoinChannelRequest
            try:
                await self.client(JoinChannelRequest(ENERGY_CHANNEL))
            except Exception:
                pass
                
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            msgs = await self.client.get_messages(ENERGY_CHANNEL, limit=10)
            for m in reversed(msgs):
                if m and m.date and m.date >= cutoff:
                    await self._handle_energy_message(m)
        except Exception as e:
            logger.warning(f"Catch-up для @{ENERGY_CHANNEL}: {e}")

        # Запускаємо безкінечний цикл батчингу як фонову таску
        asyncio.create_task(self._process_batch_loop())

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
import pytz

from telethon import TelegramClient
from telegram import Bot
from telegram.constants import ParseMode
import google.generativeai as genai

logger = logging.getLogger(__name__)

JOB_BOT_TOKEN = os.environ.get("JOB_BOT_TOKEN", "8929491816:AAEYhwJkhERlHw204InyzDvHpTuiK0JVQ-Y")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1001110859952")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

CHATS = ["krasnograd3serzem", "krasnogradbezp"]

class JobBot:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot = Bot(token=JOB_BOT_TOKEN) if JOB_BOT_TOKEN else None
        
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            self.model = genai.GenerativeModel("gemini-flash-lite-latest")
        else:
            self.model = None
            
        self.tz = pytz.timezone('Europe/Kyiv')
        self.last_posted_date = None

    async def _fetch_and_process(self):
        now = datetime.now(self.tz)
        
        # Від 17:30 попереднього дня до 17:20 поточного
        end_time = now.replace(hour=17, minute=20, second=0, microsecond=0)
        start_time = (end_time - timedelta(days=1)).replace(hour=17, minute=30)
        
        logger.info(f"Збираємо вакансії (та картинки) від {start_time} до {end_time}")
        
        messages_data = []
        for chat in CHATS:
            try:
                async for msg in self.client.iter_messages(chat):
                    msg_date = msg.date.astimezone(self.tz)
                    if msg_date < start_time:
                        break
                    if msg_date > end_time:
                        continue
                        
                    has_photo = msg.photo is not None
                    raw_text = msg.raw_text or ""
                    
                    if len(raw_text.strip()) < 5 and not has_photo:
                        continue
                        
                    sender = await msg.get_sender()
                    username = "Невідомо"
                    if sender:
                        if getattr(sender, "username", None):
                            username = f"@{sender.username}"
                        elif getattr(sender, "first_name", None):
                            username = sender.first_name
                            if getattr(sender, "last_name", None):
                                username += f" {sender.last_name}"
                                
                    photo_bytes = None
                    if has_photo:
                        try:
                            photo_bytes = await self.client.download_media(msg, file=bytes)
                        except Exception as e:
                            logger.error(f"Не вдалося завантажити фото: {e}")
                            
                    messages_data.append({
                        "user": username,
                        "text": raw_text[:300].replace('\n', ' '),
                        "link": f"https://t.me/{chat}/{msg.id}",
                        "photo_bytes": photo_bytes
                    })
            except Exception as e:
                logger.error(f"Помилка чату {chat} (Робота): {e}")
                
        if not messages_data:
            return "За останню добу оголошень про роботу не знайдено."
            
        # --- OCR PASS ---
        msgs_with_photos = [m for m in messages_data if m.get("photo_bytes")]
        logger.info(f"📸 Знайдено повідомлень з фото для OCR: {len(msgs_with_photos)}")
        
        batch_size_ocr = 5
        for i in range(0, len(msgs_with_photos), batch_size_ocr):
            batch = msgs_with_photos[i:i + batch_size_ocr]
            
            contents = [
                "Read the text from these images exactly in the order they are provided. Return ONLY a valid JSON array of strings, where each string is the recognized text from the corresponding image. Example format: [\"text from image 1\", \"text from image 2\"]. If there is no text in an image, use an empty string \"\" for that index. No markdown!"
            ]
            for m in batch:
                contents.append({"mime_type": "image/jpeg", "data": m["photo_bytes"]})
                
            try:
                response = await asyncio.to_thread(self.model.generate_content, contents)
                resp_text = response.text.strip()
                if resp_text.startswith("```json"): resp_text = resp_text[7:]
                if resp_text.endswith("```"): resp_text = resp_text[:-3]
                
                texts = json.loads(resp_text.strip())
                for m, txt in zip(batch, texts):
                    if txt.strip():
                        m["text"] = f"{m['text']} [Текст на фото: {txt.strip()}]".strip()
            except Exception as e:
                logger.error(f"OCR Error for batch: {e}")
                
        # Clean up photo_bytes to save memory
        for m in messages_data:
            if "photo_bytes" in m:
                del m["photo_bytes"]
                
        # --- CATEGORIZATION PASS ---
        logger.info(f"🧠 Фільтруємо вакансії (всього {len(messages_data)} повідомлень)...")
        batch_size_cat = 50
        batches = [messages_data[i:i + batch_size_cat] for i in range(0, len(messages_data), batch_size_cat)]
        
        vacancies = []
        seeking = []
        
        prompt_template = """Ти помічник, який фільтрує повідомлення з місцевого чату про роботу.
Твоя задача — розділити повідомлення на дві категорії:
1) Вакансії (vacancies) - ТІЛЬКИ конкретні пропозиції від роботодавців, які шукають працівників (напр. "Потрібен продавець", "Шукаємо вантажника", "Запрошуємо на роботу").
2) Шукають роботу (seeking) - люди, які хочуть знайти роботу АБО просто ставлять ПИТАННЯ (напр. "Питання щодо роботи", "Шукаю підробіток", "Чи є вільні вакансії?"). Будь-які запитання чи роздуми віднось до seeking!

СУВОРО ІГНОРУЙ: оренду житла, продаж речей, послуги таксі (якщо це реклама поїздки, а не найм водія), новини та інший спам.

Ось список повідомлень (JSON):
{json_data}

Поверни ТІЛЬКИ валідний JSON у форматі:
{
  "vacancies": [
     {"user": "username", "summary": "Потрібен продавець-консультант", "link": "https://t.me/..."}
  ],
  "seeking": [
     {"user": "username", "summary": "Шукаю підробіток на вихідні", "link": "https://t.me/..."}
  ]
}
Дуже важливо: повертай ТОЧНО ТЕ САМЕ посилання (link), яке було передано тобі у вхідному JSON для відповідного повідомлення!
Якщо нічого не знайдено, поверни порожні масиви. Без форматування markdown!"""
        
        for batch in batches:
            try:
                batch_json = json.dumps(batch, ensure_ascii=False)
                prompt = prompt_template.replace("{json_data}", batch_json)
                
                response = await asyncio.to_thread(self.model.generate_content, prompt)
                resp_text = response.text.strip()
                if resp_text.startswith("```json"):
                    resp_text = resp_text[7:]
                if resp_text.endswith("```"):
                    resp_text = resp_text[:-3]
                    
                parsed = json.loads(resp_text.strip())
                vacancies.extend(parsed.get("vacancies", []))
                seeking.extend(parsed.get("seeking", []))
            except Exception as e:
                logger.error(f"Gemini batch error (Робота): {e}")
                
        def dedup(arr):
            seen = set()
            res = []
            for item in arr:
                u = item.get("user", "")
                if u not in seen:
                    seen.add(u)
                    res.append(item)
            return res
            
        vacancies = dedup(vacancies)
        seeking = dedup(seeking)
        
        output = "<b>За останню добу:</b>\n\n"
        output += "<b>💼 Вакансії</b>\n"
        if vacancies:
            for idx, item in enumerate(vacancies, 1):
                user = item.get('user', 'Невідомо')
                summary = item.get('summary', '')
                link = item.get('link', '')
                output += f"{idx}. {user} | <a href='{link}'>{summary}</a>\n"
        else:
            output += "- Немає оголошень\n"
            
        output += "\n<b>🔎 Шукали роботу</b>\n"
        if seeking:
            for idx, item in enumerate(seeking, 1):
                user = item.get('user', 'Невідомо')
                summary = item.get('summary', '')
                link = item.get('link', '')
                output += f"{idx}. {user} | <a href='{link}'>{summary}</a>\n"
        else:
            output += "- Немає оголошень\n"
            
        return output

    async def _scheduler_loop(self):
        while True:
            now = datetime.now(self.tz)
            date_key = now.strftime("%m-%d")
            
            target_start = now.replace(hour=17, minute=20, second=0, microsecond=0)
            target_end = now.replace(hour=17, minute=35, second=0, microsecond=0)
            
            if target_start <= now < target_end and self.last_posted_date != date_key:
                if self.bot and self.model:
                    try:
                        logger.info("Починаємо збір та обробку вакансій...")
                        report = await self._fetch_and_process()
                        
                        logger.info("Звіт про роботу готовий. Очікуємо 17:30 для публікації...")
                        publish_time = now.replace(hour=17, minute=30, second=0, microsecond=0)
                        while datetime.now(self.tz) < publish_time:
                            await asyncio.sleep(10)
                            
                        logger.info("17:30 - Публікуємо звіт про роботу!")
                        # STATELESS DEDUPLICATION
                        is_duplicate = False
                        if self.client:
                            try:
                                import time
                                now_ts = time.time()
                                async for past_msg in self.client.iter_messages(int(TELEGRAM_CHAT_ID), limit=20):
                                    if past_msg.date and (now_ts - past_msg.date.timestamp()) < 86400:
                                        if past_msg.text and "💼 Вакансії" in past_msg.text and "За останню добу" in past_msg.text:
                                            msg_date_local = past_msg.date.astimezone(self.tz).strftime("%m-%d")
                                            if msg_date_local == date_key:
                                                is_duplicate = True
                                                break
                            except Exception as e:
                                logger.error(f"Stateless dedup error (job): {e}")
                                
                        if not is_duplicate:
                            await self.bot.send_message(
                                chat_id=TELEGRAM_CHAT_ID,
                                text=report,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True
                            )
                        else:
                            logger.info("Звіт про роботу вже був опублікований сьогодні. Пропускаємо.")
                        logger.info("✅ Пост про роботу успішно опубліковано!")
                        self.last_posted_date = date_key
                    except Exception as e:
                        logger.error(f"Помилка підготовки/публікації вакансій: {e}")
            
            await asyncio.sleep(30)

    async def start(self):
        logger.info("=" * 50)
        logger.info("💼 Бот 'Робота / Вакансії' запущено!")
        logger.info("=" * 50)
        
        # Catch-up logic: check if we missed today's 17:30 post
        try:
            now = datetime.now(self.tz)
            date_key = now.strftime("%m-%d")
            if now.hour >= 17 and (now.hour > 17 or now.minute >= 30):
                is_duplicate = False
                if self.client:
                    import time
                    now_ts = time.time()
                    async for past_msg in self.client.iter_messages(int(TELEGRAM_CHAT_ID), limit=20):
                        if past_msg.date and (now_ts - past_msg.date.timestamp()) < 86400:
                            if past_msg.text and "💼 Вакансії" in past_msg.text and "За останню добу" in past_msg.text:
                                msg_date_local = past_msg.date.astimezone(self.tz).strftime("%m-%d")
                                if msg_date_local == date_key:
                                    is_duplicate = True
                                    break
                
                if not is_duplicate:
                    logger.info("💼 Пропущено пост про вакансії! Публікуємо зараз...")
                    report = await self._fetch_and_process()
                    await self.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=report,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                    self.last_posted_date = date_key
                    logger.info("✅ Пропущений пост про вакансії успішно опубліковано!")
        except Exception as e:
            logger.error(f"Помилка при catch-up перевірці (Робота): {e}")
            
        asyncio.create_task(self._scheduler_loop())

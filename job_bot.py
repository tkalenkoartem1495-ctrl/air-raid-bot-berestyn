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
Знайди повідомлення, де:
1) Пропонують роботу / Вакансії (шукають працівників, наймають на роботу, пропонують підробіток)
2) Шукають роботу (людина пропонує свої послуги як працівник, шукає постійну роботу або підробіток)

СУВОРО ІГНОРУЙ: оренду житла, продаж речей, послуги таксі (якщо це просто реклама поїздки, а не найм водія), новини, продаж косметики/речей та інший спам.
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
            
            if now.hour == 17 and now.minute == 20 and self.last_posted_date != date_key:
                if self.bot and self.model:
                    try:
                        logger.info("17:20 - Починаємо збір та обробку вакансій (маємо 10 хв в запасі)...")
                        report = await self._fetch_and_process()
                        
                        logger.info("Звіт про роботу готовий. Очікуємо 17:30 для публікації...")
                        while True:
                            wait_now = datetime.now(self.tz)
                            if wait_now.hour == 17 and wait_now.minute >= 30:
                                break
                            await asyncio.sleep(10)
                            
                        logger.info("17:30 - Публікуємо звіт про роботу!")
                        await self.bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=report,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True
                        )
                        logger.info("✅ Пост про роботу успішно опубліковано!")
                        self.last_posted_date = date_key
                    except Exception as e:
                        logger.error(f"Помилка підготовки/публікації вакансій: {e}")
            
            await asyncio.sleep(30)

    async def start(self):
        logger.info("=" * 50)
        logger.info("💼 Бот 'Робота / Вакансії' запущено!")
        logger.info("=" * 50)
        asyncio.create_task(self._scheduler_loop())

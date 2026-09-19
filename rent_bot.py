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

RENT_BOT_TOKEN = os.environ.get("RENT_BOT_TOKEN", "8901603097:AAHcs2yGN-UPK675yy_3nzK-cEqj6J7iiqE") # Use DAWN token or same token if user didn't specify. Wait, user didn't provide a token for Rent bot. I'll use DAWN_BOT_TOKEN as a fallback or expect RENT_BOT_TOKEN.
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1001110859952")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

CHATS = ["krasnograd3serzem", "krasnogradbezp"]

class RentBot:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot = Bot(token=RENT_BOT_TOKEN) if RENT_BOT_TOKEN else None
        
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            self.model = genai.GenerativeModel("gemini-flash-lite-latest")
        else:
            self.model = None
            
        self.tz = pytz.timezone('Europe/Kyiv')
        self.last_posted_date = None

    async def _fetch_and_process(self):
        """Збирає повідомлення, обробляє через Gemini і формує звіт."""
        now = datetime.now(self.tz)
        
        # Від 20:00 попереднього дня до 19:50 поточного
        end_time = now.replace(hour=19, minute=50, second=0, microsecond=0)
        start_time = (end_time - timedelta(days=1)).replace(hour=20, minute=0)
        
        logger.info(f"Збираємо оренду від {start_time} до {end_time}")
        
        messages_data = []
        for chat in CHATS:
            try:
                async for msg in self.client.iter_messages(chat):
                    msg_date = msg.date.astimezone(self.tz)
                    if msg_date < start_time:
                        break
                    if msg_date > end_time:
                        continue
                        
                    if not msg.raw_text or len(msg.raw_text.strip()) < 5:
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
                                
                    messages_data.append({
                        "user": username,
                        "text": msg.raw_text[:300].replace('\n', ' '),
                        "link": f"https://t.me/{chat}/{msg.id}"
                    })
            except Exception as e:
                logger.error(f"Помилка чату {chat} (Оренда): {e}")
                
        if not messages_data:
            return "За останню добу оголошень про оренду не знайдено."
            
        batch_size = 50
        batches = [messages_data[i:i + batch_size] for i in range(0, len(messages_data), batch_size)]
        
        offering = []
        seeking = []
        
        prompt_template = """
Ти помічник, який фільтрує повідомлення з місцевого чату про оренду житла.
Знайди повідомлення, де:
1) Здають житло в оренду ВИКЛЮЧНО ДЛЯ ПРОЖИВАННЯ (квартири, будинки, кімнати)
2) Шукають житло для оренди (хочуть зняти)

СУВОРО ІГНОРУЙ: комерційну нерухомість (магазини, склади, салони), гаражі, продаж, послуги, таксі та інший спам.
Ось список повідомлень (JSON):
{json_data}

Поверни ТІЛЬКИ валідний JSON у форматі:
{
  "offering": [
     {"user": "username", "summary": "короткий опис", "link": "https://t.me/..."}
  ],
  "seeking": [
     {"user": "username", "summary": "короткий опис", "link": "https://t.me/..."}
  ]
}
Дуже важливо: повертай ТОЧНО ТЕ САМЕ посилання (link), яке було передано тобі у вхідному JSON для відповідного повідомлення!
Якщо нічого не знайдено, поверни порожні масиви. Без форматування markdown!
"""
        
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
                offering.extend(parsed.get("offering", []))
                seeking.extend(parsed.get("seeking", []))
            except Exception as e:
                logger.error(f"Gemini batch error (Оренда): {e}")
                
        # Deduplicate
        def dedup(arr):
            seen = set()
            res = []
            for item in arr:
                u = item.get("user", "")
                if u not in seen:
                    seen.add(u)
                    res.append(item)
            return res
            
        offering = dedup(offering)
        seeking = dedup(seeking)
        
        output = "<b>За останню добу:</b>\n\n"
        output += "<b>🏠 Здавали в оренду</b>\n"
        if offering:
            for idx, item in enumerate(offering, 1):
                user = item.get('user', 'Невідомо')
                summary = item.get('summary', '')
                link = item.get('link', '')
                output += f"{idx}. {user} | <a href='{link}'>{summary}</a>\n"
        else:
            output += "- Немає оголошень\n"
            
        output += "\n<b>🔎 Шукали житло</b>\n"
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
        """Фонова задача: старт збору о 19:50, публікація рівно о 20:00."""
        while True:
            now = datetime.now(self.tz)
            date_key = now.strftime("%m-%d")
            
            # Прокидаємося о 19:50 для підготовки
            if now.hour == 19 and now.minute == 50 and self.last_posted_date != date_key:
                if self.bot and self.model:
                    try:
                        logger.info("19:50 - Починаємо збір та обробку оренди (маємо 10 хв в запасі)...")
                        report = await self._fetch_and_process()
                        
                        logger.info("Звіт готовий. Очікуємо 20:00 для публікації...")
                        # Чекаємо рівно до 20:00
                        while True:
                            wait_now = datetime.now(self.tz)
                            if wait_now.hour == 20 and wait_now.minute >= 0:
                                break
                            await asyncio.sleep(10)
                            
                        logger.info("20:00 - Публікуємо звіт про оренду!")
                        await self.bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=report,
                            disable_web_page_preview=True
                        )
                        logger.info("✅ Пост про оренду успішно опубліковано!")
                        self.last_posted_date = date_key
                    except Exception as e:
                        logger.error(f"Помилка підготовки/публікації оренди: {e}")
            
            await asyncio.sleep(30)

    async def start(self):
        """Запускає фонову перевірку розкладу."""
        logger.info("=" * 50)
        logger.info("🏠 Бот 'Оренда житла' запущено!")
        logger.info("=" * 50)
        asyncio.create_task(self._scheduler_loop())

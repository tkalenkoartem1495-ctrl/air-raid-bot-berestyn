import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
import pytz

from telethon import TelegramClient
from telegram import Bot
from telegram.constants import ParseMode
import google.generativeai as genai

logger = logging.getLogger(__name__)

RENT_BOT_TOKEN = os.environ.get("RENT_BOT_TOKEN", "8901603097:AAEJ894cMqFcYrd3L-SWzQOgsllcuYGU7t4")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1001110859952")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

CHATS = ["krasnograd3serzem", "krasnogradbezp"]

RENT_KEYWORDS = [
    "аренд", "оренд", "сним", "знім", "сда", "зда", 
    "квартир", "дом", "будин", "комнат", "кімнат", 
    "житл", "жиль"
]

def smart_dedup(arr):
    seen_links = set()
    res = []
    for item in arr:
        link = item.get("link", "")
        if link and link in seen_links:
            continue
        seen_links.add(link)
        res.append(item)
    return res

class RentBot:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot = Bot(token=RENT_BOT_TOKEN) if RENT_BOT_TOKEN else None
        
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            self.model = genai.GenerativeModel("gemini-flash-latest")
        else:
            self.model = None
            
        self.tz = pytz.timezone('Europe/Kyiv')
        self.last_posted_date = None

    def _rule_based_classify(self, messages):
        """Простий резервний класифікатор (на випадок недоступності AI або для тестів)."""
        offering = []
        seeking = []

        for m in messages:
            t = m.get("text", "").lower()
            user = m.get("user", "Невідомо")
            link = m.get("link", "")

            # Ігноруємо спам про чати / гаражі / прибирання
            if any(w in t for w in ["гараж", "бокс", "склад", "офіс", "прибирання", "чат", "силку", "посилання", "зачепиловк"]):
                continue

            # Здача (тільки пряма пропозиція від власника/орендодавця)
            if any(w in t for w in ["здам", "сдам", "здається", "сдается"]) and not any(w in t for w in ["шука", "знім", "сним", "ищу", "?"]):
                offering.append({"user": user, "summary": m.get("text", "")[:80].strip(), "link": link})
            # Пошук
            elif any(w in t for w in ["шука", "знім", "сниму", "ищу", "кто сдает", "хто здає"]):
                desc = "Квартира"
                if any(w in t for w in ["2-кімнатн", "2-комнатн", "двокімнатн", "двухкомнатн", "2 комнатн", "2 кімнатн"]):
                    desc = "2-кімнатна квартира"
                elif any(w in t for w in ["1-кімнатн", "1-комнатн", "однокімнатн", "однокомнатн", "1 комнатн", "1 кімнатн"]):
                    desc = "1-кімнатна квартира"
                elif any(w in t for w in ["3-кімнатн", "3-комнатн"]):
                    desc = "3-кімнатна квартира"
                elif any(w in t for w in ["будин", "дом"]):
                    desc = "Будинок"
                elif any(w in t for w in ["кімнат", "комнат"]):
                    desc = "Кімната"
                if "без тварин" in t or "без животных" in t:
                    desc += " (без тварин)"
                phone_match = re.search(r"(\+?380\d{9}|0\d{9})", m.get("text", ""))
                if phone_match and phone_match.group(0) not in desc:
                    desc += f" ({phone_match.group(0)})"
                seeking.append({"user": user, "summary": desc, "link": link})

        return {"offering": smart_dedup(offering), "seeking": smart_dedup(seeking)}

    async def _fetch_and_process(self):
        """Збирає повідомлення за минулу добу, відправляє на Gemini і формує звіт."""
        now = datetime.now(self.tz)
        cutoff = now - timedelta(hours=24)
        
        logger.info(f"Збираємо оренду за минулу добу (від {cutoff} до {now})")
        
        messages_data = []
        for chat in CHATS:
            try:
                async for msg in self.client.iter_messages(chat):
                    msg_date = msg.date.astimezone(self.tz)
                    if msg_date < cutoff:
                        break
                        
                    if not msg.raw_text or len(msg.raw_text.strip()) < 5:
                        continue
                        
                    text_lower = msg.raw_text.lower()
                    if not any(k in text_lower for k in RENT_KEYWORDS):
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
                        "text": msg.raw_text[:400].replace('\n', ' '),
                        "link": f"https://t.me/{chat}/{msg.id}"
                    })
            except Exception as e:
                logger.error(f"Помилка чату {chat} (Оренда): {e}")
                
        if not messages_data:
            return "<b>За останню добу:</b>\n\n<b>🏠 Здавали в оренду</b>\n- Немає оголошень\n\n<b>🔎 Шукали житло</b>\n- Немає оголошень"
            
        offering = []
        seeking = []
        classified = False

        if self.model:
            try:
                prompt = f"""Ти редактор міського каналу міста Берестин.
Твоє завдання — проаналізувати повідомлення мешканців за минулу добу та відібрати дійсні оголошення про ОРЕНДУ ЖИТЛА у Берестині (Краснограді) та районі.

Список повідомлень (JSON):
{json.dumps(messages_data, ensure_ascii=False)}

ІНСТРУКЦІЯ:
1. "offering" (Здають житло):
   - Тільки конкретні пропозиції від орендодавців/власників, які здають житло (слова "Здам", "Сдам", "Здається", "Сдается").
   - УВАГА: "Сдам" російською означає "Здам" — це ОРЕНДОДАВЕЦЬ (offering), ніколи не додавай таких у seeking!
   - КАТЕГОРИЧНО ЗАБОРОНЕНО додавати сюди: запитання мешканців ("хто здає?", "де здають?"), прохання знайти чат/групу ("у кого є чат де здають квартири?").

2. "seeking" (Шукають житло):
   - Люди, які шукають житло в оренду для себе ("зніму", "сниму", "шукаю", "ищу", "підкажіть, хто здає 1-к квартиру").
   - НЕ додавати сюди тих, хто здає ("здам", "сдам")!

3. ТОЧНІСТЬ ОПИСУ (summary):
   - Summary має бути коротким (до 5-8 слів) і СУВОРО правдивим за текстом повідомлення.
   - НЕ вигадуй деталей! Якщо автор написав "без тварин" — КАТЕГОРИЧНО ЗАБОРОНЕНО писати "з песиком/собакою"!
   - Якщо шукають "1-кімнатну квартиру" — пиши "1-кімнатна квартира", а не "Кімната".
   - Якщо шукають квартиру — не пиши "Будинок".
   - Якщо вказано номер телефону (наприклад 050..., 066...) — обов'язково додай його в дужках до summary.

4. ІГНОРУВАТИ (НЕ ВКЛЮЧАТИ НІКУДИ):
   - Запитання про наявність чатів, груп, посилань ("у кого є чат...", "скиньте силку на чат").
   - Гаражі, бокси, склади, офіси, магазини, комерційні приміщення.
   - Послуги (прибирання, ремонти, догляд) та продаж.
   - Оголошення щодо інших населених пунктів (Зачепилівка тощо).

Формат відповіді (ТІЛЬКИ валідний JSON):
{{
  "offering": [
    {{"user": "@username або Ім'я", "summary": "Короткий опис типу житла та умови (тел. у дужках якщо є)", "link": "https://t.me/..."}}
  ],
  "seeking": [
    {{"user": "@username або Ім'я", "summary": "Короткий точний опис, що шукають (тел. у дужках якщо є)", "link": "https://t.me/..."}}
  ]
}}"""
                response = await asyncio.to_thread(self.model.generate_content, prompt)
                resp_text = response.text.strip()
                match = re.search(r'\{.*\}', resp_text, re.DOTALL)
                if match:
                    parsed = json.loads(match.group(0))
                    offering = parsed.get("offering", [])
                    seeking = parsed.get("seeking", [])
                    classified = True
            except Exception as e:
                logger.error(f"Gemini error (Оренда): {e}. Застосовуємо резервний класифікатор.")

        if not classified:
            rule_res = self._rule_based_classify(messages_data)
            offering = rule_res.get("offering", [])
            seeking = rule_res.get("seeking", [])

        offering = smart_dedup(offering)
        seeking = smart_dedup(seeking)
        
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

    async def _post_to_channel(self, text: str):
        if self.bot:
            try:
                await self.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
                logger.info("✅ Пост про оренду успішно опубліковано!")
            except Exception as e:
                logger.error(f"Помилка відправки в Telegram (RentBot): {e}")

    async def _scheduler_loop(self):
        """Фонова задача: публікація о 17:00 кожного дня."""
        while True:
            try:
                now = datetime.now(self.tz)
                date_key = now.strftime("%m-%d")
                
                # Публікація тільки о 17:00
                if now.hour == 17 and now.minute == 0 and self.last_posted_date != date_key:
                    logger.info("🏠 17:00 — публікація звіту про оренду...")
                    report = await self._fetch_and_process()
                    await self._post_to_channel(report)
                    self.last_posted_date = date_key
            except Exception as e:
                logger.error(f"Помилка в _scheduler_loop (RentBot): {e}")
                        
            await asyncio.sleep(30)

    async def start(self):
        """Запускає бота оренди. При перезапуску сервера НІЧОГО не публікує."""
        logger.info("=" * 50)
        logger.info("🏠 Бот 'Оренда житла' запущено!")
        logger.info("=" * 50)
        
        now = datetime.now(self.tz)
        # Фіксуємо поточну дату, щоб не публікувати при перезапуску
        self.last_posted_date = now.strftime("%m-%d")
        asyncio.create_task(self._scheduler_loop())

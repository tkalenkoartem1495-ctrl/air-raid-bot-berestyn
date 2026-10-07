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
    "житл", "жиль", "посуточн", "подобов", "поселен", "підселен"
]

IRRELEVANT_PATTERNS = [
    r"гараж", r"бокс", r"склад", r"магазин", r"офіс", r"кабінет", r"приміщен",
    r"прибирання", r"сиделк", r"ремонт.*технік", r"плитк", r"керамограніт",
    r"світл", r"обстріл", r"перебої", r"шука[єе][мт]о?\s+(кота|кішку|собак)",
    r"(?:у кого|чи є|хтось має|підкажіть|скиньте|де є|поділіться|шукаю)\s+.*(?:чат|груп[ауі]|канал|посилання|силк)",
    r"\bчат\b.*(?:здають|сдают|оренд|аренд)",
    r"зачепиловк", r"зачепилівк", r"кегичівк", r"кегичевк"
]

OFFER_KEYWORDS = [
    r"\bздам\b", r"\bсдам\b", r"\bздається\b", r"\bсдается\b", 
    r"\bздаю\b", r"\bсдаю\b", r"\bздаємо\b", r"\bсдаем\b"
]

SEEK_KEYWORDS = [
    r"\bшука[юємо]*\b", r"\bищу\b", r"\bищем\b", 
    r"\bзнім[уемо]*\b", r"\bзніме\b", r"\bсниму\b", r"\bснимет\b", r"\bснимем\b"
]

OFFERING_PATTERNS = [
    r"здам\s+(в\s+оренду\s+)?([^\n\.,!]+)",
    r"сдам\s+(в\s+аренду\s+)?([^\n\.,!]+)",
    r"здається\s+([^\n\.,!]+)",
    r"сдается\s+([^\n\.,!]+)",
]

SEEKING_PATTERNS = [
    r"шука[юємо]+\s+(в\s+оренду\s+)?(квартир|будинок|кімнат|житл)",
    r"знім[еу]+\s+(квартир|будинок|кімнат|житл)",
    r"сниму\s+(квартир|будинок|комнат|жиль)",
    r"шукаємо\s+квартиру",
]


def is_valid_offering(summary: str, text: str = "") -> bool:
    """Перевіряє, чи є запис реальною пропозицією оренди від власника."""
    combined = (summary + " " + text).lower()
    if any(re.search(p, combined, re.I) for p in IRRELEVANT_PATTERNS):
        return False
    if any(re.search(p, combined, re.I) for p in SEEK_KEYWORDS):
        return False
    if re.search(r"(?:чат|груп[ауі]|канал|посилання|силк)", combined, re.I):
        return False
    if "?" in combined and any(q in combined for q in ["хто", "чи", "де", "у кого", "підкажіть"]):
        return False
    if re.search(r"(?:у кого|підкажіть|чи є|де є|хто)\s+.*(?:здає|здають|сдает|сдают)", combined, re.I):
        return False
    return True


def is_valid_seeking(summary: str, text: str = "") -> bool:
    """Перевіряє, чи є запис реальним пошуком житла."""
    combined = (summary + " " + text).lower()
    if any(re.search(p, combined, re.I) for p in IRRELEVANT_PATTERNS):
        return False
    if re.search(r"(?:у кого|чи є|де є|підкажіть|скиньте|хтось має|поділіться)\s+.*(?:чат|груп[ауі]|канал|посилання|силк)", combined, re.I):
        return False
    if re.search(r"\bчат\b.*(?:здають|сдают|оренд|аренд)", combined, re.I):
        return False
    has_offer = any(re.search(p, combined, re.I) for p in OFFER_KEYWORDS)
    has_seek = any(re.search(p, combined, re.I) for p in SEEK_KEYWORDS)
    if has_offer and not has_seek and "?" not in combined:
        return False
    return True

def smart_dedup(arr):
    seen_links = set()
    seen_content = set()
    res = []
    for item in arr:
        link = item.get("link", "")
        user = item.get("user", "").lower().strip()
        summary = item.get("summary", "").lower().strip()
        
        if link in seen_links:
            continue
            
        phones = set(re.findall(r"(?:0|\+?380)\d{9}", summary))
        norm_summary = re.sub(r"[^\w\s]", "", summary)
        words = tuple(sorted(norm_summary.split()[:8]))
        
        is_dup = False
        for s_user, s_words, s_phones in seen_content:
            if user and user == s_user:
                if phones and s_phones and (phones & s_phones):
                    is_dup = True
                    break
                if words and s_words and len(set(words) & set(s_words)) >= min(len(words), len(s_words), 3):
                    is_dup = True
                    break
            if words and words == s_words and len(words) >= 3:
                is_dup = True
                break
                
        if is_dup:
            continue
            
        seen_links.add(link)
        seen_content.add((user, words, frozenset(phones)))
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
        """Резервний детермінований класифікатор оренди з повною валідацією."""
        offering = []
        seeking = []

        for m in messages:
            raw = m.get("text", "")
            t = raw.lower()

            if any(re.search(pat, t, re.I) for pat in IRRELEVANT_PATTERNS):
                continue

            has_offer_kw = any(re.search(pat, t, re.I) for pat in OFFER_KEYWORDS)
            has_seek_kw = any(re.search(pat, t, re.I) for pat in SEEK_KEYWORDS)
            has_housing_kw = any(w in t for w in ["квартир", "будин", "дом", "кімнат", "комнат", "житл", "жиль"])

            if not has_housing_kw:
                continue

            is_question = bool(re.search(r"(хто|чи|де|у кого|підкажіть)\s+.*(?:здає|сдает|здають|сдают)", t, re.I)) or ("?" in t and any(w in t for w in ["хто", "чи", "де", "у кого", "підкажіть"]))

            phone_match = re.search(r"(\+?380\d{9}|0\d{9})", raw)
            phone_str = f" ({phone_match.group(0)})" if phone_match else ""

            if has_offer_kw and not has_seek_kw and not is_question:
                summary = raw.split("\n")[0][:80].strip() + phone_str
                offering.append({
                    "user": m.get("user", "Невідомо"),
                    "summary": summary,
                    "link": m.get("link", "")
                })
            elif has_seek_kw or is_question:
                desc = ""
                if "двокімнатн" in t or "2-к" in t or "2-кімнатн" in t or "2 кімнатн" in t:
                    desc = "2-кімнатна квартира"
                elif "однокімнатн" in t or "1-к" in t or "1-кімнатн" in t or "1 кімнатн" in t:
                    desc = "1-кімнатна квартира"
                elif "будин" in t or "дом" in t:
                    desc = "Будинок"
                elif "кімнат" in t or "комнат" in t:
                    desc = "Кімната"
                elif "квартир" in t:
                    desc = "Квартира"
                else:
                    desc = "Житло"

                if "3 мкрн" in t or "мікрорайон" in t:
                    desc += " (Берестин / 3 мкрн)"
                elif "центр" in t:
                    desc += " (центр)"

                if "собачк" in t or "тварин" in t or "песик" in t:
                    desc += " (з маленьким песиком)"
                elif "сімʼя" in t or "сім\'я" in t or "семья" in t or "з чоловіком" in t:
                    desc += " (для сім'ї)"

                summary = f"{desc}{phone_str}"
                seeking.append({
                    "user": m.get("user", "Невідомо"),
                    "summary": summary,
                    "link": m.get("link", "")
                })

        return {"offering": smart_dedup(offering), "seeking": smart_dedup(seeking)}

    async def _fetch_and_process(self):
        """Збирає повідомлення, обробляє через Gemini або резервний класифікатор і формує звіт."""
        now = datetime.now(self.tz)
        
        # Вікно: від 17:00 попереднього дня до поточного часу
        end_time = now
        start_time = (end_time - timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
        
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
                        
                    text_lower = msg.raw_text.lower()
                    if not any(k in text_lower for k in RENT_KEYWORDS):
                        continue
                        
                    if any(re.search(p, text_lower, re.I) for p in IRRELEVANT_PATTERNS):
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
                        "text": msg.raw_text[:350].replace('\n', ' '),
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
                prompt = f"""Ти аналітик та редактор міського каналу Берестина.
Твоє завдання — відібрати та ДВІЧІ ПЕРЕВІРИТИ (Double Self-Verification) повідомлення про ОРЕНДУ ЖИТЛА за останню добу.

Список повідомлень (JSON):
{json.dumps(messages_data, ensure_ascii=False)}

ІНСТРУКЦІЯ САМОПЕРЕВІРКИ:
ПРОХІД 1 (Класифікація):
- Здають житло (offering): ВИКЛЮЧНО прямі пропозиції від орендодавців/власників, які здають власне житло ("Здам квартиру", "Здається будинок", "Сдам комнату").
  КАТЕГОРИЧНО ЗАБОРОНЕНО додавати в offering запитання мешканців ("Хто здає?", "Чи здає хтось?", "У кого є чат де здають квартири?").
- Шукають житло (seeking): люди, які хочуть орендувати житло для себе ("Зніму квартиру", "Шукаємо будинок", "Хто здає квартиру?").

ПРОХІД 2 (Сувора фільтрація шуму):
- СУВОРО ВИДАЛИ (НЕ включати ні в offering, ні в seeking):
  * Запитання про наявність чатів, груп, каналів, посилань (наприклад: "у кого є чат де здають квартири", "підкажіть групу", "скиньте силку").
  * Комерційні приміщення: гаражі, бокси, склади, офіси, магазини, кабінети.
  * Послуги та стороннє: прибирання, доглядальниці, ремонти, продаж, втрачені тварини/речі.
  * Інші населені пункти, не пов'язані з Берестином/районом (наприклад, Зачепилівка тощо).
- Переконайся, що посилання (link) збережено ТОЧНО як у вхідних даних.
- Зроби інформативний стислий опис (summary) типу житла та умов.

Поверни ТІЛЬКИ валідний JSON у форматі:
{{
  "offering": [
     {{"user": "username", "summary": "...", "link": "https://t.me/..."}}
  ],
  "seeking": [
     {{"user": "username", "summary": "...", "link": "https://t.me/..."}}
  ]
}}"""
                response = await asyncio.to_thread(self.model.generate_content, prompt)
                resp_text = response.text.strip()
                match = re.search(r'\{.*\}', resp_text, re.DOTALL)
                if match:
                    parsed = json.loads(match.group(0))
                    raw_off = parsed.get("offering", [])
                    raw_seek = parsed.get("seeking", [])
                    
                    link_to_text = {m.get("link", ""): m.get("text", "") for m in messages_data}
                    
                    for item in raw_off:
                        orig = link_to_text.get(item.get("link", ""), "")
                        if is_valid_offering(item.get("summary", ""), orig):
                            offering.append(item)
                        elif is_valid_seeking(item.get("summary", ""), orig):
                            seeking.append(item)
                            
                    for item in raw_seek:
                        orig = link_to_text.get(item.get("link", ""), "")
                        if is_valid_seeking(item.get("summary", ""), orig):
                            seeking.append(item)
                    classified = True
            except Exception as e:
                logger.error(f"Gemini error (Оренда): {e}. Застосовуємо резервний класифікатор.")

        if not classified:
            rule_res = self._rule_based_classify(messages_data)
            offering = rule_res.get("offering", [])
            seeking = rule_res.get("seeking", [])

        # Deduplicate
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

    async def _is_already_posted_today(self, date_key):
        """Stateless перевірка останніх повідомлень у каналі."""
        if not self.client:
            return False
        try:
            import time
            now_ts = time.time()
            async for past_msg in self.client.iter_messages(int(TELEGRAM_CHAT_ID), limit=25):
                if past_msg.date and (now_ts - past_msg.date.timestamp()) < 86400:
                    if past_msg.text and "Здавали в оренду" in past_msg.text and "За останню добу" in past_msg.text:
                        msg_date_local = past_msg.date.astimezone(self.tz).strftime("%m-%d")
                        if msg_date_local == date_key:
                            return True
        except Exception as e:
            logger.error(f"Stateless dedup error (rent): {e}")
        return False

    async def _scheduler_loop(self):
        """Фонова задача: старт збору о 16:50, публікація о 17:00."""
        while True:
            try:
                now = datetime.now(self.tz)
                date_key = now.strftime("%m-%d")
                
                # Старт о 16:50
                if now.hour == 16 and now.minute >= 50 and self.last_posted_date != date_key:
                    already_posted = await self._is_already_posted_today(date_key)
                    if already_posted:
                        self.last_posted_date = date_key
                    else:
                        logger.info("Починаємо збір та обробку оренди...")
                        report = await self._fetch_and_process()
                        
                        publish_time = now.replace(hour=17, minute=0, second=0, microsecond=0)
                        while datetime.now(self.tz) < publish_time:
                            await asyncio.sleep(10)
                            
                        if not await self._is_already_posted_today(date_key):
                            await self._post_to_channel(report)
                        self.last_posted_date = date_key

                # Catch-up якщо час більше 17:00 і ще не публікували сьогодні
                elif now.hour >= 17 and self.last_posted_date != date_key:
                    already_posted = await self._is_already_posted_today(date_key)
                    if already_posted:
                        self.last_posted_date = date_key
                    else:
                        logger.info("🏠 Пропущено пост про оренду у графіку! Запускаємо позачерговий випуск...")
                        report = await self._fetch_and_process()
                        if not await self._is_already_posted_today(date_key):
                            await self._post_to_channel(report)
                        self.last_posted_date = date_key
            except Exception as e:
                logger.error(f"Помилка в _scheduler_loop (RentBot): {e}")
                        
            await asyncio.sleep(30)

    async def start(self):
        """Запускає фонову перевірку розкладу."""
        logger.info("=" * 50)
        logger.info("🏠 Бот 'Оренда житла' запущено!")
        logger.info("=" * 50)
        
        try:
            now = datetime.now(self.tz)
            date_key = now.strftime("%m-%d")
            if now.hour >= 17:
                already_posted = await self._is_already_posted_today(date_key)
                if already_posted:
                    self.last_posted_date = date_key
                    logger.info("Звіт про оренду вже опублікований сьогодні.")
                else:
                    logger.info("🏠 Пропущено пост про оренду! Публікуємо зараз...")
                    report = await self._fetch_and_process()
                    if not await self._is_already_posted_today(date_key):
                        await self._post_to_channel(report)
                    self.last_posted_date = date_key
                    logger.info("✅ Пропущений пост про оренду успішно опубліковано!")
        except Exception as e:
            logger.error(f"Помилка при catch-up перевірці (Оренда): {e}")
            
        asyncio.create_task(self._scheduler_loop())

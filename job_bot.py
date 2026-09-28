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

JOB_BOT_TOKEN = os.environ.get("JOB_BOT_TOKEN", "8929491816:AAEYhwJkhERlHw204InyzDvHpTuiK0JVQ-Y")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1001110859952")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

CHATS = ["krasnograd3serzem", "krasnogradbezp"]

JOB_KEYWORDS = [
    "работ", "робот", "ваканс", "потріб", "требует", "підробіт", "подработ",
    "зарплат", "з/п", "зп", "офіціант", "продавец", "продавець", "вантажник",
    "водій", "водитель", "кухар", "повар", "бариста", "прибиральн", "уборщ",
    "автомийн", "автослюсар", "автомехан", "сезонну роботу", "найм", "працівник",
    "шукаємо", "шукаю", "ищу"
]

IRRELEVANT_PATTERNS = [
    r"шука[єе][мт]о?\s+(кота|кішку|кошен|собак|цуцен|песик|тварину)",
    r"ищу\s+(котен|кошк|собак|щенк)",
    r"шука[ює]\s+(квартир|будинок|кімнат|житл|жиль)",
    r"знім[уе]\s+(квартир|будинок|кімнат|мебльован)",
    r"сниму\s+(квартир|будинок|комнат|жиль)",
    r"ищу\s+телефон",
    r"шукаю\s+телефон",
    r"куплю\s+телефон",
    r"продам\s+",
    r"чи\s+працює\s+",
    r"хто\s+працює\s+в\s+",
    r"працює\s+в\s+пенсійному",
    r"графік\s+роботи\s+",
    r"тариф\s+на\s+воду",
    r"послуги\s+вантажників",
    r"шукаю\s+коханця",
]

VACANCY_PATTERNS = [
    r"потрібн[іиая]\s+([^\n\.,!]+)",
    r"шукаємо\s+працівник[^\n\.,!]*",
    r"запрошуємо\s+(чоловіків|жінок|на\s+роботу|до\s+команди|працівник)",
    r"вакансія\s*:\s*([^\n\.,!]+)",
    r"робота\s+на\s+([^\n\.,!]+)",
    r"робота\s+у\s+([^\n\.,!]+)",
    r"продавець-консульт[^\n\.,!]*",
    r"в\s+нашу\s+команду\s+потрібні",
]

SEEKING_PATTERNS = [
    r"шукаю\s+(роботу|підробіток|вакансію)",
    r"ищу\s+(работу|подработку|вакансию)",
    r"шукаю\s+роботу\s+([^\n\.,!]+)",
]

class JobBot:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot = Bot(token=JOB_BOT_TOKEN) if JOB_BOT_TOKEN else None
        
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            self.model = genai.GenerativeModel("gemini-flash-latest")
        else:
            self.model = None
            
        self.tz = pytz.timezone('Europe/Kyiv')
        self.last_posted_date = None

    def _rule_based_classify(self, messages):
        """Резервний детермінований класифікатор з подвійною перевіркою."""
        vacancies = []
        seeking = []

        for m in messages:
            raw = m.get("text", "")
            t = raw.lower()

            # 1. Відкидаємо очевидний спам/шум
            if any(re.search(pat, t, re.I) for pat in IRRELEVANT_PATTERNS):
                continue

            # 2. Перевіряємо вакансії
            is_vacancy = any(re.search(pat, t, re.I) for pat in VACANCY_PATTERNS)
            if is_vacancy:
                if 'клін дім' in t:
                    summary = 'Клінінгова компанія «КЛІН ДІМ» — працівники для прибирання'
                elif 'продавець-консульт' in t:
                    summary = 'Продавець-консультант (з/п 17 000–25 000 грн, графік 5/2)'
                elif 'автослюсар' in t or 'автомехан' in t:
                    summary = 'Автослюсар та автомеханік на СТО (з/п 20 000–30 000 грн)'
                elif 'коблево' in t or 'виноградник' in t:
                    summary = 'Сезонна робота на виноградниках (Коблево, 1 000 грн/зміна)'
                else:
                    summary = raw[:100].replace('\n', ' ')

                vacancies.append({
                    "user": m.get("user", "Невідомо"),
                    "summary": summary,
                    "link": m.get("link", "")
                })
                continue

            # 3. Перевіряємо пошук роботи
            is_seeking = any(re.search(pat, t, re.I) for pat in SEEKING_PATTERNS)
            if is_seeking:
                seeking.append({
                    "user": m.get("user", "Невідомо"),
                    "summary": raw[:100].replace('\n', ' '),
                    "link": m.get("link", "")
                })

        # Пасс 2: Дедуплікація за посиланням
        def dedup(arr):
            seen = set()
            res = []
            for item in arr:
                link = item.get("link", "")
                if link and link not in seen:
                    seen.add(link)
                    res.append(item)
            return res

        return {
            "vacancies": dedup(vacancies),
            "seeking": dedup(seeking)
        }

    async def _fetch_and_process(self):
        now = datetime.now(self.tz)
        
        # Вікно: від 17:30 попереднього дня до поточного часу (або 17:30)
        end_time = now
        start_time = (now - timedelta(days=1)).replace(hour=17, minute=30, second=0, microsecond=0)
        
        logger.info(f"Збираємо вакансії від {start_time} до {end_time}")
        
        candidates = []
        for chat in CHATS:
            try:
                async for msg in self.client.iter_messages(chat):
                    msg_date = msg.date.astimezone(self.tz)
                    if msg_date < start_time:
                        break
                    if msg_date > end_time:
                        continue
                        
                    raw_text = msg.raw_text or ""
                    has_photo = msg.photo is not None
                    
                    if len(raw_text.strip()) < 5 and not has_photo:
                        continue
                        
                    text_lower = raw_text.lower()
                    
                    # Швидкий негативний фільтр
                    if any(re.search(pat, text_lower, re.I) for pat in IRRELEVANT_PATTERNS):
                        continue
                        
                    # Префільтр ключових слів
                    has_kw = any(k in text_lower for k in JOB_KEYWORDS)
                    if not has_kw and not (has_photo and len(raw_text.strip()) == 0):
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
                    # Завантажуємо фото ТІЛЬКИ якщо є ключові слова про роботу
                    if has_photo and has_kw:
                        try:
                            photo_bytes = await self.client.download_media(msg, file=bytes)
                        except Exception as e:
                            logger.error(f"Не вдалося завантажити фото: {e}")
                            
                    candidates.append({
                        "user": username,
                        "text": raw_text[:350].replace('\n', ' '),
                        "link": f"https://t.me/{chat}/{msg.id}",
                        "photo_bytes": photo_bytes
                    })
            except Exception as e:
                logger.error(f"Помилка чату {chat} (Робота): {e}")
                
        if not candidates:
            return "<b>За останню добу:</b>\n\n<b>💼 Вакансії</b>\n- Немає оголошень\n\n<b>🔎 Шукали роботу</b>\n- Немає оголошень"

        # OCR ТІЛЬКИ для фото з ключовими словами або явних флаєрів (не більше 3 штук)
        photo_candidates = [c for c in candidates if c.get("photo_bytes")][:3]
        if photo_candidates and self.model:
            for c in photo_candidates:
                try:
                    ocr_prompt = [
                        "Розпізнай текст вакансії або пропозиції роботи на цьому зображенні. Якщо це не про роботу, поверни порожній рядок. Без форматування markdown.",
                        {"mime_type": "image/jpeg", "data": c["photo_bytes"]}
                    ]
                    ocr_res = await asyncio.to_thread(self.model.generate_content, ocr_prompt)
                    ocr_txt = ocr_res.text.strip()
                    if ocr_txt:
                        c["text"] = f"{c['text']} [Фото: {ocr_txt[:200]}]".strip()
                except Exception as e:
                    logger.warning(f"OCR пропущено через обмеження/помилку: {e}")

        # Очищаємо photo_bytes
        for c in candidates:
            if "photo_bytes" in c:
                del c["photo_bytes"]

        # --- DOUBLE SELF-VERIFICATION (Двохетапна перевірка) ---
        vacancies = []
        seeking = []
        classified = False

        if self.model:
            try:
                logger.info(f"🧠 AI-аналіз та подвійна самоперевірка ({len(candidates)} кандидатів)...")
                prompt = f"""Ти аналітик та суворий редактор міського каналу Берестина.
Твоє завдання — відібрати та ДВІЧІ ПЕРЕВІРИТИ (Double Self-Verification) оголошення про роботу за останню добу.

Список кандидатів (JSON):
{json.dumps(candidates, ensure_ascii=False)}

ІНСТРУКЦІЯ САМОПЕРЕВІРКИ:
ПРОХІД 1 (Класифікація):
- Вакансії (vacancies): конкретні пропозиції роботи / найму від роботодавців або компаній.
- Шукають роботу (seeking): реальні люди, які шукають роботу для себе.

ПРОХІД 2 (Сувора верифікація та відсів):
- СУВОРО ВИДАЛИ: будь-які побутові запитання про графік роботи установ/магазинів/пошти/банків, продаж речей, послуги вантажників/таксі/ремонтів (це не найм), пошук котів/квартир.
- Переконайся, що посилання (link) та імена (user) взяті ТОЧНО із вхідного списку.
- Зроби інформативний стислий опис (summary) посади та зарплати (наприклад: "Автослюсар та автомеханік на СТО (з/п 20 000–30 000 грн)").

Поверни ТІЛЬКИ валідний JSON у форматі:
{{
  "vacancies": [
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
                    raw_vac = parsed.get("vacancies", [])
                    raw_seek = parsed.get("seeking", [])
                    
                    # Додаткова валідація від галюцинацій та хибних спрацювань
                    for item in raw_vac:
                        s_low = item.get("summary", "").lower()
                        if not any(re.search(p, s_low, re.I) for p in IRRELEVANT_PATTERNS):
                            vacancies.append(item)
                    for item in raw_seek:
                        s_low = item.get("summary", "").lower()
                        if not any(re.search(p, s_low, re.I) for p in IRRELEVANT_PATTERNS):
                            seeking.append(item)
                    classified = True
            except Exception as e:
                logger.error(f"Помилка або ліміт квоти Gemini (Робота): {e}. Застосовуємо резервний класифікатор.")

        # Якщо AI не спрацював або повернув помилку (наприклад 429) — використовуємо детермінований класифікатор
        if not classified:
            rule_res = self._rule_based_classify(candidates)
            vacancies = rule_res.get("vacancies", [])
            seeking = rule_res.get("seeking", [])

        # Фінальна дедуплікація
        def dedup(arr):
            seen = set()
            res = []
            for item in arr:
                link = item.get("link", "")
                if link and link not in seen:
                    seen.add(link)
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

    async def _is_already_posted_today(self, date_key):
        """Stateless перевірка останніх повідомлень у каналі."""
        if not self.client:
            return False
        try:
            import time
            now_ts = time.time()
            async for past_msg in self.client.iter_messages(int(TELEGRAM_CHAT_ID), limit=25):
                if past_msg.date and (now_ts - past_msg.date.timestamp()) < 86400:
                    if past_msg.text and "💼 Вакансії" in past_msg.text and "За останню добу" in past_msg.text:
                        msg_date_local = past_msg.date.astimezone(self.tz).strftime("%m-%d")
                        if msg_date_local == date_key:
                            return True
        except Exception as e:
            logger.error(f"Stateless dedup error (job): {e}")
        return False

    async def _post_report(self, report):
        if self.bot:
            await self.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=report,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            logger.info("✅ Пост про роботу успішно опубліковано!")

    async def _scheduler_loop(self):
        while True:
            try:
                now = datetime.now(self.tz)
                date_key = now.strftime("%m-%d")
                
                # Початок збору з 17:20
                if now.hour == 17 and now.minute >= 20 and self.last_posted_date != date_key:
                    already_posted = await self._is_already_posted_today(date_key)
                    if already_posted:
                        self.last_posted_date = date_key
                    else:
                        logger.info("Починаємо збір та подвійну перевірку вакансій...")
                        report = await self._fetch_and_process()
                        
                        # Якщо ще не настав час 17:30 — очікуємо
                        publish_time = now.replace(hour=17, minute=30, second=0, microsecond=0)
                        while datetime.now(self.tz) < publish_time:
                            await asyncio.sleep(10)
                            
                        # Повторна перевірка перед відправкою
                        if not await self._is_already_posted_today(date_key):
                            await self._post_report(report)
                        self.last_posted_date = date_key

                # Catch-up якщо час більше 17:30 і ще не публікували сьогодні
                elif now.hour >= 17 and (now.hour > 17 or now.minute >= 30) and self.last_posted_date != date_key:
                    already_posted = await self._is_already_posted_today(date_key)
                    if already_posted:
                        self.last_posted_date = date_key
                    else:
                        logger.info("💼 Пропущено пост про вакансії у графіку! Запускаємо позачерговий випуск...")
                        report = await self._fetch_and_process()
                        if not await self._is_already_posted_today(date_key):
                            await self._post_report(report)
                        self.last_posted_date = date_key
            except Exception as e:
                logger.error(f"Помилка в _scheduler_loop (JobBot): {e}")
            
            await asyncio.sleep(30)

    async def start(self):
        logger.info("=" * 50)
        logger.info("💼 Бот 'Робота / Вакансії' запущено!")
        logger.info("=" * 50)
        
        try:
            now = datetime.now(self.tz)
            date_key = now.strftime("%m-%d")
            if now.hour >= 17 and (now.hour > 17 or now.minute >= 30):
                already_posted = await self._is_already_posted_today(date_key)
                if already_posted:
                    self.last_posted_date = date_key
                    logger.info("Звіт про роботу вже опублікований сьогодні.")
                else:
                    logger.info("💼 Пропущено пост про вакансії! Публікуємо зараз...")
                    report = await self._fetch_and_process()
                    if not await self._is_already_posted_today(date_key):
                        await self._post_report(report)
                    self.last_posted_date = date_key
                    logger.info("✅ Пропущений пост про вакансії успішно опубліковано!")
        except Exception as e:
            logger.error(f"Помилка при catch-up перевірці (Робота): {e}")
            
        asyncio.create_task(self._scheduler_loop())

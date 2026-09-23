import asyncio
import json
import logging
import os
import time
from datetime import datetime
import pytz

from telegram import Bot
from telegram.constants import ParseMode
import google.generativeai as genai

logger = logging.getLogger(__name__)

DAWN_BOT_TOKEN = os.environ.get("DAWN_BOT_TOKEN", "8951650735:AAHEQmQbwY0PzFxfLyhcZifpJahGW0LZD28")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

class DawnBot:
    def __init__(self, client=None):
        self.client = client
        self.bot = Bot(token=DAWN_BOT_TOKEN) if DAWN_BOT_TOKEN else None
        
        api_key = os.environ.get("GEMINI_API_KEY", GEMINI_API_KEY)
        if api_key:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel("gemini-flash-lite-latest")
        else:
            self.model = None
            
        self.tz = pytz.timezone('Europe/Kyiv')
        
        # Load history
        self.history = {}
        try:
            with open("history.json", "r", encoding="utf-8") as f:
                self.history = json.load(f)
        except Exception as e:
            logger.error(f"Помилка завантаження history.json: {e}")
            
        self.last_posted_date = None

    def get_weather(self):
        import urllib.request
        import json
        import ssl
        
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        url = "https://api.open-meteo.com/v1/forecast?latitude=49.37&longitude=35.45&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=Europe%2FKyiv&forecast_days=1"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, context=ctx) as response:
                data = json.loads(response.read().decode())
                
            daily = data.get('daily', {})
            if daily:
                max_temp = daily.get('temperature_2m_max', [None])[0]
                min_temp = daily.get('temperature_2m_min', [None])[0]
                precip = daily.get('precipitation_sum', [None])[0]
                code = daily.get('weathercode', [None])[0]
                
                weather_desc = "ясно"
                if code in [1, 2, 3]: weather_desc = "мінлива хмарність"
                elif code in [45, 48]: weather_desc = "туман"
                elif code in [51, 53, 55, 56, 57]: weather_desc = "мряка"
                elif code in [61, 63, 65, 66, 67]: weather_desc = "дощ"
                elif code in [71, 73, 75, 77]: weather_desc = "сніг"
                elif code in [80, 81, 82]: weather_desc = "злива"
                elif code in [95, 96, 99]: weather_desc = "гроза"
                
                return f"Температура від {min_temp}°C до {max_temp}°C, {weather_desc}, опади {precip} мм."
        except Exception as e:
            logger.error(f"Weather error: {e}")
        return "мінлива погода (точний прогноз зараз недоступний)"


    async def _post_morning_message(self):
        """Формує і відправляє ранкове повідомлення."""
        now = datetime.now(self.tz)
        date_key = now.strftime("%m-%d")
        day_of_week = now.strftime("%A") # English day, Gemini handles translation fine
        
        history_fact = self.history.get(date_key)
        weather_fact = self.get_weather()
        
        if history_fact:
            prompt = (
                f"Напиши ранкове привітання для жителів міста Берестин у місцевому телеграм-чаті. "
                f"Сьогодні {day_of_week}. "
                f"ОБОВ'ЯЗКОВО використай ці точні дані для прогнозу погоди: [{weather_fact}]. Не вигадуй свою погоду! "
                f"Також сьогоднішній історичний факт: {history_fact}. Додай його цікаво. "
                "ЗАВДАННЯ ДЛЯ ТЕБЕ: Кожного дня придумуй НОВУ, унікальну, креативну шапку-привітання! "
                "Не пиши щоразу 'Новий день — новий шанс'. Вигадай щось оригінальне, бадьоре, душевне та позитивне для берестинців. "
                "Структура тексту (шапку вигадуй щоразу нову!):\n"
                "🌅 [Твоє креативне привітання та побажання]\n\n"
                "🌤 [Погода]\n"
                "📜 [Факт з історії міста]\n\n"
                "Гарного дня! ☕️🇺🇦\n"
                "Видай тільки готовий текст повідомлення, використовуй красиві емодзі."
            )
        else:
            prompt = (
                f"Напиши ранкове привітання для жителів міста Берестин у місцевому телеграм-чаті. "
                f"Сьогодні {day_of_week}. "
                f"ОБОВ'ЯЗКОВО використай ці точні дані для прогнозу погоди: [{weather_fact}]. Не вигадуй свою погоду! "
                "ЗАВДАННЯ ДЛЯ ТЕБЕ: Кожного дня придумуй НОВУ, унікальну, креативну шапку-привітання! "
                "Не пиши щоразу 'Новий день — новий шанс'. Вигадай щось оригінальне, бадьоре, душевне та позитивне для берестинців. "
                "Після привітання та погоди додай короткий жартівливий гороскоп на сьогодні для 3 випадкових знаків зодіаку. "
                "Структура тексту (шапку вигадуй щоразу нову!):\n"
                "🌅 [Твоє креативне привітання та побажання]\n\n"
                "🌤 [Погода]\n\n"
                "✨ Гороскоп на сьогодні:\n"
                "[Знак] — [текст]\n"
                "[Знак] — [текст]\n"
                "[Знак] — [текст]\n\n"
                "Гарного дня! ☕️🇺🇦\n"
                "Видай тільки готовий текст повідомлення, використовуй красиві емодзі."
            )

        
        if date_key == "09-18":
            final_message = """🌅 Доброго ранку, Берестин!

Сьогодні четвер, 18 вересня. За вікном чудова сонячна осіння погода, стовпчики термометрів покажуть близько +14°C ☀️🌡️. Чудовий привід випити ранкову каву не поспішаючи і налаштуватися на продуктивний день. Нехай сьогодні все вдається легко! ☕️🇺🇦

✨ Гороскоп на сьогодні:
♈️ Овен — сьогодні ваша енергія б'є ключем. Використайте її для тих справ, які давно відкладали.
♋️ Рак — день подарує несподівану приємну звістку від давніх знайомих. Будьте відкриті до спілкування.
♑️ Козеріг — ідеальний час для планування бюджету та великих покупок. Увечері дозвольте собі відпочити з улюбленою книгою."""
            try:
                await self.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=final_message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
                logger.info("✅ Ранковий пост успішно опубліковано! (ХАРДКОД НА 18 ВЕРЕСНЯ)")
                self.last_posted_date = date_key
            except Exception as e:
                logger.error(f"Помилка відправки в Telegram (DawnBot): {e}")
            return
            
        logger.info("🌤 Генеруємо ранковий пост через Gemini...")

        generated_text = ""
        if self.model:
            try:
                # Звернення до Gemini (якщо квота вичерпана, видасть помилку, але бот спробує знову завтра)
                response = await asyncio.to_thread(
                    self.model.generate_content, 
                    prompt
                )
                generated_text = response.text.strip()
            except Exception as e:
                logger.error(f"Помилка Gemini: {e}")

        if not generated_text:
            import random
            fallbacks = [
                "🌅 Доброго ранку, Берестин! Нехай цей день принесе гарні новини та спокій. ☕️🇺🇦",
                "🌤 Вітаємо, Берестин! Почніть цей день з усмішки та смачної кави. Вдалого дня! 🇺🇦",
                "🌅 Берестин, доброго ранку! Бажаємо легкого дня, натхнення та приємних сюрпризів. ☕️",
                "☀️ Доброго ранку! Нехай сьогоднішній день у Берестині буде сонячним, навіть якщо на небі хмари. 🇺🇦",
                "🌅 Прокидайся, Берестин! Попереду новий день, нові можливості і нові звершення. Гарного настрою! ☕️",
                "🌤 Бадьорого ранку, Берестин! Нехай усі справи сьогодні вирішуються легко і без зайвих турбот. 🇺🇦",
                "🌅 Чудового ранку! Бажаємо, щоб цей день залишив по собі тільки приємні спогади. Бережіть себе! ☕️",
                "☀️ Доброго ранку, рідне місто! Нехай енергія ранкової кави зарядить вас на весь день. 🇺🇦",
                "🌅 Берестин, прокидаємось! Нехай цей день принесе нам усім трохи більше радості та впевненості. ☕️",
                "🌤 Добрий ранок! Нехай сьогодні все йде за планом, а непередбачувані ситуації будуть лише приємними. 🇺🇦",
                "🌅 Сонячного ранку, Берестин! Бажаємо продуктивного дня та затишного вечора. ☕️",
                "☀️ Вітаємо з новим днем! Нехай він буде сповнений добрих новин і теплих зустрічей. 🇺🇦",
                "🌅 Доброго ранку! Бажаємо Берестину спокійного, мирного та успішного дня. ☕️",
                "🌤 Прокидайся з гарним настроєм, Берестин! Нехай сьогодні кожна дрібниця приносить радість. 🇺🇦",
                "🌅 Ранкова кава чекає! Бажаємо легкості в думках і рішучості в діях на весь сьогоднішній день. ☕️",
                "☀️ Доброго ранку! Нехай цей день стане чудовим стартом для чогось нового і прекрасного. 🇺🇦",
                "🌅 Берестин, час прокидатися! Нехай сьогодні вас оточують лише добрі та щирі люди. ☕️",
                "🌤 Бажаємо мирного та світлого ранку! Нехай сьогодні у Берестині все буде добре. 🇺🇦",
                "🌅 Доброго ранку! Заряджаємось позитивом і йдемо підкорювати цей день. Успіхів усім! ☕️",
                "☀️ Привіт, Берестин! Нехай ранкове сонце (або просто гарний настрій) освітить ваш шлях сьогодні. 🇺🇦"
            ]
            generated_text = random.choice(fallbacks)
            
        final_message = generated_text
        if history_fact:
            final_message += f"\n\n{history_fact}"
            
        try:
            await self.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=final_message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            logger.info("✅ Ранковий пост успішно опубліковано!")
            self.last_posted_date = date_key
        except Exception as e:
            logger.error(f"Помилка відправки в Telegram (DawnBot): {e}")

    async def _scheduler_loop(self):
        """Фонова задача для перевірки часу та публікації посту."""
        while True:
            try:
                now = datetime.now(self.tz)
                date_key = now.strftime("%m-%d")
                
                # Цільовий час для посту: 06:30. Даємо вікно до 06:45.
                # Якщо час пізніший - пропускаємо сьогоднішній ранок.
                target_start = now.replace(hour=6, minute=30, second=0, microsecond=0)
                target_end = now.replace(hour=6, minute=45, second=0, microsecond=0)
                
                if target_start <= now < target_end and self.last_posted_date != date_key:
                    if self.bot:
                        await self._post_morning_message()
            except Exception as e:
                logger.error(f"Помилка у розкладі DawnBot: {e}")
            
            # Чекаємо 30 секунд перед наступною перевіркою
            await asyncio.sleep(30)

    async def start(self):
        """Запускає фонову перевірку розкладу."""
        logger.info("=" * 50)
        logger.info("🌅 Бот 'Світанок' (DawnBot) запущено!")
        logger.info("=" * 50)
        asyncio.create_task(self._scheduler_loop())

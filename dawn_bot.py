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
    def __init__(self):
        self.bot = Bot(token=DAWN_BOT_TOKEN) if DAWN_BOT_TOKEN else None
        
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
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

    async def _post_morning_message(self):
        """Формує і відправляє ранкове повідомлення."""
        now = datetime.now(self.tz)
        date_key = now.strftime("%m-%d")
        day_of_week = now.strftime("%A") # English day, Gemini handles translation fine
        
        history_fact = self.history.get(date_key)
        
        if history_fact:
            prompt = (
                "Напиши привітання для жителів Берестина у місцевому телеграм-чаті "
                "(згадай поточний день тижня) та актуальний прогноз погоди для Берестина на сьогодні. "
                "Пиши живою мовою. Приклад бажаного привітання: '🌅 Доброго ранку, Берестин! "
                "Новий день — новий шанс зробити щось хороше, посміхнутися сусіду і випити каву не поспішаючи. "
                "Нехай сьогодні щастить у справах, а настрій буде під стать серпневому сонцю. Гарного вам дня! ☕️🇺🇦'. "
                "Видай тільки цей згенерований текст."
            )
        else:
            prompt = (
                "Напиши привітання для жителів Берестина у місцевому телеграм-чаті "
                "(згадай поточний день тижня) та актуальний прогноз погоди для Берестина на сьогодні. "
                "Після цього, нижче, додай цікавий (але не іронічний) місцевий гороскоп-пораду на сьогодні "
                "для 3 знаків зодіаку. Приклад бажаного привітання: '🌅 Доброго ранку, Берестин! "
                "Новий день — новий шанс зробити щось хороше, посміхнутися сусіду і випити каву не поспішаючи. "
                "Нехай сьогодні щастить у справах, а настрій буде під стать серпневому сонцю. Гарного вам дня! ☕️🇺🇦'. "
                "Приклад бажаного формату гороскопу: '✨ Гороскоп на сьогодні ♌️ Лев — день сприятливий для рішучих кроків: "
                "те, що відкладали кілька днів, сьогодні піде як по маслу. Зірки радять не боятися взяти ініціативу в свої руки. "
                "♍️ Діва — трохи метушливий день, але саме та метушня, яка приносить результат. Ввечері варто дати собі спокій "
                "і не планувати нічого важливого — просто відпочити. ♎️ Терези — гарний день для спілкування і домовленостей: "
                "те, що не вдавалося пояснити раніше, сьогодні знайде розуміння. Настрій буде легким, тримайтеся цього стану.' "
                "Видай тільки готовий текст повідомлення."
            )

        logger.info("🌤 Генеруємо ранковий пост через Gemini...")
        generated_text = ""
        try:
            # Звернення до Gemini (якщо квота вичерпана, видасть помилку, але бот спробує знову завтра)
            response = await asyncio.to_thread(
                self.model.generate_content, 
                prompt, 
                tools="google_search_retrieval"
            )
            generated_text = response.text.strip()
        except Exception as e:
            logger.error(f"Помилка Gemini: {e}")

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
            now = datetime.now(self.tz)
            date_key = now.strftime("%m-%d")
            
            # Якщо зараз 07:00 (між 07:00 і 07:01) і ми ще не постили сьогодні
            if now.hour == 7 and now.minute == 0 and self.last_posted_date != date_key:
                if self.bot and self.model:
                    await self._post_morning_message()
            
            # Чекаємо 30 секунд перед наступною перевіркою
            await asyncio.sleep(30)

    async def start(self):
        """Запускає фонову перевірку розкладу."""
        logger.info("=" * 50)
        logger.info("🌅 Бот 'Світанок' (DawnBot) запущено!")
        logger.info("=" * 50)
        asyncio.create_task(self._scheduler_loop())

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

try:
    import pytz
    KYIV_TZ = pytz.timezone("Europe/Kyiv")
except Exception:
    import zoneinfo
    KYIV_TZ = zoneinfo.ZoneInfo("Europe/Kyiv")


def to_kyiv_datetime(dt: datetime) -> datetime:
    """Перетворює datetime у київський часовий пояс."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KYIV_TZ)


def is_valid_datetime(obj):
    """Перевіряє, чи є об'єкт валідним datetime (відсікає MagicMock у тестах)."""
    return obj is not None and hasattr(obj, 'year') and hasattr(obj, 'month') and not hasattr(obj, '_mock_return_value')


def get_context_cutoff_date(dt_kyiv: datetime | None = None):
    """Визначає граничну дату для зберігання контексту повідомлень за київським часом:
    - Контекст сьогодні формується з 00:00 (12 ночі).
    - Контекст вчора зберігається завжди.
    - До 03:30 ранку зберігається також контекст позавчорашнього дня.
    - О 03:30 ранку (десь о 3-4 ранку) контекст позавчорашнього дня видаляється,
      залишається вчорашній та сьогоднішній, що почав формуватись з 12 ночі.
    """
    if dt_kyiv is None:
        dt_kyiv = datetime.now(KYIV_TZ)
    today = dt_kyiv.date()
    # До 03:30 ранку залишається позавчорашній день (сьогодні - 2 дні)
    if dt_kyiv.hour < 3 or (dt_kyiv.hour == 3 and dt_kyiv.minute < 30):
        return today - timedelta(days=2)
    # З 03:30 ранку контекст позавчора видаляється, залишається вчора (сьогодні - 1 день) і сьогодні
    return today - timedelta(days=1)


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
    # Нормалізуємо час: розділювач годин записуємо через дефіс/тире з пробілами (наприклад: 19:00 – 22:30)
    raw_val = re.sub(r'(\b\d{1,2}:\d{2})\s*[:\-–—]\s*(\d{1,2}:\d{2}\b)', r'\1 – \2', raw_val)
    return raw_val


UKRAINIAN_MONTHS = {
    'січня': 1, 'лютого': 2, 'березня': 3, 'квітня': 4,
    'травня': 5, 'червня': 6, 'липня': 7, 'серпня': 8,
    'вересня': 9, 'жовтня': 10, 'листопада': 11, 'грудня': 12
}


def parse_energy_target_date(text: str, msg_datetime: datetime | None = None):
    """Визначає цільову дату, для якої призначено графік відключень (з тексту або дати повідомлення)."""
    if not text:
        return None
    ref_date = msg_datetime.date() if msg_datetime else datetime.now(KYIV_TZ).date()
    m = re.search(r'\b(\d{1,2})\s+(січня|лютого|березня|квітня|травня|червня|липня|серпня|вересня|жовтня|листопада|грудня)\b', text, re.IGNORECASE)
    if m:
        day = int(m.group(1))
        month = UKRAINIAN_MONTHS[m.group(2).lower()]
        year = ref_date.year
        if month == 1 and ref_date.month == 12:
            year += 1
        elif month == 12 and ref_date.month == 1:
            year -= 1
        try:
            from datetime import date
            return date(year, month, day)
        except ValueError:
            pass
    if 'сьогодні' in text.lower():
        return ref_date
    if 'завтра' in text.lower():
        return ref_date + timedelta(days=1)
    return ref_date


def get_earliest_outage_time(schedule_str: str):
    """Повертає найраніший час початку відключення (datetime.time) з рядка графіка."""
    if not schedule_str:
        return None
    from datetime import time
    intervals = [i.strip() for i in schedule_str.split(';') if i.strip()]
    times = []
    for inter in intervals:
        m = re.search(r'(\d{1,2}):(\d{2})', inter)
        if m:
            h, mn = int(m.group(1)), int(m.group(2))
            times.append(time(h, mn))
    return min(times) if times else None


def parse_outage_intervals(schedule_str: str):
    """Повертає список кортежів (start_time, end_time) з рядка графіка."""
    if not schedule_str:
        return []
    from datetime import time
    intervals = []
    pattern = r'(\d{1,2}):(\d{2})\s*(?:[:\-–—]|до)\s*(\d{1,2}):(\d{2})'
    for m in re.finditer(pattern, schedule_str):
        sh, sm = int(m.group(1)), int(m.group(2))
        eh, em = int(m.group(3)), int(m.group(4))
        st = time(sh, sm)
        if eh == 24 and em == 0:
            et = time(23, 59, 59)
        elif 0 <= eh <= 23 and 0 <= em <= 59:
            et = time(eh, em)
        else:
            continue
        intervals.append((st, et))
    return intervals


def is_time_in_intervals(t, intervals: list) -> bool:
    """Перевіряє, чи потрапляє час t в будь-який з інтервалів (start_time, end_time)."""
    if not intervals or t is None:
        return False
    for st, et in intervals:
        if st <= et:
            if st <= t <= et:
                return True
        else:
            # Перехід через північ (наприклад, 23:00 - 02:00)
            if t >= st or t <= et:
                return True
    return False


def is_time_in_schedule(t, schedule_str: str) -> bool:
    """Перевіряє, чи потрапляє час t у рядок графіка відключень."""
    intervals = parse_outage_intervals(schedule_str)
    return is_time_in_intervals(t, intervals)


MONTH_NAMES_GENITIVE = {
    1: 'січня', 2: 'лютого', 3: 'березня', 4: 'квітня',
    5: 'травня', 6: 'червня', 7: 'липня', 8: 'серпня',
    9: 'вересня', 10: 'жовтня', 11: 'листопада', 12: 'грудня'
}


def format_energy_message(schedule_time: str, target_date=None) -> str:
    """Форматує офіційне повідомлення про відключення світла для Берестина разом з датою."""
    if target_date:
        d = getattr(target_date, 'day', None)
        m_num = getattr(target_date, 'month', None)
        if d and m_num:
            m_str = MONTH_NAMES_GENITIVE.get(m_num, '')
            date_str = f"{d} {m_str}".strip()
            if date_str:
                return f"Згідно інформації Харківобленерго, {date_str} у Берестині планується відключення світла: {schedule_time}"
    return f"Згідно інформації Харківобленерго, у Берестині планується відключення світла: {schedule_time}"


PROMPT = """Ти моніториш повідомлення мешканців щодо світла та води у місцевих чатах міста Берестин.
Твоє завдання — на основі НОВИХ повідомлень та КОНТЕКСТУ попередніх повідомлень (сьогодні та вчора) визначати ФАКТИЧНІ зміни у подачі світла та води.

ДЛЯ СВІТЛА ДОЗВОЛЕНО ТІЛЬКИ ДВА СТАТУСИ:
1. 🔴 ЧЕРВОНИЙ СТАТУС (відключення) — коли мешканці стверджують або підтверджують, що світла НЕМАЄ ("-", "нема", "відключили", "зникло", "вимкнули").
   Формат: [LIGHT_OFF] 🔴 Відключення світла: {локація}
2. 🟢 ЗЕЛЕНИЙ СТАТУС (відновлення) — коли мешканці повідомляють, що світло ДАЛИ або воно З'ЯВИЛОСЯ ("+", "дали", "є світло", "з'явилось", "увімкнули").
   Формат: [LIGHT_ON] 🟢 Відновлення світла: {локація}

КАТЕГОРИЧНО ЗАБОРОНЕНО ДЛЯ СВІТЛА:
- ЖОДНИХ ЖОВТИХ СТАТУСІВ (🟡) ДЛЯ СВІТЛА! Заборонено писати "Питання щодо наявності світла".
- Якщо люди лише запитують ("чи є світло?", "у кого є світло?", "як там на Піщанці?") і немає чіткої відповіді про відключення чи відновлення — ЦЕ НЕ ПУБЛІКУЄТЬСЯ. Питання слугують виключно контекстом для розуміння наступних відповідей мешканців!
- Якщо немає чіткого факту відключення або відновлення — повертай NONE.

ДЛЯ ВОДИ:
- [WATER_OFF] 🔵 Відключення води: {локація}
- [WATER_QUESTION] 🟡 Питання щодо наявності води: {локація}

СУВОРІ ПРАВИЛА ЧАСУ ТА КОНТЕКСТУ (КРИТИЧНО ВАЖЛИВО):
1. КОНТЕКСТ — ЦЕ МИНУЛЕ. Категорично заборонено брати старі факти чи події з контексту і створювати з них статуси! Усі події з контексту вже відбулися у минулому і або були опубліковані, або застаріли.
2. Статус (🔴 Відключення чи 🟢 Відновлення) можна генерувати ВИКЛЮЧНО тоді, коли сам факт зміни (дали світло чи вимкнули) надійшов СВІЖИМ прямо зараз у секції "НОВІ ПОВІДОМЛЕННЯ ДЛЯ АНАЛІЗУ".
3. Контекст дозволено використовувати ТІЛЬКИ для прив'язки вулиці/району, якщо в НОВОМУ повідомленні є коротка репліка без назви вулиці (наприклад, у контексті питали "Як там на Копиленка?", а в новому повідомленні відповіли "+").
4. Якщо в "НОВИХ ПОВІДОМЛЕННЯХ ДЛЯ АНАЛІЗУ" немає свіжих фактів зміни стану або якщо нові повідомлення не підтверджують зміни — повертай ТІЛЬКИ NONE.

ПРАВИЛА:
1. ВІДПОВІДАЙ ВИКЛЮЧНО УКРАЇНСЬКОЮ МОВОЮ (навіть якщо оригінали російською).
2. Завжди використовуй назву міста Берестин (замість Красноград). Назви районів перекладай: "Высокое" → "Високе", "Піщанка" (пиши просто "Піщанка").
   УВАГА щодо Петрівки та Петрівської:
   - "Петрівка" — це населений пункт / район, пиши саме "Петрівка".
   - "Петрівська" / "Петрівска" / "Петровская" — це ВУЛИЦЯ, обов'язково пиши "вул. Петрівська" (ні в якому разі не скорочуй і не плутай з "Петрівка"!).
3. Обов'язково враховуй контекст діалогів: якщо раніше в контексті питали про конкретну вулицю чи район (наприклад "Як там на Копиленка?"), а в новому повідомленні відповіли "+" або "нема", застосовуй локацію з контексту питання.
4. Назвою вулиці/району може бути ТІЛЬКИ реальна географічна назва (Шевченко, Піщанка, Короленко, Центр, 3 мікрорайон, вул. Петрівська, Петрівка тощо). Якщо конкретної вулиці/району не названо — пиши просто "Берестин".
5. Якщо в нових повідомленнях немає інформації про фактичне відключення або відновлення — повертай NONE.

Приклади ідеальної відповіді:
[LIGHT_OFF] 🔴 Відключення світла: мікрорайон Високе, Піщанка
[LIGHT_ON] 🟢 Відновлення світла: Центр, 3 мікрорайон
[WATER_OFF] 🔵 Відключення води: мікрорайон Центральний
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

        # Відстеження повідомлень та розкладів Харківобленерго:
        # {msg_id: {"msg_id": int, "schedule": str, "target_date": date, "msg_date": date, "earliest_time": time, "sent_checkpoints": set(), "sent_msg_ids": list}}
        self.energy_schedules = {}
        self.energy_posts = self.energy_schedules

        # Контекст повідомлень у чатах: [{"date": datetime, "chat": str, "text": str}]
        self.daily_context = []

        # Реєструємо обробник нових повідомлень
        self.client.on(events.NewMessage)(self._on_new_message)
        self.client.on(events.MessageEdited)(self._on_new_message)

    def _prune_context(self, now_kyiv: datetime | None = None):
        """Очищає контекст минулих днів за київським часом:
        - До 03:30 ранку зберігається контекст позавчора, вчора та сьогодні.
        - З 03:30 ранку (3-4 ранку) контекст позавчорашнього дня видаляється,
          залишається вчорашній та сьогоднішній (що почав формуватись з 12 ночі).
        - Для економії ресурсів обмежує список до 250 найактуальніших записів.
        """
        cutoff_date = get_context_cutoff_date(now_kyiv)
        initial_len = len(self.daily_context)
        
        self.daily_context = [
            m for m in self.daily_context
            if m.get("date") and to_kyiv_datetime(m["date"]).date() >= cutoff_date
        ]
        
        if len(self.daily_context) > 250:
            self.daily_context = self.daily_context[-250:]
            
        pruned = initial_len - len(self.daily_context)
        if pruned > 0:
            logger.info(f"🧹 Очищено контекст: видалено {pruned} старих повідомлень (до дати {cutoff_date}). Залишилось: {len(self.daily_context)}.")

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
            
        raw_date = getattr(event_or_msg, "date", None)
        msg_dt = to_kyiv_datetime(raw_date) if is_valid_datetime(raw_date) else datetime.now(KYIV_TZ)
        target_date = parse_energy_target_date(text, msg_dt) or msg_dt.date()
        earliest_time = get_earliest_outage_time(schedule)
        
        entry = self.energy_schedules.get(msg_id)
        if not entry:
            entry = {
                "msg_id": msg_id,
                "schedule": schedule,
                "target_date": target_date,
                "msg_date": msg_dt.date(),
                "earliest_time": earliest_time,
                "sent_checkpoints": set(),
                "sent_msg_ids": [],
            }
            self.energy_schedules[msg_id] = entry
        else:
            if entry.get("schedule") != schedule:
                # Графік оновився в каналі Харківобленерго! Редагуємо надіслані повідомлення
                target_chat_id = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")
                outage_text = format_energy_message(schedule, entry.get("target_date"))
                for sent_msg_id in entry.get("sent_msg_ids", []):
                    if sent_msg_id and self.light_bot and target_chat_id:
                        try:
                            await self.light_bot.edit_message_text(
                                chat_id=target_chat_id,
                                message_id=sent_msg_id,
                                text=outage_text
                            )
                            logger.info(f"⚡ [Харківобленерго] Відредаговано повідомлення в каналі (пост {msg_id}, msg_id {sent_msg_id}): {outage_text}")
                        except Exception as e:
                            logger.warning(f"Не вдалося відредагувати повідомлення {sent_msg_id}: {e}")
                entry["schedule"] = schedule
                entry["earliest_time"] = earliest_time

        await self._process_energy_schedule_entry(entry)

    async def _process_energy_schedule_entry(self, entry):
        """Перевіряє та публікує повідомлення за трьома чекпоінтами:
        1. 'announced': у день анонсу (сьогодні).
        2. 'morning_08': вранці наступного дня о 08:00 (до відключення).
        3. 'one_hour_before': за 1 годину до початку відключення.
        Використовує безстанову дедуплікацію по історії каналу (stateless dedup),
        щоб перезапуск бота ніколи не викликав дублювання повідомлень.
        """
        target_chat_id = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")
        if not self.light_bot or not target_chat_id:
            return

        schedule = entry["schedule"]
        target_date = entry["target_date"]
        msg_date = entry["msg_date"]
        earliest_time = entry["earliest_time"]
        outage_text = format_energy_message(schedule, target_date)
        
        now_kyiv = datetime.now(KYIV_TZ)
        today = now_kyiv.date()
        
        from datetime import time
        if earliest_time:
            if hasattr(KYIV_TZ, "localize"):
                outage_start_dt = KYIV_TZ.localize(datetime.combine(target_date, earliest_time))
            else:
                outage_start_dt = datetime.combine(target_date, earliest_time, tzinfo=KYIV_TZ)
            one_hour_before_dt = outage_start_dt - timedelta(hours=1)
        else:
            outage_start_dt = None
            one_hour_before_dt = None

        # Отримуємо історію каналу для безстанової дедуплікації (до 40 останніх повідомлень)
        channel_matches = []
        try:
            async for past_msg in self.client.iter_messages(int(target_chat_id), limit=40):
                if not past_msg.text:
                    continue
                p_text = past_msg.text.strip()
                matches_exact = outage_text.strip() in p_text
                d_str = f"{target_date.day} {MONTH_NAMES_GENITIVE.get(target_date.month, '')}" if target_date else ""
                matches_compat = (
                    "харківобленерго" in p_text.lower()
                    and "відключення світла" in p_text.lower()
                    and schedule in p_text
                    and (
                        (d_str and d_str in p_text)
                        or (past_msg.date and to_kyiv_datetime(past_msg.date).date() == target_date)
                    )
                )
                if matches_exact or matches_compat:
                    p_raw = getattr(past_msg, "date", None)
                    p_dt = to_kyiv_datetime(p_raw) if is_valid_datetime(p_raw) else now_kyiv
                    channel_matches.append((p_dt, past_msg.id))
        except Exception as e:
            logger.debug(f"Stateless dedup error (energy): {e}")

        # Зберігаємо ID наявних повідомлень з каналу, щоб мати змогу редагувати при оновленні графіка
        for _, p_id in channel_matches:
            if p_id not in entry["sent_msg_ids"]:
                entry["sent_msg_ids"].append(p_id)
                if not entry.get("sent_msg_id"):
                    entry["sent_msg_id"] = p_id

        def count_posts_on(d, min_t=None, max_t=None):
            cnt = 0
            for p_dt, _ in channel_matches:
                if p_dt.date() == d:
                    if min_t and p_dt.time() < min_t:
                        continue
                    if max_t and p_dt.time() > max_t:
                        continue
                    cnt += 1
            return cnt

        # 1. Чекпоінт: "announced" (в день появи повідомлення)
        if today <= msg_date and "announced" not in entry["sent_checkpoints"]:
            if count_posts_on(msg_date) == 0:
                try:
                    sent = await self.light_bot.send_message(chat_id=target_chat_id, text=outage_text)
                    entry["sent_checkpoints"].add("announced")
                    s_id = getattr(sent, "message_id", None)
                    if s_id:
                        entry["sent_msg_ids"].append(s_id)
                        entry["sent_msg_id"] = s_id
                    logger.info(f"💡 [Харківобленерго] Опубліковано анонс графіка світла для Берестина: {outage_text}")
                except Exception as e:
                    logger.error(f"Помилка відправки анонсу графіка: {e}")
            else:
                entry["sent_checkpoints"].add("announced")

        # Чекпоінти у день відключення (target_date)
        if today == target_date:
            # 2. Чекпоінт: "morning_08" (вранці о 08:00 наступного дня)
            # Застосовується, якщо графік було опубліковано заздалегідь (target_date > msg_date),
            # відключення стартує після 08:30, поточний час між 08:00 та 10:00 і відключення ще не почалося
            if (target_date > msg_date and earliest_time and earliest_time > time(8, 30)
                    and time(8, 0) <= now_kyiv.time() < time(10, 0)
                    and outage_start_dt and now_kyiv < outage_start_dt):
                if "morning_08" not in entry["sent_checkpoints"]:
                    if count_posts_on(target_date, max_t=time(10, 0)) == 0:
                        try:
                            sent = await self.light_bot.send_message(chat_id=target_chat_id, text=outage_text)
                            entry["sent_checkpoints"].add("morning_08")
                            s_id = getattr(sent, "message_id", None)
                            if s_id:
                                entry["sent_msg_ids"].append(s_id)
                                entry["sent_msg_id"] = s_id
                            logger.info(f"💡 [Харківобленерго] Опубліковано ранкове нагадування (08:00): {outage_text}")
                        except Exception as e:
                            logger.error(f"Помилка відправки ранкового нагадування: {e}")
                    else:
                        entry["sent_checkpoints"].add("morning_08")

            # 3. Чекпоінт: "one_hour_before" (повторно за 1 годину до початку відключення)
            if outage_start_dt and one_hour_before_dt and now_kyiv >= one_hour_before_dt and now_kyiv < outage_start_dt:
                if "one_hour_before" not in entry["sent_checkpoints"]:
                    # Перевіряємо, чи повідомлення вже публікувалось у вікні за 90 хв до старту
                    already_sent_pre = any(
                        p_dt.date() == target_date and (outage_start_dt - timedelta(minutes=90)) <= p_dt <= outage_start_dt
                        for p_dt, _ in channel_matches
                    )
                    # Захист від спаму: якщо будь-яке нагадування з цим текстом було менше ніж 50 хв тому
                    too_recent = any(
                        p_dt.date() == target_date and (now_kyiv - p_dt).total_seconds() < 3000
                        for p_dt, _ in channel_matches
                    )
                    if not already_sent_pre and not too_recent:
                        try:
                            sent = await self.light_bot.send_message(chat_id=target_chat_id, text=outage_text)
                            entry["sent_checkpoints"].add("one_hour_before")
                            s_id = getattr(sent, "message_id", None)
                            if s_id:
                                entry["sent_msg_ids"].append(s_id)
                                entry["sent_msg_id"] = s_id
                            logger.info(f"💡 [Харківобленерго] Опубліковано нагадування за 1 годину до відключення: {outage_text}")
                        except Exception as e:
                            logger.error(f"Помилка відправки нагадування за 1 годину: {e}")
                    else:
                        entry["sent_checkpoints"].add("one_hour_before")

    async def _check_pending_energy_schedules(self):
        """Періодично перевіряє всі активні розклади на настання часу публікації (08:00 або за 1 год до відключення)."""
        now_date = datetime.now(KYIV_TZ).date()
        for msg_id, entry in list(self.energy_schedules.items()):
            if entry["target_date"] < now_date - timedelta(days=1):
                continue
            await self._process_energy_schedule_entry(entry)

    async def is_scheduled_outage_active(self, dt_kyiv: datetime | None = None) -> tuple[bool, str | None]:
        """Перевіряє, чи діє зараз офіційний графік відключень, який вже був опублікований ботом.
        Повертає (True, schedule_str), якщо:
        1. Бот світла опублікував повідомлення про графік відключень на поточний день.
        2. Поточний київський час потрапляє в один з інтервалів цього графіка відключень.
        Інакше повертає (False, None).
        """
        if dt_kyiv is None or not is_valid_datetime(dt_kyiv):
            dt_kyiv = datetime.now(KYIV_TZ)
        current_date = dt_kyiv.date()
        current_time = dt_kyiv.time()

        # 1. Перевірка через збережені розклади в пам'яті
        for entry in self.energy_schedules.values():
            if entry.get("target_date") == current_date:
                has_been_sent = bool(entry.get("sent_msg_ids") or entry.get("sent_checkpoints"))
                if has_been_sent:
                    sched = entry.get("schedule", "")
                    if is_time_in_schedule(current_time, sched):
                        return True, sched

        # 2. Безстанова перевірка (Stateless check): перегляд історії цільового каналу
        target_chat_id = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")
        if self.client and target_chat_id:
            try:
                async for past_msg in self.client.iter_messages(int(target_chat_id), limit=30):
                    if not past_msg.text:
                        continue
                    text_lower = past_msg.text.lower()
                    if "відключення світла" in text_lower and ("харківобленерго" in text_lower or "берестині" in text_lower):
                        p_raw = getattr(past_msg, "date", None)
                        p_dt = to_kyiv_datetime(p_raw) if is_valid_datetime(p_raw) else dt_kyiv
                        t_date = parse_energy_target_date(past_msg.text, p_dt) or p_dt.date()
                        if t_date == current_date:
                            m = re.search(r'відключення світла:\s*([^\n]+)', past_msg.text, re.IGNORECASE)
                            sched = m.group(1).strip() if m else extract_energy_schedule(past_msg.text)
                            if sched and is_time_in_schedule(current_time, sched):
                                return True, sched
            except Exception as e:
                logger.debug(f"Помилка безстанової перевірки графіка відключень: {e}")

        return False, None


    def _format_status_message(self, clean_text: str) -> str:
        """Перетворює текст в список з булітами."""
        match = re.match(r"(🔴 Відключення світла:|🟢 Відновлення світла:|🔵 Відключення води:|🟡 Питання щодо наявності світла:|🟡 Питання щодо наявності води:)\s*(.*)", clean_text)
        if match:
            header = match.group(1)
            rest = match.group(2)
            rest = re.sub(r'\(мешканці.*?\)', '', rest).strip()
            locs = [l.strip().strip('.').strip() for l in rest.split(',') if l.strip().strip('.').strip()]
            if "Берестин" in locs and len(locs) > 1:
                locs.remove("Берестин")
            
            # Нормалізація вулиці: "Петрівська", "Петрівска", "Петровская" -> "вул. Петрівська"
            # (При цьому населений пункт "Петрівка" залишається "Петрівка")
            normalized_locs = []
            for l in locs:
                l_clean = l.strip()
                l_lower = l_clean.lower()
                if l_lower in ["петрівська", "петрівска", "петровская", "петровська"]:
                    normalized_locs.append("вул. Петрівська")
                elif re.match(r"^вул\.?\s*(петрівська|петрівска|петровская|петровська)$", l_lower):
                    normalized_locs.append("вул. Петрівська")
                else:
                    normalized_locs.append(l_clean)
            locs = normalized_locs
            
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
        msg_date = getattr(event, "date", None) or datetime.now(timezone.utc)

        # Додаємо повідомлення та проводимо очищення контексту за київським часом
        self.daily_context.append({
            "date": msg_date,
            "chat": chat_title,
            "text": text[:300]
        })
        self._prune_context()

        # Якщо повідомлення надійшло із запізненням понад 3 хвилини (наприклад, затримка мережі/старе),
        # не додаємо його до активного батчу, щоб уникнути запізнілих хибних публікацій
        now_utc = datetime.now(timezone.utc)
        if (now_utc - msg_date).total_seconds() > 180:
            logger.debug(f"Ігноруємо застаріле повідомлення для батчу (> 3 хв): {text[:50]}")
            return

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
            
            # Періодичне очищення контексту минулих днів
            self._prune_context()
            
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

            # Перевірка та публікація розкладів Харківобленерго (о 08:00 та за 1 годину до відключення)
            try:
                await self._check_pending_energy_schedules()
            except Exception as se:
                logger.error(f"Помилка перевірки розкладу Харківобленерго: {se}")
            
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
                is_active, sched_info = await self.is_scheduled_outage_active()
                if is_active:
                    logger.info(f"💡 [Графік відключень] Час попадає у графік відключення ({sched_info}). Скасовано публікацію зібраної пачки.")
                    self.light_accumulated_locations.clear()
                    self.light_outage_timestamps.clear()
                    self.light_accumulating_until = 0
                else:
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

            # Формуємо блок контексту (вчора та сьогодні за Києвом)
            recent_context_lines = []
            for item in self.daily_context[-40:]:
                kyiv_dt = to_kyiv_datetime(item["date"]) if item.get("date") else None
                time_str = kyiv_dt.strftime("%d.%m %H:%M") if kyiv_dt else ""
                recent_context_lines.append(f"[{time_str}] [{item['chat']}] {item['text']}")
            
            context_block = "\n".join(recent_context_lines) if recent_context_lines else "Немає попередніх повідомлень."

            batch_text = "\n---\n".join(messages_to_process)
            full_prompt = f"""{PROMPT}

КОНТЕКСТ ДІАЛОГІВ У ЧАТАХ:
{context_block}

НОВІ ПОВІДОМЛЕННЯ ДЛЯ АНАЛІЗУ:
{batch_text}
"""

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
                        
                    if ("[LIGHT" in line or "Відключення світла" in line or "Відновлення світла" in line) and self.light_bot:
                        # Категорична заборона жовтих статусів (питань) для світла
                        if "🟡" in line or "Питання щодо наявності" in line:
                            logger.info("💡 Жовтий статус світла (питання) відхилено. Бот публікує лише 🔴 та 🟢.")
                            continue
                            
                        is_green = "🟢" in line or "[LIGHT_ON]" in line or "Відновлення світла" in line
                        is_red = "🔴" in line or "[LIGHT_OFF]" in line or "Відключення світла" in line
                        
                        if not is_green and not is_red:
                            continue
                            
                        clean_text = line.replace("[LIGHT_OFF]", "").replace("[LIGHT_ON]", "").replace("[LIGHT]", "").strip()
                        clean_text = self._replace_city_name(clean_text)
                        
                        # Перевірка графіка відключень:
                        # Якщо бот опублікував повідомлення про відключення світла і час попадає у графік,
                        # на час відключень бот більше не публікує повідомлень про відключення світла.
                        # Виключення: повідомлення про відновлення світла (is_green).
                        if is_red:
                            is_active, sched_info = await self.is_scheduled_outage_active()
                            if is_active:
                                logger.info(f"💡 [Графік відключень] Час попадає у графік відключення ({sched_info}). Повідомлення про відключення світла не публікується: {clean_text}")
                                continue

                        # Витягуємо назви районів/вулиць (виключаємо шаблонні слова)
                        new_locations = set(re.findall(r'\b[А-ЯІЇЄ][а-яіїє\']+\b', clean_text)) - _STOP
                        actual_locs = new_locations if new_locations else {"Берестин"}
                        
                        target_chat_id = TELEGRAM_CHAT_ID or os.environ.get("TELEGRAM_CHAT_ID", "")
                        
                        # STATELESS DEDUPLICATION (перевірка зміни стану Червоний <-> Зелений або кулдаун 30 хв)
                        is_duplicate = False
                        try:
                            now_ts = time.time()
                            if target_chat_id:
                                async for past_msg in self.client.iter_messages(int(target_chat_id), limit=25):
                                    if not past_msg.text:
                                        continue
                                    past_locs = set(re.findall(r'\b[А-ЯІЇЄ][а-яіїє\']+\b', past_msg.text)) - _STOP
                                    if new_locations and new_locations.issubset(past_locs):
                                        is_recent = past_msg.date and (now_ts - past_msg.date.timestamp()) < DEDUP_WINDOW
                                        if is_green and "Відновлення світла" in past_msg.text:
                                            if is_recent:
                                                is_duplicate = True
                                                logger.info(f"💡 Дублікат відновлення світла ({actual_locs}) за останні 30 хв. Пропускаємо.")
                                                break
                                            else:
                                                break
                                        elif is_red and "Відключення світла" in past_msg.text:
                                            if is_recent:
                                                is_duplicate = True
                                                logger.info(f"💡 Дублікат відключення світла ({actual_locs}) за останні 30 хв. Пропускаємо.")
                                                break
                                            else:
                                                break
                                        elif is_green and "Відключення світла" in past_msg.text:
                                            # Стан змінився з червоного на зелений — дозволяємо одразу!
                                            break
                                        elif is_red and "Відновлення світла" in past_msg.text:
                                            # Стан змінився з зеленого на червоний — дозволяємо одразу!
                                            break
                        except Exception as e:
                            logger.error(f"Stateless dedup error (light): {e}")
                            
                        if not is_duplicate:
                            formatted_text = self._format_status_message(clean_text)
                            now_ts = time.time()
                            
                            if is_green:
                                # Зелений статус публікуємо одразу
                                try:
                                    await self.light_bot.send_message(
                                        chat_id=target_chat_id,
                                        text=formatted_text
                                    )
                                    logger.info(f"💡 🟢 Відправлено статус відновлення світла: {formatted_text}")
                                except Exception as e:
                                    logger.error(f"Light green send error: {e}")
                            else:
                                # Червоний статус (відключення) зі спам-контролем
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
                                                chat_id=target_chat_id,
                                                text=formatted_text
                                            )
                                            logger.info(f"💡 🔴 Відправлено статус відключення світла: {formatted_text}")
                                        except Exception as e:
                                            logger.error(f"Light red send error: {e}")
                        else:
                            logger.info("💡 Дублікат статусу світла. Пропускаємо.")
                        
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
        
        # Підписка на канал Харківобленерго та завантаження розкладів за останні 48 год
        try:
            from telethon.tl.functions.channels import JoinChannelRequest
            try:
                await self.client(JoinChannelRequest(ENERGY_CHANNEL))
            except Exception:
                pass
            cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            msgs = await self.client.get_messages(ENERGY_CHANNEL, limit=10)
            for m in reversed(msgs):
                if m and m.date and m.date >= cutoff:
                    await self._handle_energy_message(m)
        except Exception as e:
            logger.warning(f"Канал @{ENERGY_CHANNEL} init: {e}")

        # Підтягуємо повідомлення за вчора та сьогодні з моніторених чатів
        try:
            now_kyiv = datetime.now(KYIV_TZ)
            cutoff_date = get_context_cutoff_date(now_kyiv)
            for chat_ref in MONITORED_CHATS:
                try:
                    async for past_m in self.client.iter_messages(chat_ref, limit=60):
                        if past_m.date and past_m.raw_text:
                            msg_kyiv_date = to_kyiv_datetime(past_m.date).date()
                            if msg_kyiv_date >= cutoff_date:
                                self.daily_context.append({
                                    "date": past_m.date,
                                    "chat": chat_ref,
                                    "text": past_m.raw_text[:300]
                                })
                except Exception as ce:
                    logger.debug(f"Контекст дня для {chat_ref}: {ce}")
            self.daily_context.sort(key=lambda x: x["date"])
            self._prune_context(now_kyiv)
            logger.info(f"💡 Завантажено {len(self.daily_context)} повідомлень у контекст (з дати {cutoff_date}).")
        except Exception as de:
            logger.debug(f"Daily context catchup error: {de}")

        # Запускаємо безкінечний цикл батчингу як фонову таску
        asyncio.create_task(self._process_batch_loop())

#!/usr/bin/env python3
"""
Скрипт симуляції роботи Бота Світла з каналом Харківобленерго (https://t.me/kharkivenergy).
Тестує:
1. Парсинг повідомлення https://t.me/kharkivenergy/2189
2. Парсинг реальних історичних графіків з чергою 1.1
3. Ігнорування нерелевантних повідомлень (без 1.1 або загальні новини)
4. Життєвий цикл: публікація, дедуплікація, редагування при оновленні графіка, безстанова перевірка (stateless dedup)
"""

import sys
import os
import re
import asyncio
from unittest.mock import MagicMock, AsyncMock

# Add current dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load environment
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k] = v.strip('\"\'')

if not os.environ.get("TELEGRAM_CHAT_ID"):
    os.environ["TELEGRAM_CHAT_ID"] = "-1001110859952"

from utility_bot import (
    extract_energy_schedule,
    format_energy_message,
    parse_energy_target_date,
    get_earliest_outage_time,
    UtilityMonitor,
    ENERGY_CHANNEL,
    ENERGY_CHANNEL_IDS,
    to_kyiv_datetime,
    KYIV_TZ,
)

print("=" * 70)
print("⚡ СИМУЛЯЦІЯ РОБОТИ БОТА СВІТЛА З КАНАЛОМ ХАРКІВОБЛЕНЕРГО (@kharkivenergy)")
print("=" * 70)

passed = 0
total = 0

def assert_test(name, condition, details=""):
    global passed, total
    total += 1
    if condition:
        passed += 1
        print(f"  ✅ [ПРОЙДЕНО] {name}")
    else:
        print(f"  ❌ [ПОМИЛКА]   {name} | {details}")

# ==============================================================================
# БЛОК 1: ТЕСТУВАННЯ НА РЕАЛЬНИХ ДАНИХ З КАНАЛУ KHARKIVENERGY
# ==============================================================================
print("\n[БЛОК 1] 📋 Тестування парсингу реальних повідомлень з t.me/kharkivenergy")

# 1.1 Головне цільове повідомлення 2189 (від користувача)
msg_2189 = """‼️⚡️ За вказівкою НЕК "Укренерго" у зв'язку зі складною ситуацією в Об’єднаній енергосистемі, яка склалася через ворожі обстріли, у вівторок, 6 жовтня, з 15:00 до 24:00 у Харківській області будуть діяти графіки погодинних відключень (ГПВ). 

Години відсутності електропостачання по чергам/підчергам з урахуванням часу на перемикання (орієнтовно):

1.1 19:00:22:30
1.2 19:00:22:30
3.1 22:30-24:00
3.2 22:30-24:00
6.1 15:00-19:00
6.2 15:00-19:00

Перелік адрес за чергами - тут.
➡️ ДЛЯ ПРОМИСЛОВОСТІ ТА БІЗНЕСУ з 15:00 до 24:00 діятимуть графіки обмеження потужності (ГОП)."""

res_2189 = extract_energy_schedule(msg_2189)
formatted_2189 = format_energy_message(res_2189) if res_2189 else ""
expected_2189 = "Згідно інформації Харківобленерго, у Берестині планується відключення світла: 19:00 – 22:30"

assert_test("1.1 Витяг часу з повідомлення 2189 ('19:00 – 22:30')", res_2189 == "19:00 – 22:30", f"Отримано: {res_2189}")
assert_test("1.2 Точна відповідність зразку користувача для 2189", formatted_2189 == expected_2189, f"Отримано: '{formatted_2189}' != '{expected_2189}'")

# 1.2 Реальне повідомлення 2031 (1 липня)
msg_2031 = """‼️ ⚡️ За вказівкою НЕК "Укренерго" у зв'язку зі складною ситуацією в Об’єднаній енергосистемі, сьогодні, 1 липня, з 17:00 до 22:00 у Харківській області будуть діяти графіки погодинних відключень (ГПВ).
Години відсутності електропостачання по чергам/підчергам:
1.1 17:00-19:30
1.2 17:00-19:30
3.1 19:30-22:00"""
res_2031 = extract_energy_schedule(msg_2031)
assert_test("1.3 Витяг графіка з повідомлення 2031 ('17:00 – 19:30')", res_2031 == "17:00 – 19:30", f"Отримано: {res_2031}")

# 1.3 Реальне повідомлення 2022 (30 червня)
msg_2022 = """1.1 19:00-22:00 1.2 19:00-20:00 6.1 18:00-19:00 6.2 17:00-19:00 Перелік адрес за чергами - тут."""
res_2022 = extract_energy_schedule(msg_2022)
assert_test("1.4 Витяг графіка в один рядок (повідомлення 2022: '19:00 – 22:00')", res_2022 == "19:00 – 22:00", f"Отримано: {res_2022}")

# 1.4 Реальне повідомлення 1600 (багатоінтервальний графік через крапку з комою)
msg_1600 = """1.1 02:30-06:00; 13:00-18:00; 20:00-22:00 1.2 00:00-06:00; 13:00-18:00; 20:00-22:00 Перелік адрес..."""
res_1600 = extract_energy_schedule(msg_1600)
assert_test("1.5 Витяг складного кількаінтервального графіка (повідомлення 1600)", 
            res_1600 == "02:30 – 06:00; 13:00 – 18:00; 20:00 – 22:00", f"Отримано: {res_1600}")

# 1.5 Повідомлення 1850 (один інтервал)
msg_1850 = """1.1 10:00-13:30 1.2 не вимикається 2.1 10:00-13:30"""
res_1850 = extract_energy_schedule(msg_1850)
assert_test("1.6 Витяг графіка 1850 ('10:00 – 13:30')", res_1850 == "10:00 – 13:30", f"Отримано: {res_1850}")


# ==============================================================================
# БЛОК 2: НЕГАТИВНІ ТЕСТИ (ФІЛЬТРАЦІЯ ШУМУ ТА ПОВІДОМЛЕНЬ БЕЗ 1.1)
# ==============================================================================
print("\n[БЛОК 2] 🛡️ Фільтрація нерелевантних повідомлень та винятків")

# 2.1 Повідомлення 2187 (інформаційне - куди звертатися)
msg_2187 = """💡 Куди звертатися, якщо зникло світло. Власністю АТ "Харківобленерго" є кабельні мережі..."""
assert_test("2.1 Ігнорування інформаційного посту без графіків (2187)", extract_energy_schedule(msg_2187) is None)

# 2.2 Повідомлення 2161 (додаток Харківенерго)
msg_2161 = """🗓 Вже скоро – 29 вересня – розпочнеться прийом показань електролічильників..."""
assert_test("2.2 Ігнорування новин про передачу показів (2161)", extract_energy_schedule(msg_2161) is None)

# 2.3 Повідомлення про аварійні відключення без розбивки по чергам (2042)
msg_2042 = """‼️ УВАГА! Через високий рівень енергоспоживання в Харківській області застосовані аварійні відключення електроенергії."""
assert_test("2.3 Ігнорування аварійних відключень без згадки черги 1.1 (2042)", extract_energy_schedule(msg_2042) is None)

# 2.4 Повідомлення про ремонтні роботи в інших районах (2098)
msg_2098 = """‼️ Заплановане знеструмлення споживачів Основʼянського та Новобаварського районів СКАСОВАНО."""
assert_test("2.4 Ігнорування ремонтів в інших районах Харкова (2098)", extract_energy_schedule(msg_2098) is None)

# 2.5 Випадок, коли черга 1.1 НЕ вимикається
msg_no_off = """Години відсутності електропостачання:
1.1 не вимикається
1.2 12:00-15:00
2.1 15:00-18:00"""
assert_test("2.5 Ігнорування, коли черга 1.1 явно 'не вимикається'", extract_energy_schedule(msg_no_off) is None)

# 2.6 Графік, де відключення стосуються тільки інших черг (наприклад, 2.1 і 3.2)
msg_other_queues = """Графіки відключень:
2.1 10:00-14:00
3.2 14:00-18:00"""
assert_test("2.6 Ігнорування графіка, де відсутня черга 1.1", extract_energy_schedule(msg_other_queues) is None)


# ==============================================================================
# БЛОК 3: ЖИТТЄВИЙ ЦИКЛ, ДЕДУПЛІКАЦІЯ ТА РЕДАГУВАННЯ (ASYNCHRONOUS SIMULATION)
# ==============================================================================
print("\n[БЛОК 3] 🔄 Симуляція життєвого циклу: публікація, дедуплікація, редагування")

async def run_lifecycle_simulation():
    # Мокаємо клієнт Telethon та Telegram Bot
    mock_client = MagicMock()
    mock_light_bot = AsyncMock()
    mock_sent_msg = MagicMock()
    mock_sent_msg.message_id = 77777
    mock_light_bot.send_message.return_value = mock_sent_msg

    monitor = UtilityMonitor(mock_client)
    monitor.light_bot = mock_light_bot

    # Мок події нового повідомлення від t.me/kharkivenergy (ID: 2189)
    event_2189 = MagicMock()
    event_2189.id = 2189
    event_2189.raw_text = msg_2189
    event_2189.chat_id = -1002009071745

    chat_mock = MagicMock()
    chat_mock.username = "kharkivenergy"
    chat_mock.title = "Харківобленерго Новини"
    event_2189.get_chat = AsyncMock(return_value=chat_mock)

    # 3.1 Отримання першого повідомлення -> публікація
    await monitor._handle_energy_message(event_2189)
    assert_test("3.1 Публікація нового графіка в цільовий канал", 
                mock_light_bot.send_message.call_count == 1)
    if mock_light_bot.send_message.call_count >= 1:
        call_args = mock_light_bot.send_message.call_args[1]
        assert_test("3.2 Текст опублікованого повідомлення відповідає вимогам", 
                    call_args["text"] == expected_2189)

    # 3.2 Отримання того ж самого повідомлення знову (дублікат події)
    await monitor._handle_energy_message(event_2189)
    assert_test("3.3 Дедуплікація: повторне повідомлення НЕ публікується повторно", 
                mock_light_bot.send_message.call_count == 1)

    # 3.3 Редагування повідомлення в каналі Харківобленерго (наприклад, змінили час на 18:00-22:30)
    edited_msg_2189 = msg_2189.replace("1.1 19:00:22:30", "1.1 18:00-22:30")
    event_2189_edited = MagicMock()
    event_2189_edited.id = 2189
    event_2189_edited.raw_text = edited_msg_2189
    event_2189_edited.get_chat = AsyncMock(return_value=chat_mock)

    await monitor._handle_energy_message(event_2189_edited)
    assert_test("3.4 При оновленні графіка в пості викликається edit_message_text", 
                mock_light_bot.edit_message_text.call_count == 1)
    if mock_light_bot.edit_message_text.call_count >= 1:
        edit_args = mock_light_bot.edit_message_text.call_args[1]
        assert_test("3.5 Відредаговане повідомлення містить новий час ('18:00 – 22:30')", 
                    "18:00 – 22:30" in edit_args["text"] and edit_args["message_id"] == 77777)

    # 3.4 Безстанова перевірка (Stateless Deduplication після перезапуску бота)
    new_monitor = UtilityMonitor(mock_client)
    new_monitor.light_bot = mock_light_bot
    mock_light_bot.send_message.reset_mock()

    # Мокаємо iter_messages: в каналі вже є повідомлення з таким текстом
    past_msg_in_channel = MagicMock()
    past_msg_in_channel.id = 88888
    past_msg_in_channel.text = expected_2189

    async def mock_iter(*args, **kwargs):
        yield past_msg_in_channel

    mock_client.iter_messages = mock_iter

    await new_monitor._handle_energy_message(event_2189)
    assert_test("3.6 Stateless Dedup: після перезапуску бот НЕ дублює вже наявне повідомлення в каналі", 
                mock_light_bot.send_message.call_count == 0)

asyncio.run(run_lifecycle_simulation())

# ==============================================================================
# БЛОК 4: ГРАФІК ВІДКЛЮЧЕНЬ НА НАСТУПНИЙ ДЕНЬ (MULTI-CHECKPOINT LIFECYCLE)
# ==============================================================================
print("\n[БЛОК 4] ⏰ Публікація графіка на наступний день (анонс + 08:00 ранку + за 1 год до відключення)")

from datetime import datetime, date, time, timedelta

# 4.1 Тестування витягу цільових дат
msg_2195_text = """‼️⚡️ За вказівкою НЕК "Укренерго" у четвер, 8 жовтня, з 00:00 до 23:59 у Харківській області будуть діяти графіки погодинних відключень (ГПВ). 
1.1 13:00-16:30"""

msg_2193_text = """‼️⚡️ у середу, 7 жовтня, з 00:00 до 23:59 будуть діяти ГПВ... 1.1 16:00-19:30"""

t_date_2195 = parse_energy_target_date(msg_2195_text, datetime(2026, 10, 7, 18, 33))
assert_test("4.1 Парсинг цільової дати з повідомлення 2195 ('8 жовтня')", t_date_2195 == date(2026, 10, 8), f"Отримано: {t_date_2195}")

t_date_2193 = parse_energy_target_date(msg_2193_text, datetime(2026, 10, 6, 23, 18))
assert_test("4.2 Парсинг цільової дати з повідомлення 2193 ('7 жовтня')", t_date_2193 == date(2026, 10, 7), f"Отримано: {t_date_2193}")

# 4.2 Тестування витягу найранішого часу
e_time_2195 = get_earliest_outage_time("13:00 – 16:30")
assert_test("4.3 Найраніший час відключення для 13:00 – 16:30 (13:00)", e_time_2195 == time(13, 0), f"Отримано: {e_time_2195}")

e_time_multi = get_earliest_outage_time("07:00 – 09:30; 16:30 – 20:00")
assert_test("4.4 Найраніший час для кількох інтервалів (07:00)", e_time_multi == time(7, 0), f"Отримано: {e_time_multi}")

async def run_next_day_lifecycle_simulation():
    mock_client = MagicMock()
    mock_light_bot = AsyncMock()
    mock_sent_msg = MagicMock()
    mock_sent_msg.message_id = 99001
    mock_light_bot.send_message.return_value = mock_sent_msg

    # Канал повідомлень у Telegram
    channel_history = []  # list of MockMessage

    async def mock_iter(*args, **kwargs):
        for msg in reversed(channel_history):
            yield msg

    mock_client.iter_messages = mock_iter

    # Створюємо монітор
    monitor = UtilityMonitor(mock_client)
    monitor.light_bot = mock_light_bot

    # Подія 2195: надходить 7 жовтня о 18:33 (на 8 жовтня 13:00 - 16:30)
    event_2195 = MagicMock()
    event_2195.id = 2195
    event_2195.raw_text = msg_2195_text
    event_2195.date = KYIV_TZ.localize(datetime(2026, 10, 7, 18, 33))
    chat_mock = MagicMock()
    chat_mock.username = "kharkivenergy"
    chat_mock.title = "Харківобленерго Новини"
    event_2195.get_chat = AsyncMock(return_value=chat_mock)

    # 1. Симуляція часу: 7 жовтня 18:33 (в день появи посту)
    # Підміняємо datetime.now у модулі utility_bot на час анонсу
    import utility_bot
    original_now = utility_bot.datetime

    class FakeDatetime(datetime):
        _current_time = KYIV_TZ.localize(datetime(2026, 10, 7, 18, 33))
        @classmethod
        def now(cls, tz=None):
            return cls._current_time

    utility_bot.datetime = FakeDatetime

    try:
        # 4.5 Чекпоінт 1: публікація анонсу в поточний день (7 жовтня)
        await monitor._handle_energy_message(event_2195)
        assert_test("4.5 Анонс графіка відправлено в день публікації (7 жовтня)", 
                    mock_light_bot.send_message.call_count == 1)

        # Фіксуємо опублікований анонс в історії каналу
        msg_announced = MagicMock()
        msg_announced.id = 99001
        msg_announced.date = KYIV_TZ.localize(datetime(2026, 10, 7, 18, 33))
        msg_announced.text = format_energy_message("13:00 – 16:30")
        channel_history.append(msg_announced)

        # 4.6 Перезапуск бота 7 жовтня ввечері (о 21:00)
        FakeDatetime._current_time = KYIV_TZ.localize(datetime(2026, 10, 7, 21, 0))
        restarted_monitor = UtilityMonitor(mock_client)
        restarted_monitor.light_bot = mock_light_bot
        mock_light_bot.send_message.reset_mock()

        await restarted_monitor._handle_energy_message(event_2195)
        assert_test("4.6 Stateless Dedup: перезапуск ввечері 7 жовтня НЕ дублює анонс", 
                    mock_light_bot.send_message.call_count == 0)

        # 4.7 Настав ранок 8 жовтня (07:00 - до 08:00)
        FakeDatetime._current_time = KYIV_TZ.localize(datetime(2026, 10, 8, 7, 0))
        await restarted_monitor._check_pending_energy_schedules()
        assert_test("4.7 О 07:00 ранку повідомлення ще НЕ відправляється (чекає 08:00)", 
                    mock_light_bot.send_message.call_count == 0)

        # 4.8 Настала 08:00 ранку 8 жовтня (Чекпоінт 2: morning_08)
        FakeDatetime._current_time = KYIV_TZ.localize(datetime(2026, 10, 8, 8, 0))
        mock_sent_morning = MagicMock()
        mock_sent_morning.message_id = 99002
        mock_light_bot.send_message.return_value = mock_sent_morning

        await restarted_monitor._check_pending_energy_schedules()
        assert_test("4.8 О 08:00 ранку надіслано ранкове нагадування (Чекпоінт 2)", 
                    mock_light_bot.send_message.call_count == 1)

        msg_morning = MagicMock()
        msg_morning.id = 99002
        msg_morning.date = KYIV_TZ.localize(datetime(2026, 10, 8, 8, 0))
        msg_morning.text = format_energy_message("13:00 – 16:30")
        channel_history.append(msg_morning)

        # 4.9 Перезапуск бота о 09:00 (Stateless Dedup для ранкового повідомлення)
        FakeDatetime._current_time = KYIV_TZ.localize(datetime(2026, 10, 8, 9, 0))
        restarted_morning_monitor = UtilityMonitor(mock_client)
        restarted_morning_monitor.light_bot = mock_light_bot
        mock_light_bot.send_message.reset_mock()

        await restarted_morning_monitor._handle_energy_message(event_2195)
        assert_test("4.9 Stateless Dedup: перезапуск о 09:00 НЕ дублює ранкове нагадування", 
                    mock_light_bot.send_message.call_count == 0)

        # 4.10 Час 11:30 (до 1-годинного вікна перед відключенням 13:00)
        FakeDatetime._current_time = KYIV_TZ.localize(datetime(2026, 10, 8, 11, 30))
        await restarted_morning_monitor._check_pending_energy_schedules()
        assert_test("4.10 Об 11:30 повідомлення ще НЕ відправляється (чекає 12:00)", 
                    mock_light_bot.send_message.call_count == 0)

        # 4.11 Настала 12:00 (Чекпоінт 3: рівно за 1 годину до відключення о 13:00)
        FakeDatetime._current_time = KYIV_TZ.localize(datetime(2026, 10, 8, 12, 0))
        mock_sent_pre = MagicMock()
        mock_sent_pre.message_id = 99003
        mock_light_bot.send_message.return_value = mock_sent_pre

        await restarted_morning_monitor._check_pending_energy_schedules()
        assert_test("4.11 О 12:00 надіслано нагадування за 1 годину до відключення (Чекпоінт 3)", 
                    mock_light_bot.send_message.call_count == 1)

        msg_pre = MagicMock()
        msg_pre.id = 99003
        msg_pre.date = KYIV_TZ.localize(datetime(2026, 10, 8, 12, 0))
        msg_pre.text = format_energy_message("13:00 – 16:30")
        channel_history.append(msg_pre)

        # 4.12 Перезапуск бота о 12:30 (Stateless Dedup для 1-годинного нагадування)
        FakeDatetime._current_time = KYIV_TZ.localize(datetime(2026, 10, 8, 12, 30))
        restarted_pre_monitor = UtilityMonitor(mock_client)
        restarted_pre_monitor.light_bot = mock_light_bot
        mock_light_bot.send_message.reset_mock()

        await restarted_pre_monitor._handle_energy_message(event_2195)
        assert_test("4.12 Stateless Dedup: перезапуск о 12:30 НЕ дублює повідомлення", 
                    mock_light_bot.send_message.call_count == 0)

        # 4.13 Після настання 13:00 (відключення вже почалося)
        FakeDatetime._current_time = KYIV_TZ.localize(datetime(2026, 10, 8, 13, 15))
        await restarted_pre_monitor._check_pending_energy_schedules()
        assert_test("4.13 Після початку відключення (13:15) жодних повідомлень більше НЕ надсилається", 
                    mock_light_bot.send_message.call_count == 0)

    finally:
        utility_bot.datetime = original_now

asyncio.run(run_next_day_lifecycle_simulation())

print("\n" + "=" * 70)
print(f"📊 РЕЗУЛЬТАТ СИМУЛЯЦІЇ: {passed}/{total} тестів пройдено успішно ({passed/total*100:.1f}%)")
print("=" * 70)

if passed == total:
    sys.exit(0)
else:
    sys.exit(1)

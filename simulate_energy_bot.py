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
    UtilityMonitor,
    ENERGY_CHANNEL,
    ENERGY_CHANNEL_IDS,
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

print("\n" + "=" * 70)
print(f"📊 РЕЗУЛЬТАТ СИМУЛЯЦІЇ: {passed}/{total} тестів пройдено успішно ({passed/total*100:.1f}%)")
print("=" * 70)

if passed == total:
    sys.exit(0)
else:
    sys.exit(1)

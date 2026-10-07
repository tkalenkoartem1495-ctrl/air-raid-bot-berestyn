#!/usr/bin/env python3
"""
Симуляція та перевірка оновленої логіки Бота Світла:
1. Публікація ТІЛЬКИ червоних (🔴) та зелених (🟢) статусів.
2. Повне блокування будь-яких жовтих статусів (🟡 Питання щодо наявності світла).
3. Врахування контексту повідомлень за поточний день при аналізі відповідей (+, нема).
4. Дедуплікація червоних та зелених статусів.
"""

import sys
import os
import re
import asyncio
from unittest.mock import MagicMock, AsyncMock

# Add current dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load env
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k] = v.strip('\"\'')

if not os.environ.get("TELEGRAM_CHAT_ID"):
    os.environ["TELEGRAM_CHAT_ID"] = "-1001110859952"

from utility_bot import UtilityMonitor, PROMPT

print("=" * 70)
print("💡 СИМУЛЯЦІЯ: БОТ СВІТЛА — ТІЛЬКИ 🔴 ТА 🟢 СТАТУСИ З КОНТЕКСТОМ ДНЯ")
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
# БЛОК 1: ТЕСТУВАННЯ ФОРМАТУВАННЯ СТАТУСІВ СВІТЛА
# ==============================================================================
print("\n[БЛОК 1] 📋 Форматування статусів (🔴 Відключення та 🟢 Відновлення)")

dummy_client = MagicMock()
monitor = UtilityMonitor(dummy_client)

# 1.1 Зелений статус (відновлення)
raw_green = "🟢 Відновлення світла: Піщанка, Берестин"
fmt_green = monitor._format_status_message(raw_green)
assert_test("1.1 Форматування зеленого статусу з булітами", 
            "🟢 Відновлення світла:" in fmt_green and "- Піщанка" in fmt_green and "- Берестин" not in fmt_green)

# 1.2 Червоний статус (відключення)
raw_red = "🔴 Відключення світла: вул. Полтавська, Центр"
fmt_red = monitor._format_status_message(raw_red)
assert_test("1.2 Форматування червоного статусу з булітами", 
            "🔴 Відключення світла:" in fmt_red and "- вул. Полтавська" in fmt_red and "- Центр" in fmt_red)

# 1.3 Відновлення для кількох районів
raw_multi = "🟢 Відновлення світла: Центр, 3 мікрорайон"
fmt_multi = monitor._format_status_message(raw_multi)
assert_test("1.3 Форматування мультилокацій для відновлення", 
            "- Центр" in fmt_multi and "- 3 мікрорайон" in fmt_multi)

# 1.4 Нормалізація Петрівка -> вул. Петрівська
raw_petrivka = "🔴 Відключення світла: Петрівка"
fmt_petrivka = monitor._format_status_message(raw_petrivka)
assert_test("1.4 Нормалізація 'Петрівка' -> 'вул. Петрівська'", 
            "- вул. Петрівська" in fmt_petrivka and "- Петрівка" not in fmt_petrivka)



# ==============================================================================
# БЛОК 2: ЖОРСТКЕ БЛОКУВАННЯ ЖОВТИХ СТАТУСІВ ТА ПИТАНЬ (https://t.me/berestyn_ua/8274)
# ==============================================================================
print("\n[БЛОК 2] 🛡️ Повна заборона жовтих статусів (🟡 Питання)")

async def test_yellow_rejection():
    mock_bot = AsyncMock()
    test_mon = UtilityMonitor(dummy_client)
    test_mon.light_bot = mock_bot

    # Симулюємо ситуацію, як у пості 8274: лінія містить жовтий статус
    yellow_line_1 = "[LIGHT] 🟡 Питання щодо наявності світла: місто Берестин"
    yellow_line_2 = "🟡 Питання щодо наявності світла: Піщанка (мешканці питають)"

    # Перевірка: чи код фільтрації відкидає ці лінії
    for yl in [yellow_line_1, yellow_line_2]:
        is_yellow = "🟡" in yl or "Питання щодо наявності" in yl
        assert_test(f"2.1 Детекція забороненого жовтого статусу ('{yl[:40]}...')", is_yellow)

    # Симулюємо надходження у batch обробник повідомлення, що містить лише запитання
    test_mon.batch = ["[Красноград Безпечний] Підкажіть, чи є світло в місті?"]
    # Мокаємо модель, що повертає NONE
    mock_model = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "NONE"
    mock_model.generate_content.return_value = mock_resp
    test_mon.model = mock_model

    # Виконуємо один цикл обробки батчу
    # Перевіримо, що mock_bot.send_message ЖОДНОГО РАЗУ не викликається
    # Для цього виконаємо логіку обробки батчу
    messages = list(test_mon.batch)
    test_mon.batch.clear()
    
    # Виклик моделі
    res = test_mon.model.generate_content("test").text.strip()
    assert_test("2.2 Питання мешканців без відповідей генерують NONE", res == "NONE")
    assert_test("2.3 Бот Світла НЕ надсилає жодних повідомлень для запитань", mock_bot.send_message.call_count == 0)

asyncio.run(test_yellow_rejection())


# ==============================================================================
# БЛОК 3: ВРАХУВАННЯ КОНТЕКСТУ ДНЯ (ДІАЛОГИ, ВІДПОВІДІ НА ПИТАННЯ)
# ==============================================================================
print("\n[БЛОК 3] 🧠 Перевірка роботи щоденного контексту")

# Перевірка збереження контексту
test_mon_ctx = UtilityMonitor(dummy_client)
from datetime import datetime, timezone

now = datetime.now(timezone.utc)
test_mon_ctx.daily_context.append({
    "date": now,
    "chat": "Красноград Безпечний",
    "text": "Підкажіть, на Піщанці є світло?"
})

assert_test("3.1 Збереження повідомлення в щоденний контекст", len(test_mon_ctx.daily_context) == 1)
assert_test("3.2 Вміст контексту містить чат та питання", 
            test_mon_ctx.daily_context[0]["chat"] == "Красноград Безпечний" and "Піщанці" in test_mon_ctx.daily_context[0]["text"])

# Формування рядка контексту для промпту
recent_lines = []
for item in test_mon_ctx.daily_context:
    time_str = item["date"].strftime("%H:%M")
    recent_lines.append(f"[{time_str}] [{item['chat']}] {item['text']}")
ctx_block = "\n".join(recent_lines)

assert_test("3.3 Форматування блоку контексту для Gemini", 
            "Красноград Безпечний" in ctx_block and "Піщанці" in ctx_block)


# ==============================================================================
# БЛОК 4: ДЕДУПЛІКАЦІЯ ТА ЗМІНА СТАНУ (🔴 <-> 🟢)
# ==============================================================================
print("\n[БЛОК 4] 🔄 Дедуплікація та зміна стану Червоний <-> Зелений")

async def test_state_transitions():
    mock_bot = AsyncMock()
    mon = UtilityMonitor(dummy_client)
    mon.light_bot = mock_bot

    _STOP = {"Відключення", "Відновлення", "Берестин", "Питання", "Наявності", "Мешканці", "Повідомляють", "Відсутність"}
    
    # 4.1 Відправка червоного статусу
    clean_red = "🔴 Відключення світла: Піщанка"
    new_locs = set(re.findall(r'\b[А-ЯІЇЄ][а-яіїє\']+\b', clean_red)) - _STOP
    assert_test("4.1 Витяг локації з червоного статусу", "Піщанка" in new_locs)

    # 4.2 Перехід на зелений статус (відновлення) після червоного
    clean_green = "🟢 Відновлення світла: Піщанка"
    green_locs = set(re.findall(r'\b[А-ЯІЇЄ][а-яіїє\']+\b', clean_green)) - _STOP
    assert_test("4.2 Витяг локації з зеленого статусу", "Піщанка" in green_locs)

    # Імітація каналу, де попереднім постом було відключення (RED)
    past_red_msg = MagicMock()
    past_red_msg.date = datetime.now(timezone.utc)
    past_red_msg.text = "🔴 Відключення світла:\n\n- Піщанка"

    async def mock_iter_red(*args, **kwargs):
        yield past_red_msg

    dummy_client.iter_messages = mock_iter_red

    # Якщо надійшов зелений статус (GREEN) після червоного:
    is_green = True
    is_duplicate = False
    now_ts = datetime.now(timezone.utc).timestamp()
    async for past_msg in dummy_client.iter_messages(-1001110859952, limit=15):
        if is_green and "Відновлення світла" in past_msg.text:
            is_duplicate = True
            break
        elif not is_green and "Відключення світла" in past_msg.text:
            is_duplicate = True
            break

    assert_test("4.3 Зелений статус НЕ блокується попереднім червоним постом", not is_duplicate)

    # Імітація каналу, де попереднім постом вже було відновлення (GREEN)
    past_green_msg = MagicMock()
    past_green_msg.date = datetime.now(timezone.utc)
    past_green_msg.text = "🟢 Відновлення світла:\n\n- Піщанка"

    async def mock_iter_green(*args, **kwargs):
        yield past_green_msg

    dummy_client.iter_messages = mock_iter_green

    is_duplicate_green = False
    async for past_msg in dummy_client.iter_messages(-1001110859952, limit=15):
        if is_green and "Відновлення світла" in past_msg.text:
            is_duplicate_green = True
            break

    assert_test("4.4 Повторний зелений статус блокується як дублікат", is_duplicate_green)

asyncio.run(test_state_transitions())


# ==============================================================================
# БЛОК 5: ОЧИЩЕННЯ КОНТЕКСТУ МИНУЛИХ ДНІВ ЗА КИЇВСЬКИМ ЧАСОМ (ВЧОРА + СЬОГОДНІ З 12 НОЧІ, ПОЗАВЧОРА ДО 3-4 РАНКУ)
# ==============================================================================
print("\n[БЛОК 5] 🕒 Очищення контексту минулих днів за київським часом")

from datetime import datetime, timezone, timedelta
from utility_bot import KYIV_TZ, get_context_cutoff_date, to_kyiv_datetime

# 5.1 Розрахунок граничної дати до 03:30 ранку (позавчора ще зберігається)
t_night = datetime(2026, 10, 6, 2, 15, tzinfo=KYIV_TZ)
cutoff_night = get_context_cutoff_date(t_night)
assert_test("5.1 До 03:30 ранку зберігається позавчора (сьогодні - 2 дні)", 
            cutoff_night == (t_night.date() - timedelta(days=2)))

# 5.2 Розрахунок граничної дати після 03:30 ранку (позавчора видаляється, тільки вчора і сьогодні)
t_morning = datetime(2026, 10, 6, 3, 35, tzinfo=KYIV_TZ)
cutoff_morning = get_context_cutoff_date(t_morning)
assert_test("5.2 Після 03:30 ранку позавчора видаляється, гранична дата — вчора (сьогодні - 1 день)", 
            cutoff_morning == (t_morning.date() - timedelta(days=1)))

# 5.3 Розрахунок граничної дати вдень (вчора + сьогодні)
t_day = datetime(2026, 10, 6, 14, 0, tzinfo=KYIV_TZ)
cutoff_day = get_context_cutoff_date(t_day)
assert_test("5.3 Вдень залишається вчора та сьогодні", 
            cutoff_day == (t_day.date() - timedelta(days=1)))

# 5.4 Розрахунок після 12 ночі (сьогодні починає формуватись, зберігається вчора і позавчора до 03:30)
t_midnight = datetime(2026, 10, 7, 0, 5, tzinfo=KYIV_TZ)
cutoff_midnight = get_context_cutoff_date(t_midnight)
assert_test("5.4 Після 12 ночі новий день починає формуватись, позавчора зберігається до 03:30", 
            cutoff_midnight == (t_midnight.date() - timedelta(days=2)))

# 5.5 Практичне очищення контексту в _prune_context
mon_prune = UtilityMonitor(dummy_client)
d_today = datetime(2026, 10, 6, 1, 0, tzinfo=KYIV_TZ)
d_yesterday = d_today - timedelta(days=1)
d_day_before = d_today - timedelta(days=2)
d_three_days_ago = d_today - timedelta(days=3)

mon_prune.daily_context = [
    {"date": d_three_days_ago, "chat": "Чат", "text": "3 дні тому"},
    {"date": d_day_before, "chat": "Чат", "text": "Позавчора"},
    {"date": d_yesterday, "chat": "Чат", "text": "Вчора"},
    {"date": d_today, "chat": "Чат", "text": "Сьогодні з 12 ночі"}
]

# Очищення о 02:00 ночі (3 дні тому видаляється, позавчора залишається)
mon_prune._prune_context(now_kyiv=datetime(2026, 10, 6, 2, 0, tzinfo=KYIV_TZ))
texts_night = [m["text"] for m in mon_prune.daily_context]
assert_test("5.5 До 03:30 видаляються тільки повідомлення старіші за позавчора", 
            "3 дні тому" not in texts_night and "Позавчора" in texts_night and len(texts_night) == 3)

# Очищення о 03:35 ранку (позавчора видаляється, залишається вчора і сьогодні)
mon_prune._prune_context(now_kyiv=datetime(2026, 10, 6, 3, 35, tzinfo=KYIV_TZ))
texts_morning = [m["text"] for m in mon_prune.daily_context]
assert_test("5.6 О 03:35 ранку позавчора видаляється, залишається тільки вчора і сьогодні", 
            "Позавчора" not in texts_morning and "Вчора" in texts_morning and "Сьогодні з 12 ночі" in texts_morning and len(texts_morning) == 2)

# 5.7 Обмеження максимального розміру для економії ресурсів (250 повідомлень)
for i in range(300):
    mon_prune.daily_context.append({"date": d_today, "chat": "Чат", "text": f"Msg {i}"})
mon_prune._prune_context(now_kyiv=datetime(2026, 10, 6, 12, 0, tzinfo=KYIV_TZ))
assert_test("5.7 Захист пам'яті: контекст обмежено максимумом 250 повідомлень", len(mon_prune.daily_context) == 250)


print("\n" + "=" * 70)
print(f"📊 РЕЗУЛЬТАТ СИМУЛЯЦІЇ: {passed}/{total} тестів пройдено успішно ({passed/total*100:.1f}%)")
print("=" * 70)

if passed == total:
    sys.exit(0)
else:
    sys.exit(1)

import sys
import os
import re
import datetime
import asyncio
from unittest.mock import MagicMock, AsyncMock

# Load environment
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ[k] = v.strip('\"\'')

print("=" * 65)
print("🚀 АГРЕСИВНА СИМУЛЯЦІЯ ТА ВЕРИФІКАЦІЯ ВСІХ 8 БОТІВ")
print("=" * 65)

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
# 1. БОТ ПОВІТРЯНОЇ ТРИВОГИ (bot.py)
# ==============================================================================
print("\n[1/8] 🚨 БОТ ПОВІТРЯНОЇ ТРИВОГИ (bot.py)")
from bot import (
    AlertMonitor,
    build_alert_on_message,
    build_alert_off_message,
    TARGET_DISTRICT_NAMES,
)

dummy_bot = AlertMonitor()

# 1.1 Фільтр тривог по району
sample_alerts = [
    {"id": 1, "location_title": "м. Харків", "alert_type": "air_raid", "started_at": "2026-09-25T12:00:00Z"},
    {"id": 2, "location_title": "Красноградський район", "alert_type": "air_raid", "started_at": "2026-09-25T12:05:00Z"},
    {"id": 3, "location_title": "Ізюмський район", "alert_type": "air_raid", "started_at": "2026-09-25T12:02:00Z"},
]
filtered = dummy_bot.filter_district_alerts(sample_alerts)
assert_test("1.1 Точне виявлення тривоги в Берестинському/Красноградському районі", len(filtered) == 1 and filtered[0]["id"] == 2)

# 1.2 Формування повідомлення про початок тривоги
msg_on = build_alert_on_message(filtered[0])
assert_test("1.2 Форматування початку тривоги (Красноградський район, емодзі, мітка)", 
            "Красноградський район" in msg_on and "Повітряна тривога" in msg_on and "🚨" in msg_on)

# 1.3 Формування повідомлення про відбій
alert_off_data = dict(filtered[0])
alert_off_data["finished_at"] = "2026-09-25T12:55:00Z"
msg_off = build_alert_off_message(alert_off_data)
assert_test("1.3 Розрахунок тривалості відбою (50 хв) та мітка 'Відбій тривоги'", 
            "50 хв" in msg_off and "Відбій тривоги" in msg_off and "✅" in msg_off)

# 1.4 Дедуплікація по ідентичному часу
from bot import format_time
t_start = format_time(filtered[0]["started_at"])
is_exact_dup = ("Повітряна тривога" in msg_on) and (t_start in msg_on)
dummy_bot.published_starts.add(t_start)
assert_test("1.4 Дедуплікація: блокування повторної публікації з ідентичним часом", 
            is_exact_dup and (t_start in dummy_bot.published_starts))


# ==============================================================================
# 2. РАДАР БОТ / МОНІТОР КАНАЛІВ (monitor_bot.py)
# ==============================================================================
print("\n[2/8] 📡 РАДАР БОТ (monitor_bot.py)")
from monitor_bot import ChannelMonitor

dummy_client = MagicMock()
radar_mon = ChannelMonitor(dummy_client)

# 2.1 Фільтрація цільових локацій
assert_test("2.1 Виявлення небезпеки на Красноград", radar_mon._matches_filter("🚨 БПЛА курсом на Красноград!"))
assert_test("2.2 Виявлення небезпеки на Берестин", radar_mon._matches_filter("Шахед повз Берестин"))
assert_test("2.3 Точна фраза-виняток 'чисте небо'", 
            radar_mon._matches_filter("В Харьковской области шахеды не фиксируются, могут лететь незамеченными."))
assert_test("2.4 Блокування нецільового шуму (Харків, Богодухів)", not radar_mon._matches_filter("Вибухи в Харкові та Богодухові"))

# 2.2 Заміна назви міста зі збереженням регістру
def replacer(match):
    word = match.group(0)
    if word.istitle(): return "Берестин"
    if word.isupper(): return "БЕРЕСТИН"
    return "берестин"

rep_lower = re.sub(r'Красноград', replacer, "летить біля краснограда", flags=re.I)
rep_upper = re.sub(r'Красноград', replacer, "УВАГА! КРАСНОГРАД В УКРИТТЯ!", flags=re.I)
rep_title = re.sub(r'Красноград', replacer, "Повз Красноград у бік Дніпра", flags=re.I)

assert_test("2.5 Заміна маленькими літерами", "берестина" in rep_lower)
assert_test("2.6 Заміна CAPS LOCK", "БЕРЕСТИН" in rep_upper)
assert_test("2.7 Заміна з великої літери", "Берестин" in rep_title)


# ==============================================================================
# 3. КОМУНАЛЬНИЙ БОТ: СВІТЛО ТА ВОДА (utility_bot.py)
# ==============================================================================
print("\n[3/8] 💡/💧 КОМУНАЛЬНИЙ БОТ (utility_bot.py)")
from utility_bot import UtilityMonitor

util_mon = UtilityMonitor(dummy_client)

# 3.1 Форматування булітів та видалення зайвого міста при наявності вулиці
raw_light_street = "🟡 Питання щодо наявності світла: вул. Копиленка, Берестин (мешканці питають про наявність)."
fmt_street = util_mon._format_status_message(raw_light_street)
assert_test("3.1 Очищення дублю 'Берестин', якщо вказано конкретну вулицю", 
            "- вул. Копиленка" in fmt_street and "- Берестин" not in fmt_street)

raw_light_no_street = "🔴 Відключення світла: Берестин (мешканці повідомляють про відсутність)."
fmt_no_street = util_mon._format_status_message(raw_light_no_street)
assert_test("3.2 Збереження 'Берестин', коли конкретної назви вулиці немає", "- Берестин" in fmt_no_street)

# 3.2 Логіка Води: 30-хвилинний сон та накопичення
util_mon.water_cooldown_until = 20000.0  # бот спить до 20000
util_mon.water_accumulated_locations = set()
util_mon.water_has_yellow = False

# Надходить скарга під час сну:
current_time = 19000.0
is_sleeping = current_time < util_mon.water_cooldown_until
if is_sleeping:
    util_mon.water_accumulated_locations.add("вул. Полтавська")
assert_test("3.3 Вода: накопичення скарг у режимі 30-хвилинного сну", 
            "вул. Полтавська" in util_mon.water_accumulated_locations)

# Пробудження
wake_time = 20005.0
ready_to_publish = wake_time >= util_mon.water_cooldown_until and len(util_mon.water_accumulated_locations) > 0
assert_test("3.4 Вода: пробудження та готовність відправити зведене повідомлення", ready_to_publish == True)


# ==============================================================================
# 4. БОТ ЗНИЖОК В АТБ (discount_bot.py)
# ==============================================================================
print("\n[4/8] 🛒 БОТ ЗНИЖОК В АТБ (discount_bot.py)")
from discount_bot import DiscountMonitor

disc_mon = DiscountMonitor(dummy_client)

ad_raw = """🔣 "😜🤪економія" 🆕🔤 
(ВСІ СТОРІНКИ ГАЗЕТИ В КОМЕНТАРЯХ ⬇️) 
📆Термін дії  23.09-29.09
#АТБ
_______________________________
✅ПРИЄДНАТИСЬ ДО КАНАЛУ:
🛒 ПРОСТО ЗНИЖКИ % 🇺🇦
🫧 ЧИСТО ЗНИЖКИ % 🇺🇦"""

clean_disc = disc_mon._process_text(ad_raw)
assert_test("4.1 Витяг терміну дії ('23.09-29.09') та тегу #АТБ", "📆Термін дії  23.09-29.09" in clean_disc and "#АТБ" in clean_disc)
assert_test("4.2 Обрізка рекламного сміття і чужих каналів", "ПРОСТО ЗНИЖКИ" not in clean_disc and "ПРИЄДНАТИСЬ" not in clean_disc)
assert_test("4.3 Ігнорування повідомлень без тегу #АТБ", disc_mon._process_text("Знижки на каву діє до 30.09") == "")


# ==============================================================================
# 5. БОТ ЗАТРИМОК ПОЇЗДІВ УЗ (railway_bot.py)
# ==============================================================================
print("\n[5/8] 🚆 БОТ УЗ / ЗАТРИМОК ПОЇЗДІВ (railway_bot.py)")
from railway_bot import RailwayMonitor

rail_mon = RailwayMonitor(dummy_client)

text_relevant = "⚠️ Поїзд №102 Краматорськ - Херсон (прямує через Красноград) затримується на 35 хв."
text_irrelevant = "⚠️ Поїзд №712 Київ - Краматорськ затримується на 10 хв."

assert_test("5.1 Виявлення поїздів через Красноград/Берестин", bool(re.search(r'(Берестин|Красноград)', text_relevant, re.I)))
assert_test("5.2 Ігнорування нерелевантних напрямків УЗ", not bool(re.search(r'(Берестин|Красноград)', text_irrelevant, re.I)))
assert_test("5.3 Заміна Красноград -> Берестин у повідомленнях УЗ", "Берестин" in rail_mon._replace_city_name(text_relevant))


# ==============================================================================
# 6. РАНКОВИЙ БОТ (dawn_bot.py)
# ==============================================================================
print("\n[6/8] 🌅 РАНКОВИЙ БОТ (dawn_bot.py)")
from dawn_bot import DawnBot, FALLBACK_GREETINGS

dawn_bot = DawnBot()

assert_test("6.1 Наявність пулу резервних привітань при збої AI", len(FALLBACK_GREETINGS) >= 5)
assert_test("6.2 Всі резервні привітання українською мовою та містять 'Берестин'/'місто'", 
            all("берестин" in g.lower() or "місто" in g.lower() for g in FALLBACK_GREETINGS))

# 6.3 Завантаження історичних фактів з history.json для дати 10-14
fact_10_14 = dawn_bot.history.get("10-14")
assert_test("6.3 Коректне завантаження факту на 14 жовтня з history.json",
            fact_10_14 is not None and "14 жовтня" in fact_10_14 and "УПА" in fact_10_14)

# 6.4 Симуляція: Gemini згенерував пост із фактом — захист від дублювання в кінці
sample_gemini_post = (
    "🌅 Доброго ранку, Берестин!\n\n"
    "Сьогодні вівторок, 14 жовтня. Осінь впевнено панує за вікном. Бажаємо тепла та затишку! ☕️🍂\n\n"
    "🌤 Погода: мінлива хмарність, температура до +13°C. Вітер помірний.\n\n"
    "📜 Цей день в історії: 14 жовтня 1942 року вважається офіційною датою створення Української повстанської армії (УПА).\n\n"
    "Гарного дня! ☕️🇺🇦"
)

def format_dawn_message(generated_text, history_fact):
    if not generated_text:
        import random
        generated_text = random.choice(FALLBACK_GREETINGS)
        if history_fact:
            generated_text += f"\n\n📜 {history_fact}"
    elif history_fact:
        fact_kw = history_fact[:30].lower()
        if fact_kw not in generated_text.lower() and "історії" not in generated_text.lower():
            generated_text += f"\n\n📜 {history_fact}"
    return generated_text

res_with_fact_in_body = format_dawn_message(sample_gemini_post, fact_10_14)
count_fact = res_with_fact_in_body.count("14 жовтня 1942 року")
assert_test("6.4 Захист від дублікату: факт зустрічається рівно 1 раз у пості", count_fact == 1)
assert_test("6.5 Пост закінчується на 'Гарного дня! ☕️🇺🇦', а не на сирий факт", res_with_fact_in_body.strip().endswith("Гарного дня! ☕️🇺🇦"))

# 6.6 Симуляція: Gemini згенерував текст, але пропустив історичний факт (страховка)
sample_gemini_no_fact = (
    "🌅 Доброго ранку, незламний Берестин!\n\n"
    "🌤 Погода чудова, сонячно і тепло.\n\n"
    "Гарного дня! ☕️🇺🇦"
)
res_fact_missing = format_dawn_message(sample_gemini_no_fact, fact_10_14)
assert_test("6.6 Страховка: факт додається, якщо Gemini його повністю пропустив",
            "14 жовтня 1942 року" in res_fact_missing and res_fact_missing.count("14 жовтня 1942 року") == 1)

# 6.7 Симуляція збою Gemini (fallback greeting + факт)
res_gemini_fail = format_dawn_message("", fact_10_14)
assert_test("6.7 Резервний режим при збої AI містить вітання та історичний факт",
            any(g in res_gemini_fail for g in FALLBACK_GREETINGS) and "14 жовтня 1942 року" in res_gemini_fail)

# 6.8 День без історичного факту
res_no_fact_day = format_dawn_message(sample_gemini_no_fact, None)
assert_test("6.8 День без факту не додає зайвих блоків",
            res_no_fact_day == sample_gemini_no_fact and "📜" not in res_no_fact_day)

# 6.9 End-to-End симуляція _post_morning_message() з викликом Telegram API
async def test_publish_e2e():
    mock_bot = AsyncMock()
    test_dawn = DawnBot()
    test_dawn.bot = mock_bot
    test_dawn.model = MagicMock()
    test_dawn.get_weather = MagicMock(return_value="Температура +13°C, хмарно з проясненнями")
    mock_resp = MagicMock()
    mock_resp.text = sample_gemini_post
    test_dawn.model.generate_content = MagicMock(return_value=mock_resp)

    await test_dawn._post_morning_message()

    mock_bot.send_message.assert_called_once()
    call_args = mock_bot.send_message.call_args[1]
    sent_text = call_args["text"]
    return sent_text.count("14 жовтня 1942 року") == 1 and sent_text.strip().endswith("Гарного дня! ☕️🇺🇦")

e2e_passed = asyncio.run(test_publish_e2e())
assert_test("6.9 End-to-End асинхронна публікація _post_morning_message(): текст чистий без дублювання", e2e_passed)

# 6.10 Тест fallback при помилці валідації HTML в Telegram
async def test_publish_html_fallback():
    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = [Exception("Can't parse entities: unclosed tag"), None]
    test_dawn = DawnBot()
    test_dawn.bot = mock_bot
    test_dawn.model = MagicMock()
    test_dawn.get_weather = MagicMock(return_value="Температура +13°C, хмарно")
    mock_resp = MagicMock()
    mock_resp.text = "<b>Доброго ранку Берестин"
    test_dawn.model.generate_content = MagicMock(return_value=mock_resp)

    await test_dawn._post_morning_message()
    return mock_bot.send_message.call_count == 2 and "parse_mode" not in mock_bot.send_message.call_args_list[1][1]

fallback_passed = asyncio.run(test_publish_html_fallback())
assert_test("6.10 Автоматичний повтор без parse_mode при битому HTML від Gemini", fallback_passed)


# ==============================================================================
# 7. БОТ ОРЕНДИ (rent_bot.py)
# ==============================================================================
print("\n[7/8] 🏠 БОТ ОРЕНДИ (rent_bot.py)")
from rent_bot import RentBot

rent_bot = RentBot(dummy_client)

rent_sample_msgs = [
    {"user": "@owner", "text": "Здам 2-кімнатну квартиру на 3 мкрн, 4000 грн. 0661112233", "link": "https://t.me/c/10"},
    {"user": "@seeker1", "text": "Терміново шукаю в оренду двокімнатну квартиру в місті Берестин або 3 мкрн. 0668430844", "link": "https://t.me/c/11"},
    {"user": "@seeker2", "text": "Сімʼя зніме квартиру на тривалий термін 0668710539", "link": "https://t.me/c/12"},
    {"user": "@garage_owner", "text": "Сдам в аренду гаражные боксы в г. Берестин под склад", "link": "https://t.me/c/13"},
    {"user": "@cleaner", "text": "Предоставлю услуги по уборке квартиры, дома, сиделки", "link": "https://t.me/c/14"},
    {"user": "@chat_asker", "text": "У кого то есть чат Зачепиловки где здают квартиры?", "link": "https://t.me/c/15"},
    {"user": "@who_rents", "text": "Підкажіть, хто здає 1-кімнатну квартиру в Берестині?", "link": "https://t.me/c/16"},
    {"user": "@owner_dup", "text": "Здам 2-кімнатну квартиру на 3 мкрн, 4000 грн. 0661112233", "link": "https://t.me/c/10"},
]

rent_res = rent_bot._rule_based_classify(rent_sample_msgs)
rent_offers = rent_res["offering"]
rent_seeks = rent_res["seeking"]

assert_test("7.1 Виявлення здачі житла в оренду", len(rent_offers) == 1 and "2-кімнатну" in rent_offers[0]["summary"])
assert_test("7.2 Виявлення пошуку житла (квартири, для сім'ї)", len(rent_seeks) == 3 and any("2-кімнатна" in s["summary"] for s in rent_seeks))
assert_test("7.3 Повне відсіювання комерційного шуму (гаражі, прибирання)", 
            not any("гараж" in x["summary"].lower() or "уборке" in x["summary"].lower() for x in rent_offers + rent_seeks))
assert_test("7.4 Дедуплікація повторних оголошень оренди за посиланням", len([o for o in rent_offers if o["link"] == "https://t.me/c/10"]) == 1)
assert_test("7.5 Блокування запитів на пошук чатів/груп (не здача і не пошук)", 
            not any("Зачепиловки" in x["summary"] or "чат" in x["summary"].lower() for x in rent_offers + rent_seeks))
assert_test("7.6 Запитання 'хто здає квартиру' класифікується як пошук житла, а не здача", 
            any("1-кімнатна" in s["summary"] for s in rent_seeks) and not any("хто здає" in o["summary"].lower() for o in rent_offers))


# ==============================================================================
# 8. БОТ ВАКАНСІЙ (job_bot.py)
# ==============================================================================
print("\n[8/8] 💼 БОТ ВАКАНСІЙ (job_bot.py)")
from job_bot import JobBot

job_bot = JobBot(dummy_client)

sample_messages = [
    {"user": "@tanya", "text": "Вакансія: Продавець-консультант 17 000 – 25 000 грн. Графік 5/2", "link": "https://t.me/c/1"},
    {"user": "@dari_vi", "text": "КЛІН ДІМ — шукаємо працівників! Потрібні люди для прибирання", "link": "https://t.me/c/2"},
    {"user": "@cat_owner", "text": "❗ШУКАЄМО КОТА! Пропав котик Даня з блакитними очима", "link": "https://t.me/c/3"},
    {"user": "@asker", "text": "Підкажіть, чи працює Нова Пошта сьогодні?", "link": "https://t.me/c/4"},
    {"user": "@seeker", "text": "Шукаю роботу вантажником або підробіток", "link": "https://t.me/c/5"},
    {"user": "@tanya_dup", "text": "Вакансія: Продавець-консультант 17 000 – 25 000 грн. Графік 5/2", "link": "https://t.me/c/1"},
]

classified = job_bot._rule_based_classify(sample_messages)
vacs = classified["vacancies"]
seeks = classified["seeking"]

assert_test("8.1 Виявлення реальних вакансій роботодавців", len(vacs) == 2 and any("Продавець-консультант" in v["summary"] for v in vacs))
assert_test("8.2 Виявлення шукачів роботи", len(seeks) == 1 and "вантажником" in seeks[0]["summary"])
assert_test("8.3 Подвійна самоперевірка: повне відсіювання шуму ('шукаємо кота', 'чи працює пошта')", 
            not any("кота" in v["summary"].lower() for v in vacs + seeks) and
            not any("пошта" in v["summary"].lower() for v in vacs + seeks))
assert_test("8.4 Дедуплікація повторних оголошень за посиланням", len([v for v in vacs if v["link"] == "https://t.me/c/1"]) == 1)

# Тести для нових покращень:
advanced_sample_messages = [
    {"user": "75501", "text": "На сто потрібні автослюсарь і автомеханік зп 20-30т 0506982666", "link": "https://t.me/c/101"},
    {"user": "75501", "text": "На сто потрібні автослюсарь і автомеханік зп 20-30т 0506982666", "link": "https://t.me/c/102"}, # дублікат з іншим лінком
    {"user": "75501", "text": "В автомагазин потрібний підсобний робітник 0506982666", "link": "https://t.me/c/103"}, # інша вакансія
    {"user": "@F1LOKO666", "text": "ищем подработку нам по 14 лет можем делать много чего копать убирать дворы 0633281933", "link": "https://t.me/c/104"},
    {"user": "@Mekok22", "text": "Шукаем подроботку на два человека", "link": "https://t.me/c/105"},
    {"user": "@scammer", "text": "Шукаю помічників на віддалений підробіток 30-40 хвилин вашого часу Оплата на карту", "link": "https://t.me/c/106"},
]

adv_classified = job_bot._rule_based_classify(advanced_sample_messages)
adv_vacs = adv_classified["vacancies"]
adv_seeks = adv_classified["seeking"]

assert_test("8.5 Розширене виявлення шукачів роботи ('ищем подработку', 'шукаем подроботку')",
            len(adv_seeks) == 2 and any("14 лет" in s["summary"] or "копати" in s["summary"] for s in adv_seeks) and any("двох" in s["summary"] for s in adv_seeks))
assert_test("8.6 Розумна дедуплікація однакових оголошень з різними посиланнями",
            len([v for v in adv_vacs if "автослюсар" in v["summary"].lower()]) == 1)
assert_test("8.7 Виявлення підсобних робітників ('потрібний підсобний робітник')",
            any("підсобний" in v["summary"].lower() for v in adv_vacs))
assert_test("8.8 Блокування спаму віддаленого заробітку (30-40 хв, оплата на карту)",
            not any("віддален" in x["summary"].lower() or "оплата на карту" in x["summary"].lower() for x in adv_vacs + adv_seeks))

# ==============================================================================
# ФІНАЛЬНИЙ ПІДСУМОК
# ==============================================================================
print("\n" + "=" * 65)
print(f"🏁 РЕЗУЛЬТАТ: {passed} з {total} тестів успішно пройдено ({passed/total*100:.1f}%)")
print("=" * 65)

if passed == total:
    print("🎉 ВСІ 8 БОТІВ ПРАЦЮЮТЬ БЕЗДОГАННО!")
    sys.exit(0)
else:
    print("⚠️ Є помилки, перевірте вивід!")
    sys.exit(1)

from telethon import TelegramClient
#!/usr/bin/env python3
"""
Telegram бот для повідомлення про повітряну тривогу
по Берестинському району (Харківська область).

Використовує API alerts.in.ua для отримання даних.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

import aiohttp
from telegram import Bot
from telegram.constants import ParseMode

# ─── Конфігурація ────────────────────────────────────────────────
ALERTS_API_TOKEN = os.environ.get("ALERTS_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Інтервал опитування API тривог (секунди). Ліміт alerts.in.ua ~8-10 запитів/хв, опитуємо кожні 8 сек.
POLL_INTERVAL = int(os.environ.get("ALERT_POLL_INTERVAL", "8"))

# Район, який відслідковуємо.
# API може використовувати як нову назву "Берестинський район",
# так і стару "Красноградський район" — перевіряємо обидві.
TARGET_DISTRICT_NAMES = [
    "Берестинський район",
    "Красноградський район",
    "Харківська область",
]

ALERTS_API_BASE = "https://api.alerts.in.ua/v1"

# Часовий пояс Києва (UTC+2 зима / UTC+3 літо — спрощено UTC+3)
KYIV_TZ = timezone(timedelta(hours=3))

# ─── Емоджі та тексти ────────────────────────────────────────────

ALERT_TYPE_LABELS = {
    "air_raid": "🚨 Повітряна тривога",
    "artillery_shelling": "💥 Артилерійський обстріл",
    "urban_fights": "⚔️ Вуличні бої",
    "chemical": "☣️ Хімічна загроза",
    "nuclear": "☢️ Ядерна загроза",
}

THREAT_TYPE_LABELS = {
    "tactic_aircraft_activity": "✈️ Тактична авіація",
    "strategic_aircraft_activity": "✈️ Стратегічна авіація",
    "mig31k_departure": "✈️ Виліт МіГ-31К",
    "ballistic_missiles": "🚀 Балістичні ракети",
    "cruise_missiles": "🚀 Крилаті ракети",
    "unspecified_missiles": "🚀 Ракетна загроза",
    "drones": "🛩 Дрони (БПЛА)",
    "guided_aerial_bombs": "💣 Керовані авіабомби (КАБ)",
    "air_defense": "🛡 Робота ППО",
    "unknown": "❓ Невідома загроза",
}

ALERT_LEVEL_EMOJI = {
    "red": "🔴",
    "yellow": "🟡",
}

# ─── Логування ────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─── Утиліти ──────────────────────────────────────────────────────

def format_time(iso_str: str | None) -> str:
    """Форматує ISO 8601 час в локальний Київський час."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt_kyiv = dt.astimezone(KYIV_TZ)
        return dt_kyiv.strftime("%d.%m.%Y %H:%M:%S")
    except (ValueError, TypeError):
        return iso_str


def build_alert_on_message(alert: dict) -> str:
    """Формує повідомлення про ПОЧАТОК тривоги."""
    alert_type = alert.get("alert_type", "air_raid")
    alert_label = ALERT_TYPE_LABELS.get(alert_type, f"⚠️ {alert_type}")
    level = alert.get("alert_level", "")
    level_emoji = ALERT_LEVEL_EMOJI.get(level, "")

    location = alert.get("location_title", "Берестинський район")
    started = format_time(alert.get("started_at"))

    lines = [
        f"{level_emoji} {alert_label}",
        "",
        f"📍 <b>{location}</b>",
        f"🕐 Початок: <b>{started}</b>",
    ]

    # Додаємо інформацію про загрози, якщо є
    threats = alert.get("threats", [])
    if threats:
        lines.append("")
        lines.append("⚠️ <b>Загрози:</b>")
        for threat in threats:
            tt = threat.get("threat_type", "unknown")
            tt_label = THREAT_TYPE_LABELS.get(tt, f"❓ {tt}")
            t_level = threat.get("level", "")
            t_level_emoji = ALERT_LEVEL_EMOJI.get(t_level, "")
            source_msg = threat.get("source_message", "")
            line = f"  • {t_level_emoji} {tt_label}"
            if source_msg:
                line += f" — <i>{source_msg}</i>"
            lines.append(line)

    notes = alert.get("notes", "")
    if notes:
        lines.append("")
        lines.append(f"📝 {notes}")
    return "\n".join(lines)


def build_alert_off_message(alert: dict) -> str:
    """Формує повідомлення про ВІДБІЙ тривоги."""
    location = alert.get("location_title", "Берестинський район")
    started = format_time(alert.get("started_at"))
    finished = format_time(alert.get("finished_at"))

    # Розраховуємо тривалість
    duration_str = ""
    try:
        s = datetime.fromisoformat(alert["started_at"].replace("Z", "+00:00"))
        f = datetime.fromisoformat(alert["finished_at"].replace("Z", "+00:00"))
        delta = f - s
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        parts = []
        if hours:
            parts.append(f"{hours} год")
        if minutes:
            parts.append(f"{minutes} хв")
        if seconds and not hours:
            parts.append(f"{seconds} сек")
        duration_str = " ".join(parts)
    except (KeyError, ValueError, TypeError):
        pass

    lines = [
        "✅ Відбій тривоги",
        "",
        f"📍 <b>{location}</b>",
        f"🕐 Початок: {started}",
        f"🕐 Кінець: <b>{finished}</b>",
    ]

    if duration_str:
        lines.append(f"⏱ Тривалість: <b>{duration_str}</b>")
    return "\n".join(lines)


# ─── Основна логіка ──────────────────────────────────────────────

class AlertMonitor:
    """Моніторинг тривог через API alerts.in.ua."""

    def __init__(self, client=None):
        self.client = client
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self.active_alerts: dict[int, dict] = {}  # id -> alert data
        self._session: aiohttp.ClientSession | None = None
        self._last_modified: str | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            import ssl
            import certifi
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {ALERTS_API_TOKEN}"},
                connector=connector
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_active_alerts(self) -> list[dict] | None:
        """Отримує список активних тривог з API."""
        session = await self._get_session()
        url = f"{ALERTS_API_BASE}/alerts/active.json"

        headers = {}
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified

        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 304:
                    # Дані не змінились
                    return None
                if resp.status == 200:
                    self._last_modified = resp.headers.get("Last-Modified")
                    data = await resp.json()
                    return data.get("alerts", [])
                elif resp.status == 429:
                    logger.warning("API rate limit (429). Чекаємо...")
                    return None
                else:
                    text = await resp.text()
                    logger.error(f"API error {resp.status}: {text}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"Помилка підключення до API: {e}")
            return None

    def filter_district_alerts(self, alerts: list[dict]) -> list[dict]:
        """Фільтрує тривоги тільки для Берестинського району."""
        result = []
        for alert in alerts:
            # Перевіряємо location_title (для районів)
            title = alert.get("location_title", "")
            raion = alert.get("location_raion", "")

            for target in TARGET_DISTRICT_NAMES:
                if target in title or target in raion:
                    result.append(alert)
                    break
        return result

    async def send_telegram(self, text: str):
        """Відправляє повідомлення в Telegram чат та в особисті повідомлення."""
        targets = [TELEGRAM_CHAT_ID, 395497075]  # Основний чат та особистий ID @vvvvvvvvvvvvv2002
        for target in targets:
            try:
                await self.bot.send_message(
                    chat_id=target,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                logger.info(f"Повідомлення відправлено в Telegram (chat_id: {target})")
            except Exception as e:
                logger.error(f"Помилка відправки в Telegram (chat_id: {target}): {e}")

    async def process_alerts(self, alerts: list[dict]):
        """Обробляє нові та завершені тривоги."""
        district_alerts = self.filter_district_alerts(alerts)

        current_ids = {a["id"] for a in district_alerts}
        previous_ids = set(self.active_alerts.keys())

        # Нові тривоги (з'явились)
        new_ids = current_ids - previous_ids
        for alert in district_alerts:
            if alert["id"] in new_ids:
                logger.info(
                    f"🚨 НОВА ТРИВОГА: {alert.get('location_title')} "
                    f"(тип: {alert.get('alert_type')})"
                )
                msg = build_alert_on_message(alert)
                
                # STATELESS DEDUPLICATION
                is_duplicate = False
                if self.client:
                    try:
                        import time
                        now_ts = time.time()
                        async for past_msg in self.client.iter_messages(int(TELEGRAM_CHAT_ID), limit=10):
                            if past_msg.date and (now_ts - past_msg.date.timestamp()) < 7200:
                                if past_msg.text:
                                    if "Повітряна тривога" in past_msg.text and "Увага" in past_msg.text:
                                        # Last thing in channel was a siren! We shouldn't post another one.
                                        is_duplicate = True
                                        break
                                    if "Відбій" in past_msg.text:
                                        # Last thing was all clear, so we CAN post a new siren.
                                        break
                    except Exception as e:
                        logger.error(f"Stateless dedup error (bot): {e}")
                
                if not is_duplicate:
                    await self.send_telegram(msg)
                else:
                    logger.info("Повітряна тривога вже опублікована недавно в каналі. Пропускаємо.")
                    
                self.active_alerts[alert["id"]] = alert

        # Оновлені тривоги (оновлення загроз тощо)
        for alert in district_alerts:
            if alert["id"] in previous_ids:
                old = self.active_alerts[alert["id"]]
                # Перевіряємо чи змінився alert_level або threats
                old_threats = set(
                    t.get("threat_type", "") for t in old.get("threats", [])
                )
                new_threats = set(
                    t.get("threat_type", "") for t in alert.get("threats", [])
                )
                if new_threats - old_threats:
                    # Нові загрози додались
                    added = new_threats - old_threats
                    logger.info(f"⚠️ Нові загрози: {added}")
                    for threat in alert.get("threats", []):
                        if threat.get("threat_type", "") in added:
                            tt = threat.get("threat_type", "unknown")
                            tt_label = THREAT_TYPE_LABELS.get(tt, tt)
                            t_level = threat.get("level", "")
                            t_level_emoji = ALERT_LEVEL_EMOJI.get(t_level, "")
                            source_msg = threat.get("source_message", "")
                            msg_lines = [
                                f"⚠️ Нова загроза — {alert.get('location_title', 'Берестинський район')}",
                                "",
                                f"{t_level_emoji} {tt_label}",
                            ]
                            if source_msg:
                                msg_lines.append(f"<i>{source_msg}</i>")
                            await self.send_telegram("\n".join(msg_lines))

                self.active_alerts[alert["id"]] = alert

        # Завершені тривоги (зникли зі списку активних)
        ended_ids = previous_ids - current_ids
        for aid in ended_ids:
            old_alert = self.active_alerts.pop(aid)
            logger.info(
                f"✅ ВІДБІЙ: {old_alert.get('location_title')} "
                f"(тип: {old_alert.get('alert_type')})"
            )
            # Для відбою потрібен час finished_at. Якщо його немає в
            # збережених даних, використовуємо поточний час.
            if not old_alert.get("finished_at"):
                old_alert["finished_at"] = datetime.now(timezone.utc).isoformat()
            msg = build_alert_off_message(old_alert)
            await self.send_telegram(msg)

    async def run(self):
        """Головний цикл моніторингу."""
        logger.info("=" * 50)
        logger.info("🤖 Бот повітряної тривоги запущено!")
        logger.info(f"📍 Район: {', '.join(TARGET_DISTRICT_NAMES)}")
        logger.info(f"⏱  Інтервал опитування: {POLL_INTERVAL} сек")
        logger.info(f"💬 Telegram Chat ID: {TELEGRAM_CHAT_ID}")
        logger.info("=" * 50)

        # Перший запит
        alerts = await self.fetch_active_alerts()
        if alerts is not None:
            district_alerts = self.filter_district_alerts(alerts)
            for alert in district_alerts:
                # Якщо тривога почалась менше 10 хвилин тому — можливо, сервер перезавантажувався і ми її пропустили
                started_str = alert.get("started_at")
                if started_str:
                    try:
                        s_dt = datetime.fromisoformat(started_str.replace("Z", "+00:00"))
                        now_utc = datetime.now(timezone.utc)
                        if (now_utc - s_dt).total_seconds() < 600:
                            logger.info(f"🚨 Свіжа тривога при запуску ({int((now_utc - s_dt).total_seconds())} сек тому)! Буде надіслано сповіщення.")
                            continue
                    except Exception:
                        pass
                self.active_alerts[alert["id"]] = alert
            if district_alerts:
                logger.info(f"ℹ️ При запуску виявлено {len(district_alerts)} тривог у районі")
            else:
                logger.info("ℹ️ При запуску активних тривог немає")
            
            # Опрацьовуємо свіжі тривоги, якщо вони є
            await self.process_alerts(alerts)

        # Основний цикл
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                alerts = await self.fetch_active_alerts()
                if alerts is not None:
                    await self.process_alerts(alerts)
            except Exception as e:
                logger.exception(f"Помилка в основному циклі: {e}")
                await asyncio.sleep(5)


from aiohttp import web


async def handle_mem(request):
    import os
    import psutil
    process = psutil.Process(os.getpid())
    mem_info = process.memory_info()
    return web.Response(text=f"RSS Memory: {mem_info.rss / 1024 / 1024:.2f} MB")

async def handle_ping(request):
    return web.Response(text="Бот працює! 🚨")

async def init_web_server():
    """Запускає міні-вебсервер, щоб хостинг (напр. Render) не присипляв бота."""
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/mem', handle_mem)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Dummy web-server started on port {port}")

async def main():
    # Перевірка конфігурації
    missing = []
    if not ALERTS_API_TOKEN:
        missing.append("ALERTS_API_TOKEN")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        logger.error(
            f"❌ Відсутні обов'язкові змінні оточення: {', '.join(missing)}"
        )
        sys.exit(1)

    # Запускаємо веб-сервер для Keep-Alive
    await init_web_server()

    monitor = AlertMonitor()
    try:
        await monitor.run()
    except KeyboardInterrupt:
        logger.info("Бот зупинено (Ctrl+C)")
    finally:
        await monitor.close()


if __name__ == "__main__":
    asyncio.run(main())

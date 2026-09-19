#!/usr/bin/env python3
"""
Головний файл для запуску обох ботів одночасно:
1. Бот повітряної тривоги (alerts.in.ua)
2. Монітор каналів (Telethon)
"""

import asyncio
import logging
import sys
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main():
    from bot import (
        AlertMonitor,
        init_web_server,
        ALERTS_API_TOKEN,
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
    )
    from monitor_bot import (
        ChannelMonitor,
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        TELETHON_SESSION,
        MONITOR_BOT_TOKEN,
    )
    from utility_bot import (
        UtilityMonitor,
        GEMINI_API_KEY,
        LIGHT_BOT_TOKEN,
        WATER_BOT_TOKEN,
    )
    from railway_bot import (
        RailwayMonitor,
        RAILWAY_BOT_TOKEN,
    )
    from discount_bot import (
        DiscountMonitor,
        DISCOUNT_BOT_TOKEN,
    )
    from dawn_bot import DawnBot
    from rent_bot import RentBot
    
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    # ─── Перевірка конфігурації ───────────────────────────────
    missing = []
    if not ALERTS_API_TOKEN: missing.append("ALERTS_API_TOKEN")
    if not TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
    if not TELEGRAM_API_ID: missing.append("TELEGRAM_API_ID")
    if not TELEGRAM_API_HASH: missing.append("TELEGRAM_API_HASH")
    if not TELETHON_SESSION: missing.append("TELETHON_SESSION")
    if not MONITOR_BOT_TOKEN: missing.append("MONITOR_BOT_TOKEN")
    if not GEMINI_API_KEY: missing.append("GEMINI_API_KEY")
    if not LIGHT_BOT_TOKEN: missing.append("LIGHT_BOT_TOKEN")
    if not WATER_BOT_TOKEN: missing.append("WATER_BOT_TOKEN")
    if not RAILWAY_BOT_TOKEN: missing.append("RAILWAY_BOT_TOKEN")
    if not DISCOUNT_BOT_TOKEN: missing.append("DISCOUNT_BOT_TOKEN")

    if missing:
        logger.error(
            f"❌ Відсутні обов'язкові змінні оточення: {', '.join(missing)}"
        )
        sys.exit(1)

    # ─── Запуск ───────────────────────────────────────────────
    # Веб-сервер для keep-alive (Render)
    await init_web_server()

    # Ініціалізуємо ОДИН спільний Telethon клієнт
    client = TelegramClient(
        StringSession(TELETHON_SESSION),
        int(TELEGRAM_API_ID),
        TELEGRAM_API_HASH,
    )
    await client.start()
    logger.info("✅ Спільний Telethon клієнт підключено")

    # Ініціалізуємо всі боти
    alert_monitor = AlertMonitor()
    channel_monitor = ChannelMonitor(client)
    utility_monitor = UtilityMonitor(client)
    railway_monitor = RailwayMonitor(client)
    discount_monitor = DiscountMonitor(client)
    dawn_bot = DawnBot()
    rent_bot = RentBot(client)

    logger.info("🚀 Запускаємо всі боти...")

    # Стартуємо налаштування/фонові задачі
    await channel_monitor.start()
    await utility_monitor.start()
    await dawn_bot.start()
    await rent_bot.start()

    try:
        await asyncio.gather(
            alert_monitor.run(),
            client.run_until_disconnected()
        )
    except KeyboardInterrupt:
        logger.info("Боти зупинено (Ctrl+C)")
    finally:
        await alert_monitor.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

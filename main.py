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

    # ─── Перевірка конфігурації ───────────────────────────────
    missing = []
    if not ALERTS_API_TOKEN:
        missing.append("ALERTS_API_TOKEN")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not TELEGRAM_API_ID:
        missing.append("TELEGRAM_API_ID")
    if not TELEGRAM_API_HASH:
        missing.append("TELEGRAM_API_HASH")
    if not TELETHON_SESSION:
        missing.append("TELETHON_SESSION")
    if not MONITOR_BOT_TOKEN:
        missing.append("MONITOR_BOT_TOKEN")

    if missing:
        logger.error(
            f"❌ Відсутні обов'язкові змінні оточення: {', '.join(missing)}"
        )
        sys.exit(1)

    # ─── Запуск ───────────────────────────────────────────────
    # Веб-сервер для keep-alive (Render)
    await init_web_server()

    # Обидва монітори
    alert_monitor = AlertMonitor()
    channel_monitor = ChannelMonitor()

    logger.info("🚀 Запускаємо обидва боти...")

    try:
        await asyncio.gather(
            alert_monitor.run(),
            channel_monitor.run(),
        )
    except KeyboardInterrupt:
        logger.info("Боти зупинено (Ctrl+C)")
    finally:
        await alert_monitor.close()


if __name__ == "__main__":
    asyncio.run(main())

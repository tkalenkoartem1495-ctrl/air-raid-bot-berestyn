#!/usr/bin/env python3
"""
Одноразовий скрипт для генерації Telethon StringSession.
Запустіть локально, введіть номер телефону та код з Telegram.
Отриманий рядок додайте як змінну TELETHON_SESSION на Render.
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 38536328
API_HASH = "401b58f9bbf9706d0f6f44cbe029cf93"


async def main():
    print("=" * 50)
    print("🔑 Генерація Telethon сесії")
    print("=" * 50)
    print()
    print("Зараз вам потрібно буде:")
    print("1. Ввести номер телефону (у форматі +380...)")
    print("2. Ввести код, який прийде в Telegram")
    print("3. Якщо є двофакторна автентифікація — ввести пароль")
    print()

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.start()

    session_string = client.session.save()

    print()
    print("=" * 50)
    print("✅ Сесію успішно створено!")
    print("=" * 50)
    print()
    print("Скопіюйте ВЕСЬ рядок нижче (це ваш TELETHON_SESSION):")
    print()
    print(session_string)
    print()
    print("=" * 50)
    print("Додайте його як змінну TELETHON_SESSION на Render.")
    print("=" * 50)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

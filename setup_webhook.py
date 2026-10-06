"""Bir martalik sozlash: bazada jadvallarni yaratadi va Telegramga Vercel manzilini beradi.

Ishlatish:  .venv/bin/python setup_webhook.py https://<loyiha>.vercel.app
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from telegram import Bot, Update

load_dotenv()

import bot  # noqa: E402  (.env yuklangandan keyin)
import db  # noqa: E402


async def main(base_url: str) -> None:
    db.init()
    print("✅ Bazada jadvallar tayyor")
    async with Bot(os.environ["TELEGRAM_BOT_TOKEN"]) as tg:
        await tg.set_webhook(
            url=base_url.rstrip("/") + "/api/webhook",
            secret_token=os.environ["WEBHOOK_SECRET"],
            allowed_updates=Update.ALL_TYPES,
        )
        await tg.set_my_commands(bot.BOT_COMMANDS)
        info = await tg.get_webhook_info()
        print("✅ Webhook:", info.url)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    asyncio.run(main(sys.argv[1]))

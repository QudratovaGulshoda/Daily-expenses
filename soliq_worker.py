"""soliq.uz yordamchisi — O'zbekistondagi kompyuterda ishlaydi.

soliq.uz chet el serverlarini (shu jumladan Vercel'ni) bloklaydi. Shuning uchun Vercel'dagi bot
chekni bazadagi `soliq_jobs` navbatiga qo'yadi, bu dastur esa uni oladi, soliq.uz sahifasini yashirin
Chromium'da ochadi va natijani bazaga qaytaradi. Bot natijani ~20 soniya kutadi; bu dastur ishlamay
turgan bo'lsa, bot summani chek rasmidan (OCR) o'qiydi.

Ishga tushirish:  .venv/bin/python soliq_worker.py
Kerak:            pip install playwright && python -m playwright install chromium
"""

import asyncio
import logging
import os

import psycopg
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import db  # noqa: E402  (.env yuklangandan keyin)
import soliq  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("soliq-worker")

HEARTBEAT_SECONDS = 20


def _listen_url() -> str:
    # LISTEN uchun doimiy (session) ulanish kerak: Supabase'da u 5432-port, 6543 esa transaction rejimi
    return os.environ["DATABASE_URL"].replace(":6543/", ":5432/")


async def process_jobs() -> None:
    while job := db.claim_soliq_job():
        log.info("Chek #%s: %s", job["id"], job["qr_url"])
        receipt, reason = await soliq.fetch_receipt(job["qr_url"])
        if receipt:
            db.finish_soliq_job(job["id"], "ok", receipt.to_dict())
            log.info("  ✅ %s — %s so'm, %d ta mahsulot", receipt.store, receipt.total, len(receipt.items))
        else:
            db.finish_soliq_job(job["id"], reason, None)
            log.info("  ⚠️ %s", reason)


async def main() -> None:
    db.init()
    db.cleanup_soliq_jobs()
    log.info("soliq.uz yordamchisi ishga tushdi")
    listener = None
    while True:
        try:
            db.soliq_heartbeat()
            await process_jobs()
            if listener is None or listener.closed:
                listener = await psycopg.AsyncConnection.connect(_listen_url(), autocommit=True)
                await listener.execute("LISTEN soliq_jobs")
            # Yangi vazifa haqida xabar kelguncha yoki heartbeat vaqti bo'lguncha kutamiz
            async for _ in listener.notifies(timeout=HEARTBEAT_SECONDS, stop_after=1):
                pass
        except (psycopg.OperationalError, OSError) as e:
            log.warning("Baza bilan aloqa uzildi (%s), 5 soniyadan keyin qayta ulanaman", e)
            listener = None
            await asyncio.sleep(5)
        except Exception:
            log.exception("Kutilmagan xato")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

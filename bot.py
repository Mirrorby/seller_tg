import asyncio
import logging

from telegram import Bot
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from config import Config
from handlers import handle_message, handle_business_message, handle_start, handle_callback, handle_photo
from scheduler import start_scheduler

# ── Логирование ──────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s  %(levelname)s  [%(name)s]  %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)
logging.getLogger("google.auth").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def _wait_for_token_free(token: str, timeout: int = 30) -> None:
    """Ждёт, пока предыдущий инстанс бота освободит токен."""
    bot = Bot(token=token)
    try:
        for attempt in range(1, timeout + 1):
            try:
                await bot.delete_webhook(drop_pending_updates=True)
                await bot.get_updates(offset=-1, timeout=3)
                logger.info("Токен свободен, запускаемся")
                return
            except Conflict:
                logger.warning(f"Токен занят — жду... ({attempt}/{timeout})")
                await asyncio.sleep(1)
        raise RuntimeError("Токен не освободился за 30 секунд — аварийный стоп")
    finally:
        await bot.shutdown()


async def post_init(application: Application) -> None:
    await application.bot.delete_webhook(drop_pending_updates=True)
    start_scheduler(application)


def main() -> None:
    asyncio.get_event_loop().run_until_complete(
        _wait_for_token_free(Config.BOT_TOKEN)
    )

    app = (
        Application.builder()
        .token(Config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.BUSINESS_MESSAGE,
        handle_message,
    ))
    app.add_handler(MessageHandler(
        filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT,
        handle_business_message,
    ))

    logger.info("🤖 Бот запущен и ждёт сообщений")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=[
            "message",
            "callback_query",
            "business_connection",
            "business_message",
            "edited_business_message",
        ],
    )


if __name__ == "__main__":
    main()

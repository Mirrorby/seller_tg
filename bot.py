import asyncio
import logging
import sys
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.error import NetworkError, TimedOut
from config import Config
from handlers import (
    handle_message, handle_business_message, handle_start,
    handle_callback, handle_broadcast,
)
from scheduler import start_scheduler
from userbot import start_userbot, stop_userbot

# ------------------------------------------------------------------ #
# Логирование: INFO -> stdout, WARNING/ERROR -> stderr               #
# (чтобы Railway помечал красным только реальные ошибки)             #
# ------------------------------------------------------------------ #
_formatter = logging.Formatter(
    "%(asctime)s  %(levelname)s  [%(name)s]  %(message)s",
    datefmt="%H:%M:%S",
)

_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setLevel(logging.DEBUG)
_stdout_handler.addFilter(lambda record: record.levelno < logging.WARNING)
_stdout_handler.setFormatter(_formatter)

_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.WARNING)
_stderr_handler.setFormatter(_formatter)

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
_root_logger.addHandler(_stdout_handler)
_root_logger.addHandler(_stderr_handler)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)
logging.getLogger("google.auth").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("telethon.client.updates").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    await application.bot.delete_webhook(drop_pending_updates=True)
    start_scheduler(application)
    logger.info("Scheduler запущен")

    # Userbot для холодных контактов (Telethon, тот же event loop)
    asyncio.create_task(start_userbot())


async def post_shutdown(application: Application) -> None:
    await stop_userbot()


async def error_handler(update, context) -> None:
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(f"Сетевая ошибка (игнорируем): {err}")
        return
    logger.error(f"Необработанная ошибка: {err}", exc_info=err)


def main() -> None:
    app = (
        Application.builder()
        .token(Config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .read_timeout(30)
        .connect_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.BUSINESS_MESSAGE,
        handle_message,
    ))
    app.add_handler(MessageHandler(
        filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT,
        handle_business_message,
    ))
    app.add_handler(CommandHandler("broadcast", handle_broadcast))
    app.add_error_handler(error_handler)
    logger.info("🤖 Бот запущен")
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

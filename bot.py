import logging
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    filters,
)
from config import Config
from handlers import handle_message, handle_business_message, handle_start
from scheduler import start_scheduler

# ── Уровни логирования ───────────────────────────────────────────────
# Убираем мусор от сторонних библиотек — оставляем только наш код
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


async def post_init(application: Application):
    await application.bot.delete_webhook(drop_pending_updates=True)
    start_scheduler(application)


def main():
    app = (
        Application.builder()
        .token(Config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", handle_start))

    # Обычные личные сообщения боту напрямую
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.BUSINESS_MESSAGE,
        handle_message,
    ))

    # Сообщения через Secretary Mode
    app.add_handler(MessageHandler(
        filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT,
        handle_business_message,
    ))

    logger.info("🤖 Бот запущен и ждёт сообщений")

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=[
            "message",
            "business_connection",
            "business_message",
            "edited_business_message",
        ],
    )


if __name__ == "__main__":
    main()

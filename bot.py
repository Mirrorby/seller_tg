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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application: Application):
    start_scheduler(application)
    logger.info("Scheduler started")


def main():
    app = (
        Application.builder()
        .token(Config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # /start
    app.add_handler(CommandHandler("start", handle_start))

    # Обычные личные сообщения боту напрямую
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.BUSINESS_MESSAGE,
        handle_message,
    ))

    # Сообщения через Secretary Mode (business_message)
    app.add_handler(MessageHandler(
        filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT,
        handle_business_message,
    ))

    logger.info("Bot started — listening for direct and business messages")

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

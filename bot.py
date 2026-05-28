import logging
import asyncio
from telegram.ext import Application, MessageHandler, filters, CommandHandler
from config import Config
from handlers import handle_message, handle_start
from scheduler import start_scheduler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def post_init(application):
    start_scheduler(application)
    logger.info("Scheduler started")


def main():
    app = (
        Application.builder()
        .token(Config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

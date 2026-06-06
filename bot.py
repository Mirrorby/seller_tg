import asyncio
import logging

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
from webhook_server import set_bot_context, start_webhook_server

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
logging.getLogger("aiohttp").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    await application.bot.delete_webhook(drop_pending_updates=True)
    start_scheduler(application)
    logger.info("Scheduler запущен")

    # Webhook сервер для Lava
    runner = await start_webhook_server()
    application.bot_data["webhook_runner"] = runner

    # Передаём bot в webhook_server
    class _Ctx:
        def __init__(self, bot):
            self.bot = bot
    set_bot_context(_Ctx(application.bot))


async def post_shutdown(application: Application) -> None:
    runner = application.bot_data.get("webhook_runner")
    if runner:
        await runner.cleanup()
        logger.info("Webhook server остановлен")

    # Закрыть aiohttp-сессии платёжных клиентов
    try:
        from crypto_client import crypto
        from lava_client import lava
        await asyncio.gather(crypto.close(), lava.close(), return_exceptions=True)
    except Exception as e:
        logger.error(f"Ошибка закрытия платёжных клиентов: {e}")


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

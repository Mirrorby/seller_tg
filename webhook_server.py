"""
webhook_server.py — aiohttp webhook endpoint для Lava.top.

Запускается внутри того же процесса что и бот (через asyncio).
Railway должен проксировать входящий HTTPS на PORT.

Настройка на Lava стороне:
  Integrations → Webhooks → Add Webhook
  URL: https://<your-railway-domain>/lava-webhook
  Event: Результат платежа
  Auth: API Key  (значение = LAVA_WEBHOOK_SECRET)

Переменные окружения:
  WEBHOOK_PORT=8080 (или PORT, Railway выставляет сам)
  LAVA_WEBHOOK_SECRET=<ваш секрет>
"""

import asyncio
import logging
from typing import Optional

from aiohttp import web

from config import Config
from lava_client import lava

logger = logging.getLogger(__name__)

# Глобальная ссылка на application context бота (устанавливается из bot.py)
_bot_context: Optional[object] = None


def set_bot_context(ctx):
    """Установить контекст бота для использования внутри webhook-обработчика."""
    global _bot_context
    _bot_context = ctx


async def lava_webhook_handler(request: web.Request) -> web.Response:
    """POST /lava-webhook"""
    body_bytes = await request.read()
    signature = request.headers.get("X-Api-Key", "")

    # ВРЕМЕННО — убрать после диагностики
    logger.info(f"Lava headers: {dict(request.headers)}")
    logger.info(f"Lava body: {body_bytes[:300]}")

    # Верификация подписи
    if not lava.verify_webhook(body_bytes, signature):
        logger.warning("Lava webhook: invalid signature")
        return web.Response(status=403, text="Invalid signature")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    logger.info(f"Lava webhook received: {payload.get('eventType')} / {payload.get('invoiceId')}")

    # Обработка события
    if _bot_context is not None:
        from payment_manager import handle_lava_webhook
        try:
            await handle_lava_webhook(payload, _bot_context)
        except Exception as e:
            logger.error(f"handle_lava_webhook error: {e}")
    else:
        logger.warning("Lava webhook: bot context not set, queuing not implemented")

    # Lava ждёт 200, иначе повторяет
    return web.Response(status=200, text="ok")


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/lava-webhook", lava_webhook_handler)
    app.router.add_get("/health", health_handler)
    return app


async def start_webhook_server():
    """Запустить webhook-сервер в фоне."""
    port = getattr(Config, "WEBHOOK_PORT", 8080)
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Webhook server started on port {port}")
    return runner

"""
payment_manager.py — автоматические платежи: Lava.top и CryptoBot.

Вызывается из handlers.py в блоке tariff: callback.
Возвращает HTML-строку с результатом (ссылка или ошибка).
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram.ext import ContextTypes

from config import Config
from crypto_client import crypto
from lava_client import lava
from sheets_client import sheets

logger = logging.getLogger(__name__)

# Тарифы (синхронизировать с TARIFF_DISPLAY в handlers.py)
TARIFFS = {
    '1m': {'label': '1 месяц',   'days': 30},
    '3m': {'label': '3 месяца',  'days': 90},
    '6m': {'label': '6 месяцев', 'days': 180},
}


# ══════════════════════════════════════════════════════════════════════════════
# Lava.top (карта РФ — рубли)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_lava_payment(
    *,
    username: str,
    email: str,
    tariff_key: str,
    tariff_name: str,
    amount_usd: float,
    amount_rub: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> str:
    """
    Создать Lava-инвойс, вернуть HTML-строку с кнопкой оплаты.
    """
    try:
        payment = await lava.create_payment(
            email=email,
            offer_id=Config.LAVA_OFFER_ID,
            amount_usd=amount_usd,
            currency="RUB",
            payment_method="BANK131",
            custom_fields={
                "utm_source": "telegram_bot",
                "utm_campaign": tariff_key,
                "utm_content": username,
            },
        )
        pay_url    = payment.get("paymentUrl", "")
        invoice_id = payment.get("id", "")

        # Сохраняем invoice_id в CRM для идентификации в webhook
        sheets.upsert_client(username, comment=f"lava:{invoice_id}")

        logger.info(f"Lava invoice for {username}: {invoice_id}")
        return (
            f'💳 <b>Оплата картой РФ</b>\n\n'
            f'Тариф: <b>{tariff_name}</b> — <b>{amount_rub} ₽</b>\n\n'
            f'👉 <a href="{pay_url}">Перейти к оплате</a>\n\n'
            f'После оплаты подписка активируется автоматически.'
        )
    except Exception as e:
        logger.error(f"Lava error for {username}: {e}")
        raise


# ══════════════════════════════════════════════════════════════════════════════
# Crypto Bot (USDT)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_crypto_payment(
    *,
    username: str,
    tariff_key: str,
    tariff_name: str,
    amount_usd: float,
    context: ContextTypes.DEFAULT_TYPE,
) -> str:
    """
    Создать CryptoBot-инвойс, запустить фоновый поллинг, вернуть HTML-строку.
    """
    try:
        inv = await crypto.create_subscription_invoice(
            username=username,
            tariff_label=tariff_name,
            amount_usd=amount_usd,
            asset="USDT",
        )
        pay_url    = inv.get("pay_url", "")
        invoice_id = inv.get("invoice_id")

        # Запускаем фоновый поллинг
        asyncio.create_task(
            _crypto_poll_task(
                invoice_id=invoice_id,
                username=username,
                tariff_key=tariff_key,
                context=context,
            )
        )

        logger.info(f"CryptoBot invoice for {username}: {invoice_id}")
        return (
            f'🪙 <b>Оплата USDT (CryptoBot)</b>\n\n'
            f'Тариф: <b>{tariff_name}</b> — <b>{amount_usd} USDT</b>\n\n'
            f'👉 <a href="{pay_url}">Открыть в @CryptoBot</a>\n\n'
            f'Ссылка действительна 24 часа.\n'
            f'После оплаты подписка активируется автоматически.'
        )
    except Exception as e:
        logger.error(f"CryptoBot error for {username}: {e}")
        raise


# ══════════════════════════════════════════════════════════════════════════════
# Фоновый поллинг крипто-инвойса
# ══════════════════════════════════════════════════════════════════════════════

async def _crypto_poll_task(
    invoice_id: int,
    username: str,
    tariff_key: str,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.info(f"Crypto poll started: {username} invoice={invoice_id}")
    paid_inv = await crypto.poll_until_paid(invoice_id, timeout=86400, interval=30)
    if paid_inv:
        await activate_subscription(username, tariff_key, "crypto_usdt", context)
    else:
        logger.info(f"Crypto invoice {invoice_id} expired for {username}")


# ══════════════════════════════════════════════════════════════════════════════
# Webhook-обработчик Lava (вызывается из webhook_server.py)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_lava_webhook(payload: dict, context):
    event = lava.parse_webhook_event(payload)
    logger.info(f"Lava webhook: {event['event_type']} / {event['invoice_id']}")

    if event["event_type"] not in ("payment.success", "PAYMENT_SUCCESS"):
        return

    invoice_id = event["invoice_id"]
    username   = _find_username_by_lava_invoice(invoice_id)
    if not username:
        logger.warning(f"Lava webhook: клиент не найден для invoice {invoice_id}")
        return

    amount_rub = float(event.get("amount", 0))
    tariff_key = _detect_tariff_by_amount_rub(amount_rub)
    await activate_subscription(username, tariff_key, "card_rub_lava", context)


# ══════════════════════════════════════════════════════════════════════════════
# Активация подписки
# ══════════════════════════════════════════════════════════════════════════════

async def activate_subscription(
    username: str,
    tariff_key: str,
    payment_method: str,
    context,
):
    tariff  = TARIFFS.get(tariff_key, TARIFFS['1m'])
    today   = datetime.now().strftime("%Y-%m-%d")
    expires = (datetime.now() + timedelta(days=tariff['days'])).strftime("%Y-%m-%d")

    sheets.upsert_client(
        username,
        subscribed="Да",
        status="✅ Активен",
        connected_at=today,
        tariff_days=str(tariff['days']),
        expires_at=expires,
        comment=f"Оплата: {payment_method}",
    )
    logger.info(f"Подписка активирована: {username} {tariff['label']} до {expires}")

    # Уведомление owner
    try:
        await context.bot.send_message(
            chat_id=Config.OWNER_CHAT_ID,
            text=(
                f'💰 <b>Новая оплата</b>\n\n'
                f'👤 {username}\n'
                f'📦 Тариф: {tariff["label"]}\n'
                f'💳 Способ: {payment_method}\n'
                f'📅 Активен до: {expires}'
            ),
            parse_mode='HTML',
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить owner об оплате: {e}")

    # Уведомление клиенту
    chat_id = sheets.history_get_client_chat_id(username)
    if chat_id:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f'✅ <b>Оплата подтверждена!</b>\n\n'
                    f'Тариф <b>{tariff["label"]}</b> активирован до {expires}.\n'
                    f'Добро пожаловать в Лид-витрину 🎉'
                ),
                parse_mode='HTML',
            )
        except Exception as e:
            logger.error(f"Не удалось отправить активацию клиенту {username}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ══════════════════════════════════════════════════════════════════════════════

def _find_username_by_lava_invoice(invoice_id: str) -> Optional[str]:
    try:
        search_str = f"lava:{invoice_id}"
        all_rows   = sheets.crm.get_all_values()
        for row in all_rows[Config.CRM_DATA_START_ROW - 1:]:
            comment_col  = 16  # Q=17, 0-indexed=16
            username_col = 1   # B=2,  0-indexed=1
            if len(row) > comment_col and search_str in row[comment_col]:
                return row[username_col]
    except Exception as e:
        logger.error(f"_find_username_by_lava_invoice error: {e}")
    return None


def _detect_tariff_by_amount_rub(amount_rub: float) -> str:
    if amount_rub < 2000:
        return '1m'
    elif amount_rub < 4500:
        return '3m'
    else:
        return '6m'

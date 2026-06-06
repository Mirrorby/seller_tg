"""
payment_manager.py — единая точка управления платежами.

Логика:
- Клиент выбирает способ оплаты: 💳 Карта (Lava) или 🪙 Крипта (CryptoBot)
- Для карты: создаём Lava-инвойс, отправляем ссылку, ждём webhook POST /lava-webhook
- Для крипты: создаём CryptoBot-инвойс, отправляем ссылку, запускаем polling task
- После успешной оплаты: активируем подписку в Sheets + уведомляем owner
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import Config
from crypto_client import crypto
from lava_client import lava
from sheets_client import sheets

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Тарифы                                                               #
# ------------------------------------------------------------------ #

TARIFFS = {
    "trial": {
        "label": "Trial 3 дня",
        "days": 3,
        "price_usd": 0,         # бесплатно
        "price_rub": 0,
        "free": True,
    },
    "month": {
        "label": "1 месяц",
        "days": 30,
        "price_usd": 19.0,
        "price_rub": 1750,      # примерный курс, можно вынести в config
        "free": False,
    },
    "quarter": {
        "label": "3 месяца",
        "days": 90,
        "price_usd": 49.0,
        "price_rub": 4500,
        "free": False,
    },
    "half_year": {
        "label": "6 месяцев",
        "days": 180,
        "price_usd": 89.0,
        "price_rub": 8200,
        "free": False,
    },
}


# ------------------------------------------------------------------ #
# Вспомогательные функции                                              #
# ------------------------------------------------------------------ #

def _expires_date(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


async def _activate_subscription(
    username: str,
    tariff_key: str,
    payment_method: str,   # "card_rub" | "crypto_usdt" | "trial"
    context: ContextTypes.DEFAULT_TYPE,
):
    """Активировать подписку в CRM и уведомить owner."""
    tariff = TARIFFS[tariff_key]
    today = datetime.now().strftime("%Y-%m-%d")
    expires = _expires_date(tariff["days"])

    status = "✅ Активен" if not tariff["free"] else "🔵 Триал"

    sheets.upsert_client(
        username,
        subscribed="Да",
        status=status,
        connected_at=today,
        tariff_days=str(tariff["days"]),
        expires_at=expires,
        comment=f"Оплата: {payment_method}",
    )

    # Уведомление owner
    msg = (
        f"💰 *Новая оплата*\n\n"
        f"👤 {username}\n"
        f"📦 Тариф: {tariff['label']}\n"
        f"💳 Способ: {payment_method}\n"
        f"📅 Активен до: {expires}\n"
    )
    try:
        await context.bot.send_message(
            chat_id=Config.OWNER_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to notify owner on payment: {e}")

    # Уведомление клиенту (если chat_id известен)
    chat_id = sheets.history_get_client_chat_id(username)
    if chat_id:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ Оплата подтверждена!\n\n"
                    f"Тариф *{tariff['label']}* активирован до {expires}.\n"
                    f"Добро пожаловать в Лид-витрину 🎉"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Failed to send activation message to {username}: {e}")


# ------------------------------------------------------------------ #
# Шаг 1: Клавиатура выбора тарифа                                     #
# ------------------------------------------------------------------ #

def build_tariff_keyboard() -> InlineKeyboardMarkup:
    """Inline-клавиатура для выбора тарифа."""
    buttons = []
    for key, t in TARIFFS.items():
        if t["free"]:
            label = f"🎁 {t['label']} — бесплатно"
        else:
            label = f"📦 {t['label']} — ${t['price_usd']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"tariff:{key}")])
    return InlineKeyboardMarkup(buttons)


# ------------------------------------------------------------------ #
# Шаг 2: Клавиатура выбора способа оплаты                             #
# ------------------------------------------------------------------ #

def build_payment_method_keyboard(tariff_key: str) -> InlineKeyboardMarkup:
    tariff = TARIFFS[tariff_key]
    if tariff["free"]:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Активировать бесплатно", callback_data=f"pay:trial:{tariff_key}")]
        ])

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"💳 Картой ({tariff['price_rub']} ₽)",
                callback_data=f"pay:card:{tariff_key}",
            )
        ],
        [
            InlineKeyboardButton(
                f"🪙 Крипта USDT ({tariff['price_usd']}$)",
                callback_data=f"pay:crypto:{tariff_key}",
            )
        ],
        [InlineKeyboardButton("◀ Назад", callback_data="pay:back")],
    ])


# ------------------------------------------------------------------ #
# Шаг 3: Обработка callback-данных оплаты                             #
# ------------------------------------------------------------------ #

async def handle_payment_callback(
    callback_data: str,
    username: str,
    email: str,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> str:
    """
    Разобрать callback_data вида "pay:<method>:<tariff_key>".
    Вернуть текст ответа пользователю.
    """
    parts = callback_data.split(":")
    if len(parts) < 3:
        return "Неверный формат. Попробуйте снова."

    _, method, tariff_key = parts[0], parts[1], parts[2]
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        return "Тариф не найден."

    # --- Бесплатный триал ---
    if method == "trial":
        await _activate_subscription(username, tariff_key, "trial", context)
        return (
            f"🎁 Триал активирован!\n\n"
            f"У вас 3 дня бесплатного доступа к Лид-витрине.\n"
            f"Попробуйте — и вы не захотите отключаться 😊"
        )

    # --- Карта (Lava.top) ---
    if method == "card":
        try:
            payment = await lava.create_payment(
                email=email,
                offer_id=Config.LAVA_OFFER_ID,
                amount_usd=tariff["price_usd"],
                currency="RUB",
                payment_method="BANK131",
                custom_fields={
                    "utm_source": "telegram_bot",
                    "utm_campaign": tariff_key,
                    "utm_content": username,
                },
            )
            pay_url = payment.get("paymentUrl", "")
            invoice_id = payment.get("id", "")

            # Сохранить invoice_id в CRM для проверки webhook
            sheets.upsert_client(username, comment=f"lava:{invoice_id}")

            return (
                f"💳 *Оплата картой*\n\n"
                f"Тариф: {tariff['label']} — {tariff['price_rub']} ₽\n\n"
                f"👉 [Перейти к оплате]({pay_url})\n\n"
                f"После оплаты подписка активируется автоматически."
            )
        except Exception as e:
            logger.error(f"Lava payment error for {username}: {e}")
            return "⚠️ Ошибка при создании платежа. Попробуйте через несколько минут."

    # --- Крипта (CryptoBot) ---
    if method == "crypto":
        try:
            inv = await crypto.create_subscription_invoice(
                username=username,
                tariff_label=tariff["label"],
                amount_usd=tariff["price_usd"],
                asset="USDT",
            )
            pay_url = inv.get("pay_url", "")
            invoice_id = inv.get("invoice_id")

            # Запустить фоновый polling
            asyncio.create_task(
                _crypto_poll_task(
                    invoice_id=invoice_id,
                    username=username,
                    tariff_key=tariff_key,
                    context=context,
                )
            )

            return (
                f"🪙 *Оплата криптой (USDT)*\n\n"
                f"Тариф: {tariff['label']} — {tariff['price_usd']} USDT\n\n"
                f"👉 [Открыть в @CryptoBot]({pay_url})\n\n"
                f"Ссылка действительна 24 часа.\n"
                f"После оплаты подписка активируется автоматически."
            )
        except Exception as e:
            logger.error(f"CryptoBot payment error for {username}: {e}")
            return "⚠️ Ошибка при создании крипто-инвойса. Попробуйте через несколько минут."

    return "Неизвестный способ оплаты."


# ------------------------------------------------------------------ #
# Фоновая задача: ожидание крипто-оплаты                               #
# ------------------------------------------------------------------ #

async def _crypto_poll_task(
    invoice_id: int,
    username: str,
    tariff_key: str,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Фоновый поллинг инвойса CryptoBot.
    При успехе активирует подписку.
    """
    logger.info(f"Starting crypto poll for {username}, invoice={invoice_id}")
    paid_inv = await crypto.poll_until_paid(
        invoice_id,
        timeout=86400,
        interval=30,
    )
    if paid_inv:
        await _activate_subscription(username, tariff_key, "crypto_usdt", context)
        logger.info(f"Crypto payment confirmed for {username}")
    else:
        logger.info(f"Crypto invoice {invoice_id} expired/timed-out for {username}")


# ------------------------------------------------------------------ #
# Webhook-обработчик Lava (вызывается из webhook сервера)              #
# ------------------------------------------------------------------ #

async def handle_lava_webhook(
    payload: dict,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Разобрать webhook Lava и активировать подписку.
    Вызывается из FastAPI/aiohttp webhook endpoint.

    Lava webhook eventType: "payment.success" / "payment.failed" / ...
    """
    event = lava.parse_webhook_event(payload)
    logger.info(f"Lava webhook: event={event['event_type']} invoice={event['invoice_id']}")

    if event["event_type"] not in ("payment.success", "PAYMENT_SUCCESS"):
        return  # игнорируем не-успешные события

    # Найти username по invoice_id в CRM (сохранён в comment как "lava:<id>")
    invoice_id = event["invoice_id"]
    username = _find_username_by_lava_invoice(invoice_id)
    if not username:
        logger.warning(f"Lava webhook: no client found for invoice {invoice_id}")
        return

    # Определить тариф по сумме (примерная логика)
    amount = float(event.get("amount", 0))
    tariff_key = _detect_tariff_by_amount_rub(amount)

    await _activate_subscription(username, tariff_key, "card_rub", context)


def _find_username_by_lava_invoice(invoice_id: str) -> Optional[str]:
    """Найти username в CRM по сохранённому lava invoice_id в колонке comment."""
    try:
        search_str = f"lava:{invoice_id}"
        all_rows = sheets.crm.get_all_values()
        from config import Config as C
        for row in all_rows[C.CRM_DATA_START_ROW - 1:]:
            comment_col = 17 - 1  # Q = 17, 0-indexed
            username_col = 2 - 1   # B = 2, 0-indexed
            if len(row) > comment_col and search_str in row[comment_col]:
                return row[username_col]
    except Exception as e:
        logger.error(f"_find_username_by_lava_invoice error: {e}")
    return None


def _detect_tariff_by_amount_rub(amount_rub: float) -> str:
    """Определить тариф по сумме в рублях (для webhook без явного тарифа)."""
    if amount_rub <= 0:
        return "trial"
    elif amount_rub < 3000:
        return "month"
    elif amount_rub < 6000:
        return "quarter"
    else:
        return "half_year"

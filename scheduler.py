import logging
import urllib.request
import json

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application

from sheets_client import sheets
from config import Config
from handlers import PAYMENT_REQUISITES_TEXT, TARIFF_LABEL, TARIFF_PRICE

logger = logging.getLogger(__name__)


def _trial_closing_message(username: str) -> str:
    """Жёсткий шаблон — без ИИ. Триал закончился сегодня."""
    return (
        f"Привет! Сегодня заканчивается твой бесплатный триал в @lead_vitrina_bot.\n\n"
        f"Если понравилось — продление на {TARIFF_LABEL}: {TARIFF_PRICE}.\n\n"
        f"{PAYMENT_REQUISITES_TEXT}\n\n"
        f"⚠️ Укажи в комментарии к платежу свой Telegram: {username}\n\n"
        f"После перевода подключу подписку вручную в течение нескольких часов."
    )


def _renewal_closing_message(username: str, days_left: int) -> str:
    """Жёсткий шаблон — без ИИ. Платная подписка заканчивается через N дней."""
    return (
        f"Привет! Подписка на @lead_vitrina_bot заканчивается через {days_left} дня.\n\n"
        f"Продление на {TARIFF_LABEL}: {TARIFF_PRICE}.\n\n"
        f"{PAYMENT_REQUISITES_TEXT}\n\n"
        f"⚠️ Укажи в комментарии к платежу свой Telegram: {username}\n\n"
        f"После перевода продлю подписку вручную в течение нескольких часов."
    )


def _send_via_broadcast_bot(token: str, chat_id: int, text: str) -> bool:
    """Отправляет сообщение через бот-рассылки (monitor_tg_2)."""
    try:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"Ошибка отправки через broadcast-бот (chat_id={chat_id}): {e}")
        return False


async def check_expiring(application: Application):
    """
    Ежедневная проверка истекающих триалов и подписок.

    Триал (tariff_days<=3) истекает СЕГОДНЯ:
      → broadcast-бот пишет клиенту с оффером 1499₽
      → Уведомление владельцу

    Платная подписка (tariff_days>3) истекает через 3 дня:
      → broadcast-бот пишет клиенту про продление за 1499₽
      → Уведомление владельцу
    """
    bot = application.bot

    # Читаем токен бота-рассылки из таблицы
    broadcast_token = sheets.get_broadcast_token()
    if not broadcast_token:
        logger.error("Токен broadcast-бота не найден в Настройки!B2 — рассылка отменена")
        return

    # Триалы истекают сегодня
    expiring_today = sheets.get_expiring_clients(0)

    # Платные подписки истекают через 3 дня
    expiring_soon = sheets.get_expiring_clients(Config.SUBSCRIPTION_WARN_DAYS)

    # ── Триалы: истекают сегодня ────────────────────────────────────────
    for client in expiring_today:
        tariff      = client.get("tariff_days", "")
        status      = client.get("status", "")
        username    = client["username"]
        chat_id_str = client.get("chat_id", "")
        expires_at  = client.get("expires_at", "")

        try:
            tariff_int = int(tariff)
        except (ValueError, TypeError):
            tariff_int = 0

        is_trial = (tariff_int <= 3 or "Триал" in status)
        if not is_trial:
            continue  # платные подписки обрабатываются ниже

        if chat_id_str:
            try:
                chat_id    = int(chat_id_str)
                client_msg = _trial_closing_message(username)
                sent = _send_via_broadcast_bot(broadcast_token, chat_id, client_msg)
                if sent:
                    sheets.history_append_message(chat_id, "🤖", client_msg)
                    logger.info(f"Триал closing отправлен → {username} ({chat_id})")
                else:
                    logger.warning(
                        f"broadcast-бот не смог отправить сообщение {username} ({chat_id})"
                    )
            except Exception as e:
                logger.error(f"Ошибка отправки trial closing для {username}: {e}")
        else:
            logger.warning(f"Нет chat_id для {username} — отправка невозможна")

        try:
            await bot.send_message(
                chat_id=Config.OWNER_CHAT_ID,
                text=(
                    f"⏰ <b>Триал истекает сегодня</b>\n\n"
                    f"👤 {username}\n"
                    f"📅 Дата: {expires_at}\n\n"
                    + (
                        "✅ Клиенту отправлено сообщение с оффером 1499₽"
                        if chat_id_str else
                        "⚠️ chat_id не найден — напишите клиенту вручную"
                    )
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить владельца о триале {username}: {e}")

    # ── Платные подписки: истекают через 3 дня ─────────────────────────
    for client in expiring_soon:
        tariff      = client.get("tariff_days", "")
        status      = client.get("status", "")
        username    = client["username"]
        chat_id_str = client.get("chat_id", "")
        expires_at  = client.get("expires_at", "")

        try:
            tariff_int = int(tariff)
        except (ValueError, TypeError):
            tariff_int = 0

        if tariff_int <= 3 or "Триал" in status:
            continue  # триалы уже обработаны выше

        if chat_id_str:
            try:
                chat_id    = int(chat_id_str)
                client_msg = _renewal_closing_message(username, days_left=3)
                sent = _send_via_broadcast_bot(broadcast_token, chat_id, client_msg)
                if sent:
                    sheets.history_append_message(chat_id, "🤖", client_msg)
                    logger.info(f"Renewal closing отправлен → {username} ({chat_id})")
                else:
                    logger.warning(
                        f"broadcast-бот не смог отправить сообщение {username} ({chat_id})"
                    )
            except Exception as e:
                logger.error(f"Ошибка отправки renewal closing для {username}: {e}")
        else:
            logger.warning(f"Нет chat_id для {username} — отправка невозможна")

        try:
            await bot.send_message(
                chat_id=Config.OWNER_CHAT_ID,
                text=(
                    f"💳 <b>Подписка истекает через 3 дня</b>\n\n"
                    f"👤 {username}\n"
                    f"📅 Тариф: {tariff} дней\n"
                    f"📅 Истекает: {expires_at}\n\n"
                    + (
                        "✅ Клиенту отправлено напоминание о продлении"
                        if chat_id_str else
                        "⚠️ chat_id не найден — напишите клиенту вручную"
                    )
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить владельца о подписке {username}: {e}")


def start_scheduler(application: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_expiring,
        trigger=CronTrigger(hour=Config.SCHEDULER_HOUR, minute=0),
        args=[application],
        id="check_expiring",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler running — daily checks at {Config.SCHEDULER_HOUR}:00 UTC")

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application
from sheets_client import sheets
from gemini_client import gemini
from config import Config

logger = logging.getLogger(__name__)


async def check_expiring(application: Application):
    """
    Ежедневная проверка истекающих триалов и подписок.
    
    Триал (tariff_days=3) истекает завтра:
      → Gemini пишет клиенту с оффером 19$ (скидка до конца триала)
      → Уведомление владельцу
    
    Платная подписка (tariff_days>3) истекает через 3 дня:
      → Gemini пишет клиенту про продление за 24$
      → Уведомление владельцу
    """
    bot = application.bot

    # ── 1. Триалы, истекающие завтра ─────────────────────────────────
    trial_expiring = sheets.get_expiring_clients(Config.TRIAL_WARN_DAYS)

    for client in trial_expiring:
        tariff = client.get("tariff_days", "")
        status = client.get("status", "")
        username = client["username"]
        chat_id_str = client.get("chat_id", "")

        is_trial = (tariff == "3" or "Триал" in status)
        if not is_trial:
            continue

        # 1a. Написать клиенту через Gemini
        if chat_id_str:
            try:
                chat_id = int(chat_id_str)
                closing_msg = await gemini.generate_trial_closing(username)

                await bot.send_message(chat_id=chat_id, text=closing_msg)
                logger.info(f"Trial closing sent to {username} ({chat_id})")

                # Сохранить в историю
                sheets.history_append_message(chat_id, "🤖", closing_msg)
            except Exception as e:
                logger.error(f"Failed to send trial closing to {username}: {e}")
        else:
            logger.warning(f"No chat_id for {username}, cannot send trial closing")

        # 1b. Уведомить владельца
        try:
            owner_msg = (
                f"⏰ *Триал истекает завтра*\n\n"
                f"👤 {username}\n"
                f"📅 Дата: {client['expires_at']}\n\n"
                f"✅ Боту уже отправлено сообщение с оффером 19$"
                if chat_id_str else
                f"⏰ *Триал истекает завтра*\n\n"
                f"👤 {username}\n"
                f"📅 Дата: {client['expires_at']}\n\n"
                f"⚠️ chat\\_id не найден — напишите клиенту вручную"
            )
            await bot.send_message(
                chat_id=Config.OWNER_CHAT_ID,
                text=owner_msg,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Failed to notify owner about trial {username}: {e}")

    # ── 2. Платные подписки, истекающие через N дней ──────────────────
    sub_expiring = sheets.get_expiring_clients(Config.SUBSCRIPTION_WARN_DAYS)

    for client in sub_expiring:
        tariff = client.get("tariff_days", "")
        username = client["username"]
        chat_id_str = client.get("chat_id", "")

        try:
            tariff_int = int(tariff)
        except (ValueError, TypeError):
            continue

        if tariff_int <= 3:
            continue  # триалы уже обработаны выше

        days_left = Config.SUBSCRIPTION_WARN_DAYS

        # 2a. Написать клиенту через Gemini
        if chat_id_str:
            try:
                chat_id = int(chat_id_str)
                renewal_msg = await gemini.generate_renewal_closing(username, days_left)

                await bot.send_message(chat_id=chat_id, text=renewal_msg)
                logger.info(f"Renewal closing sent to {username} ({chat_id})")

                sheets.history_append_message(chat_id, "🤖", renewal_msg)
            except Exception as e:
                logger.error(f"Failed to send renewal closing to {username}: {e}")
        else:
            logger.warning(f"No chat_id for {username}, cannot send renewal closing")

        # 2b. Уведомить владельца
        try:
            owner_msg = (
                f"💳 *Подписка истекает через {days_left} дня*\n\n"
                f"👤 {username}\n"
                f"📅 Тариф: {tariff} дней\n"
                f"📅 Истекает: {client['expires_at']}\n\n"
                f"✅ Боту уже отправлено напоминание о продлении"
                if chat_id_str else
                f"💳 *Подписка истекает через {days_left} дня*\n\n"
                f"👤 {username}\n"
                f"📅 Тариф: {tariff} дней\n"
                f"📅 Истекает: {client['expires_at']}\n\n"
                f"⚠️ chat\\_id не найден — напишите клиенту вручную"
            )
            await bot.send_message(
                chat_id=Config.OWNER_CHAT_ID,
                text=owner_msg,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Failed to notify owner about renewal {username}: {e}")


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

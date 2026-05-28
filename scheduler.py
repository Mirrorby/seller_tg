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
    Ежедневная проверка: пишет клиентам перед концом триала и подписки.
    """
    bot = application.bot
    logger.info("🕐 Ежедневная проверка истекающих триалов и подписок...")

    # ── 1. Триалы, истекающие завтра ─────────────────────────────────

    trial_expiring = sheets.get_expiring_clients(Config.TRIAL_WARN_DAYS)
    trials_found   = 0

    for client in trial_expiring:
        tariff   = client.get("tariff_days", "")
        status   = client.get("status", "")
        username = client["username"]
        chat_id_str = client.get("chat_id", "")

        is_trial = (tariff == "3" or "Триал" in status)
        if not is_trial:
            continue

        trials_found += 1
        logger.info(f"⏰ Триал заканчивается завтра у {username}")

        if chat_id_str:
            try:
                chat_id     = int(chat_id_str)
                closing_msg = await gemini.generate_trial_closing(username)
                await bot.send_message(chat_id=chat_id, text=closing_msg)
                sheets.history_append_message(chat_id, "🤖", closing_msg)
                logger.info(f"✅ Дожим отправлен {username} — предложена цена 19$")
            except Exception as e:
                logger.error(f"Не удалось отправить дожим {username}: {e}")
        else:
            logger.warning(f"⚠️  Нет chat_id для {username} — напиши вручную")

        try:
            owner_msg = (
                f"⏰ *Триал истекает завтра*\n\n"
                f"👤 {username}\n"
                f"📅 Дата: {client['expires_at']}\n\n"
                + (
                    f"✅ Боту отправлено сообщение с оффером 19$"
                    if chat_id_str else
                    f"⚠️ chat\\_id не найден — напишите вручную"
                )
            )
            await bot.send_message(
                chat_id=Config.OWNER_CHAT_ID,
                text=owner_msg,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить владельца о триале {username}: {e}")

    if trials_found == 0:
        logger.info("Триалов, истекающих завтра, не найдено")

    # ── 2. Платные подписки, истекающие через N дней ──────────────────

    sub_expiring = sheets.get_expiring_clients(Config.SUBSCRIPTION_WARN_DAYS)
    subs_found   = 0

    for client in sub_expiring:
        tariff   = client.get("tariff_days", "")
        username = client["username"]
        chat_id_str = client.get("chat_id", "")

        try:
            tariff_int = int(tariff)
        except (ValueError, TypeError):
            continue

        if tariff_int <= 3:
            continue  # триалы уже обработаны

        subs_found += 1
        days_left  = Config.SUBSCRIPTION_WARN_DAYS
        logger.info(f"💳 Подписка истекает через {days_left} дн. у {username}")

        if chat_id_str:
            try:
                chat_id     = int(chat_id_str)
                renewal_msg = await gemini.generate_renewal_closing(username, days_left)
                await bot.send_message(chat_id=chat_id, text=renewal_msg)
                sheets.history_append_message(chat_id, "🤖", renewal_msg)
                logger.info(f"✅ Напоминание о продлении отправлено {username}")
            except Exception as e:
                logger.error(f"Не удалось отправить напоминание {username}: {e}")
        else:
            logger.warning(f"⚠️  Нет chat_id для {username} — напиши вручную")

        try:
            owner_msg = (
                f"💳 *Подписка истекает через {days_left} дня*\n\n"
                f"👤 {username}\n"
                f"📅 Тариф: {tariff} дней\n"
                f"📅 Истекает: {client['expires_at']}\n\n"
                + (
                    f"✅ Боту отправлено напоминание о продлении"
                    if chat_id_str else
                    f"⚠️ chat\\_id не найден — напишите вручную"
                )
            )
            await bot.send_message(
                chat_id=Config.OWNER_CHAT_ID,
                text=owner_msg,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить владельца о продлении {username}: {e}")

    if subs_found == 0:
        logger.info("Платных подписок, истекающих скоро, не найдено")

    logger.info("✔️  Ежедневная проверка завершена")


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
    logger.info(f"⏱  Планировщик запущен — проверка каждый день в {Config.SCHEDULER_HOUR}:00 UTC")

exit code 0

import asyncio
import logging
import random

from telegram import Update
from telegram.ext import ContextTypes

from gemini_client import gemini
from sheets_client import sheets
from config import Config

logger = logging.getLogger(__name__)

_registered:    set[int] = set()
_offer_marked:  set[int] = set()
_trial_marked:  set[int] = set()


def _get_username(user) -> str:
    return f"@{user.username}" if user.username else str(user.id)


def _typing_delay(text: str) -> float:
    base    = random.uniform(4.0, 8.0)
    per_chr = len(text) * 0.03
    return min(base + per_chr, 18.0)


# ------------------------------------------------------------------ #
# /start                                                               #
# ------------------------------------------------------------------ #

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user     = update.effective_user
    username = _get_username(user)

    if user.id not in _registered:
        _registered.add(user.id)
        sheets.upsert_client(username, chat_id=str(user.id), name=user.full_name or "")
        sheets.history_ensure_client(username, user.id)
        logger.info(f"Новый контакт написал /start: {username}")
    else:
        logger.info(f"{username} снова нажал /start")


# ------------------------------------------------------------------ #
# Обычные сообщения боту напрямую                                      #
# ------------------------------------------------------------------ #

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user     = update.effective_user
    user_id  = user.id
    username = _get_username(user)
    text     = update.message.text

    if user_id == Config.OWNER_CHAT_ID:
        return

    is_new = user_id not in _registered
    if is_new:
        _registered.add(user_id)
        sheets.upsert_client(
            username,
            chat_id=str(user_id),
            name=user.full_name or "",
            sales_account="Никита",
            dialog="Да",
        )
        sheets.history_ensure_client(username, user_id)
        logger.info(f"━━━ Новый риэлтор написал напрямую: {username} ━━━")
    else:
        logger.info(f"💬 {username} пишет: {text[:80]}")

    sheets.history_append_message(user_id, "👤", text)

    try:
        reply, needs_takeover, trial_link_sent = await gemini.chat(user_id, text)
    except Exception as e:
        logger.error(f"Gemini не ответил для {username}: {e}")
        return

    delay = _typing_delay(reply)
    logger.info(f"⏳ Имитируем печать {delay:.1f} сек перед ответом {username}...")
    await asyncio.sleep(delay)

    await update.message.reply_text(reply)
    logger.info(f"✅ Ответ отправлен {username}: {reply[:80]}")

    sheets.history_append_message(user_id, "🤖", reply)
    _update_crm_stage(user_id, username, reply, trial_link_sent)

    if needs_takeover:
        logger.info(f"🔔 {username} спрашивает цену — отправляем уведомление владельцу")
        await _notify_owner_takeover(context, username, text, reply)


# ------------------------------------------------------------------ #
# Сообщения через Secretary Mode                                       #
# ------------------------------------------------------------------ #

async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.business_message
    if not msg or not msg.text:
        return

    user = msg.from_user
    if user is None:
        return

    user_id  = user.id
    username = _get_username(user)
    text     = msg.text

    if user_id == Config.OWNER_CHAT_ID:
        return

    business_connection_id = msg.business_connection_id
    if not business_connection_id:
        logger.warning(f"Нет business_connection_id для {username}, пропускаем")
        return

    is_new = user_id not in _registered
    if is_new:
        _registered.add(user_id)
        sheets.upsert_client(
            username,
            chat_id=str(user_id),
            name=user.full_name or "",
            sales_account="Никита",
            dialog="Да",
        )
        sheets.history_ensure_client(username, user_id)
        logger.info(f"━━━ Новый риэлтор написал через чат: {username} ━━━")
    else:
        logger.info(f"💬 {username} пишет: {text[:80]}")

    sheets.history_append_message(user_id, "👤", text)

    try:
        reply, needs_takeover, trial_link_sent = await gemini.chat(user_id, text)
    except Exception as e:
        logger.error(f"Gemini не ответил для {username}: {e}")
        return

    delay = _typing_delay(reply)
    logger.info(f"⏳ Имитируем печать {delay:.1f} сек перед ответом {username}...")

    try:
        await context.bot.send_chat_action(
            chat_id=msg.chat.id,
            action="typing",
            business_connection_id=business_connection_id,
        )
    except Exception:
        pass

    await asyncio.sleep(delay)

    try:
        await context.bot.send_message(
            chat_id=msg.chat.id,
            text=reply,
            business_connection_id=business_connection_id,
        )
    except Exception as e:
        logger.error(f"Не удалось отправить ответ {username}: {e}")
        return

    logger.info(f"✅ Ответ отправлен {username}: {reply[:80]}")

    sheets.history_append_message(user_id, "🤖", reply)
    _update_crm_stage(user_id, username, reply, trial_link_sent)

    if needs_takeover:
        logger.info(f"🔔 {username} спрашивает цену — отправляем уведомление владельцу")
        await _notify_owner_takeover(context, username, text, reply)


# ------------------------------------------------------------------ #
# Обновление этапов CRM                                                #
# ------------------------------------------------------------------ #

def _update_crm_stage(user_id: int, username: str, reply: str, trial_link_sent: bool):
    reply_lower = reply.lower()

    if user_id not in _offer_marked:
        if "лид-витрин" in reply_lower or "триал" in reply_lower or "бесплатн" in reply_lower:
            _offer_marked.add(user_id)
            sheets.upsert_client(username, offer="Да")
            logger.info(f"📋 CRM: {username} — предложение сделано")

    if user_id not in _trial_marked and trial_link_sent:
        _trial_marked.add(user_id)
        sheets.mark_trial_started(username)
        logger.info(f"🎯 {username} согласился на триал — ссылка отправлена, CRM обновлён")


# ------------------------------------------------------------------ #
# Уведомление владельца о горячем лиде                                 #
# ------------------------------------------------------------------ #

async def _notify_owner_takeover(
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
    user_message: str,
    bot_reply: str,
):
    msg = (
        f"🔔 *Требуется ваш ответ*\n\n"
        f"👤 {username}\n"
        f"💬 Написал: _{user_message[:200]}_\n\n"
        f"🤖 Бот ответил: _{bot_reply[:200]}_\n\n"
        f"➡️ Клиент спрашивает о цене или деталях — подключайтесь!"
    )
    try:
        await context.bot.send_message(
            chat_id=Config.OWNER_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление владельцу: {e}")

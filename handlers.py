import asyncio
import logging
import random

from broadcaster import run_broadcast

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from gemini_client import gemini
from sheets_client import sheets
from config import Config
from payment_manager import (
    TARIFFS,
    handle_lava_payment,
    handle_crypto_payment,
)

logger = logging.getLogger(__name__)

# Тарифы с ценами в рублях и долларах
TARIFF_DISPLAY = {
    '1m': ('1 месяц',   '$19',  '1 349 ₽', 19.0,  1349),
    '3m': ('3 месяца',  '$49',  '3 499 ₽', 49.0,  3499),
    '6m': ('6 месяцев', '$89',  '6 299 ₽', 89.0,  6299),
}

# ══════════════════════════════════════════════════════════════════════════════
# Состояния пользователей в памяти
# ══════════════════════════════════════════════════════════════════════════════

_user_state:   dict[int, str] = {}
_registered:   set[int] = set()
_offer_marked: set[int] = set()
_trial_marked: set[int] = set()

# Дебаунс
DEBOUNCE_SECONDS = 4
MAX_WAIT_SECONDS = 10

_pending_messages:   dict[int, list[str]] = {}
_debounce_tasks:     dict[int, asyncio.Task] = {}
_first_message_time: dict[int, float] = {}


def _get_username(user) -> str:
    return f"@{user.username}" if user.username else str(user.id)


def _typing_delay(text: str) -> float:
    base    = random.uniform(4.0, 8.0)
    per_chr = len(text) * 0.03
    return min(base + per_chr, 18.0)


def _make_email(username: str) -> str:
    """Синтетический email для Lava API."""
    clean = username.lstrip("@").replace(" ", "_")
    return f"{clean}@{Config.DEFAULT_EMAIL_DOMAIN}"


# ══════════════════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════════════════

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

    await _notify_owner(context, username, user.id, '▶️ Открыл бота (/start)')

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('❓ Задать вопрос',       callback_data='intent:question')],
        [InlineKeyboardButton('💬 Поделиться мнением',  callback_data='intent:feedback')],
        [InlineKeyboardButton('💳 Оплатить подписку',   callback_data='intent:pay')],
    ])

    await update.message.reply_text(
        '👋 Добро пожаловать в бот поддержки сервиса Лид-витрина!\n\nЧем могу помочь?',
        reply_markup=keyboard,
    )


async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает рассылку в группы. Только для владельца."""
    user_id = update.effective_user.id

    if user_id != Config.OWNER_CHAT_ID:
        await update.message.reply_text("⛔ Команда доступна только владельцу.")
        return

    async def send_progress(text: str):
        try:
            await context.bot.send_message(chat_id=Config.OWNER_CHAT_ID, text=text)
        except Exception as e:
            logger.error(f"Ошибка отправки прогресса: {e}")

    asyncio.create_task(run_broadcast(progress_callback=send_progress))


# ══════════════════════════════════════════════════════════════════════════════
# Обычные сообщения боту напрямую
# ══════════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user     = update.effective_user
    user_id  = user.id
    username = _get_username(user)
    text     = update.message.text

    if user_id == Config.OWNER_CHAT_ID:
        return

    state = _user_state.get(user_id)

    if state == 'waiting_question':
        _user_state.pop(user_id, None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('↩️ Главное меню', callback_data='menu:main')],
        ])
        await update.message.reply_text(
            '🙏 Спасибо за ваш вопрос!\n\n'
            'Менеджер обязательно свяжется с вами в ближайшее время.',
            reply_markup=keyboard,
        )
        await _notify_owner(context, username, user_id, f'❓ Вопрос:\n{text}')
        return

    if state == 'waiting_feedback':
        _user_state.pop(user_id, None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('↩️ Главное меню', callback_data='menu:main')],
        ])
        await update.message.reply_text(
            '🙏 Спасибо за ваш отзыв!\n\n'
            'Мы очень ценим обратную связь. '
            'Менеджер обязательно свяжется с вами в ближайшее время.',
            reply_markup=keyboard,
        )
        await _notify_owner(context, username, user_id, f'💬 Отзыв/мнение:\n{text}')
        return

    # Ждём скриншот
    if state and state.startswith('waiting_screenshot'):
        await update.message.reply_text(
            '📸 Пожалуйста, пришлите именно скриншот (изображение), а не текст.'
        )
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

    now = asyncio.get_event_loop().time()
    if user_id not in _pending_messages:
        _pending_messages[user_id] = []
        _first_message_time[user_id] = now
    _pending_messages[user_id].append(text)
    sheets.history_append_message(user_id, "👤", text)

    if user_id in _debounce_tasks and not _debounce_tasks[user_id].done():
        _debounce_tasks[user_id].cancel()

    elapsed = now - _first_message_time.get(user_id, now)
    wait = 0.3 if elapsed >= MAX_WAIT_SECONDS else DEBOUNCE_SECONDS

    async def _flush_direct():
        await asyncio.sleep(wait)
        messages = _pending_messages.pop(user_id, [])
        _first_message_time.pop(user_id, None)
        _debounce_tasks.pop(user_id, None)
        if not messages:
            return
        combined = "\n".join(messages)
        try:
            reply, needs_takeover, trial_link_sent = await gemini.chat(user_id, combined)
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
            await _notify_owner_takeover(context, username, combined, reply)

    _debounce_tasks[user_id] = asyncio.create_task(_flush_direct())


# ══════════════════════════════════════════════════════════════════════════════
# Secretary Mode (business_message)
# ══════════════════════════════════════════════════════════════════════════════

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

    now = asyncio.get_event_loop().time()
    if user_id not in _pending_messages:
        _pending_messages[user_id] = []
        _first_message_time[user_id] = now
    _pending_messages[user_id].append(text)
    sheets.history_append_message(user_id, "👤", text)

    if user_id in _debounce_tasks and not _debounce_tasks[user_id].done():
        _debounce_tasks[user_id].cancel()

    elapsed = now - _first_message_time.get(user_id, now)
    wait = 0.3 if elapsed >= MAX_WAIT_SECONDS else DEBOUNCE_SECONDS

    async def _flush_business():
        await asyncio.sleep(wait)
        messages = _pending_messages.pop(user_id, [])
        _first_message_time.pop(user_id, None)
        _debounce_tasks.pop(user_id, None)
        if not messages:
            return
        combined = "\n".join(messages)
        try:
            reply, needs_takeover, trial_link_sent = await gemini.chat(user_id, combined)
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
        logger.info(f"✅ Ответ отправлен {username}: {reply[:80]}")
        sheets.history_append_message(user_id, "🤖", reply)
        _update_crm_stage(user_id, username, reply, trial_link_sent)
        if needs_takeover:
            logger.info(f"🔔 {username} спрашивает цену — отправляем уведомление владельцу")
            await _notify_owner_takeover(context, username, combined, reply)

    _debounce_tasks[user_id] = asyncio.create_task(_flush_business())


# ══════════════════════════════════════════════════════════════════════════════
# Входящие фото (скриншот ручной оплаты)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user     = update.effective_user
    user_id  = user.id
    username = _get_username(user)

    if user_id == Config.OWNER_CHAT_ID:
        return

    state = _user_state.get(user_id, '')

    # Ждём скриншот
    if state.startswith('waiting_screenshot'):
        _user_state.pop(user_id, None)
        await update.message.reply_text(
            '✅ Скриншот получен!\n\n'
            'Менеджер проверит оплату и откроет вам доступ '
            'в течение нескольких часов. Если возникнут вопросы — '
            'с вами свяжутся.'
        )
        try:
            await context.bot.forward_message(
                chat_id=Config.OWNER_CHAT_ID,
                from_chat_id=update.message.chat_id,
                message_id=update.message.message_id,
            )
            await context.bot.send_message(
                chat_id=Config.OWNER_CHAT_ID,
                text=(
                    f'📸 Скриншот оплаты\n'
                    f'👤 {username} (id: <code>{user_id}</code>)\n'
                    f'Способ: {state.replace("waiting_screenshot:", "")}'
                ),
                parse_mode='HTML',
            )
        except Exception as e:
            logger.error(f'Ошибка пересылки скриншота: {e}')
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('💳 Оплатить подписку', callback_data='intent:pay')],
        ])
        await update.message.reply_text(
            'Получил фото. Если хотите оплатить подписку — воспользуйтесь кнопкой ниже.',
            reply_markup=keyboard,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Callback-кнопки
# ══════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user     = update.effective_user
    user_id  = user.id
    username = _get_username(user)
    data     = query.data

    # ── Намерение: задать вопрос ─────────────────────────────────────────────
    if data == 'intent:question':
        _user_state[user_id] = 'waiting_question'
        await query.edit_message_text(
            '✍️ Напишите ваш вопрос — я передам его менеджеру.',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('↩️ Главное меню', callback_data='menu:main')],
            ]),
        )
        await _notify_owner(context, username, user_id, '❓ Нажал «Задать вопрос»')
        return

    # ── Намерение: отзыв ─────────────────────────────────────────────────────
    if data == 'intent:feedback':
        _user_state[user_id] = 'waiting_feedback'
        await query.edit_message_text(
            '✍️ Напишите ваше мнение или отзыв — нам важна любая обратная связь.',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('↩️ Главное меню', callback_data='menu:main')],
            ]),
        )
        await _notify_owner(context, username, user_id, '💬 Нажал «Поделиться мнением»')
        return

    # ── Намерение: оплатить ──────────────────────────────────────────────────
    if data == 'intent:pay':
        await query.edit_message_text(
            '💳 <b>Выберите способ оплаты:</b>',
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('💳 Оплата картой',  callback_data='paymethod:card_lava')],
                [InlineKeyboardButton('🪙 Оплата криптой', callback_data='paymethod:crypto')],
                [InlineKeyboardButton('↩️ Главное меню',   callback_data='menu:main')],
            ]),
        )
        await _notify_owner(context, username, user_id, '💳 Нажал «Оплатить подписку»')
        return

    # ── Выбор способа оплаты → показ тарифов ────────────────────────────────
    if data.startswith('paymethod:'):
        method = data.split(':')[1]
        _user_state[user_id] = f'paymethod_chosen:{method}'

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('1 месяц — $19',             callback_data=f'tariff:{method}:1m')],
            [InlineKeyboardButton('3 месяца — $49',            callback_data=f'tariff:{method}:3m')],
            [InlineKeyboardButton('6 месяцев — $89',           callback_data=f'tariff:{method}:6m')],
            [InlineKeyboardButton('↩️ Назад',                   callback_data='intent:pay')],
        ])

        await query.edit_message_text(
            '📋 <b>Выберите тариф:</b>',
            parse_mode='HTML',
            reply_markup=keyboard,
        )
        await _notify_owner(context, username, user_id, f'💳 Выбрал способ: {method}')
        return

    # ── Выбор тарифа ─────────────────────────────────────────────────────────
    if data.startswith('tariff:'):
        parts  = data.split(':')
        method = parts[1]
        tariff_key = parts[2]
        tariff_name, price_usd, price_rub, amount_usd, amount_rub = TARIFF_DISPLAY[tariff_key]

        await _notify_owner(
            context, username, user_id,
            f'💰 Выбрал тариф: {tariff_name} | способ: {method}'
        )

        # ── Создаём инвойс ────────────────────────────────────────────────────
        await query.edit_message_text('⏳ Создаю ссылку на оплату...')

        try:
            if method == 'crypto':
                result_text = await handle_crypto_payment(
                    username=username,
                    tariff_key=tariff_key,
                    tariff_name=tariff_name,
                    amount_usd=amount_usd,
                    context=context,
                )
            elif method == 'card_lava':
                result_text = await handle_lava_payment(
                    username=username,
                    email=_make_email(username),
                    tariff_key=tariff_key,
                    tariff_name=tariff_name,
                    amount_usd=amount_usd,
                    amount_rub=amount_rub,
                    context=context,
                )
            else:
                result_text = '⚠️ Неизвестный способ оплаты.'
        except Exception as e:
            logger.error(f"Ошибка создания инвойса для {username}: {e}")
            result_text = '⚠️ Ошибка при создании платежа. Попробуйте через несколько минут.'

        await query.edit_message_text(
            result_text,
            parse_mode='HTML',
            disable_web_page_preview=False,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('↩️ Главное меню', callback_data='menu:main')],
            ]),
        )

        sheets.upsert_client(username, offer="Да")
        return

    # ── Главное меню ─────────────────────────────────────────────────────────
    if data == 'menu:main':
        _user_state.pop(user_id, None)
        await query.edit_message_text(
            '👋 Чем могу помочь?',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('❓ Задать вопрос',      callback_data='intent:question')],
                [InlineKeyboardButton('💬 Поделиться мнением', callback_data='intent:feedback')],
                [InlineKeyboardButton('💳 Оплатить подписку',  callback_data='intent:pay')],
            ]),
        )
        return


# ══════════════════════════════════════════════════════════════════════════════
# CRM этапы воронки
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
# Уведомления владельца
# ══════════════════════════════════════════════════════════════════════════════

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


async def _notify_owner(
    context: ContextTypes.DEFAULT_TYPE,
    username: str,
    user_id: int,
    action: str,
):
    try:
        await context.bot.send_message(
            chat_id=Config.OWNER_CHAT_ID,
            text=(
                f'👤 {username} (id: <code>{user_id}</code>)\n'
                f'{action}'
            ),
            parse_mode='HTML',
        )
    except Exception as e:
        logger.error(f'Ошибка уведомления владельца: {e}')

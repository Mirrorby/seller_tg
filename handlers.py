import asyncio
import logging
import random

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from gemini_client import gemini
from sheets_client import sheets
from config import Config

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Реквизиты для оплаты
# ══════════════════════════════════════════════════════════════════════════════

PAYMENT_DETAILS = {
    'crypto': (
        '💎 <b>Оплата USDT (TRC-20)</b>\n\n'
        'Адрес кошелька:\n'
        '<code>TDCSKh4MjamhHLKQkuN5d4vuhXcM4R2SBz</code>\n\n'
        'Сеть: TRC-20 (Tron)\n\n'
        '📸 После оплаты пришлите скриншот транзакции — '
        'мы проверим и откроем доступ в течение нескольких часов.'
    ),
    'card_ru': (
        '💳 <b>Перевод на карту РФ</b>\n\n'
        'Номер телефона (СБП / Тинькофф):\n'
        '<code>+79183895663</code>\n\n'
        'Получатель: Никита К.\n\n'
        '📸 После оплаты пришлите скриншот перевода — '
        'мы проверим и откроем доступ в течение нескольких часов.'
    ),
    # 'card_foreign': (
    #     '🌍 <b>Перевод на зарубежную карту</b>\n\n'
    #     'IBAN / номер карты:\n'
    #     '<code>ЗАГЛУШКА_РЕКВИЗИТЫ_ЗАРУБЕЖНОЙ_КАРТЫ</code>\n\n'
    #     'Банк: ЗАГЛУШКА_БАНК\n'
    #     'Получатель: ЗАГЛУШКА_ИМЯ\n\n'
    #     '📸 После оплаты пришлите скриншот перевода — '
    #     'мы проверим и откроем доступ в течение нескольких часов.'
    # ),
}

TARIFFS = {
    '1m': ('1 месяц',   '$19', '1 349 ₽'),
    '3m': ('3 месяца',  '$48', '3 399 ₽'),
    '6m': ('6 месяцев', '$69', '4 899 ₽'),
}

# ══════════════════════════════════════════════════════════════════════════════
# Состояния пользователей в памяти
# ══════════════════════════════════════════════════════════════════════════════

_user_state:   dict[int, str] = {}
_registered:   set[int] = set()
_offer_marked: set[int] = set()
_trial_marked: set[int] = set()


def _get_username(user) -> str:
    return f"@{user.username}" if user.username else str(user.id)


def _typing_delay(text: str) -> float:
    base    = random.uniform(4.0, 8.0)
    per_chr = len(text) * 0.03
    return min(base + per_chr, 18.0)


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
        '👋 Добро пожаловать!\n\nЧем могу помочь?',
        reply_markup=keyboard,
    )


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

    # Человек прислал вопрос
    if state == 'waiting_question':
        _user_state.pop(user_id, None)
        await update.message.reply_text(
            '🙏 Спасибо за ваш вопрос!\n\n'
            'Менеджер обязательно свяжется с вами в ближайшее время.'
        )
        await _notify_owner(context, username, user_id, f'❓ Вопрос:\n{text}')
        return

    # Человек поделился мнением
    if state == 'waiting_feedback':
        _user_state.pop(user_id, None)
        await update.message.reply_text(
            '🙏 Спасибо за ваш отзыв!\n\n'
            'Мы очень ценим обратную связь. '
            'Менеджер обязательно свяжется с вами в ближайшее время.'
        )
        await _notify_owner(context, username, user_id, f'💬 Отзыв/мнение:\n{text}')
        return

    # Человек прислал текст вместо скриншота
    if state and state.startswith('waiting_screenshot'):
        await update.message.reply_text(
            '📸 Пожалуйста, пришлите именно скриншот (изображение), а не текст.'
        )
        return

    # Новый контакт или любое другое сообщение — регистрируем и отвечаем через Gemini
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


# ══════════════════════════════════════════════════════════════════════════════
# Сообщения через Secretary Mode (business_message)
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

    logger.info(f"✅ Ответ отправлен {username}: {reply[:80]}")

    sheets.history_append_message(user_id, "🤖", reply)
    _update_crm_stage(user_id, username, reply, trial_link_sent)

    if needs_takeover:
        logger.info(f"🔔 {username} спрашивает цену — отправляем уведомление владельцу")
        await _notify_owner_takeover(context, username, text, reply)


# ══════════════════════════════════════════════════════════════════════════════
# Входящие фото (скриншот оплаты)
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

    if state.startswith('waiting_screenshot'):
        _user_state.pop(user_id, None)
        await update.message.reply_text(
            '✅ Скриншот получен!\n\n'
            'Менеджер проверит оплату и откроет вам доступ '
            'в течение нескольких часов. Если возникнут вопросы — '
            'с вами свяжутся.'
        )
        try:
            caption = (
                f'📸 Скриншот оплаты\n'
                f'👤 {username} (id: {user_id})\n'
                f'Способ: {state.replace("waiting_screenshot:", "")}'
            )
            await context.bot.forward_message(
                chat_id=Config.OWNER_CHAT_ID,
                from_chat_id=update.message.chat_id,
                message_id=update.message.message_id,
            )
            await context.bot.send_message(
                chat_id=Config.OWNER_CHAT_ID,
                text=caption,
            )
        except Exception as e:
            logger.error(f'Ошибка пересылки скриншота: {e}')
    else:
        await update.message.reply_text(
            'Получил фото. Если хотите оплатить подписку — '
            'воспользуйтесь кнопкой ниже.'
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('💳 Оплатить подписку', callback_data='intent:pay')],
        ])
        await update.message.reply_text('👇', reply_markup=keyboard)


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

    if data == 'intent:question':
        _user_state[user_id] = 'waiting_question'
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('↩️ Главное меню', callback_data='menu:main')],
        ])
        await query.edit_message_text(
            '✍️ Напишите ваш вопрос — я передам его менеджеру.',
            reply_markup=keyboard,
        )
        await _notify_owner(context, username, user_id, '❓ Нажал «Задать вопрос»')
        return

    if data == 'intent:feedback':
        _user_state[user_id] = 'waiting_feedback'
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('↩️ Главное меню', callback_data='menu:main')],
        ])
        await query.edit_message_text(
            '✍️ Напишите ваше мнение или отзыв — нам важна любая обратная связь.',
            reply_markup=keyboard,
        )
        await _notify_owner(context, username, user_id, '💬 Нажал «Поделиться мнением»')
        return

    if data == 'intent:pay':
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('💎 USDT (крипта)',       callback_data='paymethod:crypto')],
            [InlineKeyboardButton('💳 Карта РФ (перевод)',  callback_data='paymethod:card_ru')],
            # [InlineKeyboardButton('🌍 Карта зарубежного банка', callback_data='paymethod:card_foreign')],
            [InlineKeyboardButton('↩️ Главное меню',        callback_data='menu:main')],
        ])
        await query.edit_message_text(
            '💳 <b>Выберите способ оплаты:</b>',
            parse_mode='HTML',
            reply_markup=keyboard,
        )
        await _notify_owner(context, username, user_id, '💳 Нажал «Оплатить подписку»')
        return

    if data.startswith('paymethod:'):
        method = data.split(':')[1]
        _user_state[user_id] = f'paymethod_chosen:{method}'

        if method == 'card_ru':
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton('1 месяц — 1 349 ₽',                          callback_data=f'tariff:{method}:1m')],
                [InlineKeyboardButton('3 месяца — 3 399 ₽ (1 133 ₽/мес вместо 1 349 ₽)', callback_data=f'tariff:{method}:3m')],
                [InlineKeyboardButton('6 месяцев — 4 899 ₽ (817 ₽/мес вместо 1 349 ₽)',  callback_data=f'tariff:{method}:6m')],
                [InlineKeyboardButton('↩️ Назад', callback_data='intent:pay')],
            ])
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton('1 месяц — $19',                    callback_data=f'tariff:{method}:1m')],
                [InlineKeyboardButton('3 месяца — $48 ($16/мес вместо $19)',   callback_data=f'tariff:{method}:3m')],
                [InlineKeyboardButton('6 месяцев — $69 ($11.5/мес вместо $19)', callback_data=f'tariff:{method}:6m')],
                [InlineKeyboardButton('↩️ Назад', callback_data='intent:pay')],
            ])

        await query.edit_message_text(
            '📋 <b>Выберите тариф:</b>',
            parse_mode='HTML',
            reply_markup=keyboard,
        )
        await _notify_owner(context, username, user_id, f'💳 Выбрал способ оплаты: {method}')
        return

    if data.startswith('tariff:'):
        _, method, tariff_key = data.split(':')
        tariff_name, price_usd, price_rub = TARIFFS[tariff_key]
        price        = price_rub if method == 'card_ru' else price_usd
        payment_text = PAYMENT_DETAILS.get(method, '⚠️ Реквизиты не заданы')

        _user_state[user_id] = f'waiting_screenshot:{method}_{tariff_key}'

        await query.edit_message_text(
            f'✅ Отлично! Вы выбрали:\n'
            f'📋 Тариф: <b>{tariff_name}</b> — <b>{price}</b>\n\n'
            f'{payment_text}',
            parse_mode='HTML',
        )
        await _notify_owner(
            context, username, user_id,
            f'💰 Выбрал тариф: {tariff_name} {price} | способ: {method}'
        )
        return
        
    if data == 'menu:main':
        _user_state.pop(user_id, None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('❓ Задать вопрос',      callback_data='intent:question')],
            [InlineKeyboardButton('💬 Поделиться мнением', callback_data='intent:feedback')],
            [InlineKeyboardButton('💳 Оплатить подписку',  callback_data='intent:pay')],
        ])
        await query.edit_message_text(
            '👋 Чем могу помочь?',
            reply_markup=keyboard,
        )
        return

# ══════════════════════════════════════════════════════════════════════════════
# Обновление этапов CRM
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
# Уведомление владельца о горячем лиде
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


# ══════════════════════════════════════════════════════════════════════════════
# Уведомление владельца (общее)
# ══════════════════════════════════════════════════════════════════════════════

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

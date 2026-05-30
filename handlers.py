import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from sheets_client import sheets
from config import Config

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Реквизиты для оплаты — ЗАГЛУШКИ, замени на свои данные
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
    '1m':    ('1 месяц',    '$19',      '1 349 ₽'),
    '3m':    ('3 месяца',   '$48',      '3 399 ₽'),
    '6m':    ('6 месяцев',  '$69',      '4 899 ₽'),
}

# ══════════════════════════════════════════════════════════════════════════════
# Состояния пользователей в памяти
# ══════════════════════════════════════════════════════════════════════════════

# user_id -> 'waiting_question' | 'waiting_feedback' | 'waiting_screenshot'
#             | 'pay_method_chosen:<method>'
_user_state: dict[int, str] = {}

_registered: set[int] = set()


def _get_username(update: Update) -> str:
    user = update.effective_user
    return f"@{user.username}" if user.username else str(user.id)


# ══════════════════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════════════════

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = _get_username(update)

    if user.id not in _registered:
        _registered.add(user.id)
        sheets.upsert_client(username, chat_id=str(user.id), name=user.full_name or "")
        logger.info(f"/start from {username}")

    # Уведомить владельца
    await _notify_owner(
        context,
        username,
        user.id,
        '▶️ Открыл бота (/start)',
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('❓ Задать вопрос', callback_data='intent:question')],
        [InlineKeyboardButton('💬 Поделиться мнением', callback_data='intent:feedback')],
        [InlineKeyboardButton('💳 Оплатить подписку', callback_data='intent:pay')],
    ])

    await update.message.reply_text(
        '👋 Добро пожаловать!\n\n'
        'Чем могу помочь?',
        reply_markup=keyboard,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Входящие текстовые сообщения
# ══════════════════════════════════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    user_id = user.id
    username = _get_username(update)
    text = update.message.text

    # Сообщения от владельца игнорируем
    if user_id == Config.OWNER_CHAT_ID:
        return

    # Регистрация нового контакта
    if user_id not in _registered:
        _registered.add(user_id)
        sheets.upsert_client(username, chat_id=str(user_id), name=user.full_name or "")

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

    # Человек прислал скриншот оплаты текстом (маловероятно, но на случай)
    if state and state.startswith('waiting_screenshot'):
        await update.message.reply_text(
            '📸 Пожалуйста, пришлите именно скриншот (изображение), а не текст.'
        )
        return

    # Любое другое сообщение — показать главное меню
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('❓ Задать вопрос', callback_data='intent:question')],
        [InlineKeyboardButton('💬 Поделиться мнением', callback_data='intent:feedback')],
        [InlineKeyboardButton('💳 Оплатить подписку', callback_data='intent:pay')],
    ])
    await update.message.reply_text(
        'Выберите, чем могу помочь:',
        reply_markup=keyboard,
    )
    await _notify_owner(context, username, user_id, f'💬 Написал вне сценария:\n{text}')


# ══════════════════════════════════════════════════════════════════════════════
# Входящие фото (скриншот оплаты)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    user_id = user.id
    username = _get_username(update)

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
        # Переслать скриншот владельцу
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

    user = update.effective_user
    user_id = user.id
    username = _get_username(update)
    data = query.data

    # ── Выбор намерения ────────────────────────────────────────────────────

    if data == 'intent:question':
        _user_state[user_id] = 'waiting_question'
        await query.edit_message_text(
            '✍️ Напишите ваш вопрос — я передам его менеджеру.'
        )
        await _notify_owner(context, username, user_id, '❓ Нажал «Задать вопрос»')
        return

    if data == 'intent:feedback':
        _user_state[user_id] = 'waiting_feedback'
        await query.edit_message_text(
            '✍️ Напишите ваше мнение или отзыв — нам важна любая обратная связь.'
        )
        await _notify_owner(context, username, user_id, '💬 Нажал «Поделиться мнением»')
        return

    if data == 'intent:pay':
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton('💎 USDT (крипта)', callback_data='paymethod:crypto')],
            [InlineKeyboardButton('💳 Карта РФ (перевод)', callback_data='paymethod:card_ru')],
            #[InlineKeyboardButton('🌍 Карта зарубежного банка', callback_data='paymethod:card_foreign')],
        ])
        await query.edit_message_text(
            '💳 <b>Выберите способ оплаты:</b>',
            parse_mode='HTML',
            reply_markup=keyboard,
        )
        await _notify_owner(context, username, user_id, '💳 Нажал «Оплатить подписку»')
        return

    # ── Выбор способа оплаты ───────────────────────────────────────────────

    if data.startswith('paymethod:'):
        method = data.split(':')[1]
        _user_state[user_id] = f'paymethod_chosen:{method}'

        if method == 'card_ru':
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    '1 месяц — 1 349 ₽',
                    callback_data=f'tariff:{method}:1m'
                )],
                [InlineKeyboardButton(
                    '3 месяца — 3 399 ₽ (1 133 ₽/мес вместо 1 349 ₽)',
                    callback_data=f'tariff:{method}:3m'
                )],
                [InlineKeyboardButton(
                    '6 месяцев — 4 899 ₽ (817 ₽/мес вместо 1 349 ₽)',
                    callback_data=f'tariff:{method}:6m'
                )],
            ])
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    '1 месяц — $19',
                    callback_data=f'tariff:{method}:1m'
                )],
                [InlineKeyboardButton(
                    '3 месяца — $48 ($16/мес вместо $19)',
                    callback_data=f'tariff:{method}:3m'
                )],
                [InlineKeyboardButton(
                    '6 месяцев — $69 ($11.5/мес вместо $19)',
                    callback_data=f'tariff:{method}:6m'
                )],
            ])
        await query.edit_message_text(
            '📋 <b>Выберите тариф:</b>',
            parse_mode='HTML',
            reply_markup=keyboard,
        )
        await _notify_owner(
            context, username, user_id,
            f'💳 Выбрал способ оплаты: {method}'
        )
        return

    # ── Выбор тарифа → показать реквизиты ─────────────────────────────────

    if data.startswith('tariff:'):
        _, method, tariff_key = data.split(':')
        tariff_name, price_usd, price_rub = TARIFFS[tariff_key]
        price = price_rub if method == 'card_ru' else price_usd
        payment_text = PAYMENT_DETAILS.get(method, '⚠️ Реквизиты не заданы')

        _user_state[user_id] = f'waiting_screenshot:{method}_{tariff_key}'

        full_text = (
            f'✅ Отлично! Вы выбрали:\n'
            f'📋 Тариф: <b>{tariff_name}</b> — <b>{price}</b>\n\n'
            f'{payment_text}'
        )
        await query.edit_message_text(full_text, parse_mode='HTML')
        await _notify_owner(
            context, username, user_id,
            f'💰 Выбрал тариф: {tariff_name} {price} | способ: {method}'
        )
        return


# ══════════════════════════════════════════════════════════════════════════════
# Уведомление владельца
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

"""
userbot.py — Telethon userbot для работы с холодными контактами.

Запускается внутри event loop основного бота (из main.py post_init).
Пишет первым холодным контактам по шаблону, ведёт простой двухшаговый
диалог без ИИ, затем замолкает.

Состояния диалога (хранятся в колонке G «Результат» листа «Риэлторы»):
  пусто                → ещё не писали
  sent_opener          → opener отправлен, ждём ответа
  awaiting_clarification → лид ответил нейтрально, переспросили
  pitched              → питч отправлен, бот молчит

После pitched бот больше не отвечает — дальше владелец сам.
"""

import asyncio
import logging
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    UserPrivacyRestrictedError,
    InputUserDeactivatedError,
    PeerIdInvalidError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
)

from config import Config
from cold_templates import (
    OPENER, CLARIFY,
    classify_reply, get_pitch,
    STATE_SENT_OPENER, STATE_AWAITING_CLARIFY, STATE_PITCHED,
)
from sheets_client import sheets

logger = logging.getLogger(__name__)

# ── Константы темпа ────────────────────────────────────────────────
import os
DAILY_LIMIT   = int(os.getenv("COLD_DAILY_LIMIT", "15"))
INTERVAL_MIN  = 25 * 60
INTERVAL_MAX  = 40 * 60
CHECK_INTERVAL = 5 * 60

# ── Рабочее окно (UTC+4, Грузия) ──────────────────────────────────
TBILISI_TZ     = ZoneInfo("Asia/Tbilisi")
WORK_HOUR_START = 11
WORK_HOUR_END   = 18


def _is_within_working_hours() -> bool:
    now = datetime.now(TBILISI_TZ)
    return WORK_HOUR_START <= now.hour < WORK_HOUR_END


def _seconds_until_working_hours() -> int:
    from datetime import timedelta
    now    = datetime.now(TBILISI_TZ)
    target = now.replace(hour=WORK_HOUR_START, minute=0, second=0, microsecond=0)
    if now.hour >= WORK_HOUR_END:
        target += timedelta(days=1)
    elif now.hour >= WORK_HOUR_START:
        return 0
    return int((target - now).total_seconds())


# ── Счётчик дня ───────────────────────────────────────────────────
_sent_today    = 0
_last_reset_day = None


def _reset_daily_counter_if_needed():
    global _sent_today, _last_reset_day
    from datetime import date
    today = date.today()
    if _last_reset_day != today:
        _sent_today = 0
        _last_reset_day = today


# ── Вспомогательные функции ────────────────────────────────────────

async def _resolve_entity(client: TelegramClient, user_id, username: str):
    if username:
        uname = username.lstrip("@")
        try:
            return await client.get_entity(uname)
        except (UsernameNotOccupiedError, UsernameInvalidError):
            logger.warning(f"Username не существует/невалиден: @{uname}")
        except Exception as e:
            logger.warning(f"Не удалось резолвить @{uname}: {e}")
    try:
        return await client.get_entity(user_id)
    except Exception as e:
        logger.warning(f"Не удалось резолвить user_id={user_id}: {e}")
        return None


async def _has_existing_dialog(client: TelegramClient, entity) -> bool:
    try:
        async for _ in client.iter_messages(entity, limit=1):
            return True
        return False
    except Exception as e:
        logger.warning(f"Не удалось проверить историю с {entity}: {e}")
        return False


# ── Отправка opener'а ─────────────────────────────────────────────

async def send_cold_outreach(client: TelegramClient, user_id: int, username: str, name: str):
    global _sent_today

    _reset_daily_counter_if_needed()
    if _sent_today >= DAILY_LIMIT:
        logger.info("Дневной лимит достигнут, ждём завтра")
        return False

    entity = await _resolve_entity(client, user_id, username)
    if entity is None:
        logger.warning(f"Не удалось резолвить контакт: {username or user_id}")
        sheets.mark_cold_failed(user_id, "could_not_resolve_entity")
        return False

    if await _has_existing_dialog(client, entity):
        logger.info(f"Уже есть диалог с {username or user_id} — пропускаем")
        sheets.mark_cold_skipped(user_id, "already_in_dialog")
        return False

    try:
        await client.send_message(entity, OPENER)
        _sent_today += 1
        logger.info(f"✉️  Opener отправлен → {username or user_id} [{_sent_today}/{DAILY_LIMIT}]")

        # Сохраняем состояние: отправлен opener
        sheets.mark_cold_sent(user_id, STATE_SENT_OPENER)

        delay = random.randint(INTERVAL_MIN, INTERVAL_MAX)
        logger.info(f"⏳ Следующая отправка через {delay // 60} мин")
        await asyncio.sleep(delay)
        return True

    except FloodWaitError as e:
        logger.warning(f"FloodWait {e.seconds} сек — ждём")
        await asyncio.sleep(e.seconds + 60)
        return False
    except UserPrivacyRestrictedError:
        logger.warning(f"Приватность закрыта: {username or user_id}")
        sheets.mark_cold_failed(user_id, "privacy_restricted")
        return False
    except InputUserDeactivatedError:
        logger.warning(f"Аккаунт удалён: {username or user_id}")
        sheets.mark_cold_failed(user_id, "deactivated")
        return False
    except PeerIdInvalidError:
        logger.warning(f"Невалидный peer: {username or user_id}")
        sheets.mark_cold_failed(user_id, "invalid_peer")
        return False
    except Exception as e:
        logger.error(f"Ошибка отправки {username or user_id}: {e}")
        sheets.mark_cold_failed(user_id, str(e)[:50])
        return False


# ── Основной цикл рассылки ─────────────────────────────────────────

async def outreach_loop(client: TelegramClient):
    logger.info(
        f"🚀 Outreach loop запущен (рабочее окно {WORK_HOUR_START}:00–{WORK_HOUR_END}:00 по Тбилиси)"
    )
    while True:
        try:
            _reset_daily_counter_if_needed()

            if not _is_within_working_hours():
                wait_sec = _seconds_until_working_hours()
                now      = datetime.now(TBILISI_TZ).strftime("%H:%M")
                sleep_sec = max(min(wait_sec, CHECK_INTERVAL), 30)
                logger.info(f"⏰ {now} (Тбилиси) — вне рабочего окна, спим {sleep_sec} сек")
                await asyncio.sleep(sleep_sec)
                continue

            if _sent_today < DAILY_LIMIT:
                contact = sheets.get_next_cold_contact()
                if contact:
                    user_id  = int(contact["user_id"])
                    username = contact.get("username", "")
                    name     = contact.get("name", "")
                    await send_cold_outreach(client, user_id, username, name)
                else:
                    logger.info("Очередь пуста, ждём новых контактов")
                    await asyncio.sleep(CHECK_INTERVAL)
            else:
                logger.info(f"Лимит {DAILY_LIMIT}/день достигнут")
                await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            logger.error(f"Ошибка в outreach_loop: {e}")
            await asyncio.sleep(60)


# ── Обработчик входящих ────────────────────────────────────────────

def register_cold_reply_handler(client: TelegramClient):

    @client.on(events.NewMessage(incoming=True))
    async def handle_incoming(event):
        if not event.is_private:
            return

        sender = await event.get_sender()
        if not sender:
            return

        user_id  = sender.id
        username = f"@{sender.username}" if sender.username else str(user_id)
        text     = event.raw_text.strip()

        # Проверяем что это наш холодный контакт
        status = sheets.get_cold_contact_status(user_id)
        if status is None:
            return  # не из листа «Риэлторы» — не наш

        # Текущее состояние диалога из колонки G
        state = sheets.get_cold_contact_result(user_id) or ""

        logger.info(f"← (холодный) {username} [state={state!r}]: {text[:80]}")

        # ── Уже питчили — молчим, уведомляем владельца ────────────
        if state == STATE_PITCHED:
            logger.info(f"State=pitched для {username} — молчим, уведомляем владельца")
            await client.send_message(
                Config.OWNER_CHAT_ID,
                f"💬 *Холодный контакт пишет после питча*\n\n"
                f"👤 {username}\n"
                f"💬 _{text[:300]}_",
                parse_mode="md",
            )
            return

        # ── Первый ответ на opener ─────────────────────────────────
        if state == STATE_SENT_OPENER:
            reply_type = classify_reply(text)

            if reply_type == "neutral":
                # Переспрашиваем один раз
                reply      = CLARIFY
                next_state = STATE_AWAITING_CLARIFY
            else:
                # Сразу питч
                reply      = get_pitch(reply_type)
                next_state = STATE_PITCHED

            await asyncio.sleep(random.uniform(3, 8))
            await event.respond(reply)
            logger.info(f"→ (холодный) {username} [→{next_state}]: {reply[:80]}")

            sheets.update_cold_result(user_id, next_state)

            if next_state == STATE_PITCHED:
                sheets.mark_cold_trial(user_id, username)
            else:
                # Если ответили нейтрально — переводим в «В диалоге» в CRM
                if status == "Отправлено":
                    sheets.promote_cold_to_dialog(user_id, username)
            return

        # ── Второй ответ (после переспроса) ───────────────────────
        if state == STATE_AWAITING_CLARIFY:
            # Любой ответ → финальный питч
            reply_type = classify_reply(text)
            reply      = get_pitch(reply_type)

            await asyncio.sleep(random.uniform(3, 8))
            await event.respond(reply)
            logger.info(f"→ (холодный) {username} [→pitched]: {reply[:80]}")

            sheets.update_cold_result(user_id, STATE_PITCHED)
            sheets.mark_cold_trial(user_id, username)
            return

        # ── Неизвестное состояние (пустое / старый формат) ────────
        # Относимся как к первому ответу на opener
        if status in ("Отправлено", "В диалоге"):
            reply_type = classify_reply(text)
            if reply_type == "neutral":
                reply      = CLARIFY
                next_state = STATE_AWAITING_CLARIFY
            else:
                reply      = get_pitch(reply_type)
                next_state = STATE_PITCHED

            await asyncio.sleep(random.uniform(3, 8))
            await event.respond(reply)
            logger.info(f"→ (холодный) {username} [fallback→{next_state}]: {reply[:80]}")

            sheets.update_cold_result(user_id, next_state)
            if next_state == STATE_PITCHED:
                sheets.mark_cold_trial(user_id, username)


# ── Запуск / остановка ─────────────────────────────────────────────

_userbot_client: TelegramClient | None = None


async def start_userbot() -> TelegramClient:
    global _userbot_client

    if not Config.TG_SESSION_STRING or not Config.TG_API_ID or not Config.TG_API_HASH:
        logger.warning("Userbot не запущен: TG_API_ID/TG_API_HASH/TG_SESSION_STRING не заданы")
        return None

    session = StringSession(Config.TG_SESSION_STRING)
    client  = TelegramClient(session, int(Config.TG_API_ID), Config.TG_API_HASH)

    await client.start()
    me = await client.get_me()
    logger.info(f"✅ Userbot запущен как @{me.username} ({me.id})")

    register_cold_reply_handler(client)
    asyncio.create_task(outreach_loop(client))

    _userbot_client = client
    return client


async def stop_userbot():
    global _userbot_client
    if _userbot_client:
        await _userbot_client.disconnect()
        logger.info("🛑 Userbot отключён")
        _userbot_client = None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [USERBOT] %(levelname)s %(message)s",
    )

    async def _standalone():
        client = await start_userbot()
        if client:
            await client.run_until_disconnected()

    asyncio.run(_standalone())

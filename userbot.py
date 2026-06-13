"""
userbot.py — Telethon userbot для работы с холодными контактами.

Запускается ОТДЕЛЬНО от bot.py (два процесса):
  python bot.py        ← отвечает входящим (тёплые клиенты)
  python userbot.py    ← пишет первым холодным + ведёт их диалог

Переменные окружения (уже есть в Railway для seller_tg):
  TG_API_ID          — из my.telegram.org
  TG_API_HASH        — из my.telegram.org
  TG_SESSION_STRING  — строка сессии (Telethon StringSession)

Эти переменные уже настроены в проекте — отдельная генерация не требуется.
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
from cold_gemini import cold_gemini
from sheets_client import sheets

# Логирование настраивается в main.py (root logger). Если этот файл
# запускается отдельно (стандартный if __name__ == "__main__" внизу),
# basicConfig применится только в этом случае — см. конец файла.
logger = logging.getLogger(__name__)

# ── Константы темпа ────────────────────────────────────────────────
DAILY_LIMIT = 15          # сообщений в день максимум
INTERVAL_MIN = 25 * 60   # минимальный интервал между отправками (сек)
INTERVAL_MAX = 40 * 60   # максимальный интервал
CHECK_INTERVAL = 5 * 60  # как часто проверять очередь (сек)

# ── Рабочее окно (грузинское время, UTC+4 круглый год) ─────────────
TBILISI_TZ = ZoneInfo("Asia/Tbilisi")
WORK_HOUR_START = 11   # с 11:00
WORK_HOUR_END = 18     # до 18:00 (не включительно)


def _is_within_working_hours() -> bool:
    """Проверка, что сейчас рабочее окно по грузинскому времени."""
    now = datetime.now(TBILISI_TZ)
    return WORK_HOUR_START <= now.hour < WORK_HOUR_END


def _seconds_until_working_hours() -> int:
    """Сколько секунд ждать до начала следующего рабочего окна."""
    now = datetime.now(TBILISI_TZ)
    target = now.replace(hour=WORK_HOUR_START, minute=0, second=0, microsecond=0)
    if now.hour >= WORK_HOUR_END:
        # после конца окна сегодня -> ждём завтра
        target = target.replace(day=now.day) 
        from datetime import timedelta
        target += timedelta(days=1)
    elif now.hour < WORK_HOUR_START:
        # до начала окна сегодня -> ждём сегодня
        pass
    else:
        # внутри окна — на всякий случай 0
        return 0
    return int((target - now).total_seconds())

# ── Счётчик дня ───────────────────────────────────────────────────
_sent_today = 0
_last_reset_day = None


def _reset_daily_counter_if_needed():
    global _sent_today, _last_reset_day
    from datetime import date
    today = date.today()
    if _last_reset_day != today:
        _sent_today = 0
        _last_reset_day = today


async def _resolve_entity(client: TelegramClient, user_id, username: str):
    """
    Резолвит entity для отправки сообщения.

    Telethon не может построить InputPeerUser из чистого user_id без
    access_hash (ошибка "Could not find the input entity for PeerUser(...)"),
    если этот пользователь ещё не "известен" сессии (нет в диалогах/контактах).

    Поэтому резолвим по username через ResolveUsername — это работает для
    любого юзера с открытым публичным username, независимо от истории
    переписки. Если username нет или не резолвится — пробуем user_id как
    запасной вариант (на случай если юзер всё же уже в кэше сессии).

    Возвращает entity или None, если ничего не сработало.
    """
    if username:
        uname = username.lstrip("@")
        try:
            return await client.get_entity(uname)
        except (UsernameNotOccupiedError, UsernameInvalidError):
            logger.warning(f"Username не существует/невалиден: @{uname}")
        except Exception as e:
            logger.warning(f"Не удалось резолвить @{uname}: {e}")

    # Фоллбэк на user_id (сработает только если юзер уже в кэше сессии)
    try:
        return await client.get_entity(user_id)
    except Exception as e:
        logger.warning(f"Не удалось резолвить user_id={user_id}: {e}")
        return None


async def _has_existing_dialog(client: TelegramClient, entity) -> bool:
    """
    Проверка, есть ли уже переписка с этим пользователем
    (любой из аккаунтов уже писал друг другу — пропускаем как холодного).
    """
    try:
        async for _ in client.iter_messages(entity, limit=1):
            return True
        return False
    except Exception as e:
        logger.warning(f"Не удалось проверить историю с {entity}: {e}")
        # Если не получили доступ к диалогу — считаем что диалога нет,
        # пробуем писать.
        return False


async def send_cold_outreach(client: TelegramClient, user_id: int, username: str, name: str):
    """Отправить первое сообщение холодному контакту."""
    global _sent_today

    _reset_daily_counter_if_needed()
    if _sent_today >= DAILY_LIMIT:
        logger.info("Дневной лимит достигнут, ждём завтра")
        return False

    # Резолвим entity по username (или user_id как фоллбэк) — без этого
    # Telethon не может построить InputPeerUser для незнакомых юзеров
    entity = await _resolve_entity(client, user_id, username)
    if entity is None:
        logger.warning(f"Не удалось резолвить контакт: {username or user_id}")
        sheets.mark_cold_failed(user_id, "could_not_resolve_entity")
        return False

    # Пропускаем, если с этим контактом уже есть переписка
    if await _has_existing_dialog(client, entity):
        logger.info(f"Уже есть диалог с {username or user_id} — пропускаем, помечаем как тёплого")
        sheets.mark_cold_skipped(user_id, "already_in_dialog")
        return False

    first_message = await cold_gemini.generate_cold_opener(username, name)

    try:
        await client.send_message(entity, first_message)
        _sent_today += 1
        logger.info(f"✉️  Холодное отправлено → {username or user_id} [{_sent_today}/{DAILY_LIMIT}]")

        # Отметить в таблице что написали
        sheets.mark_cold_sent(user_id, first_message)

        # Случайный интервал после отправки
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


async def outreach_loop(client: TelegramClient):
    """Основной цикл: берёт следующего из очереди и пишет (только в рабочее окно)."""
    logger.info(
        f"🚀 Outreach loop запущен (рабочее окно {WORK_HOUR_START}:00–{WORK_HOUR_END}:00 по Тбилиси)"
    )
    while True:
        try:
            _reset_daily_counter_if_needed()

            if not _is_within_working_hours():
                wait_sec = _seconds_until_working_hours()
                now = datetime.now(TBILISI_TZ).strftime("%H:%M")
                logger.info(
                    f"⏰ {now} (Тбилиси) — вне рабочего окна, спим {wait_sec // 60} мин"
                )
                # Спим короткими интервалами, чтобы быстро реагировать на shutdown
                await asyncio.sleep(min(wait_sec, CHECK_INTERVAL))
                continue

            if _sent_today < DAILY_LIMIT:
                contact = sheets.get_next_cold_contact()
                if contact:
                    user_id = int(contact["user_id"])
                    username = contact.get("username", "")
                    name = contact.get("name", "")
                    await send_cold_outreach(client, user_id, username, name)
                else:
                    logger.info("Очередь пуста, ждём новых контактов")
                    await asyncio.sleep(CHECK_INTERVAL)
            else:
                logger.info(f"Лимит {DAILY_LIMIT}/день достигнут, ждём конца рабочего окна / завтра")
                await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            logger.error(f"Ошибка в outreach_loop: {e}")
            await asyncio.sleep(60)


def register_cold_reply_handler(client: TelegramClient):
    """
    Слушаем входящие от пользователей из листа Риэлторы.
    Если холодный контакт ответил — Gemini продолжает диалог.
    """

    @client.on(events.NewMessage(incoming=True))
    async def handle_incoming(event):
        # Игнорируем группы и каналы
        if not event.is_private:
            return

        sender = await event.get_sender()
        if not sender:
            return

        user_id = sender.id
        username = f"@{sender.username}" if sender.username else str(user_id)
        text = event.raw_text

        # Проверяем что это холодный контакт из листа "Риэлторы"
        status = sheets.get_cold_contact_status(user_id)
        if status is None:
            # Не наш контакт — пропускаем (тёплые обрабатывает bot.py)
            return
        if status not in ("Отправлено", "В диалоге"):
            # "" (новый, ещё не писали), "Пропущен", "Ошибка", "Триал" — не наш кейс здесь
            return

        logger.info(f"← (холодный) {username}: {text[:80]}")

        # Если это первый ответ — переводим в CRM/Историю как полноценный лид
        if status == "Отправлено":
            sheets.promote_cold_to_dialog(user_id, username, name="")

        # Записываем входящее в историю
        sheets.history_append_message(user_id, "👤", text)

        # Ведём диалог через Gemini
        try:
            reply, needs_takeover, trial_link_sent = await cold_gemini.chat(user_id, text)
        except Exception as e:
            logger.error(f"Gemini cold chat error: {e}")
            return

        # Небольшая задержка перед ответом (имитация печати)
        await asyncio.sleep(random.uniform(3, 8))
        await event.respond(reply)
        logger.info(f"→ (холодный) {username}: {reply[:80]}")

        # Обновляем историю
        sheets.history_append_message(user_id, "🤖", reply)

        # Уведомить владельца если нужен перехват
        if needs_takeover:
            await client.send_message(
                Config.OWNER_CHAT_ID,
                f"🔔 *Холодный клиент — требуется ответ*\n\n"
                f"👤 {username}\n"
                f"💬 Написал: _{text[:200]}_\n\n"
                f"🤖 Бот ответил: _{reply[:200]}_\n\n"
                f"➡️ Спрашивает о цене — подключайтесь!",
                parse_mode="md",
            )

        if trial_link_sent:
            sheets.mark_cold_trial(user_id, username)
            logger.info(f"Trial started (холодный): {username}")


_userbot_client: TelegramClient | None = None


async def start_userbot() -> TelegramClient:
    """
    Запускает Telethon userbot внутри уже работающего event loop основного бота.
    Используется из post_init в main.py:

        from userbot import start_userbot
        asyncio.create_task(start_userbot())

    Возвращает TelegramClient (на случай если нужно потом отключить).
    """
    global _userbot_client

    if not Config.TG_SESSION_STRING or not Config.TG_API_ID or not Config.TG_API_HASH:
        logger.warning("Userbot не запущен: TG_API_ID/TG_API_HASH/TG_SESSION_STRING не заданы")
        return None

    session = StringSession(Config.TG_SESSION_STRING)
    client = TelegramClient(
        session,
        int(Config.TG_API_ID),
        Config.TG_API_HASH,
    )

    await client.start()
    me = await client.get_me()
    logger.info(f"✅ Userbot запущен как @{me.username} ({me.id})")

    register_cold_reply_handler(client)

    # outreach_loop крутится бесконечно сам по себе — фоновая задача
    asyncio.create_task(outreach_loop(client))

    _userbot_client = client
    return client


async def stop_userbot():
    """Корректно отключить Telethon client. Вызывается из post_shutdown."""
    global _userbot_client
    if _userbot_client:
        await _userbot_client.disconnect()
        logger.info("🛑 Userbot отключён")
        _userbot_client = None


if __name__ == "__main__":
    # Standalone-запуск для локального теста (без основного бота) —
    # настраиваем logging только в этом случае
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [USERBOT] %(levelname)s %(message)s",
    )

    async def _standalone():
        client = await start_userbot()
        if client:
            await client.run_until_disconnected()

    asyncio.run(_standalone())

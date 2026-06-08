"""
Broadcaster — рассылка в Telegram-группы по команде /broadcast.
Telethon-аккаунт отдельный от бота. Консервативные задержки 120-180с.
"""
import asyncio
import random
import logging

from datetime import datetime

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, ChatWriteForbiddenError, UserBannedInChannelError,
    ChannelPrivateError, PeerFloodError, SlowModeWaitError,
)
from telethon.tl.functions.messages import ImportChatInviteRequest

from config import Config
from sheets_client import sheets

log = logging.getLogger(__name__)

DELAY_MIN = 120
DELAY_MAX = 180
FLOOD_SKIP_THRESHOLD = 180

_broadcast_running = False


async def run_broadcast(progress_callback=None):
    global _broadcast_running

    if _broadcast_running:
        if progress_callback:
            await progress_callback("⚠️ Рассылка уже идёт, подождите её завершения.")
        return

    if not Config.TG_SESSION_STRING:
        if progress_callback:
            await progress_callback(
                "❌ TG_SESSION_STRING не задан в переменных окружения.\n"
                "Рассылка невозможна."
            )
        return

    _broadcast_running = True
    try:
        await _do_broadcast(progress_callback)
    except Exception as e:
        log.error(f"[broadcaster] Критическая ошибка: {e}", exc_info=True)
        if progress_callback:
            await progress_callback(f"❌ Критическая ошибка рассылки: {e}")
    finally:
        _broadcast_running = False


async def _do_broadcast(progress_callback):
    try:
        groups, templates, groups_ws = sheets.get_broadcast_data()
    except Exception as e:
        if progress_callback:
            await progress_callback(f"❌ Ошибка чтения таблицы: {e}")
        return

    if not templates:
        if progress_callback:
            await progress_callback("❌ Лист «Шаблоны» пуст — добавьте хотя бы один шаблон.")
        return

    active = [g for g in groups if g["enabled"]]
    if not active:
        if progress_callback:
            await progress_callback("⚠️ Нет активных групп (колонка D = TRUE).")
        return

    random.shuffle(active)
    total = len(active)
    est_min = total * DELAY_MIN // 60
    est_max = total * DELAY_MAX // 60

    if progress_callback:
        await progress_callback(
            f"🚀 Рассылка запущена\n"
            f"📋 Групп: {total} | Шаблонов: {len(templates)}\n"
            f"⏱ Задержка: {DELAY_MIN}–{DELAY_MAX}с между группами\n"
            f"🕐 Примерное время: {est_min}–{est_max} мин\n\n"
            f"Буду присылать отчёт каждые 5 групп."
        )

    async with TelegramClient(
        StringSession(Config.TG_SESSION_STRING),
        Config.TG_API_ID,
        Config.TG_API_HASH,
    ) as client:

        sent = failed = 0

        for i, group in enumerate(active, start=1):
            text = _pick_template(group, templates)
            if not text:
                continue

            try:
                success, status = await _send_one(client, group, text)
            except PeerFloodError:
                msg = (
                    "🚫 PeerFloodError — Telegram заблокировал отправку.\n"
                    f"Отправлено до остановки: {sent}/{total}\n"
                    "Подождите несколько часов перед следующей рассылкой."
                )
                log.error(msg)
                if progress_callback:
                    await progress_callback(msg)
                _update_row(groups_ws, group, "PeerFlood — остановлено")
                return

            _update_row(groups_ws, group, status)

            if success:
                sent += 1
            else:
                failed += 1

            if progress_callback and (i % 5 == 0 or not success):
                icon = "✅" if success else "❌"
                await progress_callback(
                    f"{icon} [{i}/{total}] {group['username']}\n"
                    f"→ {status}"
                )

            if i < total:
                delay = random.randint(DELAY_MIN, DELAY_MAX)
                log.info(f"[broadcaster] Пауза {delay}с ({i}/{total} отправлено)")
                await asyncio.sleep(delay)

    if progress_callback:
        await progress_callback(
            f"✅ Рассылка завершена\n"
            f"Отправлено: {sent} | Ошибок: {failed} | Всего: {total}"
        )


def _pick_template(group: dict, templates: dict) -> str | None:
    key = group.get("template_key", "").strip()
    if key and key in templates:
        return templates[key]
    if templates:
        return random.choice(list(templates.values()))
    return None


async def _send_one(client, group: dict, text: str) -> tuple[bool, str]:
    target = group["username"]

    for attempt in range(2):
        try:
            if "t.me/+" in target:
                hash_ = target.split("+")[-1]
                try:
                    await client(ImportChatInviteRequest(hash_))
                except Exception:
                    pass

            entity = await client.get_entity(target)
            await client.send_message(entity, text)

            log.info(f"[broadcaster] ✅ {target}")
            return True, "Отправлено"

        except FloodWaitError as e:
            if e.seconds > FLOOD_SKIP_THRESHOLD:
                log.warning(f"[broadcaster] FloodWait {e.seconds}с на {target} — пропуск")
                return False, f"FloodWait {e.seconds}с — пропущено"
            log.warning(f"[broadcaster] FloodWait {e.seconds}с на {target} — ждём...")
            await asyncio.sleep(e.seconds + 5)
            continue

        except PeerFloodError:
            raise

        except SlowModeWaitError as e:
            return False, f"SlowMode {e.seconds}с"

        except ChatWriteForbiddenError:
            return False, "Нет прав на запись"

        except UserBannedInChannelError:
            return False, "Аккаунт забанен"

        except ChannelPrivateError:
            return False, "Приватный канал"

        except Exception as e:
            log.error(f"[broadcaster] Ошибка для {target}: {e}")
            return False, f"Ошибка: {str(e)[:80]}"

    return False, "FloodWait — не удалось после повтора"


def _update_row(ws, group: dict, status: str):
    """Обновляет только статус и время последней публикации."""
    row = group["row"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    updates = [(row, 5, status)]   # E — Статус
    if status == "Отправлено":
        updates.append((row, 3, now))  # C — Время последней публикации

    for r, c, v in updates:
        try:
            ws.update_cell(r, c, v)
        except Exception as e:
            log.error(f"[broadcaster] Ошибка записи в таблицу row={r} col={c}: {e}")

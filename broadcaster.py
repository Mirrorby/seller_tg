"""
Broadcaster — рассылка в Telegram-группы по команде /broadcast.
Telethon-аккаунт отдельный от бота. Консервативные задержки 120-180с.
"""
import asyncio
import random
import logging
import re
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

DELAY_MIN = 120          # минимальная пауза между группами (секунды)
DELAY_MAX = 180          # максимальная пауза
FLOOD_SKIP_THRESHOLD = 180  # FloodWait дольше этого — пропускаем группу

# Защита от двойного запуска
_broadcast_running = False


async def run_broadcast(progress_callback=None):
    """
    Точка входа. Вызывается из handlers.py по команде /broadcast.
    progress_callback(text) — отправляет сообщение владельцу в бот.
    """
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
    # Загружаем данные из таблицы
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
                success, status, post_link = await _send_one(client, group, text)
            except PeerFloodError:
                # PeerFlood = Telegram считает аккаунт спамером, немедленно стоп
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

            _update_row(groups_ws, group, status, post_link if success else "")

            if success:
                sent += 1
            else:
                failed += 1

            # Отчёт каждые 5 групп или при ошибке
            if progress_callback and (i % 5 == 0 or not success):
                icon = "✅" if success else "❌"
                await progress_callback(
                    f"{icon} [{i}/{total}] {group['username']}\n"
                    f"→ {status}"
                    + (f"\n🔗 {post_link}" if post_link else "")
                )

            # Пауза перед следующей группой (кроме последней)
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


async def _send_one(client, group: dict, text: str) -> tuple[bool, str, str]:
    target = group["username"]

    for attempt in range(2):
        try:
            # Приватная группа по invite-ссылке
            if "t.me/+" in target:
                hash_ = target.split("+")[-1]
                try:
                    await client(ImportChatInviteRequest(hash_))
                except Exception:
                    pass

            entity = await client.get_entity(target)
            msg = await client.send_message(entity, text)

            # Формируем ссылку на пост
            post_link = ""
            chat_username = getattr(entity, "username", None)
            if chat_username and msg.id:
                post_link = f"https://t.me/{chat_username}/{msg.id}"

            log.info(f"[broadcaster] ✅ {target}" + (f" → {post_link}" if post_link else ""))
            return True, "Отправлено", post_link

        except FloodWaitError as e:
            if e.seconds > FLOOD_SKIP_THRESHOLD:
                log.warning(f"[broadcaster] FloodWait {e.seconds}с на {target} — пропуск")
                return False, f"FloodWait {e.seconds}с — пропущено", ""
            log.warning(f"[broadcaster] FloodWait {e.seconds}с на {target} — ждём...")
            await asyncio.sleep(e.seconds + 5)
            continue  # повтор после ожидания

        except PeerFloodError:
            raise  # пробрасываем наверх — остановить всю рассылку

        except SlowModeWaitError as e:
            log.warning(f"[broadcaster] SlowMode {e.seconds}с на {target}")
            return False, f"SlowMode {e.seconds}с", ""

        except ChatWriteForbiddenError:
            log.warning(f"[broadcaster] Нет прав на запись: {target}")
            return False, "Нет прав на запись", ""

        except UserBannedInChannelError:
            log.warning(f"[broadcaster] Аккаунт забанен в: {target}")
            return False, "Аккаунт забанен", ""

        except ChannelPrivateError:
            log.warning(f"[broadcaster] Приватный канал: {target}")
            return False, "Приватный канал", ""

        except Exception as e:
            log.error(f"[broadcaster] Ошибка для {target}: {e}")
            return False, f"Ошибка: {str(e)[:80]}", ""

    return False, "FloodWait — не удалось после повтора", ""


def _update_row(ws, group: dict, status: str, new_post_link: str = ""):
    """Обновляет строку в листе «Группы» после отправки."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    post_link = group["post_link"]
    post_new = group["post_new"]
    posts_between = group["posts_between"]

    if new_post_link:
        if not post_link:
            post_link = new_post_link
            post_new = ""
            posts_between = ""
        elif not post_new:
            post_new = new_post_link
            posts_between = _calc_posts_between(post_link, post_new)
        else:
            post_link = post_new
            post_new = new_post_link
            posts_between = _calc_posts_between(post_link, post_new)

    row = group["row"]
    updates = [
        (row, 5, status),         # E — Статус
        (row, 6, post_link),      # F — Ссылка на пост
        (row, 7, post_new),       # G — Ссылка на новый пост
        (row, 8, posts_between),  # H — Постов между
    ]
    if status == "Отправлено":
        updates.append((row, 3, now))  # C — Время последней публикации

    for r, c, v in updates:
        try:
            ws.update_cell(r, c, v)
        except Exception as e:
            log.error(f"[broadcaster] Ошибка записи в таблицу row={r} col={c}: {e}")


def _calc_posts_between(old_url: str, new_url: str) -> str:
    old_id = _extract_post_id(old_url)
    new_id = _extract_post_id(new_url)
    if old_id is not None and new_id is not None:
        return str(abs(new_id - old_id))
    return ""


def _extract_post_id(url: str) -> int | None:
    if not url:
        return None
    m = re.search(r"/(\d+)\s*$", url.strip())
    return int(m.group(1)) if m else None

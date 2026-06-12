import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from config import Config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Колонки CRM (1-indexed)
# A=1  B=2        C=3   D=4              E=5                F=6      G=7
# ID   Username   Имя   Аккаунт сейлза   Дата 1го контакта  Диалог   Предложение
# H=8          I=9                J=10     K=11              L=12         M=13
# Тест-период  Подписался на бот  Статус   Дата подключения  Тариф(дней)  Дата окончания
# N=14          O=15         P=16         Q=17
# Оплата(лари)  Оплата(руб)  Город/тема   Комментарий

COL = {
    "chat_id":       1,   # A
    "username":      2,   # B
    "name":          3,   # C
    "sales_account": 4,   # D
    "first_contact": 5,   # E
    "dialog":        6,   # F
    "offer":         7,   # G
    "trial":         8,   # H
    "subscribed":    9,   # I — «Подписался на бот»
    "status":        10,  # J
    "connected_at":  11,  # K
    "tariff_days":   12,  # L
    "expires_at":    13,  # M
    "payment_lari":  14,  # N
    "payment_rub":   15,  # O
    "city_topic":    16,  # P
    "comment":       17,  # Q
}

# Данные начинаются со строки 6 (1-5: заголовок + счётчики)
CRM_DATA_START_ROW = 6

HIST_COL_USERNAME = 1
HIST_COL_CHAT_ID  = 2
HIST_DATA_START   = 3

STATUS_ACTIVE       = "✅ Активен"
STATUS_LEAD         = "🟡 Лид"
STATUS_TRIAL        = "🔵 Триал"
STATUS_DISCONNECTED = "🔴 Отключён"
STATUS_REFUSED      = "🚫 Отказ"

# ------------------------------------------------------------------ #
# Лист "Риэлторы" — очередь холодных контактов                        #
# Колонки: A=user_id B=Имя C=Комментарий D=username                   #
#          E=Статус  F=Дата отправки     G=Результат                  #
# ------------------------------------------------------------------ #
COLD_COL_USER_ID  = 1  # A
COLD_COL_NAME     = 2  # B
COLD_COL_COMMENT  = 3  # C
COLD_COL_USERNAME = 4  # D
COLD_COL_STATUS   = 5  # E
COLD_COL_SENT_AT  = 6  # F
COLD_COL_RESULT   = 7  # G

COLD_DATA_START_ROW = 2

# Статусы холодных контактов (колонка E листа "Риэлторы")
COLD_STATUS_SENT     = "Отправлено"
COLD_STATUS_DIALOG   = "В диалоге"
COLD_STATUS_SKIPPED  = "Пропущен"
COLD_STATUS_FAILED   = "Ошибка"
COLD_STATUS_TRIAL    = "Триал"

class SheetsClient:

    def __init__(self):
        creds_dict = json.loads(Config.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sheet_id    = Config.GOOGLE_SHEET_ID
        self._crm_sheet  = None
        self._hist_sheet = None
        self._leads_sheet = None

    # ------------------------------------------------------------------ #
    # Листы                                                                #
    # ------------------------------------------------------------------ #

    @property
    def crm(self) -> gspread.Worksheet:
        if self._crm_sheet is None:
            wb = self.gc.open_by_key(self.sheet_id)
            self._crm_sheet = wb.worksheet(Config.CRM_SHEET_NAME)
        return self._crm_sheet

    @property
    def hist(self) -> gspread.Worksheet:
        if self._hist_sheet is None:
            wb = self.gc.open_by_key(self.sheet_id)
            try:
                self._hist_sheet = wb.worksheet(Config.HISTORY_SHEET_NAME)
            except gspread.WorksheetNotFound:
                self._hist_sheet = wb.add_worksheet(
                    title=Config.HISTORY_SHEET_NAME, rows=1000, cols=500
                )
                self._hist_sheet.update("A1:B1", [["Username", "Chat ID"]])
                logger.info(f"Создан лист истории диалогов: «{Config.HISTORY_SHEET_NAME}»")
        return self._hist_sheet

    @property
    def leads(self) -> gspread.Worksheet:
        """Лист 'Риэлторы' — очередь холодных контактов."""
        if self._leads_sheet is None:
            wb = self.gc.open_by_key(self.sheet_id)
            self._leads_sheet = wb.worksheet(Config.COLD_LEADS_SHEET_NAME)
        return self._leads_sheet

    # ------------------------------------------------------------------ #
    # CRM: поиск строки                                                    #
    # ------------------------------------------------------------------ #

    def _crm_find_row(self, username: str = "", chat_id: str = "") -> Optional[int]:
        """
        Ищет строку по chat_id (колонка A) или username (колонка B).
        chat_id имеет приоритет — точнее.
        """
        try:
            if chat_id:
                try:
                    cell = self.crm.find(str(chat_id), in_column=COL["chat_id"])
                    return cell.row
                except Exception:
                    pass
            if username:
                try:
                    cell = self.crm.find(username, in_column=COL["username"])
                    return cell.row
                except Exception:
                    pass
            return None
        except Exception:
            return None

    def _crm_next_row(self) -> int:
        col_values = self.crm.col_values(COL["username"])
        for i in range(len(col_values) - 1, -1, -1):
            if col_values[i].strip():
                return i + 2
        return CRM_DATA_START_ROW

    # ------------------------------------------------------------------ #
    # CRM: upsert клиента (seller_tg)                                      #
    # ------------------------------------------------------------------ #

    def upsert_client(
        self,
        username: str,
        *,
        chat_id: str = "",
        name: str = "",
        sales_account: str = "Никита",
        status: str = "",
        dialog: str = "Да",
        offer: str = "",
        trial: str = "",
        subscribed: str = "",
        connected_at: str = "",
        tariff_days: str = "",
        expires_at: str = "",
        payment_lari: str = "",
        payment_rub: str = "",
        city_topic: str = "Батуми",
        comment: str = "",
    ):
        try:
            today  = datetime.now().strftime("%Y-%m-%d")
            row    = self._crm_find_row(username=username, chat_id=chat_id)
            is_new = row is None

            if is_new:
                row = self._crm_next_row()
                updates = {
                    "chat_id":       chat_id,
                    "username":      username,
                    "name":          name,
                    "sales_account": sales_account,
                    "first_contact": today,
                    "dialog":        dialog,
                    "offer":         offer,
                    "trial":         trial,
                    "subscribed":    subscribed,
                    "status":        status,
                    "connected_at":  connected_at,
                    "tariff_days":   tariff_days,
                    "expires_at":    expires_at,
                    "payment_lari":  payment_lari,
                    "payment_rub":   payment_rub,
                    "city_topic":    city_topic,
                    "comment":       comment,
                }
                logger.info(f"📝 CRM: добавлен новый контакт {username} (chat_id={chat_id})")
            else:
                updates = {
                    k: v for k, v in {
                        "chat_id":       chat_id,
                        "name":          name,
                        "sales_account": sales_account,
                        "dialog":        dialog,
                        "offer":         offer,
                        "trial":         trial,
                        "subscribed":    subscribed,
                        "status":        status,
                        "connected_at":  connected_at,
                        "tariff_days":   tariff_days,
                        "expires_at":    expires_at,
                        "payment_lari":  payment_lari,
                        "payment_rub":   payment_rub,
                        "city_topic":    city_topic,
                        "comment":       comment,
                    }.items() if v
                }
                logger.info(f"📝 CRM: обновлён {username} — {list(updates.keys())}")

            cells = [
                gspread.Cell(row, COL[field], value)
                for field, value in updates.items()
                if field in COL
            ]
            if cells:
                self.crm.update_cells(cells, value_input_option="USER_ENTERED")

        except Exception as e:
            logger.error(f"Не удалось сохранить {username} в CRM: {e}")

    # ------------------------------------------------------------------ #
    # CRM: подписка через monitor_tg (/start в lead_vitrina_bot)           #
    # ------------------------------------------------------------------ #

    def mark_bot_subscribed(self, chat_id: str, username: str, subscribed_at: str = ""):
        """
        Вызывается из monitor_tg когда риэлтор нажал /start в lead_vitrina_bot.
        Ищет строку по chat_id или username.
        Если нашёл — ставит 'Подписался на бот' = Да, дату подключения, статус Триал.
        Если не нашёл — создаёт новую строку (подписался сам, без seller_tg).
        """
        try:
            today    = subscribed_at or datetime.now().strftime("%Y-%m-%d")
            trial_end = (
                datetime.strptime(today[:10], "%Y-%m-%d") + timedelta(days=3)
            ).strftime("%Y-%m-%d")

            row    = self._crm_find_row(username=username, chat_id=chat_id)
            is_new = row is None

            if is_new:
                row = self._crm_next_row()
                updates = {
                    "chat_id":      chat_id,
                    "username":     username,
                    "first_contact": today,
                    "subscribed":   "Да",
                    "status":       STATUS_TRIAL,
                    "connected_at": today,
                    "tariff_days":  "3",
                    "expires_at":   trial_end,
                    "city_topic":   "Батуми",
                }
                logger.info(
                    f"📝 CRM: новый подписчик бота {username} (chat_id={chat_id}) "
                    f"— пришёл напрямую без seller_tg, триал до {trial_end}"
                )
            else:
                updates = {
                    "chat_id":      chat_id,
                    "subscribed":   "Да",
                    "status":       STATUS_TRIAL,
                    "connected_at": today,
                    "tariff_days":  "3",
                    "expires_at":   trial_end,
                }
                logger.info(
                    f"🔵 CRM: {username} подписался на бота "
                    f"— триал запущен с {today}, истекает {trial_end}"
                )

            cells = [
                gspread.Cell(row, COL[field], value)
                for field, value in updates.items()
                if field in COL
            ]
            if cells:
                self.crm.update_cells(cells, value_input_option="USER_ENTERED")

        except Exception as e:
            logger.error(f"Ошибка mark_bot_subscribed для {username}: {e}")

    def mark_bot_unsubscribed(self, chat_id: str, username: str):
        """
        Вызывается из monitor_tg когда риэлтор нажал /stop.
        Ставит статус Отключён, не трогает остальные данные.
        """
        try:
            row = self._crm_find_row(username=username, chat_id=chat_id)
            if row is None:
                logger.warning(f"⚠️  mark_bot_unsubscribed: {username} не найден в CRM")
                return

            cells = [
                gspread.Cell(row, COL["subscribed"], "Нет"),
                gspread.Cell(row, COL["status"],     STATUS_DISCONNECTED),
            ]
            self.crm.update_cells(cells, value_input_option="USER_ENTERED")
            logger.info(f"🔴 CRM: {username} отписался от бота")

        except Exception as e:
            logger.error(f"Ошибка mark_bot_unsubscribed для {username}: {e}")

    def mark_trial_started(self, username: str):
        """
        Вызывается когда Никита отправил ссылку @lead_vitrina_bot.
        Ставит статус 🟡 Лид и дату первого касания.
        tariff_days и expires_at НЕ трогаем — триал ещё не запущен,
        клиент только получил ссылку на бот.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        self.upsert_client(
            username,
            trial="Да",
            status=STATUS_LEAD,
            connected_at=today,
        )
        logger.info(f"🟡 Лид: {username} получил ссылку на триал ({today})")

    # ------------------------------------------------------------------ #
    # CRM: экспирация                                                      #
    # ------------------------------------------------------------------ #

    def get_expiring_clients(self, days_ahead: int) -> list[dict]:
        try:
            target   = (datetime.now() + timedelta(days=days_ahead)).date()
            all_rows = self.crm.get_all_values()
            results  = []

            for row in all_rows[CRM_DATA_START_ROW - 1:]:
                if len(row) < COL["expires_at"]:
                    continue
                chat_id     = row[COL["chat_id"]     - 1].strip()
                username    = row[COL["username"]    - 1].strip()
                status      = row[COL["status"]      - 1].strip()
                expires_str = row[COL["expires_at"]  - 1].strip()
                tariff      = row[COL["tariff_days"] - 1].strip()

                if not expires_str or not username:
                    continue
                try:
                    exp_date = datetime.strptime(expires_str[:10], "%Y-%m-%d").date()
                    if exp_date == target:
                        results.append({
                            "chat_id":     chat_id,
                            "username":    username,
                            "status":      status,
                            "expires_at":  expires_str[:10],
                            "tariff_days": tariff,
                        })
                except ValueError:
                    continue

            if results:
                logger.info(
                    f"📅 Найдено клиентов с истекающим доступом "
                    f"через {days_ahead} дн.: {len(results)}"
                )
            return results

        except Exception as e:
            logger.error(f"Ошибка при проверке истекающих подписок: {e}")
            return []

    # ------------------------------------------------------------------ #
    # История диалогов                                                     #
    # ------------------------------------------------------------------ #

    def _hist_find_row(self, chat_id: int) -> Optional[int]:
        try:
            cell = self.hist.find(str(chat_id), in_column=HIST_COL_CHAT_ID)
            return cell.row
        except Exception:
            return None

    def _hist_next_row(self) -> int:
        col_values = self.hist.col_values(HIST_COL_CHAT_ID)
        for i in range(len(col_values) - 1, -1, -1):
            if col_values[i].strip():
                return i + 2
        return 2

    def _hist_next_msg_col(self, row: int) -> int:
        row_values = self.hist.row_values(row)
        return max(len(row_values) + 1, HIST_DATA_START)

    def history_ensure_client(self, username: str, chat_id: int):
        try:
            row = self._hist_find_row(chat_id)
            if row is None:
                row = self._hist_next_row()
                self.hist.update_cell(row, HIST_COL_USERNAME, username)
                self.hist.update_cell(row, HIST_COL_CHAT_ID,  str(chat_id))
                logger.info(f"📖 История: создана строка для {username}")
        except Exception as e:
            logger.error(f"Не удалось создать строку истории для {username}: {e}")

    def history_append_message(self, chat_id: int, role: str, text: str):
        try:
            row = self._hist_find_row(chat_id)
            if row is None:
                logger.warning(
                    f"История не найдена для chat_id={chat_id}, пропускаем запись"
                )
                return
            col        = self._hist_next_msg_col(row)
            ts         = datetime.now().strftime("%H:%M")
            cell_value = f"[{ts}] {role}: {text[:500]}"
            self.hist.update_cell(row, col, cell_value)
        except Exception as e:
            logger.error(
                f"Не удалось записать сообщение в историю (chat_id={chat_id}): {e}"
            )

    def history_get_client_chat_id(self, username: str) -> Optional[int]:
        try:
            cell        = self.hist.find(username, in_column=HIST_COL_USERNAME)
            chat_id_str = self.hist.cell(cell.row, HIST_COL_CHAT_ID).value
            return int(chat_id_str) if chat_id_str else None
        except Exception:
            return None

    def history_load_for_gemini(self, chat_id: int, max_messages: int = 20) -> list:
        """
        Загружает историю диалога из листа «💬 История диалогов»
        и конвертирует её в формат Gemini: список {role, parts}.

        Формат ячеек: "[HH:MM] 👤: текст"  или  "[HH:MM] 🤖: текст"
        👤 → role="user", 🤖 → role="model"

        Возвращает последние max_messages сообщений.
        Если клиент не найден — возвращает [].
        """
        try:
            row = self._hist_find_row(chat_id)
            if row is None:
                return []

            row_values = self.hist.row_values(row)
            # Сообщения начинаются с колонки C (индекс 2)
            messages_raw = row_values[HIST_DATA_START - 1:]
            messages_raw = [m for m in messages_raw if m.strip()]

            # Берём последние max_messages
            messages_raw = messages_raw[-max_messages:]

            history = []
            for cell_text in messages_raw:
                # Формат: "[HH:MM] 👤: текст"
                # Отрезаем метку времени "[HH:MM] "
                if "] " in cell_text:
                    rest = cell_text.split("] ", 1)[1]
                else:
                    rest = cell_text

                if rest.startswith("👤:"):
                    role = "user"
                    text = rest[len("👤:"):].strip()
                elif rest.startswith("🤖:"):
                    role = "model"
                    text = rest[len("🤖:"):].strip()
                else:
                    # Неизвестный формат — пропускаем
                    continue

                if text:
                    history.append({"role": role, "parts": [{"text": text}]})

            return history

        except Exception as e:
            logger.error(f"history_load_for_gemini error (chat_id={chat_id}): {e}")
            return []

    def get_broadcast_data(self) -> tuple:
        """
        Читает листы «Группы» и «Шаблоны».
        Возвращает (groups, templates, groups_ws).
        """
        wb = self.gc.open_by_key(self.sheet_id)
        groups_ws = wb.worksheet("Группы")
        rows = groups_ws.get_all_values()[1:]  # пропускаем заголовок
    
        groups = []
        for i, row in enumerate(rows, start=2):
            row = row + [""] * (8 - len(row))  # дополняем до 8 колонок
            url = row[0].strip()
            if not url:
                continue
            active_raw = row[3].strip().upper()
            groups.append({
                "row":           i,
                "username":      url,
                "template_key":  row[1].strip(),
                "last_sent":     row[2].strip(),
                "enabled":       active_raw != "FALSE",
                "status":        row[4].strip(),
                "post_link":     row[5].strip(),
                "post_new":      row[6].strip(),
                "posts_between": row[7].strip(),
            })
    
        templates_ws = wb.worksheet("Шаблоны")
        templates = {}
        for row in templates_ws.get_all_values()[1:]:
            if len(row) >= 2 and row[0].strip():
                templates[row[0].strip()] = row[1].strip()
    
        logger.info(f"Broadcaster: групп={len(groups)}, шаблонов={len(templates)}")
        return groups, templates, groups_ws

    def get_broadcast_token(self) -> str:
        """Читает токен бота-рассылки из листа «Настройки» B2."""
        try:
            wb = self.gc.open_by_key(self.sheet_id)
            ws = wb.worksheet("Настройки")
            return ws.acell("B2").value or ""
        except Exception as e:
            logger.error(f"Не удалось прочитать токен рассылки из Настроек: {e}")
            return ""

    # ------------------------------------------------------------------ #
    # Холодные контакты: очередь в листе "Риэлторы"                       #
    # ------------------------------------------------------------------ #

    def _leads_find_row(self, user_id) -> Optional[int]:
        """Найти строку в листе 'Риэлторы' по user_id (колонка A)."""
        try:
            cell = self.leads.find(str(user_id), in_column=COLD_COL_USER_ID)
            return cell.row
        except Exception:
            return None

    def _get_crm_lookup_sets(self) -> tuple[set, set]:
        """
        Читает CRM один раз и возвращает (set chat_id'ов, set username'ов)
        для быстрой проверки is_in_crm без отдельного API-запроса на каждую строку.
        """
        try:
            all_rows = self.crm.get_all_values()
            chat_ids = set()
            usernames = set()
            for row in all_rows[CRM_DATA_START_ROW - 1:]:
                if len(row) >= COL["chat_id"]:
                    val = row[COL["chat_id"] - 1].strip()
                    if val:
                        chat_ids.add(val)
                if len(row) >= COL["username"]:
                    val = row[COL["username"] - 1].strip()
                    if val:
                        usernames.add(val)
            return chat_ids, usernames
        except Exception as e:
            logger.error(f"_get_crm_lookup_sets error: {e}")
            return set(), set()

    def is_in_crm(self, user_id, username: str = "") -> bool:
        """
        Проверяет, есть ли уже запись в CRM по chat_id или username.
        Используется чтобы не писать холодные сообщения тем, кто уже
        является тёплым/известным контактом.
        """
        try:
            row = self._crm_find_row(username=username, chat_id=str(user_id))
            return row is not None
        except Exception as e:
            logger.error(f"is_in_crm error (user_id={user_id}): {e}")
            return False

    def get_next_cold_contact(self) -> Optional[dict]:
        """
        Возвращает следующий необработанный контакт из листа "Риэлторы"
        (колонка E «Статус» пустая), либо None если таких нет.

        Контакты, уже присутствующие в CRM (по chat_id или username),
        пропускаются и помечаются как "Пропущен" — не пишем холодное
        тем, кто уже тёплый/известный.
        """
        try:
            rows = self.leads.get_all_values()
            crm_chat_ids, crm_usernames = self._get_crm_lookup_sets()

            for i, row in enumerate(rows[COLD_DATA_START_ROW - 1:], start=COLD_DATA_START_ROW):
                if len(row) < COLD_COL_USER_ID or not row[COLD_COL_USER_ID - 1].strip():
                    continue

                status = row[COLD_COL_STATUS - 1].strip() if len(row) >= COLD_COL_STATUS else ""
                if status:
                    continue  # уже обработан

                user_id  = row[COLD_COL_USER_ID - 1].strip()
                username = row[COLD_COL_USERNAME - 1].strip() if len(row) >= COLD_COL_USERNAME else ""

                # Если контакт уже есть в CRM (тёплый/известный) — не пишем холодное
                if user_id in crm_chat_ids or (username and username in crm_usernames):
                    self.mark_cold_skipped(user_id, "already_in_crm")
                    continue

                return {
                    "row":      i,
                    "user_id":  user_id,
                    "name":     row[COLD_COL_NAME - 1].strip() if len(row) >= COLD_COL_NAME else "",
                    "comment":  row[COLD_COL_COMMENT - 1].strip() if len(row) >= COLD_COL_COMMENT else "",
                    "username": username,
                }

            return None

        except Exception as e:
            logger.error(f"get_next_cold_contact error: {e}")
            return None

    def get_cold_contact_status(self, user_id) -> Optional[str]:
        """
        Возвращает значение колонки 'Статус' (E) для user_id из листа "Риэлторы".
        None — если такого user_id в листе нет вообще (не наш холодный контакт).
        "" — есть в листе, но статус пуст (новый, необработанный).
        """
        try:
            row = self._leads_find_row(user_id)
            if row is None:
                return None
            val = self.leads.cell(row, COLD_COL_STATUS).value
            return (val or "").strip()
        except Exception as e:
            logger.error(f"get_cold_contact_status error: {e}")
            return None

    def mark_cold_sent(self, user_id, message: str):
        """Отметить: первое холодное сообщение отправлено."""
        try:
            row = self._leads_find_row(user_id)
            if row is None:
                logger.warning(f"mark_cold_sent: user_id={user_id} не найден в листе Риэлторы")
                return

            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            cells = [
                gspread.Cell(row, COLD_COL_STATUS, COLD_STATUS_SENT),
                gspread.Cell(row, COLD_COL_SENT_AT, ts),
                gspread.Cell(row, COLD_COL_RESULT, message[:300]),
            ]
            self.leads.update_cells(cells, value_input_option="USER_ENTERED")
            logger.info(f"📤 Холодное: отправлено user_id={user_id}")

        except Exception as e:
            logger.error(f"mark_cold_sent error (user_id={user_id}): {e}")

    def mark_cold_failed(self, user_id, reason: str):
        """Отметить: ошибка отправки (приватность/деактивирован/etc)."""
        try:
            row = self._leads_find_row(user_id)
            if row is None:
                return
            cells = [
                gspread.Cell(row, COLD_COL_STATUS, COLD_STATUS_FAILED),
                gspread.Cell(row, COLD_COL_RESULT, reason[:300]),
            ]
            self.leads.update_cells(cells, value_input_option="USER_ENTERED")
            logger.info(f"⚠️ Холодное: ошибка user_id={user_id} — {reason}")

        except Exception as e:
            logger.error(f"mark_cold_failed error (user_id={user_id}): {e}")

    def mark_cold_skipped(self, user_id, reason: str):
        """Отметить: пропущен (например, уже был диалог с этим контактом)."""
        try:
            row = self._leads_find_row(user_id)
            if row is None:
                return
            cells = [
                gspread.Cell(row, COLD_COL_STATUS, COLD_STATUS_SKIPPED),
                gspread.Cell(row, COLD_COL_RESULT, reason[:300]),
            ]
            self.leads.update_cells(cells, value_input_option="USER_ENTERED")
            logger.info(f"⏭️ Холодное: пропущен user_id={user_id} — {reason}")

        except Exception as e:
            logger.error(f"mark_cold_skipped error (user_id={user_id}): {e}")

    def promote_cold_to_dialog(self, user_id, username: str, name: str = ""):
        """
        Вызывается когда холодный контакт ОТВЕТИЛ.
        1. Статус в листе "Риэлторы" -> "В диалоге"
        2. Создаётся/обновляется строка в CRM (upsert_client)
        3. Заводится строка в "Истории диалогов" для дальнейших history_* вызовов
        """
        try:
            row = self._leads_find_row(user_id)
            if row:
                self.leads.update_cell(row, COLD_COL_STATUS, COLD_STATUS_DIALOG)

            uname = username or str(user_id)

            self.upsert_client(
                uname,
                chat_id=str(user_id),
                name=name,
                dialog="Да",
                status=STATUS_LEAD,
                comment="Холодный контакт — ответил, диалог начат",
            )

            self.history_ensure_client(uname, int(user_id))
            logger.info(f"🟡 Холодный → Лид: {uname} (user_id={user_id})")

        except Exception as e:
            logger.error(f"promote_cold_to_dialog error (user_id={user_id}): {e}")

    def mark_cold_trial(self, user_id, username: str = ""):
        """Отметить: контакту отправлена ссылка на @lead_vitrina_bot (триал)."""
        try:
            row = self._leads_find_row(user_id)
            if row:
                self.leads.update_cell(row, COLD_COL_STATUS, COLD_STATUS_TRIAL)

            if username:
                self.mark_trial_started(username)

        except Exception as e:
            logger.error(f"mark_cold_trial error (user_id={user_id}): {e}")


sheets = SheetsClient()

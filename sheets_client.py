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
STATUS_TRIAL        = "🔵 Триал"
STATUS_DISCONNECTED = "🔴 Отключён"
STATUS_REFUSED      = "🚫 Отказ"


class SheetsClient:

    def __init__(self):
        creds_dict = json.loads(Config.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sheet_id    = Config.GOOGLE_SHEET_ID
        self._crm_sheet  = None
        self._hist_sheet = None

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
        city_topic: str = "Батуми / аренда",
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
                    "city_topic":   "Батуми / аренда",
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
        Устаревший метод — оставлен для совместимости.
        Теперь триал запускается через mark_bot_subscribed из monitor_tg.
        Используется как fallback если monitor_tg ещё не обновлён.
        """
        today     = datetime.now().strftime("%Y-%m-%d")
        trial_end = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
        self.upsert_client(
            username,
            trial="Да",
            status=STATUS_TRIAL,
            connected_at=today,
            tariff_days="3",
            expires_at=trial_end,
        )
        logger.info(f"🔵 Триал (fallback) запущен для {username}, истекает {trial_end}")

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

sheets = SheetsClient()

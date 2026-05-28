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

# CRM column mapping (1-indexed, B=2)
COL = {
    "username": 2,       # B
    "name": 3,           # C
    "sales_account": 4,  # D
    "first_contact": 5,  # E
    "dialog": 6,         # F
    "offer": 7,          # G
    "trial": 8,          # H
    "subscribed": 9,     # I
    "status": 10,        # J
    "connected_at": 11,  # K
    "tariff_days": 12,   # L
    "expires_at": 13,    # M
    "payment_lari": 14,  # N
    "payment_rub": 15,   # O
    "city_topic": 16,    # P
    "comment": 17,       # Q
    "chat_id": 18,       # R  ← добавлено для проактивных сообщений
}

# История диалогов: лист "💬 История"
# A=username  B=chat_id  C,D,E...=сообщения по одному в колонке
HIST_COL_USERNAME = 1   # A
HIST_COL_CHAT_ID  = 2   # B
HIST_DATA_START   = 3   # сообщения начинаются с C

STATUS_ACTIVE      = "✅ Активен"
STATUS_TRIAL       = "🔵 Триал"
STATUS_DISCONNECTED = "🔴 Отключён"
STATUS_REFUSED     = "🚫 Отказ"


class SheetsClient:
    def __init__(self):
        creds_dict = json.loads(Config.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sheet_id = Config.GOOGLE_SHEET_ID
        self._crm_sheet = None
        self._hist_sheet = None

    # ------------------------------------------------------------------ #
    #  Листы                                                               #
    # ------------------------------------------------------------------ #

    @property
    def crm(self) -> gspread.Worksheet:
        if self._crm_sheet is None:
            wb = self.gc.open_by_key(self.sheet_id)
            self._crm_sheet = wb.worksheet(Config.CRM_SHEET_NAME)
        return self._crm_sheet

    @property
    def hist(self) -> gspread.Worksheet:
        """Лист истории диалогов. Создаётся автоматически если отсутствует."""
        if self._hist_sheet is None:
            wb = self.gc.open_by_key(self.sheet_id)
            try:
                self._hist_sheet = wb.worksheet(Config.HISTORY_SHEET_NAME)
            except gspread.exceptions.WorksheetNotFound:
                self._hist_sheet = wb.add_worksheet(
                    title=Config.HISTORY_SHEET_NAME, rows=1000, cols=500
                )
                # Заголовки
                self._hist_sheet.update(
                    "A1:B1", [["Username", "Chat ID"]]
                )
                logger.info(f"Created sheet '{Config.HISTORY_SHEET_NAME}'")
        return self._hist_sheet

    # ------------------------------------------------------------------ #
    #  CRM helpers                                                         #
    # ------------------------------------------------------------------ #

    def _crm_find_row(self, username: str) -> Optional[int]:
        try:
            cell = self.crm.find(username, in_column=COL["username"])
            return cell.row
        except gspread.exceptions.CellNotFound:
            return None

    def _crm_next_row(self) -> int:
        col_values = self.crm.col_values(COL["username"])
        for i in range(len(col_values) - 1, -1, -1):
            if col_values[i].strip():
                return i + 2
        return Config.CRM_DATA_START_ROW

    # ------------------------------------------------------------------ #
    #  CRM: upsert клиента                                                 #
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
            today = datetime.now().strftime("%Y-%m-%d")
            row = self._crm_find_row(username)
            is_new = row is None

            if is_new:
                row = self._crm_next_row()
                updates = {
                    "username": username,
                    "chat_id": chat_id,
                    "name": name,
                    "sales_account": sales_account,
                    "first_contact": today,
                    "dialog": dialog,
                    "offer": offer,
                    "trial": trial,
                    "subscribed": subscribed,
                    "status": status,
                    "connected_at": connected_at,
                    "tariff_days": tariff_days,
                    "expires_at": expires_at,
                    "payment_lari": payment_lari,
                    "payment_rub": payment_rub,
                    "city_topic": city_topic,
                    "comment": comment,
                }
            else:
                updates = {}
                for key, val in {
                    "chat_id": chat_id,
                    "name": name,
                    "sales_account": sales_account,
                    "dialog": dialog,
                    "offer": offer,
                    "trial": trial,
                    "subscribed": subscribed,
                    "status": status,
                    "connected_at": connected_at,
                    "tariff_days": tariff_days,
                    "expires_at": expires_at,
                    "payment_lari": payment_lari,
                    "payment_rub": payment_rub,
                    "city_topic": city_topic,
                    "comment": comment,
                }.items():
                    if val:
                        updates[key] = val

            cells = [
                gspread.Cell(row, COL[field], value)
                for field, value in updates.items()
                if field in COL
            ]
            if cells:
                self.crm.update_cells(cells, value_input_option="USER_ENTERED")
                logger.info(f"CRM {'created' if is_new else 'updated'}: {username}")
        except Exception as e:
            logger.error(f"CRM upsert error for {username}: {e}")

    def mark_trial_started(self, username: str):
        today = datetime.now().strftime("%Y-%m-%d")
        trial_end = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
        self.upsert_client(
            username,
            trial="Да",
            status=STATUS_TRIAL,
            connected_at=today,
            tariff_days="3",
            expires_at=trial_end,
        )

    # ------------------------------------------------------------------ #
    #  CRM: экспирация                                                     #
    # ------------------------------------------------------------------ #

    def get_expiring_clients(self, days_ahead: int) -> list[dict]:
        """Клиенты, у которых expires_at = сегодня + days_ahead."""
        try:
            target = (datetime.now() + timedelta(days=days_ahead)).date()
            all_rows = self.crm.get_all_values()
            results = []
            for row in all_rows[Config.CRM_DATA_START_ROW - 1:]:
                if len(row) < COL["expires_at"]:
                    continue
                username   = row[COL["username"] - 1].strip()
                status     = row[COL["status"] - 1].strip()
                expires_str = row[COL["expires_at"] - 1].strip()
                tariff     = row[COL["tariff_days"] - 1].strip()
                chat_id    = row[COL["chat_id"] - 1].strip() if len(row) >= COL["chat_id"] else ""

                if not expires_str or not username:
                    continue
                try:
                    exp_date = datetime.strptime(expires_str[:10], "%Y-%m-%d").date()
                    if exp_date == target:
                        results.append({
                            "username": username,
                            "chat_id": chat_id,
                            "status": status,
                            "expires_at": expires_str[:10],
                            "tariff_days": tariff,
                        })
                except ValueError:
                    continue
            return results
        except Exception as e:
            logger.error(f"get_expiring_clients error: {e}")
            return []

    # ------------------------------------------------------------------ #
    #  История диалогов                                                    #
    # ------------------------------------------------------------------ #

    def _hist_find_row(self, chat_id: int) -> Optional[int]:
        """Найти строку в листе истории по chat_id."""
        try:
            cell = self.hist.find(str(chat_id), in_column=HIST_COL_CHAT_ID)
            return cell.row
        except gspread.exceptions.CellNotFound:
            return None

    def _hist_next_row(self) -> int:
        col_values = self.hist.col_values(HIST_COL_CHAT_ID)
        for i in range(len(col_values) - 1, -1, -1):
            if col_values[i].strip():
                return i + 2
        return 2  # строка 1 — заголовки

    def _hist_next_msg_col(self, row: int) -> int:
        """Найти первую пустую колонку после B в строке клиента."""
        row_values = self.hist.row_values(row)
        # Заполненные колонки включая A и B
        filled = len(row_values)
        # Следующая пустая колонка (1-indexed), минимум C=3
        return max(filled + 1, HIST_DATA_START)

    def history_ensure_client(self, username: str, chat_id: int):
        """Создать строку клиента в истории если её нет."""
        try:
            row = self._hist_find_row(chat_id)
            if row is None:
                row = self._hist_next_row()
                self.hist.update_cell(row, HIST_COL_USERNAME, username)
                self.hist.update_cell(row, HIST_COL_CHAT_ID, str(chat_id))
                logger.info(f"History row created for {username} ({chat_id})")
        except Exception as e:
            logger.error(f"history_ensure_client error: {e}")

    def history_append_message(self, chat_id: int, role: str, text: str):
        """
        Добавить одно сообщение в следующую свободную колонку строки клиента.
        role: "👤" (клиент) или "🤖" (бот)
        Формат ячейки: "[HH:MM] role: text"
        """
        try:
            row = self._hist_find_row(chat_id)
            if row is None:
                logger.warning(f"No history row for chat_id={chat_id}, skipping")
                return
            col = self._hist_next_msg_col(row)
            ts = datetime.now().strftime("%H:%M")
            cell_value = f"[{ts}] {role}: {text[:500]}"  # обрезаем длинные сообщения
            self.hist.update_cell(row, col, cell_value)
        except Exception as e:
            logger.error(f"history_append_message error: {e}")

    def history_get_client_chat_id(self, username: str) -> Optional[int]:
        """Получить chat_id из листа истории по username (для проактивных сообщений)."""
        try:
            cell = self.hist.find(username, in_column=HIST_COL_USERNAME)
            chat_id_str = self.hist.cell(cell.row, HIST_COL_CHAT_ID).value
            return int(chat_id_str) if chat_id_str else None
        except Exception:
            return None


sheets = SheetsClient()

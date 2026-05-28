import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Telegram
    BOT_TOKEN: str = os.environ["BOT_TOKEN"]
    OWNER_CHAT_ID: int = int(os.environ["OWNER_CHAT_ID"])

    # Gemini
    GEMINI_API_KEY: str = os.environ["GEMINI_API_KEY"]
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

    # Google Sheets
    GOOGLE_SHEET_ID: str = os.environ["GOOGLE_SHEET_ID"]
    GOOGLE_CREDENTIALS_JSON: str = os.environ["GOOGLE_CREDENTIALS_JSON"]

    # Sheet names
    CRM_SHEET_NAME: str = "👥 CRM клиентов"
    HISTORY_SHEET_NAME: str = "💬 История диалогов"

    # CRM row config
    CRM_DATA_START_ROW: int = 6
    CRM_HEADER_ROW: int = 5

    # Notification / scheduler
    TRIAL_WARN_DAYS: int = int(os.getenv("TRIAL_WARN_DAYS", "1"))
    SUBSCRIPTION_WARN_DAYS: int = int(os.getenv("SUBSCRIPTION_WARN_DAYS", "3"))
    SCHEDULER_HOUR: int = int(os.getenv("SCHEDULER_HOUR", "10"))  # UTC

    # Pricing
    PRICE_REGULAR_USD: int = 24
    PRICE_TRIAL_DISCOUNT_USD: int = 19

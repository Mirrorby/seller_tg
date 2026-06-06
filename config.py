import os
from dotenv import load_dotenv
import base64

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
    GOOGLE_CREDENTIALS_B64: str = os.environ["GOOGLE_CREDENTIALS_B64"]
    GOOGLE_CREDENTIALS_JSON: str = base64.b64decode(
        GOOGLE_CREDENTIALS_B64
    ).decode("utf-8")

    # Sheet names
    CRM_SHEET_NAME: str = "CRM"
    HISTORY_SHEET_NAME: str = "💬 История диалогов"

    # CRM row config
    CRM_DATA_START_ROW: int = 6
    CRM_HEADER_ROW: int = 5

    # Notification / scheduler
    TRIAL_WARN_DAYS: int = int(os.getenv("TRIAL_WARN_DAYS", "1"))
    SUBSCRIPTION_WARN_DAYS: int = int(os.getenv("SUBSCRIPTION_WARN_DAYS", "3"))
    SCHEDULER_HOUR: int = int(os.getenv("SCHEDULER_HOUR", "10"))  # UTC

    # Pricing (legacy — теперь в payment_manager.py TARIFFS)
    PRICE_REGULAR_USD: int = 19
    PRICE_TRIAL_DISCOUNT_USD: int = 0

    # ------------------------------------------------------------------ #
    # Lava.top (карточная оплата)                                         #
    # ------------------------------------------------------------------ #
    # Получить в lava.top → Интеграции → API → Создать ключ
    LAVA_API_KEY: str = os.getenv("LAVA_API_KEY", "")

    # Секрет для верификации вебхуков (задаётся при создании вебхука в Lava)
    LAVA_WEBHOOK_SECRET: str = os.getenv("LAVA_WEBHOOK_SECRET", "")

    # Offer ID: UUID продукта в Lava.top (Контент → ваш продукт → Настройки → ID)
    LAVA_OFFER_ID: str = os.getenv("LAVA_OFFER_ID", "")

    # ------------------------------------------------------------------ #
    # Crypto Bot (крипто-оплата)                                          #
    # ------------------------------------------------------------------ #
    # Получить: https://t.me/CryptoBot → /pay → Create App → API Token
    CRYPTO_BOT_TOKEN: str = os.getenv("CRYPTO_BOT_TOKEN", "")

    # True = тестнет CryptoBot (https://t.me/CryptoTestnetBot)
    CRYPTO_BOT_TESTNET: bool = os.getenv("CRYPTO_BOT_TESTNET", "false").lower() == "true"

    # ------------------------------------------------------------------ #
    # Webhook сервер                                                       #
    # ------------------------------------------------------------------ #
    # Railway сам выставляет PORT; fallback 8080
    WEBHOOK_PORT: int = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080")))

    # Email клиента по умолчанию (если Lava требует email, а у нас его нет)
    DEFAULT_EMAIL_DOMAIN: str = os.getenv("DEFAULT_EMAIL_DOMAIN", "tg.placeholder.com")

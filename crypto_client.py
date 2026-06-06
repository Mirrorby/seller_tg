"""
crypto_client.py — интеграция с Crypto Bot (https://t.me/CryptoBot)
API: https://help.crypt.bot/crypto-pay-api

Принцип работы:
1. Создаём инвойс через createInvoice → получаем pay_url
2. Отправляем клиенту ссылку
3. Polling каждые N секунд проверяет статус через getInvoices
4. При статусе "paid" → активируем подписку

Поддерживаемые монеты: USDT, TON, BTC, ETH, LTC, BNB, TRX, USDC

Для получения токена: https://t.me/CryptoBot → /pay → Create App
"""

import asyncio
import logging
from typing import Optional

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

CRYPTO_BOT_MAINNET = "https://pay.crypt.bot/api"
CRYPTO_BOT_TESTNET = "https://testnet-pay.crypt.bot/api"


class CryptoBotClient:
    """
    Async-клиент для Crypto Pay API.
    """

    def __init__(self):
        self.token = Config.CRYPTO_BOT_TOKEN
        self.base_url = (
            CRYPTO_BOT_TESTNET
            if getattr(Config, "CRYPTO_BOT_TESTNET", False)
            else CRYPTO_BOT_MAINNET
        )
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Crypto-Pay-API-Token": self.token}
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ #
    # Низкоуровневые запросы                                               #
    # ------------------------------------------------------------------ #

    async def _get(self, method: str, params: dict = None) -> dict:
        url = f"{self.base_url}/{method}"
        async with self.session.get(url, params=params or {}) as resp:
            data = await resp.json()
            if not data.get("ok"):
                raise RuntimeError(
                    f"CryptoBot API error [{method}]: {data.get('error', data)}"
                )
            return data["result"]

    async def _post(self, method: str, body: dict) -> dict:
        url = f"{self.base_url}/{method}"
        async with self.session.post(url, json=body) as resp:
            data = await resp.json()
            if not data.get("ok"):
                raise RuntimeError(
                    f"CryptoBot API error [{method}]: {data.get('error', data)}"
                )
            return data["result"]

    # ------------------------------------------------------------------ #
    # Создание инвойса                                                     #
    # ------------------------------------------------------------------ #

    async def create_invoice(
        self,
        *,
        asset: str = "USDT",       # "USDT" | "TON" | "BTC" | "ETH" | ...
        amount: float,              # в единицах asset
        description: str = "",
        hidden_message: str = "",   # показывается после оплаты
        payload: str = "",          # произвольная метка (например username + tariff)
        expires_in: int = 3600,     # секунды до истечения (макс 86400)
        allow_comments: bool = False,
        allow_anonymous: bool = False,
    ) -> dict:
        """
        Создать инвойс.
        Возвращает dict с ключами:
            invoice_id, status, asset, amount, pay_url, created_at, expiration_date, payload
        """
        body = {
            "asset": asset,
            "amount": str(amount),
            "description": description[:1024] if description else "",
            "payload": payload[:4096] if payload else "",
            "expires_in": expires_in,
            "allow_comments": allow_comments,
            "allow_anonymous": allow_anonymous,
        }
        if hidden_message:
            body["hidden_message"] = hidden_message[:2048]

        result = await self._post("createInvoice", body)
        logger.info(
            f"CryptoBot invoice created: id={result['invoice_id']} "
            f"asset={asset} amount={amount} url={result.get('pay_url')}"
        )
        return result

    # ------------------------------------------------------------------ #
    # Проверка статуса                                                     #
    # ------------------------------------------------------------------ #

    async def get_invoice(self, invoice_id: int) -> Optional[dict]:
        """Получить инвойс по ID."""
        result = await self._get("getInvoices", {"invoice_ids": str(invoice_id)})
        items = result.get("items", [])
        return items[0] if items else None

    async def get_invoices(
        self,
        *,
        asset: str = None,
        invoice_ids: list[int] = None,
        status: str = None,         # "active" | "paid" | "expired"
        offset: int = 0,
        count: int = 100,
    ) -> list[dict]:
        """Получить список инвойсов с фильтрацией."""
        params: dict = {"offset": offset, "count": count}
        if asset:
            params["asset"] = asset
        if invoice_ids:
            params["invoice_ids"] = ",".join(str(i) for i in invoice_ids)
        if status:
            params["status"] = status

        result = await self._get("getInvoices", params)
        return result.get("items", [])

    # ------------------------------------------------------------------ #
    # Конкретные тарифы для подписки                                       #
    # ------------------------------------------------------------------ #

    async def create_subscription_invoice(
        self,
        *,
        username: str,
        tariff_label: str,      # "30 дней" / "90 дней" / "Trial 3 дня"
        amount_usd: float,
        asset: str = "USDT",
    ) -> dict:
        """
        Хелпер: создать инвойс под подписку.
        payload = "username|tariff_label" для разбора в webhook.
        """
        payload = f"{username}|{tariff_label}"
        description = (
            f"Лид-витрина — {tariff_label}\n"
            f"Аренда Батуми/Тбилиси • {username}"
        )
        hidden_message = (
            "✅ Оплата получена! Бот активируется в течение нескольких минут."
        )
        return await self.create_invoice(
            asset=asset,
            amount=amount_usd,
            description=description,
            hidden_message=hidden_message,
            payload=payload,
            expires_in=86400,   # 24 часа
        )

    # ------------------------------------------------------------------ #
    # Polling-чекер активных инвойсов                                     #
    # ------------------------------------------------------------------ #

    async def poll_until_paid(
        self,
        invoice_id: int,
        *,
        timeout: int = 86400,       # 24 часа
        interval: int = 30,         # проверять каждые 30 сек
    ) -> Optional[dict]:
        """
        Ждёт оплаты инвойса в фоновом режиме.
        Возвращает объект инвойса при статусе 'paid', None при истечении.
        Вызывается как asyncio.create_task(crypto.poll_until_paid(...))
        """
        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(interval)
            elapsed += interval
            try:
                inv = await self.get_invoice(invoice_id)
                if inv is None:
                    logger.warning(f"CryptoBot: invoice {invoice_id} not found")
                    return None
                if inv["status"] == "paid":
                    logger.info(f"CryptoBot: invoice {invoice_id} PAID")
                    return inv
                if inv["status"] == "expired":
                    logger.info(f"CryptoBot: invoice {invoice_id} expired")
                    return None
            except Exception as e:
                logger.error(f"CryptoBot poll error for {invoice_id}: {e}")

        logger.warning(f"CryptoBot: poll timeout for invoice {invoice_id}")
        return None

    # ------------------------------------------------------------------ #
    # Проверка токена                                                      #
    # ------------------------------------------------------------------ #

    async def get_me(self) -> dict:
        """Проверить работоспособность токена."""
        return await self._get("getMe")


crypto = CryptoBotClient()

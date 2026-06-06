"""
lava_client.py — интеграция с Lava.top (gate.lava.top)
Карточная оплата: RUB через BANK131, USD/EUR через UNLIMINT/STRIPE

Документация: https://gate.lava.top/docs
SDK: pip install lava-top-sdk
"""

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Optional

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

LAVA_BASE = "https://gate.lava.top"


class LavaClient:
    """
    Async-обёртка над Lava.top Public API.
    Не использует синхронный lava-top-sdk, чтобы не блокировать event loop бота.
    """

    def __init__(self):
        self.api_key = Config.LAVA_API_KEY
        self.webhook_secret = Config.LAVA_WEBHOOK_SECRET
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "X-Api-Key": self.api_key,
                    "Content-Type": "application/json",
                }
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ #
    # Создание платежа                                                     #
    # ------------------------------------------------------------------ #

    async def create_payment(
        self,
        *,
        email: str,
        offer_id: str,
        amount_usd: float,
        buyer_name: str = "",
        buyer_tg: str = "",
        currency: str = "USD",          # "RUB" | "USD" | "EUR"
        payment_method: str = "STRIPE", # "BANK131" для RUB, "STRIPE"/"UNLIMINT" для USD/EUR
        custom_fields: Optional[dict] = None,
    ) -> dict:
        """
        Создать инвойс на разовую оплату.
        Возвращает dict с ключами: id, paymentUrl, status
        """
        body: dict = {
            "email": email,
            "offerId": offer_id,
            "currency": currency,
            "paymentMethod": payment_method,
            "buyerLanguage": "RU",
        }
        if custom_fields:
            body["clientUtm"] = custom_fields

        try:
            async with self.session.post(
                f"{LAVA_BASE}/business/invoice", json=body
            ) as resp:
                data = await resp.json()
                if resp.status not in (200, 201):
                    logger.error(f"Lava create_payment error {resp.status}: {data}")
                    raise RuntimeError(f"Lava API error: {data.get('message', data)}")
                logger.info(
                    f"Lava invoice created: id={data.get('id')} url={data.get('paymentUrl')}"
                )
                return data
        except aiohttp.ClientError as e:
            logger.error(f"Lava network error: {e}")
            raise

    # ------------------------------------------------------------------ #
    # Проверка статуса                                                     #
    # ------------------------------------------------------------------ #

    async def get_invoice(self, invoice_id: str) -> dict:
        """Получить статус инвойса по ID."""
        async with self.session.get(
            f"{LAVA_BASE}/business/invoice/{invoice_id}"
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"Lava get_invoice error {resp.status}: {data}")
            return data

    # ------------------------------------------------------------------ #
    # Верификация webhook                                                  #
    # ------------------------------------------------------------------ #

    def verify_webhook(self, body_bytes: bytes, signature: str) -> bool:
        """
        Проверить подпись входящего вебхука от Lava.
        Lava подписывает: HMAC-SHA256(body, webhook_secret)
        """
        if not self.webhook_secret:
            logger.warning("LAVA_WEBHOOK_SECRET not set — skipping webhook verification")
            return True
        expected = hmac.new(
            self.webhook_secret.encode(),
            body_bytes,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------ #
    # Разбор webhook-события                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def parse_webhook_event(data: dict) -> dict:
        """
        Нормализованный словарь из webhook payload Lava.
        Поля: event_type, invoice_id, contract_id, status, email, amount, currency
        """
        return {
            "event_type": data.get("eventType", ""),
            "invoice_id": data.get("invoiceId") or data.get("id", ""),
            "contract_id": data.get("contractId", ""),
            "status": data.get("status", ""),
            "email": data.get("buyerEmail", ""),
            "amount": data.get("amount", 0),
            "currency": data.get("currency", ""),
            "raw": data,
        }


lava = LavaClient()

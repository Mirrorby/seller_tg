"""
lava_client.py — интеграция с Lava.top

Правильный endpoint (из официальной документации):
  POST https://gate.lava.top/api/v3/invoice
  Header: X-Api-Key: <ключ>

Тело запроса: email, offerId, currency, buyerLanguage
"""

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

    def __init__(self):
        self.api_key        = Config.LAVA_API_KEY
        self.webhook_secret = Config.LAVA_WEBHOOK_SECRET
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "X-Api-Key": self.api_key,
                    "Content-Type": "application/json",
                    "accept": "application/json",
                }
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ #
    # Создание инвойса                                                     #
    # ------------------------------------------------------------------ #

    async def create_payment(
        self,
        *,
        email: str,
        offer_id: str,
        amount_usd: float,
        currency: str = "USD",
        custom_fields: Optional[dict] = None,
    ) -> dict:
        body: dict = {
            "email": email,
            "offerId": offer_id,
            "currency": currency,
            "buyerLanguage": "RU",
        }
    
        if custom_fields:
            body["clientUtm"] = custom_fields
    
        url = f"{LAVA_BASE}/api/v3/invoice"
        try:
            async with self.session.post(url, json=body) as resp:
                raw = await resp.text()
                logger.debug(f"Lava {resp.status}: {raw[:500]}")

                if resp.status not in (200, 201):
                    logger.error(f"Lava create_payment HTTP {resp.status}: {raw[:300]}")
                    raise RuntimeError(f"Lava HTTP {resp.status}: {raw[:200]}")

                data = json.loads(raw)
                logger.info(
                    f"Lava invoice created: id={data.get('id')} url={data.get('paymentUrl')}"
                )
                return data

        except aiohttp.ClientError as e:
            logger.error(f"Lava network error: {e}")
            raise

    # ------------------------------------------------------------------ #
    # Верификация webhook                                                  #
    # ------------------------------------------------------------------ #

    def verify_webhook(self, body_bytes: bytes, signature: str) -> bool:
        """Lava шлёт X-Api-Key вместо HMAC-подписи."""
        if not self.webhook_secret:
            logger.warning("LAVA_WEBHOOK_SECRET не задан — пропускаем верификацию")
            return True
        return hmac.compare_digest(signature, self.webhook_secret)

    # ------------------------------------------------------------------ #
    # Разбор webhook-события                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def parse_webhook_event(data: dict) -> dict:
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

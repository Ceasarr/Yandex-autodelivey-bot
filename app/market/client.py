"""Клиент Яндекс Маркет Partner API.

Покрывает методы, нужные для автовыдачи цифровых товаров:
  * getBusinessOrders     — POST /v1/businesses/{businessId}/orders
  * provideOrderDigitalCodes — POST /v2/campaigns/{campaignId}/orders/{orderId}/deliverDigitalGoods

Авторизация: заголовок `Api-Key: <токен>` (см. спецификацию OpenAPI Яндекса).
Лимиты: 10 000 запросов/час, параллелизм 6. Клиент уважает их через семафор и
backoff при ответе 420 (превышение лимита) и 5xx.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Маркет допускает не более 6 параллельных запросов на ресурс
_MAX_PARALLEL = 6
# Сколько раз повторять запрос при временных ошибках (420/5xx/сеть)
_MAX_RETRIES = 4


class MarketApiError(Exception):
    """Ошибка API Маркета."""

    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message
        self.payload = payload


class DigitalItem:
    """Позиция цифрового товара для передачи ключей."""

    def __init__(self, item_id: int, codes: list[str], slip: str, activate_till: str):
        self.item_id = item_id
        self.codes = codes
        self.slip = slip
        self.activate_till = activate_till

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "codes": self.codes,
            "slip": self.slip,
            "activate_till": self.activate_till,
        }


class MarketClient:
    """Асинхронный клиент Partner API для одного магазина (один Api-Key)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.partner.market.yandex.ru",
        session: aiohttp.ClientSession | None = None,
    ):
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._session = session
        self._own_session = session is None
        self._semaphore = asyncio.Semaphore(_MAX_PARALLEL)

    async def __aenter__(self) -> "MarketClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Api-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True

        url = f"{self._base}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with self._semaphore:
                    async with self._session.request(
                        method,
                        url,
                        json=json_body,
                        headers=self._headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        text = await resp.text()
                        # Успех
                        if resp.status == 200:
                            return _safe_json(text)
                        # Превышение лимита запросов — ждём и повторяем
                        if resp.status == 420:
                            delay = _backoff_delay(attempt)
                            logger.warning(
                                "Лимит запросов (420) на %s, повтор через %.1fs",
                                path, delay,
                            )
                            await asyncio.sleep(delay)
                            continue
                        # Временная ошибка сервера — повтор
                        if resp.status >= 500:
                            delay = _backoff_delay(attempt)
                            logger.warning(
                                "Ошибка сервера %s на %s, повтор через %.1fs",
                                resp.status, path, delay,
                            )
                            await asyncio.sleep(delay)
                            continue
                        # Остальные коды — не повторяем
                        raise MarketApiError(
                            resp.status, _extract_error(text), _safe_json(text)
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                delay = _backoff_delay(attempt)
                logger.warning("Сетевая ошибка на %s: %s, повтор через %.1fs",
                               path, exc, delay)
                await asyncio.sleep(delay)

        if last_exc is not None:
            raise MarketApiError(0, f"Сеть недоступна: {last_exc}")
        raise MarketApiError(429, "Исчерпаны попытки запроса (лимиты/5xx)")

    # ---- Методы API ----

    async def get_business_orders(
        self,
        business_id: int,
        statuses: list[str] | None = None,
        substatuses: list[str] | None = None,
        page_token: str | None = None,
        limit: int = 50,
        fake: bool | None = None,
    ) -> dict[str, Any]:
        """POST /v1/businesses/{businessId}/orders — список заказов кабинета."""
        path = f"/v1/businesses/{business_id}/orders?limit={limit}"
        if page_token:
            path += f"&page_token={page_token}"
        body: dict[str, Any] = {}
        if statuses:
            body["statuses"] = statuses
        if substatuses:
            body["substatuses"] = substatuses
        if fake is not None:
            body["fake"] = fake
        return await self._request("POST", path, body)

    async def provide_digital_codes(
        self, campaign_id: int, order_id: int, items: list[DigitalItem]
    ) -> dict[str, Any]:
        """POST /v2/campaigns/{campaignId}/orders/{orderId}/deliverDigitalGoods.

        Передаёт ключи цифровых товаров покупателю. Ответ 200 не гарантирует
        доставку: финальное подтверждение — переход заказа в статус DELIVERED.
        """
        path = (
            f"/v2/campaigns/{campaign_id}/orders/{order_id}/deliverDigitalGoods"
        )
        body = {"items": [item.to_payload() for item in items]}
        return await self._request("POST", path, body)


def _safe_json(text: str) -> dict[str, Any]:
    import json

    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


def _extract_error(text: str) -> str:
    data = _safe_json(text)
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list) and errors:
        parts = [
            f"{e.get('code', '')}: {e.get('message', '')}".strip(": ")
            for e in errors
            if isinstance(e, dict)
        ]
        return "; ".join(p for p in parts if p) or "Неизвестная ошибка"
    return data.get("_raw", "Неизвестная ошибка") if isinstance(data, dict) else "Неизвестная ошибка"


def _backoff_delay(attempt: int) -> float:
    """Экспоненциальный backoff: 2, 4, 8, 16 ... секунд (без случайности)."""
    return float(2 ** attempt)

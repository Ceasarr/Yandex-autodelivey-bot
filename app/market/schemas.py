"""Лёгкие модели разбора ответов Partner API.

Берём только те поля, что нужны для автовыдачи цифровых товаров.
Поля ответа подтверждены по OpenAPI-спецификации Яндекса
(BusinessOrderDTO / OrderItemDTO / GetBusinessOrdersResponse).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedItem:
    item_id: int          # OrderItemDTO.id — нужен для deliverDigitalGoods.items[].id
    offer_id: str         # OrderItemDTO.offerId — SKU продавца, по нему ищем товар
    offer_name: str
    count: int


@dataclass
class ParsedOrder:
    order_id: int         # OrderDTO.id
    status: str           # OrderStatusType, напр. PROCESSING
    substatus: str        # OrderSubstatusType, напр. STARTED
    fake: bool
    items: list[ParsedItem] = field(default_factory=list)


def parse_orders(payload: dict[str, Any]) -> tuple[list[ParsedOrder], str | None]:
    """Разбирает ответ getBusinessOrders.

    Возвращает (список заказов, токен следующей страницы | None).
    """
    orders: list[ParsedOrder] = []
    for raw in payload.get("orders", []) or []:
        items = [
            ParsedItem(
                item_id=int(it["id"]),
                offer_id=str(it.get("offerId", "")),
                offer_name=str(it.get("offerName", "")),
                count=int(it.get("count", 1)),
            )
            for it in raw.get("items", []) or []
            if it.get("id") is not None
        ]
        orders.append(
            ParsedOrder(
                # getBusinessOrders (v1) возвращает orderId; у старого
                # getOrders (v2) было id — поддерживаем оба на всякий случай.
                order_id=int(raw.get("orderId") or raw["id"]),
                status=str(raw.get("status", "")),
                substatus=str(raw.get("substatus", "")),
                fake=bool(raw.get("fake", False)),
                items=items,
            )
        )

    paging = payload.get("paging") or {}
    next_token = paging.get("nextPageToken") or None
    return orders, next_token

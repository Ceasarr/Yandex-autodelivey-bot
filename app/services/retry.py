"""Переотправка заказов, которые не удалось выдать автоматически (FAILED).

Используется кнопкой в Telegram-боте. Восстанавливает заказ и его позиции из
БД, формирует ParsedOrder и повторно прогоняет его через dispenser — например,
после того как администратор пополнил пул ключей.
"""
from __future__ import annotations

import logging

import aiohttp
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import ShopConfig, settings
from app.db.models import Order, OrderState, Shop
from app.db.session import get_session
from app.market.client import MarketClient
from app.market.schemas import ParsedItem, ParsedOrder
from app.services.dispenser import DispenseResult, dispense_order

logger = logging.getLogger(__name__)


def _shop_config(shop: Shop) -> ShopConfig | None:
    """Находит конфиг магазина (с api_key) по его slug."""
    for cfg in settings.configured_shops:
        if cfg.slug == shop.slug:
            return cfg
    return None


async def retry_order(market_order_id: int) -> DispenseResult | None:
    """Повторно выдаёт ключи по ранее упавшему заказу.

    Возвращает результат выдачи или None, если заказ не найден / магазин не
    настроен / заказ не в состоянии, допускающем переотправку.
    """
    async with get_session() as session:
        res = await session.execute(
            select(Order)
            .where(Order.market_order_id == market_order_id)
            .options(selectinload(Order.items), selectinload(Order.shop))
        )
        order = res.scalar_one_or_none()
        if order is None:
            logger.warning("Переотправка: заказ %s не найден", market_order_id)
            return None

        # Переотправляем только заказы, которые ещё не доставлены
        if order.state == OrderState.DELIVERED:
            return DispenseResult(
                market_order_id, True, OrderState.DELIVERED, {},
                message="Заказ уже доставлен",
            )

        shop = order.shop
        cfg = _shop_config(shop)
        if cfg is None:
            logger.error("Переотправка: магазин %s не настроен (нет api_key)",
                         shop.slug)
            return DispenseResult(
                market_order_id, False, OrderState.FAILED, {},
                message=f"Магазин {shop.name} не настроен в .env",
            )

        parsed = ParsedOrder(
            order_id=order.market_order_id,
            status=order.market_status or "PROCESSING",
            substatus=order.market_substatus or "STARTED",
            fake=False,
            items=[
                ParsedItem(
                    item_id=it.market_item_id,
                    offer_id=it.offer_id,
                    offer_name=it.title,
                    count=it.count,
                )
                for it in order.items
            ],
        )
        # Сохраняем примитивы до закрытия сессии — объекты станут detached
        campaign_id = shop.campaign_id
        shop_id = shop.id
        shop_name = shop.name

    # Сетевой вызов вне сессии-чтения: dispense_order открывает свою работу с БД
    async with aiohttp.ClientSession() as http:
        client = MarketClient(cfg.api_key, settings.market_api_base, session=http)
        async with get_session() as session:
            shop = (await session.execute(
                select(Shop).where(Shop.id == shop_id)
            )).scalar_one()
            return await dispense_order(session, client, shop, campaign_id, parsed)

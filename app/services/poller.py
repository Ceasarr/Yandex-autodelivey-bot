"""Поллер заказов: периодически опрашивает Маркет по каждому магазину
и запускает выдачу ключей для новых оплаченных заказов.

Берём заказы в статусе PROCESSING / подстатусе STARTED — это подтверждённые
заказы, которые можно обрабатывать (передавать ключи). Для каждого нового
заказа вызывается dispenser. Результат уходит в notifier (Telegram).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp
from sqlalchemy import select

from app.config import ShopConfig, settings
from app.db.models import Order, OrderState, Shop
from app.db.session import get_session
from app.market.client import MarketApiError, MarketClient
from app.market.schemas import parse_orders
from app.services.dispenser import DispenseResult, dispense_order

logger = logging.getLogger(__name__)

# Тип колбэка уведомлений: получает результат выдачи и конфиг магазина
NotifyCallback = Callable[[DispenseResult, ShopConfig], Awaitable[None]]


async def _ensure_shop(shop_cfg: ShopConfig) -> int:
    """Создаёт/обновляет запись магазина в БД, возвращает его внутренний id."""
    async with get_session() as session:
        res = await session.execute(select(Shop).where(Shop.slug == shop_cfg.slug))
        shop = res.scalar_one_or_none()
        if shop is None:
            shop = Shop(
                slug=shop_cfg.slug,
                name=shop_cfg.name,
                business_id=shop_cfg.business_id,
                campaign_id=shop_cfg.campaign_id,
            )
            session.add(shop)
        else:
            shop.name = shop_cfg.name
            shop.business_id = shop_cfg.business_id
            shop.campaign_id = shop_cfg.campaign_id
        await session.commit()
        return shop.id


async def _poll_shop_once(
    shop_cfg: ShopConfig,
    http: aiohttp.ClientSession,
    notify: NotifyCallback | None,
) -> None:
    """Один проход опроса для одного магазина."""
    client = MarketClient(shop_cfg.api_key, settings.market_api_base, session=http)

    # В тест-режиме запрашиваем только тестовые заказы (fake=true) — за них
    # Маркет не берёт плату. В боевом режиме fake не передаём (по умолчанию
    # Маркет возвращает только настоящие заказы).
    fake_filter: bool | None = True if settings.test_mode else None

    page_token: str | None = None
    while True:
        try:
            payload = await client.get_business_orders(
                business_id=shop_cfg.business_id,
                statuses=["PROCESSING"],
                substatuses=["STARTED"],
                page_token=page_token,
                limit=50,
                fake=fake_filter,
            )
        except MarketApiError as exc:
            logger.error("Магазин %s: ошибка опроса заказов: %s", shop_cfg.slug, exc)
            return

        orders, page_token = parse_orders(payload)
        if not orders:
            return

        for parsed in orders:
            # Тест-режим: обрабатываем только тестовые заказы.
            # Боевой режим: только настоящие. Так тестовые и реальные заказы
            # никогда не смешиваются.
            if settings.test_mode != parsed.fake:
                continue
            await _process_order(shop_cfg, client, parsed, notify)

        if not page_token:
            return


async def _process_order(
    shop_cfg: ShopConfig,
    client: MarketClient,
    parsed,
    notify: NotifyCallback | None,
) -> None:
    """Обрабатывает один заказ, если он ещё не доставлен."""
    async with get_session() as session:
        # Загружаем магазин
        res = await session.execute(select(Shop).where(Shop.slug == shop_cfg.slug))
        shop = res.scalar_one()

        # Пропускаем уже доставленные/пропущенные заказы без повторной работы
        res = await session.execute(
            select(Order).where(
                Order.shop_id == shop.id,
                Order.market_order_id == parsed.order_id,
            )
        )
        existing = res.scalar_one_or_none()
        if existing is not None and existing.state in (
            OrderState.DELIVERED,
            OrderState.SKIPPED,
        ):
            return

        result = await dispense_order(
            session, client, shop, shop_cfg.campaign_id, parsed
        )

    # Уведомляем только о значимых событиях
    if notify is not None and result.state in (
        OrderState.DELIVERED,
        OrderState.FAILED,
    ):
        try:
            await notify(result, shop_cfg)
        except Exception:  # уведомления не должны ронять поллер
            logger.exception("Ошибка отправки уведомления по заказу %s",
                             result.order_id)


async def run_poller(notify: NotifyCallback | None = None) -> None:
    """Бесконечный цикл опроса всех настроенных магазинов."""
    shops = settings.configured_shops
    if not shops:
        logger.warning("Нет настроенных магазинов — поллер не запущен")
        return

    for shop_cfg in shops:
        await _ensure_shop(shop_cfg)

    mode = "ТЕСТ (только fake-заказы)" if settings.test_mode else "боевой"
    logger.info("Поллер запущен: %d магазин(ов), интервал %d сек, режим: %s",
                len(shops), settings.poll_interval_seconds, mode)

    async with aiohttp.ClientSession() as http:
        while True:
            for shop_cfg in shops:
                try:
                    await _poll_shop_once(shop_cfg, http, notify)
                except Exception:
                    logger.exception("Сбой опроса магазина %s", shop_cfg.slug)
            await asyncio.sleep(settings.poll_interval_seconds)

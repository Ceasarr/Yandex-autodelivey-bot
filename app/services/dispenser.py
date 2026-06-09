"""Сервис выдачи цифровых ключей по заказу.

Алгоритм для одного заказа:
  1. Найти/создать запись Order в БД (идемпотентность по market_order_id).
  2. Для каждой позиции заказа найти Product по offer_id магазина.
  3. В транзакции зарезервировать нужное число свободных ключей
     (status AVAILABLE -> RESERVED) — чтобы ключ не ушёл в два заказа.
  4. Вызвать deliverDigitalGoods со всеми позициями одним запросом.
  5. При 200 — пометить ключи DELIVERED, заказ DELIVERED.
     При ошибке — вернуть ключи в AVAILABLE, заказ FAILED.

Дедлайн Маркета: ключ нужно передать в течение 30 минут после перехода
заказа в PROCESSING. Поллер вызывает выдачу сразу при обнаружении заказа.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    Key,
    KeyStatus,
    Order,
    OrderItem,
    OrderState,
    Product,
    Shop,
)
from app.market.client import DigitalItem, MarketApiError, MarketClient
from app.market.schemas import ParsedOrder

logger = logging.getLogger(__name__)

# Глобальная блокировка резерва ключей. SQLite не поддерживает SELECT FOR UPDATE,
# а сервис работает в одном процессе с одним поллером, поэтому сериализуем
# критическую секцию (резерв свободных ключей) через asyncio.Lock — это
# гарантирует, что один ключ не уйдёт в два заказа одновременно.
_reserve_lock = asyncio.Lock()


@dataclass
class DispenseResult:
    order_id: int            # market_order_id
    success: bool
    state: OrderState
    delivered_counts: dict[str, int]   # offer_id -> сколько ключей выдано
    message: str = ""
    missing: dict[str, int] | None = None  # offer_id -> сколько ключей не хватило


async def _get_or_create_order(
    session: AsyncSession, shop: Shop, parsed: ParsedOrder
) -> Order:
    res = await session.execute(
        select(Order).where(
            Order.shop_id == shop.id,
            Order.market_order_id == parsed.order_id,
        )
    )
    order = res.scalar_one_or_none()
    if order is None:
        order = Order(
            shop_id=shop.id,
            market_order_id=parsed.order_id,
            market_status=parsed.status,
            market_substatus=parsed.substatus,
            state=OrderState.NEW,
            processing_at=datetime.now(timezone.utc),
        )
        session.add(order)
        await session.flush()
        for it in parsed.items:
            session.add(
                OrderItem(
                    order_id=order.id,
                    market_item_id=it.item_id,
                    offer_id=it.offer_id,
                    title=it.offer_name,
                    count=it.count,
                )
            )
        await session.flush()
    else:
        order.market_status = parsed.status
        order.market_substatus = parsed.substatus
    return order


async def _reserve_keys(
    session: AsyncSession, product: Product, order: Order, count: int
) -> list[Key]:
    """Резервирует до `count` свободных ключей товара под заказ.

    Возвращает список зарезервированных ключей (может быть меньше count,
    если ключей не хватает).
    """
    res = await session.execute(
        select(Key)
        .where(
            Key.product_id == product.id,
            Key.status == KeyStatus.AVAILABLE,
        )
        .order_by(Key.id)
        .limit(count)
    )
    keys = list(res.scalars().all())
    for k in keys:
        k.status = KeyStatus.RESERVED
        k.order_id = order.id
    await session.flush()
    return keys


def _activate_till(product: Product) -> str:
    days = product.activate_days or settings.default_activate_days
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.strftime("%Y-%m-%d")


async def dispense_order(
    session: AsyncSession,
    client: MarketClient,
    shop: Shop,
    campaign_id: int,
    parsed: ParsedOrder,
) -> DispenseResult:
    """Обрабатывает один заказ: резервирует ключи и передаёт их в Маркет."""
    order = await _get_or_create_order(session, shop, parsed)

    # Уже доставлен ранее — ничего не делаем (идемпотентность)
    if order.state == OrderState.DELIVERED:
        return DispenseResult(
            parsed.order_id, True, OrderState.DELIVERED, {},
            message="Заказ уже обработан ранее",
        )

    order.state = OrderState.PROCESSING
    order.attempts += 1

    digital_items: list[DigitalItem] = []
    reserved_keys: list[Key] = []
    delivered_counts: dict[str, int] = {}
    missing: dict[str, int] = {}

    # Критическая секция: резерв свободных ключей сериализуется блокировкой и
    # фиксируется в БД (AVAILABLE -> RESERVED) до выхода из неё, чтобы параллельная
    # обработка другого заказа не выбрала те же ключи.
    async with _reserve_lock:
        for it in parsed.items:
            # Ищем товар магазина по offer_id
            res = await session.execute(
                select(Product).where(
                    Product.shop_id == shop.id,
                    Product.offer_id == it.offer_id,
                )
            )
            product = res.scalar_one_or_none()
            if product is None:
                # Нет такого товара в системе — пропускаем позицию (не цифровой товар)
                logger.info(
                    "Магазин %s: позиция offer_id=%s не зарегистрирована, пропуск",
                    shop.slug, it.offer_id,
                )
                continue

            keys = await _reserve_keys(session, product, order, it.count)
            if len(keys) < it.count:
                missing[it.offer_id] = it.count - len(keys)

            if not keys:
                continue

            reserved_keys.extend(keys)
            delivered_counts[it.offer_id] = len(keys)
            digital_items.append(
                DigitalItem(
                    item_id=it.item_id,
                    codes=[k.code for k in keys],
                    slip=product.slip or "Спасибо за покупку!",
                    activate_till=_activate_till(product),
                )
            )

        # Если не набрали ни одного цифрового товара — это не наш заказ
        if not digital_items:
            # Откатываем возможные резервы (на случай частичного)
            await _release_keys(session, reserved_keys)
            if missing:
                order.state = OrderState.FAILED
                order.last_error = f"Недостаточно ключей: {missing}"
                await session.commit()
                return DispenseResult(
                    parsed.order_id, False, OrderState.FAILED, {},
                    message="Недостаточно ключей", missing=missing,
                )
            order.state = OrderState.SKIPPED
            order.last_error = ""
            await session.commit()
            return DispenseResult(
                parsed.order_id, True, OrderState.SKIPPED, {},
                message="В заказе нет цифровых товаров системы",
            )

        # Фиксируем резерв перед сетевым вызовом, чтобы при сбое процесса
        # ключи остались в RESERVED, а не потерялись
        await session.commit()

    # Передаём ключи в Маркет
    try:
        await client.provide_digital_codes(campaign_id, parsed.order_id, digital_items)
    except MarketApiError as exc:
        # Возвращаем ключи в пул, помечаем заказ как FAILED
        await _release_keys(session, reserved_keys)
        order.state = OrderState.FAILED
        order.last_error = str(exc)
        await session.commit()
        logger.error("Магазин %s: ошибка выдачи заказа %s: %s",
                     shop.slug, parsed.order_id, exc)
        return DispenseResult(
            parsed.order_id, False, OrderState.FAILED, {},
            message=f"Ошибка API: {exc}", missing=missing or None,
        )

    # Успех: помечаем ключи и заказ
    now = datetime.now(timezone.utc)
    for k in reserved_keys:
        k.status = KeyStatus.DELIVERED
        k.delivered_at = now
    order.state = OrderState.DELIVERED
    order.delivered_at = now
    order.last_error = ""
    await session.commit()

    return DispenseResult(
        parsed.order_id, True, OrderState.DELIVERED, delivered_counts,
        message="Ключи переданы в Маркет", missing=missing or None,
    )


async def _release_keys(session: AsyncSession, keys: list[Key]) -> None:
    """Возвращает ключи в пул (RESERVED -> AVAILABLE)."""
    if not keys:
        return
    ids = [k.id for k in keys]
    await session.execute(
        update(Key)
        .where(Key.id.in_(ids))
        .values(status=KeyStatus.AVAILABLE, order_id=None)
    )
    await session.flush()

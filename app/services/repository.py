"""Операции с БД для Telegram-бота: товары, ключи, статистика, переотправка."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select

from app.db.models import (
    Key,
    KeyStatus,
    Order,
    OrderState,
    Product,
    Shop,
)
from app.db.session import get_session


@dataclass
class ShopStock:
    shop_name: str
    shop_slug: str
    products: list["ProductStock"]


@dataclass
class ProductStock:
    product_id: int
    offer_id: str
    title: str
    available: int
    delivered: int
    low_threshold: int

    @property
    def is_low(self) -> bool:
        return self.available <= self.low_threshold


async def list_shops() -> list[Shop]:
    async with get_session() as session:
        res = await session.execute(select(Shop).order_by(Shop.id))
        return list(res.scalars().all())


async def get_shop_by_slug(slug: str) -> Shop | None:
    async with get_session() as session:
        res = await session.execute(select(Shop).where(Shop.slug == slug))
        return res.scalar_one_or_none()


async def upsert_product(
    shop_id: int,
    offer_id: str,
    title: str = "",
    slip: str = "",
    activate_days: int = 0,
) -> Product:
    """Создаёт или обновляет товар по (shop_id, offer_id)."""
    async with get_session() as session:
        res = await session.execute(
            select(Product).where(
                Product.shop_id == shop_id, Product.offer_id == offer_id
            )
        )
        product = res.scalar_one_or_none()
        if product is None:
            product = Product(
                shop_id=shop_id,
                offer_id=offer_id,
                title=title,
                slip=slip,
                activate_days=activate_days,
            )
            session.add(product)
        else:
            if title:
                product.title = title
            if slip:
                product.slip = slip
            if activate_days:
                product.activate_days = activate_days
        await session.commit()
        await session.refresh(product)
        return product


async def get_product(shop_id: int, offer_id: str) -> Product | None:
    async with get_session() as session:
        res = await session.execute(
            select(Product).where(
                Product.shop_id == shop_id, Product.offer_id == offer_id
            )
        )
        return res.scalar_one_or_none()


async def list_products(shop_id: int) -> list[tuple[Product, int, int]]:
    """Товары магазина со счётчиками ключей.

    Возвращает список (product, свободных_ключей, всего_ключей).
    """
    async with get_session() as session:
        products = (await session.execute(
            select(Product).where(Product.shop_id == shop_id).order_by(Product.id)
        )).scalars().all()
        result: list[tuple[Product, int, int]] = []
        for p in products:
            avail = (await session.execute(
                select(func.count()).select_from(Key)
                .where(Key.product_id == p.id, Key.status == KeyStatus.AVAILABLE)
            )).scalar_one()
            total = (await session.execute(
                select(func.count()).select_from(Key).where(Key.product_id == p.id)
            )).scalar_one()
            result.append((p, int(avail), int(total)))
        return result


async def get_product_by_id(product_id: int) -> Product | None:
    async with get_session() as session:
        return await session.get(Product, product_id)


async def list_available_codes(product_id: int) -> list[str]:
    """Все свободные (ещё не выданные) коды товара в порядке добавления."""
    async with get_session() as session:
        res = await session.execute(
            select(Key.code)
            .where(Key.product_id == product_id, Key.status == KeyStatus.AVAILABLE)
            .order_by(Key.id)
        )
        return [row[0] for row in res.all()]


async def delete_product(product_id: int) -> tuple[str, int] | None:
    """Удаляет товар (SKU) вместе со всеми его ключами.

    Возвращает (offer_id, число_удалённых_ключей) или None, если товара нет.
    """
    async with get_session() as session:
        product = await session.get(Product, product_id)
        if product is None:
            return None
        offer_id = product.offer_id
        keys_count = (await session.execute(
            select(func.count()).select_from(Key).where(Key.product_id == product_id)
        )).scalar_one()
        # Сначала ключи (на них ссылается FK), затем сам товар
        await session.execute(delete(Key).where(Key.product_id == product_id))
        await session.delete(product)
        await session.commit()
        return offer_id, int(keys_count)


async def add_keys
    deleted: list[str]              # коды, которые удалили
    not_found: list[str]            # кодов нет у этого SKU
    not_available: list[tuple[str, str]]  # (код, статус) — найден, но не свободен


async def delete_keys_by_codes(
    product_id: int, codes: list[str]
) -> DeleteKeysResult:
    """Удаляет несколько ключей товара по их кодам за один проход.

    Удаляются только свободные (AVAILABLE) ключи. Выданные/зарезервированные
    не трогаем, чтобы не терять историю выдачи покупателю. Дубликаты во вводе
    схлопываются, порядок сохраняется.
    """
    # Уникализируем, сохраняя порядок
    seen: set[str] = set()
    wanted: list[str] = []
    for raw in codes:
        c = raw.strip()
        if c and c not in seen:
            seen.add(c)
            wanted.append(c)

    result = DeleteKeysResult(deleted=[], not_found=[], not_available=[])
    if not wanted:
        return result

    async with get_session() as session:
        rows = (await session.execute(
            select(Key).where(Key.product_id == product_id, Key.code.in_(wanted))
        )).scalars().all()
        by_code = {k.code: k for k in rows}
        for code in wanted:
            key = by_code.get(code)
            if key is None:
                result.not_found.append(code)
            elif key.status != KeyStatus.AVAILABLE:
                result.not_available.append((code, key.status.value))
            else:
                await session.delete(key)
                result.deleted.append(code)
        if result.deleted:
            await session.commit()
    return result


async def add_keys(product_id: int, codes: list[str]) -> tuple[int, int]:
    """Добавляет ключи в пул товара.

    Возвращает (добавлено, пропущено_дубликатов).
    """
    added = 0
    skipped = 0
    async with get_session() as session:
        # Существующие коды этого товара — чтобы не плодить дубликаты
        res = await session.execute(
            select(Key.code).where(Key.product_id == product_id)
        )
        existing = {row[0] for row in res.all()}
        for code in codes:
            code = code.strip()
            if not code or code in existing:
                skipped += 1
                continue
            session.add(Key(product_id=product_id, code=code, status=KeyStatus.AVAILABLE))
            existing.add(code)
            added += 1
        await session.commit()
    return added, skipped


async def stock_overview() -> list[ShopStock]:
    """Сводка остатков по всем магазинам и товарам."""
    result: list[ShopStock] = []
    async with get_session() as session:
        shops = (await session.execute(select(Shop).order_by(Shop.id))).scalars().all()
        for shop in shops:
            products = (
                await session.execute(
                    select(Product).where(Product.shop_id == shop.id).order_by(Product.id)
                )
            ).scalars().all()
            product_stocks: list[ProductStock] = []
            for p in products:
                avail = (
                    await session.execute(
                        select(func.count())
                        .select_from(Key)
                        .where(Key.product_id == p.id, Key.status == KeyStatus.AVAILABLE)
                    )
                ).scalar_one()
                delivered = (
                    await session.execute(
                        select(func.count())
                        .select_from(Key)
                        .where(Key.product_id == p.id, Key.status == KeyStatus.DELIVERED)
                    )
                ).scalar_one()
                product_stocks.append(
                    ProductStock(
                        product_id=p.id,
                        offer_id=p.offer_id,
                        title=p.title,
                        available=int(avail),
                        delivered=int(delivered),
                        low_threshold=p.low_stock_threshold,
                    )
                )
            result.append(
                ShopStock(shop_name=shop.name, shop_slug=shop.slug, products=product_stocks)
            )
    return result


async def recent_orders(limit: int = 10) -> list[tuple[Order, str]]:
    """Последние заказы со статусом и названием магазина."""
    async with get_session() as session:
        res = await session.execute(
            select(Order, Shop.name)
            .join(Shop, Order.shop_id == Shop.id)
            .order_by(Order.updated_at.desc())
            .limit(limit)
        )
        return [(row[0], row[1]) for row in res.all()]


# ---- Редактирование инструкции (slip) ----

async def update_product_slip(product_id: int, slip: str) -> Product | None:
    """Обновляет текст инструкции (slip) товара.

    Возвращает обновлённый товар или None, если товара нет.
    """
    async with get_session() as session:
        product = await session.get(Product, product_id)
        if product is None:
            return None
        product.slip = slip
        await session.commit()
        await session.refresh(product)
        return product


# ---- Просмотр выданных ключей ----

@dataclass
class DeliveredKeyGroup:
    offer_id: str
    title: str
    codes: list[str]


@dataclass
class OrderKeys:
    market_order_id: int
    shop_name: str
    state: OrderState
    delivered_at: object  # datetime | None
    groups: list[DeliveredKeyGroup]

    @property
    def total_codes(self) -> int:
        return sum(len(g.codes) for g in self.groups)


async def get_order_keys(market_order_id: int) -> OrderKeys | None:
    """Какие ключи были выданы по заказу.

    Берёт ключи, привязанные к заказу и помеченные DELIVERED, сгруппированные
    по товару. Возвращает None, если заказа нет в базе.
    """
    async with get_session() as session:
        res = await session.execute(
            select(Order, Shop.name)
            .join(Shop, Order.shop_id == Shop.id)
            .where(Order.market_order_id == market_order_id)
            .order_by(Order.id.desc())
        )
        row = res.first()
        if row is None:
            return None
        order, shop_name = row[0], row[1]

        key_rows = (await session.execute(
            select(Product.offer_id, Product.title, Key.code)
            .join(Product, Key.product_id == Product.id)
            .where(Key.order_id == order.id, Key.status == KeyStatus.DELIVERED)
            .order_by(Product.offer_id, Key.id)
        )).all()

    grouped: dict[str, DeliveredKeyGroup] = {}
    for offer_id, title, code in key_rows:
        g = grouped.get(offer_id)
        if g is None:
            g = DeliveredKeyGroup(offer_id=offer_id, title=title or "", codes=[])
            grouped[offer_id] = g
        g.codes.append(code)

    return OrderKeys(
        market_order_id=order.market_order_id,
        shop_name=shop_name,
        state=order.state,
        delivered_at=order.delivered_at,
        groups=list(grouped.values()),
    )


async def recent_delivered_orders(limit: int = 10) -> list[tuple[Order, str]]:
    """Последние успешно выданные заказы (state=DELIVERED)."""
    async with get_session() as session:
        res = await session.execute(
            select(Order, Shop.name)
            .join(Shop, Order.shop_id == Shop.id)
            .where(Order.state == OrderState.DELIVERED)
            .order_by(Order.delivered_at.desc().nullslast(),
                      Order.updated_at.desc())
            .limit(limit)
        )
        return [(row[0], row[1]) for row in res.all()]

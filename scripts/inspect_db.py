"""Показывает текущее содержимое БД: магазины, товары, ключи, заказы."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app.db.models import Key, KeyStatus, Order, OrderItem, Product, Shop  # noqa: E402
from app.db.session import get_session  # noqa: E402


async def main() -> None:
    async with get_session() as s:
        shops = (await s.execute(select(Shop))).scalars().all()
        print(f"=== МАГАЗИНЫ ({len(shops)}) ===")
        for sh in shops:
            print(f"  [{sh.id}] {sh.slug} / {sh.name} (campaign={sh.campaign_id})")

        products = (await s.execute(select(Product))).scalars().all()
        print(f"\n=== ТОВАРЫ ({len(products)}) ===")
        for p in products:
            avail = (await s.execute(
                select(func.count()).select_from(Key)
                .where(Key.product_id == p.id, Key.status == KeyStatus.AVAILABLE)
            )).scalar_one()
            total = (await s.execute(
                select(func.count()).select_from(Key).where(Key.product_id == p.id)
            )).scalar_one()
            print(f"  [{p.id}] shop={p.shop_id} offer_id='{p.offer_id}' "
                  f"ключей: {avail} своб. / {total} всего")

        orders = (await s.execute(select(Order).order_by(Order.id))).scalars().all()
        print(f"\n=== ЗАКАЗЫ ({len(orders)}) ===")
        for o in orders:
            items = (await s.execute(
                select(OrderItem).where(OrderItem.order_id == o.id)
            )).scalars().all()
            offers = ", ".join(f"{it.offer_id}×{it.count}" for it in items)
            print(f"  market_id={o.market_order_id} state={o.state.value} "
                  f"[{offers}] err='{o.last_error[:60]}'")


if __name__ == "__main__":
    asyncio.run(main())

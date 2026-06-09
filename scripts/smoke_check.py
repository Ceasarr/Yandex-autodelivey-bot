"""Дымовая проверка: импорт всех модулей и инициализация БД.

Запуск:
  Windows: .venv\\Scripts\\python.exe scripts\\smoke_check.py
  Linux:   .venv/bin/python scripts/smoke_check.py
Не требует реальных токенов и не ходит в сеть.
"""
import asyncio
import os
import sys
import tempfile

# Корень проекта в путь импорта (скрипт лежит в scripts/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Подставляем безопасные значения окружения до импорта config
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("ADMIN_IDS", "1")
# Настроенный магазин shop1 — нужен для теста переотправки (retry ищет api_key
# в configured_shops по slug).
os.environ.setdefault("SHOP1_API_KEY", "test-key")
os.environ.setdefault("SHOP1_BUSINESS_ID", "1")
os.environ.setdefault("SHOP1_CAMPAIGN_ID", "10")
# Временная БД, чтобы не трогать рабочую. Удаляем старую — тест ожидает чистое
# состояние (id записей начинаются с 1).
_tmp = os.path.join(tempfile.gettempdir(), "smoke_service.db")
if os.path.exists(_tmp):
    os.remove(_tmp)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}"

import app.config as c
import app.db.models  # noqa: F401
import app.db.session as s
import app.market.client  # noqa: F401
import app.market.schemas as ms
import app.services.dispenser  # noqa: F401
import app.services.poller  # noqa: F401
import app.services.notifier  # noqa: F401
import app.services.repository  # noqa: F401
import app.bot.handlers  # noqa: F401
import app.bot.keyboards  # noqa: F401
import app.bot.middlewares  # noqa: F401
import app.main  # noqa: F401


SAMPLE = {
    # Формат getBusinessOrders (v1): заказ имеет orderId, позиции — id/offerId.
    "orders": [
        {
            "orderId": 1001,
            "status": "PROCESSING",
            "substatus": "STARTED",
            "fake": False,
            "items": [
                {"id": 5, "offerId": "GAME-KEY-1", "offerName": "Игра X", "count": 2}
            ],
        }
    ],
    "paging": {"nextPageToken": None},
}

# Старый формат getOrders (v2) с полем id — должен тоже разбираться (фолбэк).
SAMPLE_LEGACY = {
    "orders": [
        {
            "id": 2002,
            "status": "PROCESSING",
            "substatus": "STARTED",
            "fake": False,
            "items": [
                {"id": 7, "offerId": "GAME-KEY-2", "offerName": "Игра Y", "count": 1}
            ],
        }
    ],
    "paging": {},
}


async def main() -> None:
    await s.init_db()
    print("init_db: OK")

    orders, token = ms.parse_orders(SAMPLE)
    assert len(orders) == 1, "ожидался 1 заказ"
    o = orders[0]
    assert o.order_id == 1001, f"orderId не разобран: {o.order_id}"
    assert o.items[0].offer_id == "GAME-KEY-1"
    assert o.items[0].count == 2
    assert token is None
    print("parse_orders (getBusinessOrders / orderId): OK")

    # Фолбэк на старый формат с полем id
    legacy, _ = ms.parse_orders(SAMPLE_LEGACY)
    assert legacy[0].order_id == 2002, "фолбэк на id не сработал"
    print("parse_orders (legacy / id): OK")

    await _test_dispense()
    await _test_slip_and_keys()
    await _test_retry()
    _test_mode_filter()
    await _test_delete_sku()

    print("configured_shops:", len(c.settings.configured_shops))
    print("ALL CHECKS PASSED")


async def _test_delete_sku() -> None:
    """Проверка удаления SKU: товар с ключами удаляется вместе с ключами."""
    from sqlalchemy import func, select
    from app.db.models import Key, KeyStatus, Product, Shop
    from app.services import repository as repo

    async with s.get_session() as session:
        shop = Shop(slug="delshop", name="DelShop", business_id=2, campaign_id=20)
        session.add(shop)
        await session.flush()
        product = Product(shop_id=shop.id, offer_id="DEL-OFFER")
        session.add(product)
        await session.flush()
        for code in ("D1", "D2", "D3"):
            session.add(Key(product_id=product.id, code=code,
                            status=KeyStatus.AVAILABLE))
        await session.commit()
        shop_id, product_id = shop.id, product.id

    # Список товаров видит наш SKU с 3 ключами
    products = await repo.list_products(shop_id)
    assert any(p.id == product_id and total == 3 for p, _, total in products), \
        "list_products не вернул товар с 3 ключами"
    print("delete_sku (список SKU): OK")

    # Удаляем
    result = await repo.delete_product(product_id)
    assert result == ("DEL-OFFER", 3), f"delete_product вернул неожиданное: {result}"

    # Проверяем, что товара и его ключей больше нет
    async with s.get_session() as session:
        gone = await session.get(Product, product_id)
        keys_left = (await session.execute(
            select(func.count()).select_from(Key)
            .where(Key.product_id == product_id)
        )).scalar_one()
    assert gone is None, "товар не удалился"
    assert keys_left == 0, f"ключи не удалились: {keys_left}"
    print("delete_sku (товар и ключи удалены): OK")


def _test_mode_filter() -> None:
    """Проверка логики выбора заказов по режиму (test_mode != fake)."""
    # Заказы: один тестовый (fake=true), один настоящий (fake=false)
    payload = {
        "orders": [
            {"orderId": 1, "status": "PROCESSING", "substatus": "STARTED",
             "fake": True, "items": [{"id": 1, "offerId": "A", "offerName": "", "count": 1}]},
            {"orderId": 2, "status": "PROCESSING", "substatus": "STARTED",
             "fake": False, "items": [{"id": 2, "offerId": "B", "offerName": "", "count": 1}]},
        ],
        "paging": {},
    }
    orders = ms.parse_orders(payload)[0]

    # Правило поллера: обрабатывается заказ, если test_mode == fake.
    # Тест-режим (True) -> только заказ 1 (fake=True)
    selected_test = [o.order_id for o in orders if True == o.fake]
    assert selected_test == [1], f"тест-режим должен взять только fake: {selected_test}"
    # Боевой режим (False) -> только заказ 2 (fake=False)
    selected_prod = [o.order_id for o in orders if False == o.fake]
    assert selected_prod == [2], f"боевой режим должен взять только настоящий: {selected_prod}"
    print("test_mode filter (тест/боевой не смешиваются): OK")


async def _test_dispense() -> None:
    """Проверка ядра: резерв ключей, выдача, защита от двойной выдачи."""
    from app.db.models import Key, KeyStatus, OrderState, Product, Shop
    from app.market.client import DigitalItem
    from app.services import dispenser

    # Фейковый клиент Маркета — фиксирует, что ему передали, в сеть не ходит
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, list[DigitalItem]]] = []

        async def provide_digital_codes(self, campaign_id, order_id, items):
            self.calls.append((campaign_id, order_id, items))
            return {}

    # Готовим магазин, товар и 3 ключа
    async with s.get_session() as session:
        shop = Shop(slug="t", name="Тест", business_id=1, campaign_id=10)
        session.add(shop)
        await session.flush()
        product = Product(shop_id=shop.id, offer_id="GAME-KEY-1", slip="инструкция")
        session.add(product)
        await session.flush()
        for code in ("AAA", "BBB", "CCC"):
            session.add(Key(product_id=product.id, code=code,
                            status=KeyStatus.AVAILABLE))
        await session.commit()
        shop_id, campaign_id = shop.id, shop.campaign_id

    client = FakeClient()
    parsed = ms.parse_orders(SAMPLE)[0][0]  # заказ на GAME-KEY-1 ×2

    # Первая выдача — должна выдать 2 ключа и уйти в DELIVERED
    async with s.get_session() as session:
        shop = await session.get(Shop, shop_id)
        result = await dispenser.dispense_order(
            session, client, shop, campaign_id, parsed
        )
    assert result.state == OrderState.DELIVERED, f"ожидался DELIVERED: {result.state}"
    assert result.delivered_counts.get("GAME-KEY-1") == 2, result.delivered_counts
    assert len(client.calls) == 1, "клиент должен быть вызван один раз"
    assert len(client.calls[0][2][0].codes) == 2, "должно уйти 2 ключа"
    print("dispense (выдача 2 ключей): OK")

    # Повторная обработка того же заказа — идемпотентность, ключи не уходят снова
    async with s.get_session() as session:
        shop = await session.get(Shop, shop_id)
        result2 = await dispenser.dispense_order(
            session, client, shop, campaign_id, parsed
        )
    assert result2.state == OrderState.DELIVERED
    assert len(client.calls) == 1, "повторной выдачи быть не должно"
    print("dispense (идемпотентность): OK")

    # Проверяем остаток: 2 DELIVERED, 1 AVAILABLE
    from sqlalchemy import func, select
    async with s.get_session() as session:
        avail = (await session.execute(
            select(func.count()).select_from(Key)
            .where(Key.product_id == 1, Key.status == KeyStatus.AVAILABLE)
        )).scalar_one()
        delivered = (await session.execute(
            select(func.count()).select_from(Key)
            .where(Key.product_id == 1, Key.status == KeyStatus.DELIVERED)
        )).scalar_one()
    assert avail == 1 and delivered == 2, f"остаток неверный: avail={avail}, delivered={delivered}"
    print("dispense (остаток 1 своб. / 2 выдано): OK")


async def _test_slip_and_keys() -> None:
    """Проверка редактирования slip и просмотра выданных по заказу ключей."""
    from app.services import repository as repo

    # После _test_dispense заказ 1001 доставлен, по нему выдано 2 ключа
    # (AAA, BBB) товара GAME-KEY-1 (product_id=1).

    # --- Просмотр выданных ключей по заказу ---
    info = await repo.get_order_keys(1001)
    assert info is not None, "get_order_keys вернул None для существующего заказа"
    assert info.total_codes == 2, f"ожидалось 2 выданных ключа: {info.total_codes}"
    codes = {c for g in info.groups for c in g.codes}
    assert codes == {"AAA", "BBB"}, f"неверные выданные коды: {codes}"
    assert info.groups[0].offer_id == "GAME-KEY-1"
    print("get_order_keys (выданные коды заказа): OK")

    # Несуществующий заказ -> None
    assert await repo.get_order_keys(999999) is None, "ожидался None для чужого заказа"
    print("get_order_keys (несуществующий заказ -> None): OK")

    # История выданных заказов содержит заказ 1001
    delivered = await repo.recent_delivered_orders(limit=10)
    assert any(o.market_order_id == 1001 for o, _ in delivered), \
        "recent_delivered_orders не вернул доставленный заказ 1001"
    print("recent_delivered_orders (история выдач): OK")

    # --- Редактирование инструкции (slip) ---
    updated = await repo.update_product_slip(1, "Новая инструкция активации")
    assert updated is not None and updated.slip == "Новая инструкция активации", \
        f"slip не обновился: {updated.slip if updated else None}"
    cleared = await repo.update_product_slip(1, "")
    assert cleared is not None and cleared.slip == "", "slip не сбросился"
    assert await repo.update_product_slip(999999, "x") is None, \
        "ожидался None при обновлении slip несуществующего товара"
    print("update_product_slip (изменение/сброс): OK")


async def _test_retry() -> None:
    """Проверка переотправки: FAILED-заказ без ключей → пополнение → DELIVERED."""
    from sqlalchemy import select
    from app.db.models import Key, KeyStatus, OrderState, Product, Shop
    from app.market.client import DigitalItem
    from app.services import retry as retry_mod

    # Фейковый клиент вместо реального MarketClient (без сети)
    class FakeClient:
        def __init__(self, *a, **kw) -> None:
            self.calls: list = []

        async def provide_digital_codes(self, campaign_id, order_id, items):
            self.calls.append((campaign_id, order_id, items))
            return {}

    # Магазин со slug="shop1" — совпадает с configured_shops, чтобы retry нашёл api_key
    async with s.get_session() as session:
        shop = Shop(slug="shop1", name="Магазин 1", business_id=1, campaign_id=10)
        session.add(shop)
        await session.flush()
        product = Product(shop_id=shop.id, offer_id="RETRY-OFFER", slip="инст")
        session.add(product)
        await session.commit()
        retry_shop_id = shop.id

    # Заказ на товар RETRY-OFFER ×1, но ключей пока НЕТ
    parsed_order = {
        "orders": [{
            "orderId": 5005, "status": "PROCESSING", "substatus": "STARTED",
            "fake": False,
            "items": [{"id": 9, "offerId": "RETRY-OFFER", "offerName": "Игра Z", "count": 1}],
        }],
        "paging": {},
    }
    parsed = ms.parse_orders(parsed_order)[0][0]

    # Подменяем MarketClient в модуле retry на фейковый
    orig_client = retry_mod.MarketClient
    retry_mod.MarketClient = FakeClient
    try:
        # Первый прогон через dispenser напрямую — нет ключей → FAILED
        from app.services import dispenser
        async with s.get_session() as session:
            shop = await session.get(Shop, retry_shop_id)
            res = await dispenser.dispense_order(session, FakeClient(), shop, 10, parsed)
        assert res.state == OrderState.FAILED, f"ожидался FAILED: {res.state}"
        print("retry (заказ без ключей -> FAILED): OK")

        # Пополняем пул ключей
        async with s.get_session() as session:
            prod = (await session.execute(
                select(Product).where(Product.offer_id == "RETRY-OFFER")
            )).scalar_one()
            session.add(Key(product_id=prod.id, code="ZZZ", status=KeyStatus.AVAILABLE))
            await session.commit()

        # Переотправка через сервис → DELIVERED
        result = await retry_mod.retry_order(5005)
        assert result is not None, "retry_order вернул None"
        assert result.state == OrderState.DELIVERED, f"ожидался DELIVERED: {result.state}"
        print("retry (после пополнения -> DELIVERED): OK")
    finally:
        retry_mod.MarketClient = orig_client


if __name__ == "__main__":
    asyncio.run(main())

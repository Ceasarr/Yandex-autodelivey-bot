"""Модели базы данных.

Схема:
  Shop      — магазин (кабинет) на Маркете.
  Product   — товар магазина, к которому привязываются цифровые ключи.
              Сопоставление с заказом идёт по offer_id (SKU продавца).
  Key       — отдельный цифровой ключ из пула товара. Имеет статус.
  Order     — заказ с Маркета (на уровне кабинета).
  OrderItem — позиция заказа: сколько ключей какого товара нужно выдать.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class KeyStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"   # свободен, можно выдать
    RESERVED = "RESERVED"     # зарезервирован под конкретный заказ (в процессе выдачи)
    DELIVERED = "DELIVERED"   # передан покупателю
    REVOKED = "REVOKED"       # изъят вручную (например, оказался невалидным)


class OrderState(str, enum.Enum):
    NEW = "NEW"               # обнаружен поллером, ещё не обработан
    PROCESSING = "PROCESSING" # идёт выдача
    DELIVERED = "DELIVERED"   # ключи успешно переданы в Маркет
    FAILED = "FAILED"         # ошибка выдачи (нет ключей / ошибка API)
    SKIPPED = "SKIPPED"       # не требует выдачи (нет цифровых товаров / отменён)


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    business_id: Mapped[int] = mapped_column(BigInteger, index=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    products: Mapped[list["Product"]] = relationship(back_populates="shop")
    orders: Mapped[list["Order"]] = relationship(back_populates="shop")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("shop_id", "offer_id", name="uq_product_shop_offer"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    offer_id: Mapped[str] = mapped_column(String(255), index=True)  # SKU продавца
    title: Mapped[str] = mapped_column(String(512), default="")
    # Инструкция активации, попадает в поле slip при выдаче
    slip: Mapped[str] = mapped_column(Text, default="")
    # Срок активации в днях от даты выдачи (0 -> берётся из настроек по умолчанию)
    activate_days: Mapped[int] = mapped_column(Integer, default=0)
    # Порог остатка для предупреждения в Telegram
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    shop: Mapped["Shop"] = relationship(back_populates="products")
    keys: Mapped[list["Key"]] = relationship(back_populates="product")


class Key(Base):
    __tablename__ = "keys"
    __table_args__ = (
        UniqueConstraint("product_id", "code", name="uq_key_product_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    code: Mapped[str] = mapped_column(String(256))
    status: Mapped[KeyStatus] = mapped_column(
        Enum(KeyStatus), default=KeyStatus.AVAILABLE, index=True
    )
    # К какому заказу привязан ключ (заполняется при резерве/выдаче)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    product: Mapped["Product"] = relationship(back_populates="keys")
    order: Mapped["Order | None"] = relationship(back_populates="keys")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("shop_id", "market_order_id", name="uq_order_shop_market"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), index=True)
    # Идентификатор заказа в Маркете
    market_order_id: Mapped[int] = mapped_column(BigInteger, index=True)
    market_status: Mapped[str] = mapped_column(String(32), default="")
    market_substatus: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[OrderState] = mapped_column(
        Enum(OrderState), default=OrderState.NEW, index=True
    )
    # Когда заказ перешёл в PROCESSING (от него считается дедлайн 30 минут)
    processing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    shop: Mapped["Shop"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    keys: Mapped[list["Key"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    # id позиции внутри заказа Маркета (нужен для deliverDigitalGoods.items[].id)
    market_item_id: Mapped[int] = mapped_column(BigInteger)
    offer_id: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    count: Mapped[int] = mapped_column(Integer, default=1)

    order: Mapped["Order"] = relationship(back_populates="items")

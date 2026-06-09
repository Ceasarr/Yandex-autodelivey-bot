"""Клавиатуры Telegram-бота."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.db.models import Shop


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Остатки"), KeyboardButton(text="🧾 Заказы")],
            [KeyboardButton(text="➕ Загрузить ключи"), KeyboardButton(text="🗑 Удалить SKU")],
            [KeyboardButton(text="📜 Выданные ключи"), KeyboardButton(text="✏️ Инструкция SKU")],
            [KeyboardButton(text="📤 Выгрузить ключи"), KeyboardButton(text="🔑 Удалить ключ")],
            [KeyboardButton(text="🏪 Магазины"), KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с единственной кнопкой отмены текущего действия (FSM)."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✖️ Отмена")]],
        resize_keyboard=True,
    )


def shops_keyboard(shops: list[Shop], action: str) -> InlineKeyboardMarkup:
    """Список магазинов. callback_data: '<action>:<shop_slug>'."""
    rows = [
        [InlineKeyboardButton(text=shop.name, callback_data=f"{action}:{shop.slug}")]
        for shop in shops
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def products_keyboard(
    products: list[tuple[str, str]], action: str
) -> InlineKeyboardMarkup:
    """products: список (offer_id, title). callback_data: '<action>:<offer_id>'."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{title or offer_id}", callback_data=f"{action}:{offer_id}"
            )
        ]
        for offer_id, title in products
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def retry_keyboard(market_order_id: int) -> InlineKeyboardMarkup:
    """Кнопка переотправки одного упавшего заказа.

    callback_data: 'retry:<market_order_id>'.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Переотправить",
                    callback_data=f"retry:{market_order_id}",
                )
            ]
        ]
    )


def delete_products_keyboard(
    products: list[tuple[int, str, int, int]]
) -> InlineKeyboardMarkup:
    """Список товаров для удаления.

    products: список (product_id, offer_id, свободных, всего).
    callback_data: 'del_sku:<product_id>'.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=f"🗑 {offer_id} ({avail}/{total} ключей)",
                callback_data=f"del_sku:{product_id}",
            )
        ]
        for product_id, offer_id, avail, total in products
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_delete_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления товара.

    callback_data: 'del_yes:<product_id>' / 'del_no'.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить", callback_data=f"del_yes:{product_id}"
                ),
                InlineKeyboardButton(text="↩️ Отмена", callback_data="del_no"),
            ]
        ]
    )


def show_keys_keyboard(market_order_id: int) -> InlineKeyboardMarkup:
    """Кнопка показа выданных ключей конкретного заказа.

    callback_data: 'showkeys:<market_order_id>'.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔑 Показать выданные ключи",
                    callback_data=f"showkeys:{market_order_id}",
                )
            ]
        ]
    )


def sku_picker_keyboard(
    products: list[tuple[int, str, str]], action: str
) -> InlineKeyboardMarkup:
    """Список товаров для выбора по product_id.

    products: список (product_id, offer_id, title).
    callback_data: '<action>:<product_id>'.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=f"{offer_id}" + (f" — {title}" if title else ""),
                callback_data=f"{action}:{product_id}",
            )
        ]
        for product_id, offer_id, title in products
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slip_edit_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Действия над инструкцией товара: изменить / очистить.

    callback_data: 'slip_set:<product_id>' / 'slip_clear:<product_id>'.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Изменить текст",
                    callback_data=f"slip_set:{product_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧹 Очистить (сбросить на стандарт)",
                    callback_data=f"slip_clear:{product_id}",
                )
            ],
        ]
    )

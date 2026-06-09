"""Хендлеры Telegram-бота.

Доступ только для администраторов (settings.admin_ids).
Возможности:
  * /start, меню
  * Остатки ключей по магазинам и товарам
  * Список последних заказов и их статусов
  * Просмотр ключей, выданных по заказу, и история выдач
  * Регистрация товара (offer_id) и загрузка ключей (текст или файл .txt)
  * Редактирование инструкции (slip), уходящей покупателю вместе с ключами
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.db.models import OrderState
from app.bot import keyboards as kb
from app.services import repository as repo
from app.services.retry import retry_order

logger = logging.getLogger(__name__)
router = Router()

# Тексты кнопок главного меню. Нажатие любой из них прерывает текущий
# пошаговый сценарий (FSM), чтобы пользователь не «застревал» в вводе.
MENU_TEXTS = {
    "📦 Остатки", "🧾 Заказы", "➕ Загрузить ключи", "🗑 Удалить SKU",
    "📜 Выданные ключи", "✏️ Инструкция SKU", "📤 Выгрузить ключи",
    "🔑 Удалить ключ", "🏪 Магазины", "ℹ️ Помощь",
}
CANCEL_TEXT = "✖️ Отмена"

# Сколько ключей показывать одним сообщением (Telegram-лимит ~4096 символов)
_KEYS_PER_MESSAGE = 50


def _esc(text: str) -> str:
    """Экранирует HTML-спецсимволы для parse_mode=HTML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---- FSM загрузки ключей ----

class UploadKeys(StatesGroup):
    choosing_shop = State()
    entering_offer = State()
    entering_keys = State()


# ---- FSM удаления SKU ----

class DeleteSku(StatesGroup):
    choosing_shop = State()
    choosing_product = State()


# ---- FSM редактирования инструкции (slip) ----

class EditSlip(StatesGroup):
    choosing_shop = State()
    choosing_product = State()
    entering_slip = State()


# ---- FSM выгрузки оставшихся ключей ----

class ExportKeys(StatesGroup):
    choosing_shop = State()
    choosing_product = State()


# ---- FSM удаления одного ключа по коду ----

class DeleteKey(StatesGroup):
    choosing_shop = State()
    choosing_product = State()
    entering_code = State()


# ---- Отмена текущего сценария ----

@router.message(StateFilter("*"), Command("cancel"))
@router.message(StateFilter("*"), F.text == CANCEL_TEXT)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять.", reply_markup=kb.main_menu())
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=kb.main_menu())


# Нажатие пункта меню во время пошагового сценария прерывает сценарий,
# чтобы пользователь не «застрял». Регистрируем раньше FSM-хендлеров, но
# пропускаем апдейт дальше (raise SkipHandler нельзя — просто сбрасываем
# состояние и даём обычным меню-хендлерам отработать на следующем шаге).
@router.message(StateFilter(UploadKeys, DeleteSku, EditSlip, ExportKeys, DeleteKey), F.text.in_(MENU_TEXTS))
async def abort_flow_on_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Текущее действие прервано — открыл меню. Нажмите пункт ещё раз.",
        reply_markup=kb.main_menu(),
    )


# ---- Базовые команды ----

@router.message(StateFilter("*"), Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! Я управляю автовыдачей цифровых ключей на Яндекс Маркете.\n\n"
        "Выдача заказов идёт автоматически. Через меню можно смотреть остатки, "
        "заказы, выданные ключи и загружать ключи.",
        reply_markup=kb.main_menu(),
    )


@router.message(F.text == "ℹ️ Помощь")
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды</b>\n"
        "📦 Остатки — свободные/выданные ключи по товарам\n"
        "🧾 Заказы — последние заказы и их статусы\n"
        "📜 Выданные ключи — история выдач и какие коды ушли покупателю\n"
        "✏️ Инструкция SKU — текст, который Маркет шлёт покупателю с ключами\n"
        "📤 Выгрузить ключи — получить все свободные ключи SKU файлом\n"
        "🔑 Удалить ключ — удалить один свободный ключ SKU по его коду\n"
        "🏪 Магазины — подключённые магазины\n"
        "➕ Загрузить ключи — добавить ключи к товару\n"
        "🗑 Удалить SKU — удалить товар вместе с его ключами\n\n"
        "Чтобы добавить ключи: «Загрузить ключи» → выбрать магазин → ввести "
        "offer_id товара → отправить ключи (по одному в строке) текстом или "
        "файлом .txt.\n\n"
        "Любой шаг можно прервать кнопкой «✖️ Отмена» или командой /cancel."
    )


# ---- Магазины ----

@router.message(F.text == "🏪 Магазины")
async def show_shops(message: Message) -> None:
    shops = await repo.list_shops()
    if not shops:
        await message.answer("Магазины ещё не инициализированы. Запустите поллер.")
        return
    lines = ["<b>Подключённые магазины</b>"]
    for s in shops:
        lines.append(f"• {_esc(s.name)} — campaign <code>{s.campaign_id}</code>, "
                     f"business <code>{s.business_id}</code>")
    await message.answer("\n".join(lines))


# ---- Остатки ----

@router.message(F.text == "📦 Остатки")
async def show_stock(message: Message) -> None:
    overview = await repo.stock_overview()
    if not overview:
        await message.answer("Нет данных. Сначала подключите магазины и товары.")
        return
    lines: list[str] = []
    for shop in overview:
        lines.append(f"\n<b>🏪 {_esc(shop.shop_name)}</b>")
        if not shop.products:
            lines.append("  товары не добавлены")
            continue
        for p in shop.products:
            mark = "⚠️" if p.is_low else "✅"
            lines.append(
                f"  {mark} {_esc(p.title or p.offer_id)} "
                f"(<code>{_esc(p.offer_id)}</code>): {p.available} своб. / "
                f"{p.delivered} выдано"
            )
    await message.answer("\n".join(lines))


# ---- Заказы ----

@router.message(F.text == "🧾 Заказы")
async def show_orders(message: Message) -> None:
    orders = await repo.recent_orders(limit=10)
    if not orders:
        await message.answer("Заказов пока нет.")
        return
    state_icon = {
        OrderState.DELIVERED: "✅",
        OrderState.FAILED: "🛑",
        OrderState.PROCESSING: "⏳",
        OrderState.NEW: "🆕",
        OrderState.SKIPPED: "➖",
    }
    lines = ["<b>Последние заказы</b>"]
    retryable: list[tuple[int, str, str]] = []  # (market_order_id, shop_name, state)
    delivered: list[tuple[int, str]] = []        # (market_order_id, shop_name)
    for order, shop_name in orders:
        icon = state_icon.get(order.state, "•")
        line = f"{icon} <code>{order.market_order_id}</code> · {shop_name} · {order.state.value}"
        # FAILED — не хватило ключей / ошибка API.
        # SKIPPED — на момент заказа товар не был заведён (или нет наших товаров).
        # Оба случая выправляются после загрузки товара/ключей → даём переотправку.
        if order.state in (OrderState.FAILED, OrderState.SKIPPED):
            if order.last_error:
                line += f"\n    {_esc(order.last_error[:120])}"
            retryable.append((order.market_order_id, shop_name, order.state.value))
        elif order.state == OrderState.DELIVERED:
            delivered.append((order.market_order_id, shop_name))
        lines.append(line)
    await message.answer("\n".join(lines))

    # По каждому невыданному заказу — отдельное сообщение с кнопкой переотправки
    for market_order_id, shop_name, state in retryable:
        await message.answer(
            f"⚠️ Заказ <code>{market_order_id}</code> ({shop_name}) не выдан "
            f"(статус {state}). Загрузите ключи и нажмите переотправку.",
            reply_markup=kb.retry_keyboard(market_order_id),
        )

    # По выданным заказам — кнопка посмотреть, какие коды ушли покупателю
    for market_order_id, shop_name in delivered:
        await message.answer(
            f"✅ Заказ <code>{market_order_id}</code> ({shop_name}) выдан.",
            reply_markup=kb.show_keys_keyboard(market_order_id),
        )


@router.callback_query(F.data.startswith("retry:"))
async def retry_order_cb(call: CallbackQuery) -> None:
    try:
        market_order_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный заказ", show_alert=True)
        return

    await call.answer("Переотправляю…")
    result = await retry_order(market_order_id)

    if result is None:
        await call.message.answer(
            f"Заказ <code>{market_order_id}</code> не найден в базе."
        )
        return

    if result.state == OrderState.DELIVERED:
        text = f"✅ Заказ <code>{market_order_id}</code> успешно выдан."
        if result.delivered_counts:
            details = ", ".join(
                f"{_esc(offer)} ×{cnt}" for offer, cnt in result.delivered_counts.items()
            )
            text += f"\nВыдано: {details}"
        # Убираем кнопку с исходного сообщения
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    else:
        text = (
            f"🛑 Заказ <code>{market_order_id}</code> снова не удалось выдать.\n"
            f"Причина: {_esc(result.message)}"
        )
        if result.missing:
            miss = ", ".join(f"{_esc(o)} (−{n})" for o, n in result.missing.items())
            text += f"\nНе хватило ключей: {miss}"

    await call.message.answer(text)


# ---- Просмотр выданных ключей ----

async def _send_order_keys(message: Message, market_order_id: int) -> None:
    """Выводит коды, выданные по заказу, сгруппированные по товару."""
    info = await repo.get_order_keys(market_order_id)
    if info is None:
        await message.answer(
            f"Заказ <code>{market_order_id}</code> не найден в базе."
        )
        return
    if info.total_codes == 0:
        note = ""
        if info.state != OrderState.DELIVERED:
            note = (
                f"\nСтатус заказа: {info.state.value}. Ключи по нему ещё не "
                "выдавались."
            )
        await message.answer(
            f"По заказу <code>{market_order_id}</code> нет выданных ключей.{note}"
        )
        return

    header = (
        f"🔑 <b>Ключи заказа</b> <code>{market_order_id}</code>\n"
        f"Магазин: {_esc(info.shop_name)}"
    )
    if info.delivered_at is not None:
        header += f"\nВыдано: {info.delivered_at:%Y-%m-%d %H:%M UTC}"
    await message.answer(header)

    for g in info.groups:
        title = _esc(g.title) if g.title else ""
        label = f"<b>{_esc(g.offer_id)}</b>" + (f" — {title}" if title else "")
        # Бьём на части, чтобы не упереться в лимит длины сообщения Telegram
        for start in range(0, len(g.codes), _KEYS_PER_MESSAGE):
            chunk = g.codes[start:start + _KEYS_PER_MESSAGE]
            body = "\n".join(f"<code>{_esc(c)}</code>" for c in chunk)
            head = label if start == 0 else f"{label} (продолжение)"
            await message.answer(f"{head} ×{len(g.codes)}\n{body}")


@router.callback_query(F.data.startswith("showkeys:"))
async def show_keys_cb(call: CallbackQuery) -> None:
    try:
        market_order_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный заказ", show_alert=True)
        return
    await call.answer()
    await _send_order_keys(call.message, market_order_id)


@router.message(F.text == "📜 Выданные ключи")
async def show_delivered_history(message: Message) -> None:
    orders = await repo.recent_delivered_orders(limit=10)
    if not orders:
        await message.answer(
            "Пока нет выданных заказов. Здесь появится история выдач, "
            "и можно будет посмотреть, какие коды ушли покупателю."
        )
        return
    await message.answer(
        "<b>Последние выданные заказы</b>\n"
        "Нажмите кнопку, чтобы увидеть отправленные покупателю коды."
    )
    for order, shop_name in orders:
        when = (
            f" · {order.delivered_at:%d.%m %H:%M}"
            if order.delivered_at is not None else ""
        )
        await message.answer(
            f"✅ <code>{order.market_order_id}</code> · {_esc(shop_name)}{when}",
            reply_markup=kb.show_keys_keyboard(order.market_order_id),
        )


# ---- Загрузка ключей (FSM) ----

@router.message(F.text == "➕ Загрузить ключи")
async def upload_start(message: Message, state: FSMContext) -> None:
    shops = await repo.list_shops()
    if not shops:
        await message.answer("Сначала подключите магазины (запустите поллер).")
        return
    await state.set_state(UploadKeys.choosing_shop)
    await message.answer(
        "Можно прервать в любой момент кнопкой ниже.",
        reply_markup=kb.cancel_keyboard(),
    )
    await message.answer(
        "Выберите магазин:", reply_markup=kb.shops_keyboard(shops, "upl_shop")
    )


@router.callback_query(UploadKeys.choosing_shop, F.data.startswith("upl_shop:"))
async def upload_choose_shop(call: CallbackQuery, state: FSMContext) -> None:
    slug = call.data.split(":", 1)[1]
    shop = await repo.get_shop_by_slug(slug)
    if shop is None:
        await call.answer("Магазин не найден", show_alert=True)
        return
    await state.update_data(shop_id=shop.id, shop_name=shop.name)
    await state.set_state(UploadKeys.entering_offer)
    await call.message.answer(
        f"Магазин: {_esc(shop.name)}\n"
        "Введите <b>offer_id</b> товара (SKU продавца), к которому добавляем ключи:"
    )
    await call.answer()


@router.message(UploadKeys.entering_offer, F.text)
async def upload_enter_offer(message: Message, state: FSMContext) -> None:
    offer_id = (message.text or "").strip()
    if not offer_id or len(offer_id) > 255 or "\n" in offer_id:
        await message.answer(
            "offer_id не должен быть пустым, многострочным или длиннее 255 "
            "символов. Введите корректный SKU продавца."
        )
        return
    data = await state.get_data()
    shop_id = data["shop_id"]
    # Регистрируем товар, если его ещё нет
    product = await repo.upsert_product(shop_id=shop_id, offer_id=offer_id)
    await state.update_data(offer_id=offer_id, product_id=product.id)
    await state.set_state(UploadKeys.entering_keys)
    await message.answer(
        f"Товар <code>{_esc(offer_id)}</code> готов.\n"
        "Теперь отправьте ключи — по одному в строке (текстом) или файлом .txt."
    )


@router.message(UploadKeys.entering_keys, F.document)
async def upload_keys_file(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    product_id = data["product_id"]
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".txt"):
        await message.answer("Нужен текстовый файл .txt с ключами.")
        return
    file = await message.bot.get_file(doc.file_id)
    buf = await message.bot.download_file(file.file_path)
    content = buf.read().decode("utf-8", errors="replace")
    codes = [line for line in content.splitlines() if line.strip()]
    added, skipped = await repo.add_keys(product_id, codes)
    await state.clear()
    await message.answer(
        f"Готово. Добавлено: {added}, пропущено дубликатов: {skipped}.",
        reply_markup=kb.main_menu(),
    )


@router.message(UploadKeys.entering_keys, F.text)
async def upload_keys_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    product_id = data["product_id"]
    codes = [line for line in message.text.splitlines() if line.strip()]
    if not codes:
        await message.answer("Не вижу ключей. Отправьте по одному в строке.")
        return
    added, skipped = await repo.add_keys(product_id, codes)
    await state.clear()
    await message.answer(
        f"Готово. Добавлено: {added}, пропущено дубликатов: {skipped}.",
        reply_markup=kb.main_menu(),
    )


# ---- Удаление SKU (FSM) ----

@router.message(F.text == "🗑 Удалить SKU")
async def delete_start(message: Message, state: FSMContext) -> None:
    shops = await repo.list_shops()
    if not shops:
        await message.answer("Сначала подключите магазины (запустите поллер).")
        return
    # Один магазин — сразу к выбору товара, без лишнего шага
    if len(shops) == 1:
        await _show_products_to_delete(message, state, shops[0].id, shops[0].name)
        return
    await state.set_state(DeleteSku.choosing_shop)
    await message.answer(
        "Выберите магазин:", reply_markup=kb.shops_keyboard(shops, "del_shop")
    )


@router.callback_query(DeleteSku.choosing_shop, F.data.startswith("del_shop:"))
async def delete_choose_shop(call: CallbackQuery, state: FSMContext) -> None:
    slug = call.data.split(":", 1)[1]
    shop = await repo.get_shop_by_slug(slug)
    if shop is None:
        await call.answer("Магазин не найден", show_alert=True)
        return
    await _show_products_to_delete(call.message, state, shop.id, shop.name)
    await call.answer()


async def _show_products_to_delete(
    message: Message, state: FSMContext, shop_id: int, shop_name: str
) -> None:
    products = await repo.list_products(shop_id)
    if not products:
        await state.clear()
        await message.answer(
            f"В магазине «{shop_name}» нет зарегистрированных SKU.",
            reply_markup=kb.main_menu(),
        )
        return
    rows = [(p.id, p.offer_id, avail, total) for p, avail, total in products]
    await state.set_state(DeleteSku.choosing_product)
    await message.answer(
        f"Магазин: {shop_name}\nВыберите SKU для удаления:",
        reply_markup=kb.delete_products_keyboard(rows),
    )


@router.callback_query(DeleteSku.choosing_product, F.data.startswith("del_sku:"))
async def delete_choose_product(call: CallbackQuery, state: FSMContext) -> None:
    try:
        product_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный товар", show_alert=True)
        return
    product = await repo.get_product_by_id(product_id)
    if product is None:
        await call.answer("Товар уже удалён", show_alert=True)
        return
    await call.message.answer(
        f"Удалить SKU <code>{_esc(product.offer_id)}</code> вместе со всеми его ключами?\n"
        "Действие необратимо.",
        reply_markup=kb.confirm_delete_keyboard(product_id),
    )
    await call.answer()


@router.callback_query(DeleteSku.choosing_product, F.data.startswith("del_yes:"))
async def delete_confirm(call: CallbackQuery, state: FSMContext) -> None:
    try:
        product_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный товар", show_alert=True)
        return
    result = await repo.delete_product(product_id)
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    if result is None:
        await call.message.answer("Товар уже был удалён.", reply_markup=kb.main_menu())
    else:
        offer_id, keys_count = result
        await call.message.answer(
            f"🗑 SKU <code>{offer_id}</code> удалён. "
            f"Вместе с ним удалено ключей: {keys_count}.",
            reply_markup=kb.main_menu(),
        )
    await call.answer()


@router.callback_query(DeleteSku.choosing_product, F.data == "del_no")
async def delete_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await call.message.answer("Удаление отменено.", reply_markup=kb.main_menu())
    await call.answer()


# ---- Редактирование инструкции (slip) ----

# Маркет ограничивает длину slip; держим разумный предел
_SLIP_MAX_LEN = 2000


@router.message(F.text == "✏️ Инструкция SKU")
async def slip_start(message: Message, state: FSMContext) -> None:
    shops = await repo.list_shops()
    if not shops:
        await message.answer("Сначала подключите магазины (запустите поллер).")
        return
    if len(shops) == 1:
        await _show_products_for_slip(message, state, shops[0].id, shops[0].name)
        return
    await state.set_state(EditSlip.choosing_shop)
    await message.answer(
        "Можно прервать кнопкой ниже.", reply_markup=kb.cancel_keyboard()
    )
    await message.answer(
        "Выберите магазин:", reply_markup=kb.shops_keyboard(shops, "slip_shop")
    )


@router.callback_query(EditSlip.choosing_shop, F.data.startswith("slip_shop:"))
async def slip_choose_shop(call: CallbackQuery, state: FSMContext) -> None:
    slug = call.data.split(":", 1)[1]
    shop = await repo.get_shop_by_slug(slug)
    if shop is None:
        await call.answer("Магазин не найден", show_alert=True)
        return
    await _show_products_for_slip(call.message, state, shop.id, shop.name)
    await call.answer()


async def _show_products_for_slip(
    message: Message, state: FSMContext, shop_id: int, shop_name: str
) -> None:
    products = await repo.list_products(shop_id)
    if not products:
        await state.clear()
        await message.answer(
            f"В магазине «{_esc(shop_name)}» нет зарегистрированных SKU. "
            "Сначала загрузите ключи для товара.",
            reply_markup=kb.main_menu(),
        )
        return
    rows = [(p.id, p.offer_id, p.title) for p, _avail, _total in products]
    await state.set_state(EditSlip.choosing_product)
    await message.answer(
        f"Магазин: {_esc(shop_name)}\nВыберите SKU:",
        reply_markup=kb.sku_picker_keyboard(rows, "slip_pick"),
    )


@router.callback_query(EditSlip.choosing_product, F.data.startswith("slip_pick:"))
async def slip_choose_product(call: CallbackQuery, state: FSMContext) -> None:
    try:
        product_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный товар", show_alert=True)
        return
    product = await repo.get_product_by_id(product_id)
    if product is None:
        await call.answer("Товар не найден", show_alert=True)
        return
    await state.update_data(product_id=product_id)
    current = product.slip.strip()
    if current:
        shown = current if len(current) <= 500 else current[:500] + "…"
        body = f"Текущая инструкция:\n<blockquote>{_esc(shown)}</blockquote>"
    else:
        body = (
            "Инструкция не задана — покупателю уходит стандартный текст "
            "«Спасибо за покупку!»."
        )
    await call.message.answer(
        f"SKU <code>{_esc(product.offer_id)}</code>\n{body}",
        reply_markup=kb.slip_edit_keyboard(product_id),
    )
    await call.answer()


@router.callback_query(EditSlip.choosing_product, F.data.startswith("slip_set:"))
async def slip_set(call: CallbackQuery, state: FSMContext) -> None:
    try:
        product_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный товар", show_alert=True)
        return
    await state.update_data(product_id=product_id)
    await state.set_state(EditSlip.entering_slip)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await call.message.answer(
        "Пришлите новый текст инструкции одним сообщением. Он уйдёт покупателю "
        "вместе с ключами при выдаче.\n\n"
        "Чтобы отменить — нажмите «✖️ Отмена».",
        reply_markup=kb.cancel_keyboard(),
    )
    await call.answer()


@router.callback_query(EditSlip.choosing_product, F.data.startswith("slip_clear:"))
async def slip_clear(call: CallbackQuery, state: FSMContext) -> None:
    try:
        product_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный товар", show_alert=True)
        return
    product = await repo.update_product_slip(product_id, "")
    await state.clear()
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    if product is None:
        await call.message.answer("Товар не найден.", reply_markup=kb.main_menu())
    else:
        await call.message.answer(
            f"🧹 Инструкция для <code>{_esc(product.offer_id)}</code> сброшена. "
            "Покупателю будет уходить стандартный текст.",
            reply_markup=kb.main_menu(),
        )
    await call.answer()


@router.message(EditSlip.entering_slip, F.text)
async def slip_save(message: Message, state: FSMContext) -> None:
    slip = (message.text or "").strip()
    if not slip:
        await message.answer("Пустой текст. Пришлите инструкцию или «✖️ Отмена».")
        return
    if len(slip) > _SLIP_MAX_LEN:
        await message.answer(
            f"Текст слишком длинный ({len(slip)} символов). "
            f"Максимум — {_SLIP_MAX_LEN}. Сократите и пришлите снова."
        )
        return
    data = await state.get_data()
    product_id = data.get("product_id")
    product = await repo.update_product_slip(product_id, slip) if product_id else None
    await state.clear()
    if product is None:
        await message.answer(
            "Товар не найден — возможно, был удалён. Попробуйте заново.",
            reply_markup=kb.main_menu(),
        )
        return
    await message.answer(
        f"✅ Инструкция для <code>{_esc(product.offer_id)}</code> обновлена.",
        reply_markup=kb.main_menu(),
    )


@router.message(EditSlip.entering_slip)
async def slip_save_wrong_type(message: Message) -> None:
    await message.answer(
        "Нужен текст. Пришлите инструкцию сообщением или нажмите «✖️ Отмена»."
    )


# ---- Выгрузка оставшихся ключей (FSM) ----

@router.message(F.text == "📤 Выгрузить ключи")
async def export_start(message: Message, state: FSMContext) -> None:
    shops = await repo.list_shops()
    if not shops:
        await message.answer("Сначала подключите магазины (запустите поллер).")
        return
    if len(shops) == 1:
        await _show_products_to_export(message, state, shops[0].id, shops[0].name)
        return
    await state.set_state(ExportKeys.choosing_shop)
    await message.answer(
        "Выберите магазин:", reply_markup=kb.shops_keyboard(shops, "exp_shop")
    )


@router.callback_query(ExportKeys.choosing_shop, F.data.startswith("exp_shop:"))
async def export_choose_shop(call: CallbackQuery, state: FSMContext) -> None:
    slug = call.data.split(":", 1)[1]
    shop = await repo.get_shop_by_slug(slug)
    if shop is None:
        await call.answer("Магазин не найден", show_alert=True)
        return
    await _show_products_to_export(call.message, state, shop.id, shop.name)
    await call.answer()


async def _show_products_to_export(
    message: Message, state: FSMContext, shop_id: int, shop_name: str
) -> None:
    products = await repo.list_products(shop_id)
    if not products:
        await state.clear()
        await message.answer(
            f"В магазине «{_esc(shop_name)}» нет зарегистрированных SKU.",
            reply_markup=kb.main_menu(),
        )
        return
    # Показываем только SKU, где есть свободные ключи (avail > 0)
    rows = [
        (p.id, f"{p.offer_id} ({avail} своб.)", p.title)
        for p, avail, _total in products
        if avail > 0
    ]
    if not rows:
        await state.clear()
        await message.answer(
            f"В магазине «{_esc(shop_name)}» нет свободных ключей для выгрузки.",
            reply_markup=kb.main_menu(),
        )
        return
    await state.set_state(ExportKeys.choosing_product)
    await message.answer(
        f"Магазин: {_esc(shop_name)}\nВыберите SKU для выгрузки свободных ключей:",
        reply_markup=kb.sku_picker_keyboard(rows, "exp_pick"),
    )


@router.callback_query(ExportKeys.choosing_product, F.data.startswith("exp_pick:"))
async def export_pick_product(call: CallbackQuery, state: FSMContext) -> None:
    try:
        product_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный товар", show_alert=True)
        return
    product = await repo.get_product_by_id(product_id)
    if product is None:
        await call.answer("Товар не найден", show_alert=True)
        return
    codes = await repo.list_available_codes(product_id)
    await state.clear()
    await call.answer()
    if not codes:
        await call.message.answer(
            f"По SKU <code>{_esc(product.offer_id)}</code> нет свободных ключей.",
            reply_markup=kb.main_menu(),
        )
        return
    content = "\n".join(codes).encode("utf-8")
    filename = f"{product.offer_id}_available_{len(codes)}.txt"
    await call.message.answer_document(
        BufferedInputFile(content, filename=filename),
        caption=(
            f"📤 Свободные ключи SKU <code>{_esc(product.offer_id)}</code>: "
            f"{len(codes)} шт.\n"
            "Это копия — ключи остаются в пуле и будут выданы при заказах."
        ),
    )
    await call.message.answer("Готово.", reply_markup=kb.main_menu())


# ---- Удаление одного ключа по коду (FSM) ----

@router.message(F.text == "🔑 Удалить ключ")
async def delkey_start(message: Message, state: FSMContext) -> None:
    shops = await repo.list_shops()
    if not shops:
        await message.answer("Сначала подключите магазины (запустите поллер).")
        return
    if len(shops) == 1:
        await _show_products_for_delkey(message, state, shops[0].id, shops[0].name)
        return
    await state.set_state(DeleteKey.choosing_shop)
    await message.answer(
        "Выберите магазин:", reply_markup=kb.shops_keyboard(shops, "delkey_shop")
    )


@router.callback_query(DeleteKey.choosing_shop, F.data.startswith("delkey_shop:"))
async def delkey_choose_shop(call: CallbackQuery, state: FSMContext) -> None:
    slug = call.data.split(":", 1)[1]
    shop = await repo.get_shop_by_slug(slug)
    if shop is None:
        await call.answer("Магазин не найден", show_alert=True)
        return
    await _show_products_for_delkey(call.message, state, shop.id, shop.name)
    await call.answer()


async def _show_products_for_delkey(
    message: Message, state: FSMContext, shop_id: int, shop_name: str
) -> None:
    products = await repo.list_products(shop_id)
    rows = [
        (p.id, f"{p.offer_id} ({avail} своб.)", p.title)
        for p, avail, _total in products
        if avail > 0
    ]
    if not rows:
        await state.clear()
        await message.answer(
            f"В магазине «{_esc(shop_name)}» нет свободных ключей для удаления.",
            reply_markup=kb.main_menu(),
        )
        return
    await state.set_state(DeleteKey.choosing_product)
    await message.answer(
        f"Магазин: {_esc(shop_name)}\nВыберите SKU, из которого удалить ключ:",
        reply_markup=kb.sku_picker_keyboard(rows, "delkey_pick"),
    )


@router.callback_query(DeleteKey.choosing_product, F.data.startswith("delkey_pick:"))
async def delkey_pick_product(call: CallbackQuery, state: FSMContext) -> None:
    try:
        product_id = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Некорректный товар", show_alert=True)
        return
    product = await repo.get_product_by_id(product_id)
    if product is None:
        await call.answer("Товар не найден", show_alert=True)
        return
    await state.update_data(product_id=product_id, offer_id=product.offer_id)
    await state.set_state(DeleteKey.entering_code)
    await call.message.answer(
        f"SKU <code>{_esc(product.offer_id)}</code>.\n"
        "Пришлите код ключа, который нужно удалить (одной строкой).\n\n"
        "Удалить можно только свободный (ещё не выданный) ключ.\n"
        "Для отмены — «✖️ Отмена».",
        reply_markup=kb.cancel_keyboard(),
    )
    await call.answer()


@router.message(DeleteKey.entering_code, F.text)
async def delkey_enter_code(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if not code or "\n" in code:
        await message.answer(
            "Нужен один код одной строкой. Пришлите код или нажмите «✖️ Отмена»."
        )
        return
    data = await state.get_data()
    product_id = data.get("product_id")
    offer_id = data.get("offer_id", "")
    if not product_id:
        await state.clear()
        await message.answer(
            "Что-то пошло не так — начните заново.", reply_markup=kb.main_menu()
        )
        return
    result = await repo.delete_key_by_code(product_id, code)
    await state.clear()
    if result is None:
        await message.answer(
            f"Ключ <code>{_esc(code)}</code> не найден среди ключей SKU "
            f"<code>{_esc(offer_id)}</code>.",
            reply_markup=kb.main_menu(),
        )
        return
    status, deleted = result
    if deleted:
        await message.answer(
            f"🔑 Ключ <code>{_esc(code)}</code> удалён из SKU "
            f"<code>{_esc(offer_id)}</code>.",
            reply_markup=kb.main_menu(),
        )
    else:
        await message.answer(
            f"Ключ <code>{_esc(code)}</code> найден, но его нельзя удалить: "
            f"статус {status} (не свободен). Удаляются только свободные ключи.",
            reply_markup=kb.main_menu(),
        )


@router.message(DeleteKey.entering_code)
async def delkey_enter_code_wrong_type(message: Message) -> None:
    await message.answer(
        "Нужен код текстом. Пришлите код ключа или нажмите «✖️ Отмена»."
    )

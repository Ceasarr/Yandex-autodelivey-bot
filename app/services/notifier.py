"""Отправка уведомлений администраторам в Telegram."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import ShopConfig, settings
from app.db.models import OrderState
from app.services.dispenser import DispenseResult

logger = logging.getLogger(__name__)


def _esc(text: str) -> str:
    """Экранирует HTML-спецсимволы для parse_mode=HTML."""
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _format_result(result: DispenseResult, shop: ShopConfig) -> str:
    # В тест-режиме помечаем уведомления, чтобы не путать с боевыми
    prefix = "🧪 <b>ТЕСТ</b>\n" if settings.test_mode else ""
    if result.state == OrderState.DELIVERED:
        lines = [
            f"{prefix}✅ <b>Ключи выданы автоматически</b>",
            f"Магазин: {_esc(shop.name)}",
            f"Заказ: <code>{result.order_id}</code>",
        ]
        if result.delivered_counts:
            details = ", ".join(
                f"{_esc(offer)} ×{cnt}" for offer, cnt in result.delivered_counts.items()
            )
            lines.append(f"Выдано: {details}")
        if result.missing:
            miss = ", ".join(f"{_esc(o)} (−{n})" for o, n in result.missing.items())
            lines.append(f"⚠️ Не хватило ключей: {miss}")
        return "\n".join(lines)

    # FAILED
    lines = [
        f"{prefix}🛑 <b>Не удалось выдать ключи</b>",
        f"Магазин: {_esc(shop.name)}",
        f"Заказ: <code>{result.order_id}</code>",
        f"Причина: {_esc(result.message)}",
    ]
    if result.missing:
        miss = ", ".join(f"{_esc(o)} (−{n})" for o, n in result.missing.items())
        lines.append(f"Не хватило ключей: {miss}")
    lines.append("\nНужно вмешательство: пополните ключи и переотправьте заказ.")
    return "\n".join(lines)


class Notifier:
    """Шлёт сообщения всем администраторам из настроек."""

    def __init__(self, bot: Bot):
        self._bot = bot

    async def notify_result(self, result: DispenseResult, shop: ShopConfig) -> None:
        await self.broadcast(_format_result(result, shop))

    async def broadcast(self, text: str) -> None:
        for admin_id in settings.admin_ids:
            try:
                await self._bot.send_message(admin_id, text)
            except TelegramAPIError as exc:
                logger.warning("Не удалось отправить уведомление %s: %s",
                               admin_id, exc)

"""Middleware: пускает к боту только администраторов из настроек."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.config import settings


class AdminOnlyMiddleware(BaseMiddleware):
    """Блокирует любые апдейты от пользователей не из admin_ids."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        user_id = user.id if user is not None else None

        if user_id is None or user_id not in settings.admin_ids:
            # Вежливо отказываем и не пропускаем дальше
            if isinstance(event, Update) and event.message is not None:
                await event.message.answer(
                    "Доступ запрещён. Бот только для администраторов."
                )
            elif isinstance(event, Message):
                await event.answer("Доступ запрещён. Бот только для администраторов.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Доступ запрещён.", show_alert=True)
            return None

        return await handler(event, data)

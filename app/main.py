"""Точка входа сервиса автовыдачи цифровых ключей.

Запускает параллельно:
  * Telegram-бота (aiogram, long polling)
  * Поллер заказов Яндекс Маркета, который при обнаружении оплаченного
    заказа автоматически выдаёт ключи и шлёт уведомление в Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

# Позволяет запускать как `python main.py` (из app/) или `python app/main.py`,
# а не только `python -m app.main` из корня проекта.
# Скрипты в scripts/ используют аналогичный приём.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.bot.handlers import router as bot_router
from app.bot.middlewares import AdminOnlyMiddleware
from app.config import settings
from app.db.session import init_db
from app.services.notifier import Notifier
from app.services.poller import run_poller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("service")


async def _set_commands(bot: Bot) -> None:
    """Регистрирует подсказки команд в интерфейсе Telegram.

    Необязательный косметический вызов. При сетевом сбое не должен ронять
    сервис — поллер выдачи важнее подсказок команд, поэтому ошибку только
    логируем.
    """
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="help", description="Помощь по командам"),
            BotCommand(command="cancel", description="Прервать текущее действие"),
        ])
    except Exception as exc:  # noqa: BLE001 — старт не должен падать из-за этого
        logger.warning("Не удалось зарегистрировать команды бота: %s", exc)


async def main() -> None:
    if not settings.bot_token or settings.bot_token.startswith("123456"):
        raise SystemExit("BOT_TOKEN не задан. Заполните .env (см. .env.example).")
    if not settings.admin_ids:
        logger.warning("ADMIN_IDS пуст — управлять ботом и получать уведомления "
                       "будет некому.")
    if not settings.configured_shops:
        logger.warning("Не настроен ни один магазин — поллер работать не будет. "
                       "Заполните SHOP1_*/SHOP2_* в .env.")

    await init_db()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Доступ только администраторам — на сообщения и колбэки
    guard = AdminOnlyMiddleware()
    dp.message.middleware(guard)
    dp.callback_query.middleware(guard)
    dp.include_router(bot_router)

    notifier = Notifier(bot)

    # Поллер как фоновая задача
    poller_task = asyncio.create_task(run_poller(notify=notifier.notify_result))

    logger.info("Запуск бота и поллера…")
    try:
        await _set_commands(bot)
        try:
            if settings.test_mode:
                await notifier.broadcast(
                    "🧪 Сервис запущен в <b>ТЕСТ-режиме</b>.\n"
                    "Обрабатываются только тестовые заказы Маркета (fake). "
                    "Настоящие заказы игнорируются."
                )
            else:
                await notifier.broadcast("🚀 Сервис автовыдачи запущен.")
        except Exception as exc:  # noqa: BLE001 — приветствие не должно ронять старт
            logger.warning("Не удалось отправить стартовое уведомление: %s", exc)
        await dp.start_polling(bot)
    finally:
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        logger.info("Остановка: %s", exc)

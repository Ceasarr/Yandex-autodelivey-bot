"""Конфигурация приложения. Все секреты читаются из переменных окружения (.env)."""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class ShopConfig:
    """Параметры одного магазина на Яндекс Маркете."""

    slug: str          # внутренний идентификатор: "shop1" / "shop2"
    name: str
    api_key: str
    business_id: int
    campaign_id: int

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and self.business_id > 0 and self.campaign_id > 0


class Settings(BaseSettings):
    """Глобальные настройки сервиса."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Telegram
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")

    # Market API
    market_api_base: str = Field(
        default="https://api.partner.market.yandex.ru", alias="MARKET_API_BASE"
    )

    # Магазин 1
    shop1_name: str = Field(default="Магазин 1", alias="SHOP1_NAME")
    shop1_api_key: str = Field(default="", alias="SHOP1_API_KEY")
    shop1_business_id: int = Field(default=0, alias="SHOP1_BUSINESS_ID")
    shop1_campaign_id: int = Field(default=0, alias="SHOP1_CAMPAIGN_ID")

    # Магазин 2
    shop2_name: str = Field(default="Магазин 2", alias="SHOP2_NAME")
    shop2_api_key: str = Field(default="", alias="SHOP2_API_KEY")
    shop2_business_id: int = Field(default=0, alias="SHOP2_BUSINESS_ID")
    shop2_campaign_id: int = Field(default=0, alias="SHOP2_CAMPAIGN_ID")

    # Поллер
    poll_interval_seconds: int = Field(default=45, alias="POLL_INTERVAL_SECONDS")
    poll_lookback_hours: int = Field(default=24, alias="POLL_LOOKBACK_HOURS")

    # Тест-режим: обрабатывать только тестовые заказы Маркета (fake=true).
    # Маркет не берёт за них плату. Настоящие заказы при этом НЕ обрабатываются.
    test_mode: bool = Field(default=False, alias="TEST_MODE")

    # Выдача
    default_activate_days: int = Field(default=3650, alias="DEFAULT_ACTIVATE_DAYS")

    # БД
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/service.db", alias="DATABASE_URL"
    )

    @property
    def admin_ids(self) -> list[int]:
        ids: list[int] = []
        for chunk in self.admin_ids_raw.split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                ids.append(int(chunk))
        return ids

    @property
    def shops(self) -> list[ShopConfig]:
        return [
            ShopConfig(
                slug="shop1",
                name=self.shop1_name,
                api_key=self.shop1_api_key,
                business_id=self.shop1_business_id,
                campaign_id=self.shop1_campaign_id,
            ),
            ShopConfig(
                slug="shop2",
                name=self.shop2_name,
                api_key=self.shop2_api_key,
                business_id=self.shop2_business_id,
                campaign_id=self.shop2_campaign_id,
            ),
        ]

    @property
    def configured_shops(self) -> list[ShopConfig]:
        return [s for s in self.shops if s.is_configured]


settings = Settings()

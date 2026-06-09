"""Проверка готовности окружения к запуску (без вывода секретов).

Запуск:
  Windows: .venv\\Scripts\\python.exe scripts\\check_config.py
  Linux:   .venv/bin/python scripts/check_config.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402


def main() -> int:
    ok = True

    token_ok = bool(settings.bot_token) and not settings.bot_token.startswith("123456")
    print("BOT_TOKEN:", "OK" if token_ok else "НЕ ЗАДАН")
    ok = ok and token_ok

    admin_ok = bool(settings.admin_ids)
    print("ADMIN_IDS:", f"OK ({len(settings.admin_ids)})" if admin_ok else "ПУСТО")
    ok = ok and admin_ok

    print("TEST_MODE:", settings.test_mode)
    print("DATABASE_URL:", settings.database_url)

    shops = settings.configured_shops
    print("Активных магазинов:", len(shops))
    for s in shops:
        print(f"  - {s.slug}: {s.name} | business_id={s.business_id} | "
              f"campaign_id={s.campaign_id} | api_key={'задан' if s.api_key else 'НЕТ'}")

    if not shops:
        print("  ВНИМАНИЕ: ни один магазин не настроен — автовыдача работать не будет.")
        ok = False

    print()
    print("ИТОГ:", "готово к запуску" if ok else "есть незаполненные поля")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

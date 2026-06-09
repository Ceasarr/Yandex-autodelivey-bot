# Образ сервиса автовыдачи цифровых ключей (Telegram-бот + поллер Маркета).
FROM python:3.12-slim

# Python: не писать .pyc, не буферизовать вывод (логи сразу в stdout)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала зависимости — слой кэшируется, пока requirements.txt не меняется
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем код приложения
COPY app ./app
COPY scripts ./scripts

# Каталог для SQLite-базы (монтируется томом, см. docker-compose.yml)
RUN mkdir -p /app/data

# Запуск без root
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Дымовая проверка как healthcheck-ориентир (в сеть не ходит)
# Запуск сервиса
CMD ["python", "-m", "app.main"]

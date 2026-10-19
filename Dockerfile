# Легковесный базовый образ.
FROM python:3.11-alpine

# Без .pyc-файлов и с небуферизованным выводом (логи сразу падают в stdout Docker).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Минимальный набор: корневые сертификаты для HTTPS к Telegram.
RUN apk add --no-cache ca-certificates

# Принцип наименьших привилегий: создаём системного пользователя без root.
RUN addgroup -S appuser -g 1000 && adduser -S appuser -u 1000 -G appuser

WORKDIR /app

# Сначала зависимости (кэшируется отдельно от кода).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Затем код приложения.
COPY app ./app

# Папка данных: монтируется как volume (./data:/app/data) в docker-compose.
RUN mkdir -p /app/data && chown -R appuser:appuser /app

# Переключаем контекст выполнения на непривилегированного пользователя.
USER appuser

EXPOSE 8000

# Проверка живости (busybox wget есть в alpine).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8000/readyz || exit 1

# Ошибки и логи пишем в stdout/stderr (собирает Docker).
# Один воркер: внутри процесса живут long-polling и in-memory-состояние (кэш аватарок, режим ответа).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--proxy-headers"]

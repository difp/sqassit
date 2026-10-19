# Makefile — локальные команды.
# Использование: make help

PYTHON_VERSION ?= python3.11
VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
PORT ?= 8000
DB_PATH ?= ./data/squash.db

.DEFAULT_GOAL := help
.PHONY: help venv install install-prod run test lint typecheck audit backup release clean

help: ## Показать список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Создать виртуальное окружение (если ещё нет)
	@test -x $(PYTHON) || ( $(PYTHON_VERSION) -m venv $(VENV) && $(PYTHON) -m pip install --upgrade pip )

install: venv ## Установить зависимости для разработки
	$(PYTHON) -m pip install -r requirements-dev.txt

install-prod: venv ## Установить только production-зависимости
	$(PYTHON) -m pip install -r requirements.txt

run: ## Запустить приложение на PORT (по умолчанию 8000)
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port $(PORT) --proxy-headers

test: ## Тесты
	$(PYTHON) -m pytest -q

lint: ## Проверка стиля (ruff)
	$(PYTHON) -m ruff check app tests

typecheck: ## Проверка типов (mypy)
	$(PYTHON) -m mypy

audit: ## Проверка зависимостей (pip-audit)
	$(PYTHON) -m pip_audit -r requirements.txt

backup: ## Бэкап SQLite в data/backup-<дата>.db
	@set -a; [ -f .env ] && . ./.env; set +a; \
	DB_PATH="$${DB_PATH:-$(DB_PATH)}" $(PYTHON) -c "import os, sqlite3, time; p=os.environ['DB_PATH']; d=os.path.dirname(p) or '.'; os.makedirs(d, exist_ok=True); src=sqlite3.connect(p); dst=sqlite3.connect(os.path.join(d, 'backup-' + time.strftime('%Y%m%d-%H%M%S') + '.db')); src.backup(dst); dst.close(); src.close(); print('backup ok')"

release: ## Показать чек-лист релиза
	@echo "Чек-лист релиза:"
	@echo "  1. .env заполнен (BOT_TOKEN, COACH_TELEGRAM_ID), ALLOW_DEV_INIT_DATA=false, ENABLE_API_DOCS=false"
	@echo "  2. HTTPS reverse-proxy настроен; при webhook — WEBHOOK_BASE_URL и WEBHOOK_SECRET (>=32)"
	@echo "  3. make install && make test (на сервере — make install-prod)"
	@echo "  4. systemctl restart sqassit && curl -fsS http://127.0.0.1:8000/readyz"
	@echo "  5. Открыть бота, /start, проверить расписание и запись"
	@echo "  6. Настроить регулярный бэкап (make backup по cron)"

clean: ## Удалить кэши (данные в ./data НЕ трогает)
	rm -rf __pycache__ app/__pycache__ tests/__pycache__ .pytest_cache .mypy_cache .ruff_cache

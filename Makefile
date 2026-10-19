# Makefile — операционные команды проекта (не запускается из кода приложения).
# Использование: make help

COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help build up down restart logs ps doctor shell test backup release clean

help: ## Показать список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

build: ## Собрать образ
	$(COMPOSE) build

up: ## Запустить (сборка + фон)
	$(COMPOSE) up -d --build

down: ## Остановить и удалить контейнеры (данные в ./data сохраняются)
	$(COMPOSE) down

restart: ## Перезапустить
	$(COMPOSE) restart

logs: ## Логи в реальном времени
	$(COMPOSE) logs -f

ps: ## Статус контейнера и health
	$(COMPOSE) ps

doctor: ## Диагностика: статус, health, окружение (без секретов), последние логи
	@echo "== containers =="; $(COMPOSE) ps
	@echo "== health =="; curl -s http://127.0.0.1:8000/healthz || echo "(нет ответа)"
	@echo; echo "== env (значения секретов скрыты) =="; \
	$(COMPOSE) exec -T squash-bot sh -lc 'echo "BOT_TOKEN set: $$([ -n "$$BOT_TOKEN" ] && echo yes || echo no)"; echo "COACH_TELEGRAM_ID: $$COACH_TELEGRAM_ID"; echo "BOT_MODE: $$BOT_MODE"; echo "WEBHOOK_BASE_URL: $$WEBHOOK_BASE_URL"; echo "ALLOW_DEV_INIT_DATA: $$ALLOW_DEV_INIT_DATA"' || echo "(контейнер не запущен)"
	@echo "== last logs (50) =="; $(COMPOSE) logs --tail=50 || true

shell: ## Зайти в контейнер (непривилегированный пользователь)
	$(COMPOSE) exec squash-bot sh

test: ## Локальные тесты (нужен python3.11 и установленные зависимости)
	python3.11 -m pytest -q || python3.11 -m compileall -q app

backup: ## Бэкап БД в ./data/backup-<дата>.db (hot backup SQLite)
	$(COMPOSE) exec -T squash-bot python -c "\
import sqlite3, time, os; \
src = sqlite3.connect(os.environ.get('DB_PATH', '/app/data/squash.db')); \
dst = sqlite3.connect('/app/data/backup-' + time.strftime('%Y%m%d-%H%M%S') + '.db'); \
src.backup(dst); dst.close(); src.close(); print('backup ok')"

release: ## Показать чек-лист релиза
	@echo "Чек-лист релиза:"
	@echo "  1. .env заполнен (BOT_TOKEN, COACH_TELEGRAM_ID), ALLOW_DEV_INIT_DATA=false"
	@echo "  2. HTTPS reverse-proxy настроен; при webhook — WEBHOOK_BASE_URL и случайный WEBHOOK_SECRET"
	@echo "  3. mkdir -p ./data && sudo chown -R 1000:1000 ./data"
	@echo "  4. make up && make ps (контейнер healthy)"
	@echo "  5. Открыть бота, /start, проверить расписание и запись"
	@echo "  6. Настроить регулярный бэкап (make backup по cron)"

clean: ## Удалить образы проекта (данные ./data НЕ трогает)
	$(COMPOSE) down --rmi local

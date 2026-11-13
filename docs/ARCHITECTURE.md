# Архитектура и потоки

Визуализации для репозитория: как SQASSIT устроен внутри, как проходит заявка
и как разделены личные чаты.

## Компоненты

<p align="center">
  <a href="diagrams/architecture.svg"><img src="diagrams/architecture.svg" alt="Архитектура SQASSIT: Telegram, FastAPI и SQLite" width="100%"></a>
</p>

Один процесс FastAPI обслуживает Web App, REST API, aiogram-бота и планировщик
напоминаний. Данные хранятся в SQLite. Telegram присылает `initData` для
авторизации, а бот — только уведомления и кнопки перехода в приложение.

`initData` живёт 24 часа и не обновляется в уже открытом WebView. Поэтому на
bootstrap-запросе `/api/me` сервер выдаёт непрозрачный токен сессии на 7 дней
(заголовок `X-Session-Token`); клиент хранит его в `sessionStorage`, а в БД
лежит только SHA-256. Токен отзывается при удалении профиля. Это позволяет
пережить восстановление iframe/bfcache без повторного запуска Mini App.

## Путь заявки

<p align="center">
  <a href="diagrams/booking-flow.svg"><img src="diagrams/booking-flow.svg" alt="Поток заявки: pending, решение тренера, booked / waitlist / rejected" width="100%"></a>
</p>

Заявка создаётся в статусе `pending` и не занимает место. Создатель занятия или
соавтор подтверждает или отклоняет её; после решения назначается `booked` или
`waitlist`, либо заявка получает `rejected`.

## Приватность чата

<p align="center">
  <a href="diagrams/chat-privacy.svg"><img src="diagrams/chat-privacy.svg" alt="Приватность чата: отдельный диалог с каждым игроком" width="100%"></a>
</p>

У тренера отдельный диалог с каждым игроком. Другие игроки и тренеры эти
переписки не видят, общего инбокса нет. Текст сообщений не пересылается в
Telegram — бот присылает только уведомление.

## Исходники

- [`diagrams/architecture.svg`](diagrams/architecture.svg)
- [`diagrams/booking-flow.svg`](diagrams/booking-flow.svg)
- [`diagrams/chat-privacy.svg`](diagrams/chat-privacy.svg)

Схемы статические, без внешних зависимостей и шрифтов; их можно править как
обычный SVG.

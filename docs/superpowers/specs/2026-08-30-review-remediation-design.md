# Review Remediation Design

## Goal

Исправить подтверждённые дефекты владения Plan, обработки ошибок Agent,
optimistic concurrency, жизненного цикла SQLite и актуальности модалки, а также
точно описать границу LLM/MCP и provenance демонстрации. Деплой, установка
Playwright-браузеров и превращение LLM в tool-calling agent в этот объём не входят.
Также сознательно отложены: форматирование длинной JSX-строки, переиспользование
одной MCP-сессии между вызовами, лимит размера Excel upload, унификация
локальных/UTC дат и согласование источника metadata при импорте (`plan_name`
сейчас берётся из первой строки, `plan_start` — из последней).

## Ownership Boundary

Cookie `kanbain_owner` остаётся анонимной идентичностью MVP. Только
`GET /api/plan` и `POST /api/import` имеют право создать owner cookie и Plan.
Остальные пользовательские endpoints не создают владельца или seed Plan.

`POST /api/turn` и `POST /api/chat` проверяют, что переданный `plan_id`
принадлежит owner из cookie. `POST /api/revert/{turn_id}` сначала находит Plan
хода, затем выполняет ту же проверку. Попытка обратиться к чужому существующему
Plan или Turn возвращает `404`, не подтверждая существование объекта. Отсутствие
обязательного owner context также возвращает `404`: в MVP нет authentication
challenge или credential, предъявление которого могло бы исправить `401`.
`GET /api/export` и `GET /api/turns` работают только с уже существующим Plan
текущего владельца и никогда не создают Plan как побочный эффект.

Проверка остаётся в HTTP boundary: внутренний Agent и локальный MCP используют
application service без browser cookie. Публичный mutating MCP сохраняет
собственную Bearer-авторизацию и не смешивается с анонимной browser session.

## Agent Streaming and Version Conflicts

NDJSON stream всегда завершается одним терминальным событием: `result` или
`error`. Ошибки модели, OpenRouter, MCP, валидации и неизвестного Plan,
возникшие внутри Agent task, преобразуются в `{"type":"error","detail":...}`.
Отмена клиентом соединения не маскируется как прикладная ошибка.

Каждая попытка Agent начинает работу со свежего `get_plan` через in-memory MCP.
Если между чтением и `apply_turn` возникает `StalePlanError`, следующая попытка
повторно читает Plan, заново строит prompt/детерминированную интерпретацию и
применяет mutation с новой `base_version`. Невалидные mutation также сохраняют
существующий максимум в три попытки. После третьего отказа Agent возвращает
актуальный Plan и диагностический ответ без частичного Turn.

Это повтор пользовательского намерения, а не байтовое повторение прежних
mutations. Например, «перенеси на неделю» после конкурентного сдвига считается
на семь дней от нового Schedule. Regression-тест обязан различать эти варианты
и подтверждать именно пересчитанную дату.

## SQLite Connection Lifetime

`PlanService` предоставляет один внутренний context manager соединения. Он
открывает connection с текущими PRAGMA, сохраняет существующую транзакционную
семантику `with connection` и гарантированно вызывает `close()` в `finally`.
Все методы сервиса используют только этот context manager. Атомарность
`apply_turn` и `revert_turn` остаётся неизменной.

## Frontend Task Selection

React хранит `selectedTaskId`, а не снимок `Task`. Выбранная задача,
предшественники и последователи вычисляются из текущего `plan` на каждом render.
После drag, Agent Turn, import или revert открытая модалка показывает актуальные
поля. Если задача исчезла из нового Plan, модалка закрывается естественно,
поскольку вычисленный `selectedTask` отсутствует.

## Documentation Accuracy

README и ADR различают три уровня:

- модель возвращает JSON с `mutations` и `reply`, а не вызывает MCP tools сама;
- `AgentService` получает Plan и применяет Turn через настоящий in-memory MCP
  transport;
- deterministic interpreter является production fallback без ключа и именно он
  использован в smoke-тесте, GIF и MP4.

README не называет demo доказательством живого LLM/OpenRouter пути. ADR-0001
описывает фактически используемый `mcp.server.fastmcp` и не ссылается на
`run_in_thread` из другой FastMCP-библиотеки. Live-provider acceptance остаётся
отдельной непроведённой проверкой.

## Tests and Acceptance

Изменения выполняются TDD-циклами: сначала regression-тест должен упасть по
ожидаемой причине, затем минимальная реализация делает его зелёным.

Backend acceptance покрывает:

- чужой owner не может выполнить Turn, Chat или Revert;
- запрос без cookie не экспортирует и не создаёт Plan;
- один HTTP client с cookie-jar проходит браузерную последовательность
  `GET /api/plan` → `POST /api/turn` → `GET /api/export`, а выгруженный workbook
  содержит изменённый Plan именно этого владельца;
- исключение модели выдаёт терминальное NDJSON `error`;
- StalePlan заставляет Agent перечитать snapshot и применить пересчитанное
  намерение к свежему Schedule, а не повторить старые mutations;
- каждое соединение PlanService закрывается, включая исключения.

Последний тест является осознанным white-box исключением из правила проверки
внешнего поведения: `sqlite3.connect` подменяется контролируемым connection и
фиксируется вызов `close()`. Внешняя альтернатива через подсчёт файловых
дескрипторов зависит от ОС и сборщика мусора и хуже локализует регрессию
владения ресурсом.

Frontend acceptance покрывает вывод актуальной задачи по id после замены Plan.
Финальная локальная проверка: полный backend/frontend test suite, Ruff check и
format check, TypeScript/Vite build. Playwright smoke не запускается, поскольку
браузеры пользователь устанавливать не будет. Деплой и live OpenRouter smoke не
выполняются и не заявляются как проверенные.

# 16 — Honker публикует версию Plan

**What to build:** После каждой успешной записи в Plan приложение публикует
сигнал `{plan_id, version}` в канал Honker поверх того же SQLite-файла. Пока это
никто не слушает — тикет считается сделанным, когда сигнал доказуемо доходит до
второго процесса.

Событие несёт **только идентификатор и версию**, не данные. Получатель сверяет
версию со своей и, если отстал, перезапрашивает Plan целиком. Так потеря сигнала
не приводит к расхождению, а лечится следующим сигналом или переподключением.

## Проверенные факты об API

Документация на сайте описывает `Database.notify` — **в 0.5.0 такого метода нет**.
Фактический API (проверен на этой машине, wheel `cp311-abi3`, работает и на
Python 3.14, и в образе `python:3.12-slim`):

```python
db = honker.open(path)  # honker.Database
with db.transaction() as tx:  # tx: execute, query, notify, bootstrap_honker_schema
    tx.notify("plan", {"plan_id": ..., "version": ...})

async for note in db.listen("plan", fallback_poll_s=15.0):
    note.channel, note.payload  # honker.Notification
```

Замеренная задержка между процессами на общем файле — **1.9 мс**.

Публикацию делаем **после** успешного коммита `apply_turn`/`revert_turn`, отдельной
транзакцией Honker. Атомарность с записью плана не нужна: потерянный сигнал
означает лишь, что наблюдатель увидит правку позже, а автор правки получает свежий
Plan прямо в ответе.

Ещё одна проверенная деталь: таблица уведомлений **не чистится сама** — «rows
accumulate until you call `db.prune_notifications(...)`». Без явной чистки файл БД
растёт с каждой правкой навсегда. Чистку вызываем на старте приложения; это ещё
один довод в пользу того, чтобы событие несло версию, а не план на 5.5 КБ.

**Blocked by:** 15 — Членство вместо владения.

**Status:** done

- [x] `honker` добавлен в зависимости с зафиксированной версией (`honker==0.5.0` в
      `pyproject.toml`, зафиксировано и в `uv.lock`). `requires-python` поднят до
      `>=3.11`, потому что honker 0.5.0 требует Python 3.11+. Wheel `cp311-abi3`
      установился и импортируется без сборки из исходников — проверено локально
      на Python 3.14 и **фактическим `docker build .`** на `python:3.12-slim`
      (образ Dockerfile не менялся): внутри контейнера
      `.venv/bin/python -c "import honker"` резолвится в
      `.venv/lib/python3.12/site-packages/honker/__init__.py`, приложение
      поднимается и отвечает на `/health` и `/api/plan` при `docker run`.
- [x] Схема Honker разворачивается в том же файле, что и таблицы приложения, и
      не конфликтует с ними (`PlanService.initialize()` вызывает
      `PlanNotifier.bootstrap()` сразу после `executescript` с таблицами
      приложения; `tests/test_notifications.py` пишет и читает по тому же файлу)
- [x] Каждая успешная запись публикует `{plan_id, version}` после коммита
      (`PlanService._publish_after_commit`, вызывается после выхода из
      `with self._lock, self._connect()` в `apply_turn`, `import_into_plan` и
      `revert_turn`)
- [x] Неудачная запись (конфликт версии, ошибка валидации) не публикует ничего
      (`test_failed_write_does_not_publish`)
- [x] Тест: второй процесс на том же файле получает сигнал с ожидаемой версией
      (`test_successful_turn_publishes_to_a_genuinely_separate_os_process` — реально
      спавнит `python tests/_cross_process_writer.py` через `subprocess.run` и
      слушает из тестового процесса; плюс более быстрый
      `test_successful_turn_publishes_plan_id_and_version_to_a_second_handle` для
      случая независимого хендла в том же процессе)
- [x] Падение публикации не роняет запись — план сохранён, сигнал потерян и это
      не фатально (`test_apply_turn_succeeds_even_when_publish_raises`;
      защита продублирована и в `PlanNotifier.publish`, и в
      `PlanService._publish_after_commit`)
- [x] `prune_notifications` вызывается на старте приложения; таблица уведомлений
      не растёт бесконечно (`service.notifier.prune()` в `create_app`,
      `test_prune_is_safe_to_call_and_returns_an_int`)

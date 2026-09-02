# 15 — Членство вместо владения

**What to build:** Второй человек открывает ссылку `/plan/{id}` и получает право
редактировать этот Plan наравне с первым. Сейчас на его месте 404, потому что
проверка привязывает Plan к владельцу из cookie.

⚠️ Проверку нельзя ослаблять — её нужно **заменить**. Отсутствие привязки объекта
к пользователю было подтверждённой дырой (чужой мог писать в любой Plan и
откатывать чужие ходы), и возврат к «проверяем только наличие cookie» вернёт её.
Модель доступа становится **capability-URL**: `plan_id` — это uuid4, и знание
ссылки и есть право на редактирование. Первое открытие ссылки добавляет владельца
cookie в участники Plan.

Цена этой модели: ссылка утекает в историю браузера, в реферер и в пересылку.
Это допустимо для демо, но должно быть написано в README прямым текстом, а не
умолчано.

**Blocked by:** None — can start immediately.

**Status:** done

- [x] Участники Plan хранятся явно, а не выводятся из единственного `owner_id`
      (таблица `plan_members(plan_id, member_id, joined_at)` в `gaintt/service.py`)
- [x] Открытие `/plan/{id}` добавляет текущего анонимного пользователя в участники
      (`GET /api/plan/{plan_id}` в `gaintt/main.py` вызывает `service.add_member`)
- [x] Запись и откат разрешены участнику, а не только создателю
      (`is_plan_member`/`is_turn_member` в `gaintt/main.py` заменили `owns_plan`/`owns_turn`)
- [x] Запрос к несуществующему Plan по-прежнему отдаёт 404 и не подтверждает существование id
      (`test_opening_a_nonexistent_plan_id_returns_404_without_confirming_or_denying_existence`)
- [x] Пользователь без cookie не может писать: cookie выдаётся при первом чтении
      (`test_reading_the_plan_url_without_a_cookie_issues_one_so_the_visitor_can_write_next`,
      `test_turn_chat_and_revert_without_owner_cookie_are_hidden`)
- [x] Существующие тесты на изоляцию переписаны под членство, а не удалены
      (`tests/test_http.py::test_a_stranger_who_never_read_this_plans_url_cannot_turn_chat_or_revert_it`
      заменил старый `test_other_owner_cannot_...`, плюс новый
      `test_opening_the_plan_url_grants_membership_that_allows_write_and_revert`)
- [x] README называет модель доступа capability-URL и её ограничения
      (раздел «Совместное редактирование» в `README.md`)

## Найден и исправлен дефект (P1-1), 2026-09-01

`/api/export` и `/api/turns` не участвовали в переходе на членство до конца:
вместо проверки `is_plan_member(request, plan_id)` (как у `/api/turn`,
`/api/chat`) они брали `service.get_member_plan(cookie)` — «Plan, в который этот
member_id входил последним по `joined_at`». Проблема в том, что `joined_at`
обновляется при **каждом** посещении `GET /api/plan/{plan_id}`
(`PlanService.add_member`), а этот эндпоинт вызывается не только при открытии
ссылки, но и при каждом SSE-триггерном перезапросе (`frontend/src/App.tsx`,
`EventSource.onmessage`). При двух вкладках на одном cookie (два разных Plan,
или вкладка A и вкладка, которую переоткрыли по ссылке B) фоновый refetch в
одной вкладке двигал общий для cookie указатель «последний Plan» — и экспорт /
список ходов из другой вкладки уходил не туда.

Исправлено: оба эндпоинта принимают `plan_id` явно и проверяют членство через
`is_plan_member`, как остальные пишущие/читающие ручки. `get_member_plan`
сохранён — он всё ещё нужен `get_or_create_plan` для голого `GET /api/plan`
(посещение `/` без id в пути), где семантика «последний открытый Plan»
осмысленна и не участвует в этом дефекте (см. комментарий над
`PlanService.add_member`).

Регрессия закрыта
`tests/test_http.py::test_export_and_turns_use_the_plan_id_of_the_calling_tab_not_the_most_recently_opened_plan`
(плюс отдельные `test_export_requires_plan_id_and_a_stranger_cannot_export_a_plan_they_never_opened`
на проверку доступа). Проверено падением на добаговом коде (временный откат
`export_excel`/`turns` к `get_member_plan`) — падало ровно там, где ожидалось:
экспорт стороннего давал 200 вместо 404, счёт ходов путал Plan A и Plan B.

Фронтенд (`frontend/src/App.tsx`) обновлён: кнопка «Выгрузить Excel» была
статической ссылкой `href="/api/export"` без `plan_id` — теперь это
`fetch`-запрос с явным `plan_id` текущего Plan и скачиванием через blob-URL.
Регрессия на рассинхрон контракта клиент/сервер закрыта
`frontend/src/App.test.ts` ("export and import wire the current plan_id").

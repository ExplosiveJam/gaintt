import json
import sqlite3
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from kanbain.agent import AgentService
from kanbain.excel import import_plan
from kanbain.main import create_app
from kanbain.service import PlanService


def sample_excel() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["задача", "описание", "исполнитель", "длительность", "предшественники"])
    sheet.append(["Импорт", "", "Иванов", 1, ""])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_http_plan_turn_import_export_and_health(tmp_path):
    client = TestClient(create_app(tmp_path / "plans.sqlite"))

    assert client.get("/health").json() == {"status": "ok"}
    plan_response = client.get("/api/plan")
    plan = plan_response.json()["plan"]
    assert len(plan["tasks"]) >= 15

    turn_response = client.post(
        "/api/turn",
        json={
            "plan_id": plan["id"],
            "base_version": plan["version"],
            "mutations": [{"type": "reassign", "task_id": plan["tasks"][0]["id"], "assignee": "QA"}],
        },
    )
    assert turn_response.status_code == 200
    assert turn_response.json()["plan"]["version"] == plan["version"] + 1

    imported = client.post(
        "/api/import",
        data={"plan_id": plan["id"], "base_version": turn_response.json()["plan"]["version"]},
        files={"file": ("plan.xlsx", sample_excel(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert imported.status_code == 200
    assert imported.json()["report"]["loaded_count"] == 1
    # Import replaces the SAME Plan (P1-3): id is unchanged, version keeps advancing.
    assert imported.json()["plan"]["id"] == plan["id"]
    assert imported.json()["plan"]["version"] == turn_response.json()["plan"]["version"] + 1
    exported = client.get("/api/export", params={"plan_id": plan["id"]})
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/vnd.openxmlformats")


def test_import_requires_membership_and_a_stranger_cannot_import_into_a_plan(tmp_path):
    app = create_app(tmp_path / "import-membership.sqlite")
    owner = TestClient(app)
    stranger = TestClient(app)
    plan = owner.get("/api/plan").json()["plan"]
    stranger.get("/api/plan")  # gets their own, unrelated Plan and cookie

    foreign_import = stranger.post(
        "/api/import",
        data={"plan_id": plan["id"], "base_version": plan["version"]},
        files={"file": ("plan.xlsx", sample_excel(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert foreign_import.status_code == 404


def test_import_replaces_the_open_plan_in_place_and_is_visible_to_another_member_without_reload(tmp_path):
    """P1-3: importer's Excel must land on the Plan every member already has open.

    Regression guard: before the fix, /api/import called replace_member_plan,
    which minted a brand-new Plan id owned only by the importer -- other members
    stayed on the old Plan, no signal was published, and the URL of anyone who
    already had this Plan open kept pointing at content that no longer matched
    what the importer sees.
    """
    app = create_app(tmp_path / "import-live.sqlite")
    importer = TestClient(app)
    other_member = TestClient(app)
    plan = importer.get("/api/plan").json()["plan"]
    other_member.get(f"/api/plan/{plan['id']}")  # becomes a member via the capability URL

    imported = importer.post(
        "/api/import",
        data={"plan_id": plan["id"], "base_version": plan["version"]},
        files={"file": ("plan.xlsx", sample_excel(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert imported.status_code == 200
    assert imported.json()["plan"]["id"] == plan["id"]  # same Plan, not a new one

    # The other member reads through the SAME plan_id/URL and sees the import,
    # with no separate signal needed to prove membership -- version advanced and
    # the content is the imported one.
    seen_by_other = other_member.get(f"/api/plan/{plan['id']}").json()["plan"]
    assert seen_by_other["version"] == imported.json()["plan"]["version"]
    assert seen_by_other["tasks"][0]["name"] == "Импорт"


def test_import_rejects_a_stale_base_version_instead_of_overwriting_a_concurrent_turn(tmp_path):
    app = create_app(tmp_path / "import-stale.sqlite")
    client = TestClient(app)
    plan = client.get("/api/plan").json()["plan"]

    turn = client.post(
        "/api/turn",
        json={
            "plan_id": plan["id"],
            "base_version": plan["version"],
            "mutations": [{"type": "reassign", "task_id": plan["tasks"][0]["id"], "assignee": "CONCURRENT"}],
        },
    )
    assert turn.status_code == 200

    imported = client.post(
        "/api/import",
        data={"plan_id": plan["id"], "base_version": plan["version"]},
        files={"file": ("plan.xlsx", sample_excel(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert imported.status_code == 409
    assert imported.json()["plan"]["version"] == turn.json()["plan"]["version"]
    current = app.state.service.get_plan(plan["id"])
    assert current.tasks[plan["tasks"][0]["id"]].assignee == "CONCURRENT"


def test_direct_import_without_a_cookie_still_creates_a_plan_and_member_cookie(tmp_path):
    client = TestClient(create_app(tmp_path / "direct-import.sqlite"))

    imported = client.post(
        "/api/import",
        files={"file": ("plan.xlsx", sample_excel(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert imported.status_code == 200
    assert imported.json()["plan"]["tasks"][0]["name"] == "Импорт"
    assert "kanbain_member" in imported.cookies


def test_mcp_http_is_disabled_by_default_and_exposes_write_only_with_token(tmp_path, monkeypatch):
    disabled = TestClient(create_app(tmp_path / "disabled.sqlite"))
    assert disabled.post("/mcp", json={}).status_code == 404

    monkeypatch.setenv("MCP_WRITE_TOKEN", "test-token")
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    with TestClient(create_app(tmp_path / "enabled.sqlite", mcp_enabled=True), base_url="http://127.0.0.1:8000") as enabled:
        readonly = enabled.post("/mcp/", json=request, headers=headers)
        writable = enabled.post("/mcp/", json=request, headers={**headers, "Authorization": "Bearer test-token"})

    assert readonly.status_code == 200
    assert [tool["name"] for tool in readonly.json()["result"]["tools"]] == ["get_plan", "find_tasks"]
    assert writable.status_code == 200
    assert [tool["name"] for tool in writable.json()["result"]["tools"]] == ["get_plan", "find_tasks", "apply_turn"]


def test_chat_streams_stages_before_the_final_result(tmp_path):
    with TestClient(create_app(tmp_path / "chat-stream.sqlite")) as client:
        plan = client.get("/api/plan").json()["plan"]

        with client.stream(
            "POST", "/api/chat", json={"plan_id": plan["id"], "message": "Перенеси задачу Развернуть демо на неделю"}
        ) as response:
            events = [json.loads(line) for line in response.iter_lines() if line]

    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert [event["stage"] for event in events[:3]] == ["анализирую", "проверяю", "применяю"]
    assert events[-1]["type"] == "result"
    assert events[-1]["result"]["changes"]


def test_a_stranger_who_never_read_this_plans_url_cannot_turn_chat_or_revert_it(tmp_path):
    """Isolation without any capability link opened: two independent Plans, no overlap."""
    app = create_app(tmp_path / "isolation.sqlite")
    owner = TestClient(app)
    attacker = TestClient(app)
    plan = owner.get("/api/plan").json()["plan"]
    attacker.get("/api/plan")  # gets their own, unrelated Plan and cookie

    applied = owner.post(
        "/api/turn",
        json={
            "plan_id": plan["id"],
            "base_version": plan["version"],
            "mutations": [{"type": "reassign", "task_id": plan["tasks"][0]["id"], "assignee": "OWNER"}],
        },
    )
    turn_id = applied.json()["turn_id"]
    current = applied.json()["plan"]

    foreign_turn = attacker.post(
        "/api/turn",
        json={
            "plan_id": plan["id"],
            "base_version": current["version"],
            "mutations": [{"type": "reassign", "task_id": plan["tasks"][0]["id"], "assignee": "ATTACKER"}],
        },
    )
    foreign_chat = attacker.post("/api/chat", json={"plan_id": plan["id"], "message": "Перенеси задачу Развернуть демо на неделю"})
    foreign_revert = attacker.post(f"/api/revert/{turn_id}")

    assert foreign_turn.status_code == 404
    assert foreign_chat.status_code == 404
    assert foreign_revert.status_code == 404
    assert app.state.service.get_plan(plan["id"]).tasks[plan["tasks"][0]["id"]].assignee == "OWNER"


def test_opening_the_plan_url_grants_membership_that_allows_write_and_revert(tmp_path):
    """The core of the capability-URL model: knowing plan_id and reading it once is enough."""
    app = create_app(tmp_path / "capability-url.sqlite")
    creator = TestClient(app)
    invitee = TestClient(app)

    plan = creator.get("/api/plan").json()["plan"]
    opened = invitee.get(f"/api/plan/{plan['id']}")
    assert opened.status_code == 200
    assert opened.json()["plan"]["id"] == plan["id"]

    assert opened.json()["plan"]["member_count"] == 2

    invitee_turn = invitee.post(
        "/api/turn",
        json={
            "plan_id": plan["id"],
            "base_version": plan["version"],
            "mutations": [{"type": "reassign", "task_id": plan["tasks"][0]["id"], "assignee": "INVITEE"}],
        },
    )
    assert invitee_turn.status_code == 200
    turn_id = invitee_turn.json()["turn_id"]

    creator_revert = creator.post(f"/api/revert/{turn_id}")
    assert creator_revert.status_code == 200
    original_assignee = plan["tasks"][0]["assignee"]
    assert app.state.service.get_plan(plan["id"]).tasks[plan["tasks"][0]["id"]].assignee == original_assignee


def test_opening_a_nonexistent_plan_id_returns_404_without_confirming_or_denying_existence(tmp_path):
    app = create_app(tmp_path / "nonexistent-plan.sqlite")
    client = TestClient(app)

    response = client.get("/api/plan/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert "set-cookie" not in response.headers


def test_reading_the_plan_url_without_a_cookie_issues_one_so_the_visitor_can_write_next(tmp_path):
    app = create_app(tmp_path / "cookie-on-first-read.sqlite")
    creator = TestClient(app)
    plan = creator.get("/api/plan").json()["plan"]

    invitee = TestClient(app)
    opened = invitee.get(f"/api/plan/{plan['id']}")

    assert "kanbain_member" in opened.cookies
    turn = invitee.post(
        "/api/turn",
        json={"plan_id": plan["id"], "base_version": plan["version"], "mutations": []},
    )
    assert turn.status_code == 200


def test_export_without_owner_cookie_does_not_create_a_plan(tmp_path):
    database = tmp_path / "anonymous-export.sqlite"
    app = create_app(database)
    creator = TestClient(app)
    plan = creator.get("/api/plan").json()["plan"]
    anonymous = TestClient(app)

    response = anonymous.get("/api/export", params={"plan_id": plan["id"]})

    with sqlite3.connect(database) as connection:
        plan_count = connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    assert response.status_code == 404
    assert plan_count == 1  # the creator's own visit made exactly one Plan


def test_cookie_owner_can_turn_and_export_the_changed_plan(tmp_path):
    client = TestClient(create_app(tmp_path / "owned-export.sqlite"))
    plan = client.get("/api/plan").json()["plan"]
    task = plan["tasks"][0]

    applied = client.post(
        "/api/turn",
        json={
            "plan_id": plan["id"],
            "base_version": plan["version"],
            "mutations": [{"type": "reassign", "task_id": task["id"], "assignee": "QA-OWNER"}],
        },
    )
    exported = client.get("/api/export", params={"plan_id": plan["id"]})
    round_tripped, report = import_plan(exported.content)

    assert applied.status_code == 200
    assert exported.status_code == 200
    assert not report.errors
    assert next(item for item in round_tripped.tasks.values() if item.name == task["name"]).assignee == "QA-OWNER"


def test_export_requires_plan_id_and_a_stranger_cannot_export_a_plan_they_never_opened(tmp_path):
    app = create_app(tmp_path / "export-membership.sqlite")
    owner = TestClient(app)
    stranger = TestClient(app)
    plan = owner.get("/api/plan").json()["plan"]
    stranger.get("/api/plan")  # gets their own, unrelated Plan and cookie

    response = stranger.get("/api/export", params={"plan_id": plan["id"]})

    assert response.status_code == 404


def test_export_and_turns_use_the_plan_id_of_the_calling_tab_not_the_most_recently_opened_plan(tmp_path):
    """P1-1 regression guard.

    One cookie, two tabs open on two different Plans (A created first, B opened
    second via its capability URL). Before the fix, /api/export and /api/turns
    ignored the plan_id the tab actually asked about and instead used
    get_member_plan (`ORDER BY joined_at DESC`), which always points at whichever
    Plan was opened -- i.e. re-GET -- most recently. Since GET /api/plan/{id}
    (used by every SSE-triggered refetch, not just the initial open) bumps
    joined_at, a refetch in tab B alone was enough to make tab A's own export
    silently download Plan B.
    """
    app = create_app(tmp_path / "two-tabs-two-plans.sqlite")
    tab_a = TestClient(app)
    tab_b = TestClient(app)

    plan_a = tab_a.get("/api/plan").json()["plan"]
    plan_b = tab_b.get("/api/plan").json()["plan"]
    cookie = tab_a.cookies.get("kanbain_member")
    tab_b.cookies.set("kanbain_member", cookie)  # same person, second tab: same cookie

    # tab_b "re-opens" (or SSE-refetches) Plan B, which is exactly what used to
    # move get_member_plan's "most recently joined" pointer onto Plan B.
    tab_b.get(f"/api/plan/{plan_b['id']}")
    turn_on_b = tab_b.post(
        "/api/turn",
        json={
            "plan_id": plan_b["id"],
            "base_version": plan_b["version"],
            "mutations": [{"type": "reassign", "task_id": plan_b["tasks"][0]["id"], "assignee": "ON-B"}],
        },
    )
    assert turn_on_b.status_code == 200

    exported_from_tab_a = tab_a.get("/api/export", params={"plan_id": plan_a["id"]})
    round_tripped, _ = import_plan(exported_from_tab_a.content)
    assert exported_from_tab_a.status_code == 200
    assert set(round_tripped.tasks.keys()) == {task["id"] for task in plan_a["tasks"]}

    turns_from_tab_a = tab_a.get("/api/turns", params={"plan_id": plan_a["id"]})
    assert turns_from_tab_a.status_code == 200
    assert turns_from_tab_a.json()["turns"] == []  # Plan A has no turns of its own -- must not show Plan B's


def test_chat_model_failure_ends_with_an_error_event(tmp_path):
    database = tmp_path / "chat-error.sqlite"
    service = PlanService(database)
    service.initialize()

    def failing_model(*_):
        raise RuntimeError("OpenRouter 401")

    app = create_app(database, agent=AgentService(service, model=failing_model))
    with TestClient(app, raise_server_exceptions=False) as client:
        plan = client.get("/api/plan").json()["plan"]
        response = client.post("/api/chat", json={"plan_id": plan["id"], "message": "Сделай правку"})

    events = [json.loads(line) for line in response.iter_lines() if line]
    assert response.status_code == 200
    assert events and events[-1] == {"type": "error", "detail": "OpenRouter 401"}


def test_owner_cookie_can_be_marked_secure_for_https_deployments(tmp_path, monkeypatch):
    monkeypatch.setenv("KANBAIN_COOKIE_SECURE", "true")

    response = TestClient(create_app(tmp_path / "secure-cookie.sqlite")).get("/api/plan")

    assert "Secure" in response.headers["set-cookie"]


def test_turn_chat_and_revert_without_owner_cookie_are_hidden(tmp_path):
    app = create_app(tmp_path / "missing-owner.sqlite")
    owner = TestClient(app)
    anonymous = TestClient(app)
    plan = owner.get("/api/plan").json()["plan"]
    applied = owner.post(
        "/api/turn",
        json={
            "plan_id": plan["id"],
            "base_version": plan["version"],
            "mutations": [{"type": "reassign", "task_id": plan["tasks"][0]["id"], "assignee": "OWNER"}],
        },
    )

    turn = anonymous.post(
        "/api/turn",
        json={"plan_id": plan["id"], "base_version": applied.json()["plan"]["version"], "mutations": []},
    )
    chat = anonymous.post("/api/chat", json={"plan_id": plan["id"], "message": "Сделай правку"})
    revert = anonymous.post(f"/api/revert/{applied.json()['turn_id']}")

    assert [turn.status_code, chat.status_code, revert.status_code] == [404, 404, 404]

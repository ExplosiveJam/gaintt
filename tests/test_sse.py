import asyncio
import json
import threading
import time

import httpx
import uvicorn
from fastapi.testclient import TestClient

from gaintt.main import create_app


def read_sse_payload(line: str) -> dict:
    assert line.startswith("data: ")
    return json.loads(line[len("data: ") :])


class _LiveServer:
    """A real uvicorn server on a background thread.

    SSE relies on the ASGI server correctly distinguishing "client still
    connected, just waiting" from "client disconnected" via long-lived
    receive()/send(). In-memory ASGI transports used by TestClient don't model
    that faithfully -- they end the stream as soon as the request has been
    fully sent. A genuine socket is required to test the awaiting-for-events
    behaviour end to end.
    """

    def __init__(self, app):
        self.config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive():
            if time.monotonic() >= deadline:
                raise RuntimeError("uvicorn did not start within 5 seconds")
            time.sleep(0.01)
        if not self.server.started:
            raise RuntimeError("uvicorn stopped before accepting connections")
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc_info):
        self.server.should_exit = True
        self.thread.join(timeout=5)


def test_sse_requires_membership(tmp_path):
    app = create_app(tmp_path / "sse-membership.sqlite")
    owner = TestClient(app)
    stranger = TestClient(app)
    plan = owner.get("/api/plan").json()["plan"]
    stranger.get("/api/plan")  # gets an unrelated Plan and cookie, never opens this one

    response = stranger.get(f"/api/plan/{plan['id']}/events")

    assert response.status_code == 404


async def test_sse_first_frame_carries_the_current_plan_version(tmp_path):
    app = create_app(tmp_path / "sse-first-frame.sqlite")
    client = TestClient(app)
    plan = client.get("/api/plan").json()["plan"]
    member_cookie = client.cookies.get("gaintt_member")

    # Someone else's write happens before this client ever subscribes.
    client.post(
        "/api/turn",
        json={
            "plan_id": plan["id"],
            "base_version": plan["version"],
            "mutations": [{"type": "reassign", "task_id": plan["tasks"][0]["id"], "assignee": "Q"}],
        },
    )

    with _LiveServer(app) as live:
        async with httpx.AsyncClient(base_url=live.base_url, cookies={"gaintt_member": member_cookie}) as async_client:
            async with async_client.stream("GET", f"/api/plan/{plan['id']}/events") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                first_line = await asyncio.wait_for(response.aiter_lines().__anext__(), timeout=5)
                payload = read_sse_payload(first_line)

    assert payload == {"plan_id": plan["id"], "version": plan["version"] + 1}


async def test_sse_delivers_an_event_when_another_client_writes_and_versions_converge(tmp_path):
    database = tmp_path / "sse-converge.sqlite"
    app = create_app(database)
    subscriber = TestClient(app)
    writer = TestClient(app)
    plan = subscriber.get("/api/plan").json()["plan"]
    writer.get(f"/api/plan/{plan['id']}")  # writer becomes a member via the capability URL
    subscriber_cookie = subscriber.cookies.get("gaintt_member")

    with _LiveServer(app) as live:
        async with httpx.AsyncClient(base_url=live.base_url, cookies={"gaintt_member": subscriber_cookie}) as client:
            async with client.stream("GET", f"/api/plan/{plan['id']}/events") as response:
                assert response.status_code == 200
                lines = response.aiter_lines()

                async def next_nonblank_line():
                    line = await asyncio.wait_for(lines.__anext__(), timeout=5)
                    while not line:
                        line = await asyncio.wait_for(lines.__anext__(), timeout=5)
                    return line

                first = read_sse_payload(await next_nonblank_line())
                assert first["version"] == plan["version"]

                applied = writer.post(
                    "/api/turn",
                    json={
                        "plan_id": plan["id"],
                        "base_version": plan["version"],
                        "mutations": [{"type": "reassign", "task_id": plan["tasks"][0]["id"], "assignee": "WRITER"}],
                    },
                )
                assert applied.status_code == 200

                second = read_sse_payload(await next_nonblank_line())

    assert second == {"plan_id": plan["id"], "version": plan["version"] + 1}


async def test_sse_write_between_subscribe_and_first_frame_is_delivered(tmp_path, monkeypatch):
    """P1-2: a write landing between subscribing and sending the first frame must
    still reach the client.

    We inject the competing write from inside the first `get_plan` call: the
    returned snapshot is version N, but the committed database is already N+1
    before the first frame is sent. The immediate authoritative recheck after
    that frame must therefore deliver N+1 even if Honker starts from a later
    MAX(id) and does not replay the signal.
    """
    database = tmp_path / "sse-race.sqlite"
    app = create_app(database)
    subscriber = TestClient(app)

    plan = subscriber.get("/api/plan").json()["plan"]
    subscriber_cookie = subscriber.cookies.get("gaintt_member")

    service = app.state.service
    original_get_plan = service.get_plan
    triggered = {"done": False}

    def get_plan_and_race(plan_id_arg):
        result = original_get_plan(plan_id_arg)
        if not triggered["done"] and plan_id_arg == plan["id"]:
            triggered["done"] = True
            service.apply_turn(
                plan["id"],
                plan["version"],
                [{"type": "reassign", "task_id": plan["tasks"][0]["id"], "assignee": "RACED"}],
            )
        return result

    monkeypatch.setattr(service, "get_plan", get_plan_and_race)

    with _LiveServer(app) as live:
        async with httpx.AsyncClient(base_url=live.base_url, cookies={"gaintt_member": subscriber_cookie}) as client:
            async with client.stream("GET", f"/api/plan/{plan['id']}/events") as response:
                assert response.status_code == 200
                lines = response.aiter_lines()

                async def next_nonblank_line():
                    line = await asyncio.wait_for(lines.__anext__(), timeout=5)
                    while not line:
                        line = await asyncio.wait_for(lines.__anext__(), timeout=5)
                    return line

                first = read_sse_payload(await next_nonblank_line())
                assert first["version"] == plan["version"]  # written by get_plan_and_race AFTER this snapshot

                second = read_sse_payload(await next_nonblank_line())

    assert second == {"plan_id": plan["id"], "version": plan["version"] + 1}


async def test_sse_reconnecting_after_a_drop_still_converges(tmp_path):
    """First connection is dropped mid-stream; a fresh subscription still lands on the latest version."""
    database = tmp_path / "sse-reconnect.sqlite"
    app = create_app(database)
    subscriber = TestClient(app)
    plan = subscriber.get("/api/plan").json()["plan"]
    subscriber_cookie = subscriber.cookies.get("gaintt_member")

    with _LiveServer(app) as live:
        async with httpx.AsyncClient(base_url=live.base_url, cookies={"gaintt_member": subscriber_cookie}) as client:
            async with client.stream("GET", f"/api/plan/{plan['id']}/events") as response:
                await response.aiter_lines().__anext__()
            # response context manager exited -> connection dropped without an explicit close call

        subscriber.post(
            "/api/turn",
            json={
                "plan_id": plan["id"],
                "base_version": plan["version"],
                "mutations": [{"type": "reassign", "task_id": plan["tasks"][0]["id"], "assignee": "RECONNECTED"}],
            },
        )

        async with httpx.AsyncClient(base_url=live.base_url, cookies={"gaintt_member": subscriber_cookie}) as client:
            async with client.stream("GET", f"/api/plan/{plan['id']}/events") as response:
                line = await response.aiter_lines().__anext__()
                while not line:
                    line = await response.aiter_lines().__anext__()
                payload = read_sse_payload(line)

    assert payload == {"plan_id": plan["id"], "version": plan["version"] + 1}

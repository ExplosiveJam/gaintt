"""FastAPI HTTP boundary for Gaintt."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .agent import AgentService
from .domain import DomainValidationError
from .excel import export_plan, import_plan
from .mcp_tools import build_fastmcp
from .service import PlanNotFoundError, PlanService, StalePlanError

SSE_RECONCILE_SECONDS = 5.0

# Capability-URL access model (see README "Совместное редактирование"): a plan_id
# is a uuid4 and knowing it is sufficient to become a member. This cookie is only
# a pointer to "who is asking", never an ownership claim -- see is_plan_member.
MEMBER_COOKIE = "gaintt_member"


class MCPDispatcher:
    """Choose read-only or authorized FastMCP app without duplicating tools."""

    def __init__(self, readonly: Any, full: Any, write_token: str) -> None:
        self.readonly = readonly
        self.full = full
        self.write_token = write_token

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        supplied = headers.get("authorization", "")
        expected = f"Bearer {self.write_token}" if self.write_token else ""
        selected = self.full if expected and hmac.compare_digest(supplied, expected) else self.readonly
        await selected(scope, receive, send)


def _frontend_dist() -> Path:
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"


def create_app(
    db_path: Optional[Path | str] = None,
    agent: Optional[AgentService] = None,
    mcp_enabled: Optional[bool] = None,
) -> FastAPI:
    database = Path(db_path or os.getenv("GAINTT_DB_PATH", "data/gaintt.sqlite"))
    service = PlanService(database)
    service.initialize()
    mcp_servers = []

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with AsyncExitStack() as stack:
            for server in mcp_servers:
                await stack.enter_async_context(server.session_manager.run())
            yield

    app = FastAPI(title="Gaintt", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.state.agent = agent or AgentService(service)
    cookie_secure = os.getenv("GAINTT_COOKIE_SECURE", "false").lower() == "true"

    enabled = mcp_enabled if mcp_enabled is not None else os.getenv("MCP_HTTP_ENABLED", "false").lower() == "true"
    if enabled:
        readonly_server = build_fastmcp(service, include_write=False)
        full_server = build_fastmcp(service, include_write=True)
        mcp_servers.extend([readonly_server, full_server])
        app.mount(
            "/mcp",
            MCPDispatcher(
                readonly_server.streamable_http_app(),
                full_server.streamable_http_app(),
                os.getenv("MCP_WRITE_TOKEN", ""),
            ),
        )

    service.notifier.prune()

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    def member_id(request: Request) -> str:
        return request.cookies.get(MEMBER_COOKIE) or str(uuid.uuid4())

    def is_plan_member(request: Request, plan_id: str) -> bool:
        """Capability-URL access: writing requires membership, not creation.

        Membership is granted only by GET /api/plan/{plan_id} (see that handler);
        no cookie means no membership, and a missing Plan must not be
        distinguishable from a Plan the caller merely isn't a member of.
        """
        current_member = request.cookies.get(MEMBER_COOKIE)
        if not current_member:
            return False
        try:
            return service.is_member(plan_id, current_member)
        except PlanNotFoundError:
            return False

    def is_turn_member(request: Request, turn_id: str) -> bool:
        current_member = request.cookies.get(MEMBER_COOKIE)
        if not current_member:
            return False
        try:
            plan_id = service.plan_id_for_turn(turn_id)
            return service.is_member(plan_id, current_member)
        except PlanNotFoundError:
            return False

    def hidden_resource() -> JSONResponse:
        return JSONResponse({"detail": "Resource not found"}, status_code=404)

    def set_member_cookie(response: JSONResponse, current_member: str) -> None:
        response.set_cookie(
            MEMBER_COOKIE,
            current_member,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            max_age=60 * 60 * 24 * 365,
        )

    def plan_payload(service_plan: Any) -> Dict[str, Any]:
        payload = service_plan.to_public_dict()
        payload["turns"] = service.list_turns(service_plan.id)
        payload["member_count"] = service.member_count(service_plan.id)
        return payload

    @app.get("/api/plan")
    async def read_plan(request: Request) -> JSONResponse:
        current_member = member_id(request)
        plan = service.get_or_create_plan(current_member)
        response = JSONResponse({"plan": plan_payload(plan)})
        if not request.cookies.get(MEMBER_COOKIE):
            set_member_cookie(response, current_member)
        return response

    @app.get("/api/plan/{plan_id}")
    async def read_plan_by_id(request: Request, plan_id: str) -> JSONResponse:
        """Capability-URL entry point: opening this link grants membership.

        A nonexistent Plan and a Plan the caller has no reason to see must look
        identical from the outside -- both are a plain 404, and neither one sets
        a cookie, so probing ids can't be told apart from a real miss.
        """
        current_member = member_id(request)
        try:
            service.add_member(plan_id, current_member)
        except PlanNotFoundError:
            return hidden_resource()
        plan = service.get_plan(plan_id)
        response = JSONResponse({"plan": plan_payload(plan)})
        if not request.cookies.get(MEMBER_COOKIE):
            set_member_cookie(response, current_member)
        return response

    @app.post("/api/turn")
    async def apply_turn(request: Request) -> JSONResponse:
        body = await request.json()
        if not is_plan_member(request, body.get("plan_id", "")):
            return hidden_resource()
        try:
            result = service.apply_turn(body["plan_id"], body["base_version"], body.get("mutations", []))
        except StalePlanError as exc:
            current = service.get_plan(body["plan_id"])
            return JSONResponse({"detail": str(exc), "plan": plan_payload(current)}, status_code=409)
        except (DomainValidationError, KeyError, PlanNotFoundError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        return JSONResponse({"plan": plan_payload(result.plan), "changes": result.changes, "turn_id": result.turn_id})

    @app.post("/api/chat")
    async def chat(request: Request) -> Response:
        body = await request.json()
        if not is_plan_member(request, body.get("plan_id", "")):
            return hidden_resource()

        async def events():
            stages: asyncio.Queue[str] = asyncio.Queue()
            task = asyncio.create_task(app.state.agent.handle(body["plan_id"], body["message"], stage_callback=stages.put))
            while not task.done() or not stages.empty():
                try:
                    stage = await asyncio.wait_for(stages.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                yield json.dumps({"type": "stage", "stage": stage}, ensure_ascii=False) + "\n"
            try:
                result = await task
            except Exception as exc:
                yield json.dumps({"type": "error", "detail": str(exc)}, ensure_ascii=False) + "\n"
                return
            yield json.dumps({"type": "result", "result": result}, ensure_ascii=False) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.post("/api/import")
    async def import_excel(
        request: Request,
        file: UploadFile = File(...),
        plan_id: Optional[str] = Form(None),
        base_version: Optional[int] = Form(None),
    ) -> JSONResponse:
        # Imports the file INTO the Plan the caller already has open (same id, a
        # fresh version), rather than minting a new Plan: other members must see
        # the result without a page reload, and the browser URL must keep working
        # on refresh -- see P1-3 in the ticket / docs/adr/0003.
        if plan_id and not is_plan_member(request, plan_id):
            return hidden_resource()
        if plan_id and base_version is None:
            return JSONResponse({"detail": "base_version is required when plan_id is provided"}, status_code=400)
        try:
            imported, report = import_plan(await file.read())
            if plan_id:
                replacement = service.import_into_plan(plan_id, base_version, imported)
            else:
                current_member = member_id(request)
                replacement = service.create_imported_plan(current_member, imported)
        except StalePlanError as exc:
            current = service.get_plan(plan_id) if plan_id else None
            return JSONResponse(
                {"detail": str(exc), "plan": plan_payload(current)} if current else {"detail": str(exc)},
                status_code=409,
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except PlanNotFoundError:
            return hidden_resource()
        response = JSONResponse({"plan": plan_payload(replacement), "report": report.to_dict()})
        if not request.cookies.get(MEMBER_COOKIE):
            set_member_cookie(response, current_member)
        return response

    @app.get("/api/export")
    async def export_excel(request: Request, plan_id: str) -> Response:
        # Must name the Plan explicitly and check membership like /api/turn and
        # /api/chat do -- see P1-1: the tab that calls this is not necessarily the
        # one holding this member's most-recently-opened Plan (get_member_plan's
        # "last joined" pointer moves on every SSE-triggered refetch of ANY tab
        # sharing this cookie, including other Plans).
        if not is_plan_member(request, plan_id):
            return hidden_resource()
        plan = service.get_plan(plan_id)
        return StreamingResponse(
            BytesIO(export_plan(plan)),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="gaintt-plan.xlsx"'},
        )

    @app.post("/api/revert/{turn_id}")
    async def revert(request: Request, turn_id: str) -> JSONResponse:
        if not is_turn_member(request, turn_id):
            return hidden_resource()
        try:
            result = service.revert_turn(turn_id)
        except (StalePlanError, PlanNotFoundError) as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return JSONResponse({"plan": plan_payload(result.plan), "changes": result.changes, "turn_id": turn_id})

    @app.get("/api/turns")
    async def turns(request: Request, plan_id: str) -> JSONResponse:
        # See export_excel above for why this takes plan_id explicitly now.
        if not is_plan_member(request, plan_id):
            return hidden_resource()
        return JSONResponse({"turns": service.list_turns(plan_id)})

    @app.get("/api/plan/{plan_id}/events")
    async def plan_events(request: Request, plan_id: str) -> Response:
        """SSE stream of {plan_id, version} signals for members of this Plan.

        db.listen starts at the current MAX(id) and never replays history, so a
        client that connects after someone else's write would stay stale forever
        without help. The first frame closes that gap by carrying the Plan's
        current version unconditionally, before any Honker notification arrives.
        """
        if not is_plan_member(request, plan_id):
            return hidden_resource()

        async def events():
            current = service.get_plan(plan_id)
            last_sent_version = current.version
            yield f"data: {json.dumps({'plan_id': plan_id, 'version': current.version}, ensure_ascii=False)}\n\n"

            # A Listener object exists already, but Honker may initialize its MAX(id)
            # cursor on the first __anext__. Re-read the authoritative Plan before
            # waiting, then keep doing so on a bounded timeout. This closes both the
            # snapshot/subscription boundary and the separate-transaction case where
            # a committed write succeeds but its notification is lost.
            listener = service.notifier.listen(fallback_poll_s=5.0)
            pending_note = asyncio.create_task(listener.__anext__())
            try:
                while True:
                    latest = service.get_plan(plan_id)
                    if latest.version > last_sent_version:
                        last_sent_version = latest.version
                        payload = {"plan_id": plan_id, "version": latest.version}
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                    done, _ = await asyncio.wait({pending_note}, timeout=SSE_RECONCILE_SECONDS)
                    if not done:
                        continue
                    try:
                        note = pending_note.result()
                    except StopAsyncIteration:
                        return
                    pending_note = asyncio.create_task(listener.__anext__())
                    payload = note.payload
                    if (
                        isinstance(payload, dict)
                        and payload.get("plan_id") == plan_id
                        and isinstance(payload.get("version"), int)
                        and payload["version"] > last_sent_version
                    ):
                        last_sent_version = payload["version"]
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                # Honker's Rust-backed __anext__ does not complete promptly when
                # awaited after cancellation. Mark the task cancelled and let the
                # connection-scoped generator release its final reference instead
                # of blocking SSE disconnect indefinitely.
                pending_note.cancel()

        return StreamingResponse(events(), media_type="text/event-stream")

    dist = _frontend_dist()
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", response_class=HTMLResponse)
    @app.get("/plan/{plan_id}", response_class=HTMLResponse)
    async def index(plan_id: Optional[str] = None) -> Any:
        # Same SPA shell for "/" and for a capability URL "/plan/{id}" -- the
        # client reads plan_id from the path itself (see model.planIdFromPath).
        index_file = dist / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return HTMLResponse("<h1>Gaintt</h1><p>Frontend is not built. Run <code>pnpm install && pnpm build</code>.</p>")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("gaintt.main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

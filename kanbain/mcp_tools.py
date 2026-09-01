"""One MCP tool registry shared by the internal Agent and the HTTP server."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from .domain import DomainValidationError
from .service import PlanService, StalePlanError


class MCPToolCallError(RuntimeError):
    """A tool returned a machine-readable application error."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class MCPToolRegistry:
    def __init__(self, service: PlanService, include_write: bool = True) -> None:
        self.service = service
        self.include_write = include_write

    def get_plan(self, plan_id: str) -> Dict[str, Any]:
        return self.service.get_plan(plan_id).to_public_dict()

    def find_tasks(self, plan_id: str, query: str) -> List[Dict[str, Any]]:
        plan = self.service.get_plan(plan_id)
        needle = query.strip().casefold()
        schedule = plan.schedule()
        return [
            {
                "id": task.id,
                "name": task.name,
                "assignee": task.assignee,
                "start": schedule[task.id].start.isoformat(),
                "last_day": schedule[task.id].last_day.isoformat(),
            }
            for task in plan.tasks.values()
            if needle in task.name.casefold() or needle in task.assignee.casefold() or needle in task.id.casefold()
        ]

    def apply_turn(self, plan_id: str, base_version: int, mutations: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self.include_write:
            raise PermissionError("Mutating MCP tools require authorization")
        try:
            result = self.service.apply_turn(plan_id, base_version, mutations)
        except (DomainValidationError, StalePlanError) as exc:
            return {"error": {"code": exc.code, "detail": str(exc)}}
        return {"plan": result.plan.to_public_dict(), "changes": result.changes, "turn_id": result.turn_id}


class InMemoryMCPClient:
    """Small in-process transport used by the Agent and deterministic tests.

    It deliberately calls the same named tools as the FastMCP server; no domain
    logic is duplicated in the transport.
    """

    def __init__(self, transport: MCPToolRegistry | FastMCP) -> None:
        self.registry = transport if isinstance(transport, MCPToolRegistry) else None
        self.server = transport if isinstance(transport, FastMCP) else None

    @staticmethod
    def _unwrap_application_error(value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            error = value["error"]
            raise MCPToolCallError(str(error.get("code", "tool_error")), str(error.get("detail", "Tool call failed")))
        return value

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        if self.registry is not None:
            method = getattr(self.registry, name)
            return self._unwrap_application_error(method(**arguments))
        assert self.server is not None
        async with create_connected_server_and_client_session(self.server) as client:
            result = await client.call_tool(name, arguments)
        if result.isError:
            detail = "; ".join(getattr(item, "text", str(item)) for item in result.content)
            raise MCPToolCallError("tool_error", detail)
        if result.structuredContent is not None:
            structured = result.structuredContent
            value = structured.get("result", structured) if isinstance(structured, dict) else structured
            return self._unwrap_application_error(value)
        if not result.content:
            return None
        content = result.content[0]
        text = getattr(content, "text", "")
        try:
            return self._unwrap_application_error(json.loads(text))
        except (TypeError, ValueError):
            return text


def build_fastmcp(service: PlanService, include_write: bool = True) -> FastMCP:
    registry = MCPToolRegistry(service, include_write=include_write)
    server = FastMCP(
        "Kanbain",
        instructions="Редактируйте ровно один Plan через apply_turn.",
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
    )

    @server.tool()
    def get_plan(plan_id: str) -> Dict[str, Any]:
        """Read a complete Plan with derived Schedule and links."""
        return registry.get_plan(plan_id)

    @server.tool()
    def find_tasks(plan_id: str, query: str) -> List[Dict[str, Any]]:
        """Find Tasks by id, name, or Assignee."""
        return registry.find_tasks(plan_id, query)

    if include_write:

        @server.tool()
        def apply_turn(plan_id: str, base_version: int, mutations: List[Dict[str, Any]]) -> Dict[str, Any]:
            """Apply one atomic Turn to a Plan."""
            return registry.apply_turn(plan_id, base_version, mutations)

    return server

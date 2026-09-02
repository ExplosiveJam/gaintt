import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import type { Plan } from "./model";

vi.mock("@svar-ui/react-gantt", () => ({ Gantt: () => null }));
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const plan: Plan = {
  id: "plan-1",
  name: "Plan",
  plan_start: "2026-09-01",
  version: 1,
  tasks: [
    {
      id: "task-1",
      name: "Task",
      description: "",
      assignee: "Before",
      duration: 1,
      predecessors: [],
      pinned_start: null,
      due_date: null,
      start: "2026-09-01",
      finish: "2026-09-02",
      last_day: "2026-09-01",
      overdue: false
    }
  ],
  links: []
};

const updatedPlan: Plan = {
  ...plan,
  version: 2,
  tasks: plan.tasks.map((task) => ({ ...task, assignee: "After" }))
};

let root: Root | undefined;
let container: HTMLDivElement | undefined;

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  container?.remove();
  root = undefined;
  container = undefined;
  vi.unstubAllGlobals();
});

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((event: { data: string }) => void) | null = null;
  closed = false;
  url: string;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }

  close() {
    this.closed = true;
  }
}

describe("live updates via SSE", () => {
  it("refetches and shows a notice when another participant's version arrives, and closes the modal on deletion", async () => {
    const remotePlan: Plan = { ...plan, version: 2, tasks: [] };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/plan") {
        return new Response(JSON.stringify({ plan }), { headers: { "Content-Type": "application/json" } });
      }
      if (url === `/api/plan/${plan.id}`) {
        return new Response(JSON.stringify({ plan: remotePlan }), { headers: { "Content-Type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
    FakeEventSource.instances = [];
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(React.createElement(App));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const taskButton = container.querySelector<HTMLButtonElement>(".task-index-row");
    await act(async () => taskButton?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(container.querySelector(".task-modal")).not.toBeNull();

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe(`/api/plan/${plan.id}/events`);

    await act(async () => {
      FakeEventSource.instances[0].emit({ plan_id: plan.id, version: 2 });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(container.textContent).toContain("План обновлён другим участником");
    // The Task shown in the modal was removed by the remote edit -> modal closes.
    expect(container.querySelector(".task-modal")).toBeNull();

    // Leaving the page must close the subscription so the Honker listener doesn't leak.
    expect(FakeEventSource.instances[0].closed).toBe(false);
    await act(async () => root?.unmount());
    root = undefined;
    expect(FakeEventSource.instances[0].closed).toBe(true);
  });
});

describe("task modal", () => {
  it("shows the selected task from the latest chat plan snapshot", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/plan") {
        return new Response(JSON.stringify({ plan }), { headers: { "Content-Type": "application/json" } });
      }
      if (url === "/api/chat") {
        const event = { type: "result", result: { plan: updatedPlan, reply: "Updated", changes: [] } };
        return new Response(`${JSON.stringify(event)}\n`, { headers: { "Content-Type": "application/x-ndjson" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(React.createElement(App));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const taskButton = container.querySelector<HTMLButtonElement>(".task-index-row");
    await act(async () => taskButton?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(container.querySelector(".task-modal")?.textContent).toContain("Before");

    const textarea = container.querySelector<HTMLTextAreaElement>("textarea");
    const valueSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    await act(async () => {
      valueSetter?.call(textarea, "Update Task");
      textarea?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const form = container.querySelector<HTMLFormElement>("form");
    await act(async () => {
      form?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(container.querySelector(".task-modal")?.textContent).toContain("After");
  });
});

describe("Russian interface copy", () => {
  it("renders the main workspace and task details without leftover English labels", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ plan }), { headers: { "Content-Type": "application/json" } })));
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
    FakeEventSource.instances = [];
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(React.createElement(App));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await act(async () => container?.querySelector<HTMLButtonElement>(".task-index-row")?.click());

    expect(container.querySelector(".brand-mark")?.textContent).toBe("G");
    expect(container.textContent).toContain("РАБОЧИЙ ПЛАН");
    expect(container.textContent).toContain("ВИЗУАЛЬНЫЙ ГРАФИК");
    expect(container.textContent).toContain("ЧАТ С АГЕНТОМ");
    expect(container.textContent).toContain("НАЧАЛО ПО РАСПИСАНИЮ");
    expect(container.textContent).not.toMatch(/WORKING PLAN|VISUAL SCHEDULE|AGENT CONSOLE|TASK DETAILS|Schedule start|Pinned Start|Due Date/);
  });
});

describe("export and import wire the current plan_id", () => {
  // Regression guard: the backend now requires plan_id on both /api/export
  // (query string, P1-1) and /api/import (multipart field, P1-3) and checks
  // membership with it -- a client that still calls the old, plan_id-less
  // contract gets a 404 in the browser, invisible to any backend-only test.
  it("the export button requests /api/export with the open plan's id", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/plan") {
        return new Response(JSON.stringify({ plan }), { headers: { "Content-Type": "application/json" } });
      }
      if (url === `/api/export?plan_id=${plan.id}`) {
        return new Response(new Blob(["xlsx-bytes"]), {
          headers: { "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:fake"), revokeObjectURL: vi.fn() });
    // jsdom has no real download sink; the app's download link would otherwise
    // attempt (and log a warning for) an actual browser navigation.
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(React.createElement(App));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const exportButton = Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "Выгрузить Excel");
    await act(async () => {
      exportButton?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(fetchMock).toHaveBeenCalledWith(`/api/export?plan_id=${plan.id}`);
    clickSpy.mockRestore();
  });

  it("uploading a file sends plan_id alongside it in the form data", async () => {
    const importedPlan: Plan = { ...plan, version: 2 };
    let capturedBody: FormData | undefined;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/plan") {
        return new Response(JSON.stringify({ plan }), { headers: { "Content-Type": "application/json" } });
      }
      if (url === "/api/import") {
        capturedBody = init?.body as FormData;
        return new Response(
          JSON.stringify({ plan: importedPlan, report: { summary: "Загружено", warnings: [], errors: [] } }),
          { headers: { "Content-Type": "application/json" } }
        );
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(React.createElement(App));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const fileInput = container.querySelector<HTMLInputElement>('input[type="file"]');
    const file = new File(["a,b"], "plan.xlsx");
    const fileListLike = { 0: file, length: 1, item: () => file };
    Object.defineProperty(fileInput, "files", { value: fileListLike });
    await act(async () => {
      fileInput?.dispatchEvent(new Event("change", { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(fetchMock).toHaveBeenCalledWith("/api/import", expect.objectContaining({ method: "POST" }));
    // The assertion is made outside the fetch mock on purpose: a failure
    // thrown from inside the mock is swallowed by uploadFile's own try/catch
    // (it just calls setError), so it would never fail the test.
    expect(capturedBody).toBeDefined();
    expect(capturedBody?.get("plan_id")).toBe(plan.id);
    expect(capturedBody?.get("base_version")).toBe(String(plan.version));
    expect(capturedBody?.get("file")).toBeInstanceOf(File);
  });
});

describe("a write in flight is never reported as someone else's edit", () => {
  // P2-1 regression guard: the SSE signal for this tab's own write can arrive
  // before that write's own HTTP response does. Before the fix, the client only
  // compared payload.version to its local version, so it could not tell its own
  // in-flight write apart from a genuinely remote one and flashed "План обновлён
  // другим участником" on its own edit.
  it("suppresses the remote-edit banner for a version bump that arrives while this tab's own chat write is still pending", async () => {
    let resolveChat!: (value: Response) => void;
    const chatPromise = new Promise<Response>((resolve) => {
      resolveChat = resolve;
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/plan") {
        return new Response(JSON.stringify({ plan }), { headers: { "Content-Type": "application/json" } });
      }
      if (url === `/api/plan/${plan.id}`) {
        return new Response(JSON.stringify({ plan: updatedPlan }), { headers: { "Content-Type": "application/json" } });
      }
      if (url === "/api/chat") {
        return chatPromise;
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
    FakeEventSource.instances = [];
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(React.createElement(App));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const textarea = container.querySelector<HTMLTextAreaElement>("textarea");
    const valueSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    await act(async () => {
      valueSetter?.call(textarea, "Перенеси задачу");
      textarea?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const form = container.querySelector<HTMLFormElement>("form");
    // Submit but do not await completion -- the chat POST is deliberately
    // stuck on chatPromise, mirroring the request still being in flight.
    act(() => {
      form?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    // The SSE signal for this very write's own commit arrives before the chat
    // POST's own HTTP response does.
    await act(async () => {
      FakeEventSource.instances[0].emit({ plan_id: plan.id, version: 2 });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(container.textContent).not.toContain("План обновлён другим участником");

    // Now let the chat POST itself resolve; still must not have shown the banner.
    await act(async () => {
      const event = { type: "result", result: { plan: updatedPlan, reply: "Готово", changes: [] } };
      resolveChat(new Response(`${JSON.stringify(event)}\n`, { headers: { "Content-Type": "application/x-ndjson" } }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(container.textContent).not.toContain("План обновлён другим участником");
  });

  it("still shows the banner for a remote edit that is not tied to a pending write of this tab's own", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/plan") {
        return new Response(JSON.stringify({ plan }), { headers: { "Content-Type": "application/json" } });
      }
      if (url === `/api/plan/${plan.id}`) {
        return new Response(JSON.stringify({ plan: updatedPlan }), { headers: { "Content-Type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
    FakeEventSource.instances = [];
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(React.createElement(App));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    await act(async () => {
      FakeEventSource.instances[0].emit({ plan_id: plan.id, version: 2 });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(container.textContent).toContain("План обновлён другим участником");
  });

  it("shows a remote notice and keeps the newer snapshot when a later remote version arrives during a local write", async () => {
    const remotePlan: Plan = { ...updatedPlan, version: 3, name: "Remote v3" };
    let resolveChat!: (value: Response) => void;
    const chatPromise = new Promise<Response>((resolve) => {
      resolveChat = resolve;
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/plan") {
        return new Response(JSON.stringify({ plan }), { headers: { "Content-Type": "application/json" } });
      }
      if (url === `/api/plan/${plan.id}`) {
        return new Response(JSON.stringify({ plan: remotePlan }), { headers: { "Content-Type": "application/json" } });
      }
      if (url === "/api/chat") return chatPromise;
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", FakeEventSource as unknown as typeof EventSource);
    FakeEventSource.instances = [];
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(React.createElement(App));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    const textarea = container.querySelector<HTMLTextAreaElement>("textarea");
    const valueSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    await act(async () => {
      valueSetter?.call(textarea, "Перенеси задачу");
      textarea?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    act(() => container?.querySelector<HTMLFormElement>("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));

    await act(async () => {
      // The observed signal can still be our own v2 while the refetch already
      // sees another participant's immediately-following v3.
      FakeEventSource.instances[0].emit({ plan_id: plan.id, version: 2 });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(container.textContent).toContain("Remote v3");
    expect(container.textContent).not.toContain("План обновлён другим участником");

    await act(async () => {
      const ownResult = { type: "result", result: { plan: updatedPlan, reply: "Готово", changes: [] } };
      resolveChat(new Response(`${JSON.stringify(ownResult)}\n`, { headers: { "Content-Type": "application/x-ndjson" } }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(container.textContent).toContain("Remote v3");
    expect(container.textContent).toContain("План обновлён другим участником");
  });
});

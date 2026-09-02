import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Gantt } from "@svar-ui/react-gantt";
import "@svar-ui/react-gantt/all.css";
import {
  dragDiffToDate,
  formatDate,
  isOverdue,
  planIdFromPath,
  planUrl,
  taskById,
  toGanttLinks,
  toGanttTasks,
  type Plan,
  type Task,
  type Turn
} from "./model";

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  changes?: { label: string }[];
  turnId?: string;
};

type ChatResult = {
  plan: Plan;
  reply: string;
  changes: { label: string }[];
  turn_id?: string;
  candidates?: Task[];
};

const initialMessages: ChatMessage[] = [
  {
    role: "assistant",
    content: "Я вижу ваш план. Напишите, например: «перенеси задачу Подключить диаграмму Гантта на неделю»."
  }
];

async function jsonRequest<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    const error = Object.assign(new Error(payload.detail || "Запрос не выполнен"), {
      status: response.status,
      payload
    });
    throw error;
  }
  return payload as T;
}

async function streamChat(url: string, options: RequestInit, onStage: (stage: string) => void): Promise<ChatResult> {
  const response = await fetch(url, options);
  if (!response.ok || !response.body) {
    throw new Error(response.ok ? "Сервер не открыл поток ответа" : `Запрос не выполнен: ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: ChatResult | undefined;

  const consume = (line: string) => {
    if (!line.trim()) return;
    const event = JSON.parse(line) as
      | { type: "stage"; stage: string }
      | { type: "result"; result: ChatResult }
      | { type: "error"; detail: string };
    if (event.type === "stage") onStage(event.stage);
    if (event.type === "result") result = event.result;
    if (event.type === "error") throw new Error(event.detail);
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    lines.forEach(consume);
    if (done) break;
  }
  consume(buffer);
  if (!result) throw new Error("Сервер завершил поток без результата");
  return result;
}

function slack(task: Task): string {
  if (!task.due_date) return "Срок не задан";
  const days = Math.round((new Date(`${task.due_date}T00:00:00Z`).getTime() - new Date(`${task.last_day}T00:00:00Z`).getTime()) / 86400000);
  return days < 0 ? `просрочено на ${Math.abs(days)} дн.` : `${days} дн. в запасе`;
}

export default function App() {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState("");
  const [error, setError] = useState("");
  const [importReport, setImportReport] = useState<{ summary: string; warnings: string[]; errors: string[] } | null>(null);
  const [remoteNotice, setRemoteNotice] = useState(false);
  const [shareFeedback, setShareFeedback] = useState("");
  const planRef = useRef<Plan | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const selectedTaskIdRef = useRef<string | null>(null);
  // True while this tab has a write (turn/chat/import/revert) in flight. The
  // SSE signal can outrace that write's own HTTP response -- see P2-1: without
  // this, a client would see its own edit arrive as a version bump from "someone
  // else" and flash the "another participant changed the Plan" banner on itself.
  // While a write is pending we still refetch and apply the fresh Plan (so a
  // genuinely different participant's edit landing in the same window is not
  // lost -- convergence holds either way), we just suppress the banner, because
  // we cannot tell "it's my own write landing early" apart from "someone else
  // wrote at the same time" without a per-write id round-tripped through the
  // event, which the server does not carry (see notifications.py).
  const pendingWriteRef = useRef(false);
  const pendingSignalVersionRef = useRef(0);

  useEffect(() => {
    selectedTaskIdRef.current = selectedTaskId;
  }, [selectedTaskId]);

  const replacePlan = useCallback((next: Plan) => {
    const previous = planRef.current;
    if (previous?.id === next.id && next.version < previous.version) return;
    planRef.current = next;
    setPlan(next);
    // A Task edited or removed by someone else must update or close an open modal.
    if (previous?.id === next.id && selectedTaskIdRef.current) {
      const stillExists = next.tasks.some((task) => task.id === selectedTaskIdRef.current);
      if (!stillExists) setSelectedTaskId(null);
    }
  }, []);

  const showRemoteNotice = useCallback(() => {
    setRemoteNotice(true);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setRemoteNotice(false), 4000);
  }, []);

  const beginLocalWrite = useCallback(() => {
    pendingWriteRef.current = true;
    pendingSignalVersionRef.current = 0;
  }, []);

  const finishLocalWrite = useCallback((ownVersion?: number) => {
    const pendingSignalVersion = pendingSignalVersionRef.current;
    pendingWriteRef.current = false;
    pendingSignalVersionRef.current = 0;
    if (pendingSignalVersion > (ownVersion ?? 0)) showRemoteNotice();
  }, [showRemoteNotice]);

  useEffect(() => {
    const requestedId = planIdFromPath(window.location.pathname);
    const request = requestedId
      ? jsonRequest<{ plan: Plan }>(`/api/plan/${requestedId}`)
      : jsonRequest<{ plan: Plan }>("/api/plan");
    request
      .then(({ plan: next }) => {
        replacePlan(next);
        // A fresh visit to "/" gets its own Plan; push its capability URL so a
        // refresh or a copied link keeps working, without adding a history entry.
        window.history.replaceState(null, "", planUrl(next.id));
      })
      .catch((reason: Error) => setError(reason.message));
  }, [replacePlan]);

  // Live updates: subscribe once per Plan id (not per version) so our own writes,
  // which already update local state via their own response, don't churn the
  // connection. The event carries only {plan_id, version} -- a re-fetch is what
  // actually brings the data, which is also how a reconnect converges again.
  useEffect(() => {
    const planId = plan?.id;
    if (!planId || typeof EventSource === "undefined") return undefined;
    const source = new EventSource(`/api/plan/${planId}/events`);
    source.onmessage = (event) => {
      let payload: { plan_id?: string; version?: number };
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      if (payload.plan_id !== planId || typeof payload.version !== "number") return;
      const localVersion = planRef.current?.version ?? 0;
      if (payload.version <= localVersion) return;
      const isOwnWriteInFlight = pendingWriteRef.current;
      if (isOwnWriteInFlight) {
        pendingSignalVersionRef.current = Math.max(pendingSignalVersionRef.current, payload.version);
      }
      jsonRequest<{ plan: Plan }>(`/api/plan/${planId}`)
        .then(({ plan: fresh }) => {
          replacePlan(fresh);
          // Don't call this edit "another participant's" while we have a write of
          // our own in flight -- it may well be that very write's own signal
          // arriving before its HTTP response did. State still converges above
          // either way; only the notice is suppressed.
          if (isOwnWriteInFlight) {
            pendingSignalVersionRef.current = Math.max(pendingSignalVersionRef.current, fresh.version);
            return;
          }
          showRemoteNotice();
        })
        .catch(() => undefined);
    };
    return () => source.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan?.id, replacePlan, showRemoteNotice]);

  const applyDrag = useCallback(
    async (taskId: string, diffDays: number) => {
      const current = planRef.current;
      const task = current?.tasks.find((item) => item.id === taskId);
      if (!current || !task) return;
      const date = dragDiffToDate(task.start, diffDays);
      const sendPin = (candidate: Plan, candidateDate: string) => jsonRequest<{ plan: Plan }>("/api/turn", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan_id: candidate.id,
          base_version: candidate.version,
          mutations: [{ type: "pin_start", task_id: taskId, date: candidateDate }]
        })
      });
      setBusy(true);
      setError("");
      beginLocalWrite();
      let completedVersion: number | undefined;
      try {
        let result: { plan: Plan };
        try {
          result = await sendPin(current, date);
        } catch (reason) {
          const stale = reason as Error & { status?: number; payload?: { plan?: Plan } };
          if (stale.status !== 409 || !stale.payload?.plan) throw reason;
          const fresh = stale.payload.plan;
          const freshTask = fresh.tasks.find((item) => item.id === taskId);
          replacePlan(fresh);
          result = await sendPin(fresh, freshTask ? dragDiffToDate(freshTask.start, diffDays) : date);
        }
        replacePlan(result.plan);
        completedVersion = result.plan.version;
      } catch (reason) {
        setError((reason as Error).message);
      } finally {
        setBusy(false);
        finishLocalWrite(completedVersion);
      }
    },
    [beginLocalWrite, finishLocalWrite, replacePlan]
  );

  const initGantt = useCallback(
    (api: { intercept: (event: string, handler: (event: { id: string | number; diff?: number }) => boolean) => void; on: (event: string, handler: (event: { id: string | number }) => void) => void }) => {
      api.intercept("update-task", (event) => {
        if (typeof event.diff === "number" && event.diff !== 0) {
          void applyDrag(String(event.id), event.diff);
          return false;
        }
        return true;
      });
      api.on("open-task", (event) => {
        const task = planRef.current?.tasks.find((item) => item.id === String(event.id));
        if (task) setSelectedTaskId(task.id);
      });
    },
    [applyDrag]
  );

  const ganttTasks = useMemo(() => (plan ? toGanttTasks(plan) : []), [plan]);
  const ganttLinks = useMemo(() => (plan ? toGanttLinks(plan) : []), [plan]);
  const selectedTask = useMemo(() => taskById(plan, selectedTaskId), [plan, selectedTaskId]);

  async function sendMessage(event: React.FormEvent) {
    event.preventDefault();
    const current = planRef.current;
    const message = draft.trim();
    if (!current || !message || busy) return;
    setMessages((items) => [...items, { role: "user", content: message }]);
    setDraft("");
    setBusy(true);
    setError("");
    beginLocalWrite();
    let completedVersion: number | undefined;
    try {
      const result = await streamChat("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_id: current.id, message })
      }, setStage);
      replacePlan(result.plan);
      completedVersion = result.plan.version;
      const reply = result.candidates?.length
        ? `${result.reply} ${result.candidates.map((item) => `${item.name} — ${item.assignee}, ${formatDate(item.start)}`).join("; ")}`
        : result.reply;
      setMessages((items) => [...items, { role: "assistant", content: reply, changes: result.changes, turnId: result.turn_id }]);
    } catch (reason) {
      setError((reason as Error).message);
      setMessages((items) => [...items, { role: "assistant", content: `Не получилось: ${(reason as Error).message}` }]);
    } finally {
      setBusy(false);
      setStage("");
      finishLocalWrite(completedVersion);
    }
  }

  async function uploadFile(file: File) {
    const current = planRef.current;
    if (!current) return;
    const data = new FormData();
    data.append("plan_id", current.id);
    data.append("base_version", String(current.version));
    data.append("file", file);
    setBusy(true);
    setError("");
    beginLocalWrite();
    let completedVersion: number | undefined;
    try {
      const result = await jsonRequest<{ plan: Plan; report: { summary: string; warnings: string[]; errors: string[] } }>("/api/import", { method: "POST", body: data });
      replacePlan(result.plan);
      completedVersion = result.plan.version;
      setImportReport(result.report);
      setMessages((items) => [...items, { role: "assistant", content: result.report.summary }]);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
      finishLocalWrite(completedVersion);
    }
  }

  async function downloadExport() {
    const current = planRef.current;
    if (!current) return;
    try {
      const response = await fetch(`/api/export?plan_id=${encodeURIComponent(current.id)}`);
      if (!response.ok) throw new Error(`Запрос не выполнен: ${response.status}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "gaintt-plan.xlsx";
      link.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  async function revert(turnId: string) {
    if (busy) return;
    setBusy(true);
    beginLocalWrite();
    let completedVersion: number | undefined;
    try {
      const result = await jsonRequest<{ plan: Plan }>(`/api/revert/${turnId}`, { method: "POST" });
      replacePlan(result.plan);
      completedVersion = result.plan.version;
      setMessages((items) => [...items, { role: "assistant", content: "Ход откатан." }]);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
      finishLocalWrite(completedVersion);
    }
  }

  const successors = selectedTask && plan
    ? plan.tasks.filter((task) => task.predecessors.includes(selectedTask.id))
    : [];
  const predecessors = selectedTask && plan
    ? selectedTask.predecessors.map((id) => plan.tasks.find((task) => task.id === id)).filter(Boolean) as Task[]
    : [];

  async function shareLink() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setShareFeedback("Ссылка скопирована");
    } catch {
      setShareFeedback(window.location.href);
    }
    setTimeout(() => setShareFeedback(""), 3000);
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">K</span><div><strong>Gaintt</strong><span>план, который держит форму</span></div></div>
        <div className="toolbar">
          <button className="button secondary" onClick={() => void shareLink()} disabled={!plan}>Поделиться ссылкой</button>
          {shareFeedback && <span className="share-feedback">{shareFeedback}</span>}
          <button className="button secondary" onClick={() => fileRef.current?.click()} disabled={busy}>Импорт Excel</button>
          <input ref={fileRef} className="visually-hidden" type="file" accept=".xlsx" onChange={(event) => event.target.files?.[0] && void uploadFile(event.target.files[0])} />
          <button className="button primary" onClick={() => void downloadExport()} disabled={!plan}>Выгрузить Excel</button>
        </div>
      </header>

      <main className="content">
        <section className="intro-row"><div><p className="eyebrow">WORKING PLAN / {plan ? `v${plan.version}` : "loading"}</p><h1>{plan?.name || "Загружаю план…"}</h1><p className="subhead">Schedule считается на сервере. Перетаскивайте бары или объясняйте правку агенту — результат всегда приходит целиком.</p></div><div className="status-pill"><span className="status-dot" /> один Plan · календарные дни{plan?.member_count !== undefined && plan.member_count > 1 && <span className="presence-badge"> · {plan.member_count} участника в этом Plan</span>}</div></section>

        {remoteNotice && <div className="alert notice">План обновлён другим участником.</div>}
        {error && <div className="alert error">{error}</div>}
        {importReport && <div className="alert"><strong>{importReport.summary}</strong>{importReport.warnings.length > 0 && <><span> Предупреждений: {importReport.warnings.length}.</span><ul>{importReport.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></>}{importReport.errors.length > 0 && <><span> Ошибок: {importReport.errors.length}.</span><ul>{importReport.errors.map((item) => <li key={item}>{item}</li>)}</ul></>}<button className="close-alert" onClick={() => setImportReport(null)}>×</button></div>}

        <section className="workspace">
          <div className="gantt-card">
            <div className="card-heading"><div><span className="section-kicker">VISUAL SCHEDULE</span><h2>Диаграмма Гантта</h2></div><span className="date-chip">Старт · {plan ? formatDate(plan.plan_start) : "—"}</span></div>
            <div className="gantt-wrap">
              {plan && <Gantt tasks={ganttTasks} links={ganttLinks} init={initGantt} readonly={busy} />}
            </div>
            <div className="task-legend"><span><i className="legend-swatch normal" /> задача</span><span><i className="legend-swatch overdue" /> срок нарушен</span><span>↔ тяните бар, чтобы задать привязку</span></div>
            <div className="task-index">
              {plan?.tasks.map((task) => <button key={task.id} className={`task-index-row ${isOverdue(task) ? "overdue" : ""}`} onClick={() => setSelectedTaskId(task.id)}><span className="task-index-name">{task.name}</span><span>{task.assignee || "Без исполнителя"}</span><span>{formatDate(task.start)} → {formatDate(task.last_day)}</span></button>)}
            </div>
          </div>

          <aside className="chat-card">
            <div className="card-heading"><div><span className="section-kicker">AGENT CONSOLE</span><h2>Чат с агентом</h2></div><span className="online-dot">●</span></div>
            <div className="chat-messages">
              {messages.map((message, index) => <div key={`${index}-${message.content}`} className={`message ${message.role}`}><div className="message-label">{message.role === "assistant" ? "AGENT" : "ВЫ"}</div><div className="message-body">{message.content}</div>{message.changes && message.changes.length > 0 && <div className="change-list">{message.changes.map((change) => <div key={change.label}>✓ {change.label}</div>)}{message.turnId && <button className="revert-button" onClick={() => revert(message.turnId!)} disabled={!plan?.turns?.find((turn) => turn.id === message.turnId)?.can_revert}>Откатить ход</button>}</div>}</div>)}
              {busy && <div className="working"><span className="spinner" /> {stage || "обрабатываю"}</div>}
            </div>
            <form className="chat-form" onSubmit={sendMessage}><textarea aria-label="Сообщение агенту" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Например: перенеси всё после релиза на неделю…" disabled={busy} /><button className="send-button" type="submit" disabled={busy || !draft.trim()}>↑</button></form>
            <p className="chat-footnote">Изменения применяются одним атомарным ходом. Диаграмма не меняется, пока агент проверяет запрос.</p>
          </aside>
        </section>
      </main>

      {selectedTask && <div className="modal-backdrop" role="presentation" onClick={() => setSelectedTaskId(null)}><section className="task-modal" role="dialog" aria-modal="true" aria-label={`Задача ${selectedTask.name}`} onClick={(event) => event.stopPropagation()}><button className="modal-close" onClick={() => setSelectedTaskId(null)}>×</button><span className="section-kicker">TASK DETAILS</span><h2>{selectedTask.name}</h2><p className="modal-description">{selectedTask.description || "Описание не задано"}</p><div className="detail-grid"><div><span>Исполнитель</span><strong>{selectedTask.assignee || "Без исполнителя"}</strong></div><div><span>Длительность</span><strong>{selectedTask.duration} дн.</strong></div><div><span>Schedule start</span><strong>{formatDate(selectedTask.start)}</strong></div><div><span>Schedule finish</span><strong>{formatDate(selectedTask.last_day)}</strong></div><div><span>Pinned Start</span><strong>{formatDate(selectedTask.pinned_start)}</strong></div><div><span>Due Date</span><strong className={isOverdue(selectedTask) ? "danger-text" : ""}>{formatDate(selectedTask.due_date)}</strong></div></div><div className="slack-box"><span>Запас до срока</span><strong className={isOverdue(selectedTask) ? "danger-text" : ""}>{slack(selectedTask)}</strong></div><div className="relations"><div><span>Предшественники</span>{predecessors.length ? predecessors.map((task) => <button key={task.id} onClick={() => setSelectedTaskId(task.id)}>{task.name}</button>) : <em>нет</em>}</div><div><span>Последователи</span>{successors.length ? successors.map((task) => <button key={task.id} onClick={() => setSelectedTaskId(task.id)}>{task.name}</button>) : <em>нет</em>}</div></div></section></div>}
    </div>
  );
}

import { formatDate, isOverdue, type Plan, type Task } from "./model";

type TaskModalProps = {
  plan: Plan;
  task: Task;
  onClose: () => void;
  onSelectTask: (taskId: string) => void;
};

function slack(task: Task): string {
  if (!task.due_date) return "Срок не задан";
  const due = new Date(`${task.due_date}T00:00:00Z`).getTime();
  const finish = new Date(`${task.last_day}T00:00:00Z`).getTime();
  const days = Math.round((due - finish) / 86400000);
  return days < 0 ? `просрочено на ${Math.abs(days)} дн.` : `${days} дн. в запасе`;
}

export function TaskModal({ plan, task, onClose, onSelectTask }: TaskModalProps) {
  const predecessors = task.predecessors
    .map((id) => plan.tasks.find((candidate) => candidate.id === id))
    .filter((candidate): candidate is Task => Boolean(candidate));
  const successors = plan.tasks.filter((candidate) => candidate.predecessors.includes(task.id));

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <section
        className="task-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Задача ${task.name}`}
        onClick={(event) => event.stopPropagation()}
      >
        <button className="modal-close" onClick={onClose}>×</button>
        <span className="section-kicker">ДЕТАЛИ ЗАДАЧИ</span>
        <h2>{task.name}</h2>
        <p className="modal-description">{task.description || "Описание не задано"}</p>
        <div className="detail-grid">
          <div><span>Исполнитель</span><strong>{task.assignee || "Без исполнителя"}</strong></div>
          <div><span>Длительность</span><strong>{task.duration} дн.</strong></div>
          <div><span>НАЧАЛО ПО РАСПИСАНИЮ</span><strong>{formatDate(task.start)}</strong></div>
          <div><span>КОНЕЦ ПО РАСПИСАНИЮ</span><strong>{formatDate(task.last_day)}</strong></div>
          <div><span>Фиксированный старт</span><strong>{formatDate(task.pinned_start)}</strong></div>
          <div><span>Срок</span><strong className={isOverdue(task) ? "danger-text" : ""}>{formatDate(task.due_date)}</strong></div>
        </div>
        <div className="slack-box">
          <span>Запас до срока</span>
          <strong className={isOverdue(task) ? "danger-text" : ""}>{slack(task)}</strong>
        </div>
        <div className="relations">
          <div>
            <span>Предшественники</span>
            {predecessors.length
              ? predecessors.map((item) => <button key={item.id} onClick={() => onSelectTask(item.id)}>{item.name}</button>)
              : <em>нет</em>}
          </div>
          <div>
            <span>Последователи</span>
            {successors.length
              ? successors.map((item) => <button key={item.id} onClick={() => onSelectTask(item.id)}>{item.name}</button>)
              : <em>нет</em>}
          </div>
        </div>
      </section>
    </div>
  );
}

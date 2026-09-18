"use client";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  KIND_NAMES,
  LEAD_STATUS_NAMES,
  PROFILE_LABELS,
  STATE_NAMES,
  api,
  formatDate,
  formatFull,
  formatMessageTime,
  patch,
  post,
} from "../../lib/api";

const POLL_MS = 5000;
const ROLE_NAMES = { client: "Клиент", bot: "Бот", owner: "Вы", system: "Система" };
const LEAD_FIELDS = [
  ["goal", "цель"],
  ["level", "уровень"],
  ["format", "формат"],
  ["timezone", "часовой пояс"],
  ["preferred_time", "удобное время"],
  ["team_size", "человек в команде"],
  ["sphere", "сфера"],
  ["company", "компания"],
];

export default function ConversationPage() {
  const router = useRouter();
  const { id } = useParams();
  const [data, setData] = useState(null);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await api(`/conversations/${id}`));
      setError("");
    } catch (problem) {
      if (problem.unauthorized) {
        router.push("/");
        return;
      }
      setError(problem.message);
    }
  }, [id, router]);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  // Сообщения и события в одной ленте: передача разговора видна там же, где
  // реплики, иначе непонятно, после чего владельцу написали.
  const timeline = useMemo(() => {
    if (!data) return [];
    const messages = data.messages.map((m) => ({ ...m, kind: "message" }));
    const events = data.events.map((e, index) => ({ ...e, kind: "event", id: `e${index}` }));
    return [...messages, ...events].sort(
      (a, b) => new Date(a.created_at) - new Date(b.created_at)
    );
  }, [data]);

  async function act(action, body) {
    setBusy(true);
    setError("");
    try {
      await post(`/conversations/${id}/${action}`, body);
      if (action === "reply") setText("");
      await load();
    } catch (problem) {
      setError(problem.message);
    } finally {
      setBusy(false);
    }
  }

  async function changeLead(lead, changes) {
    try {
      await patch(`/leads/${lead.id}`, changes);
      await load();
    } catch (problem) {
      setError(problem.message);
    }
  }

  if (!data) return <div className="empty">{error || "Загружаю…"}</div>;

  const profile = data.client.profile || {};
  const profileRows = Object.entries(PROFILE_LABELS)
    .filter(([key]) => profile[key] !== undefined && profile[key] !== null && profile[key] !== "")
    .map(([key, label]) => [label, String(profile[key])]);
  profileRows.push(["первое сообщение", formatDate(data.client.created_at)]);

  return (
    <div className="detail">
      <section className="thread">
        <div className="thread__head">
          <h1>
            {data.client.first_name || "Без имени"}
            {data.client.username ? ` (@${data.client.username})` : ""}
          </h1>
          <div className="thread__actions">
            <button
              className="btn"
              onClick={() => act("return")}
              disabled={busy || data.state === "AI_ACTIVE"}
            >
              Вернуть разговор боту
            </button>
            <button className="btn" onClick={() => act("seen")} disabled={busy}>
              Отметить прочитанным
            </button>
          </div>
        </div>
        <div className="thread__sub meta">
          Разговор №{data.id} · {STATE_NAMES[data.state] || data.state}
          {data.handoff_reason_name ? ` · причина передачи: ${data.handoff_reason_name}` : ""}
        </div>

        {timeline.map((item) =>
          item.kind === "event" ? (
            <div className="system" key={item.id}>
              <div>Система · {formatMessageTime(item.created_at)}</div>
              <div>{item.title}</div>
            </div>
          ) : (
            <article className={`msg msg--${item.role}`} key={item.id}>
              <div className="msg__meta">
                {ROLE_NAMES[item.role] || item.role} · {formatMessageTime(item.created_at)}
                {item.meta?.sources?.length
                  ? ` · источники: ${item.meta.sources.map((s) => s.path).join(", ")}`
                  : ""}
              </div>
              <div>{item.text}</div>
            </article>
          )
        )}

        <div className="reply">
          <textarea
            placeholder="Ответить клиенту..."
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
          <div className="reply__row">
            <button
              className="btn btn--primary"
              onClick={() => act("reply", { text })}
              disabled={busy || !text.trim()}
            >
              Отправить
            </button>
            <span className="meta">Отправится клиенту в Telegram</span>
          </div>
          {error ? <p className="error">{error}</p> : null}
        </div>
      </section>

      <aside className="panel">
        <h2>Профиль</h2>
        <div className="rows">
          {profileRows.map(([label, value]) => (
            <div className="row" key={label}>
              <span className="row__label">{label}</span>
              <span className="row__value">{value}</span>
            </div>
          ))}
        </div>

        {data.client.summary ? (
          <>
            <h2>Память</h2>
            <p className="summary">{data.client.summary}</p>
          </>
        ) : null}

        {data.leads.map((lead) => (
          <div className="card" key={lead.id} style={{ marginBottom: 12 }}>
            <h3>Заявка</h3>
            <div className="meta">
              {KIND_NAMES[lead.kind] || lead.kind} · {LEAD_STATUS_NAMES[lead.status]} ·{" "}
              {lead.is_qualified ? "собрана" : "неполная"}
            </div>
            <div className="rows" style={{ marginTop: 10 }}>
              {LEAD_FIELDS.filter(([key]) => lead.fields[key] || key === "preferred_time").map(
                ([key, label]) => (
                  <div className="row" key={key}>
                    <span className="row__label">{label}</span>
                    {lead.fields[key] ? (
                      <span className="row__value">{String(lead.fields[key])}</span>
                    ) : (
                      <span className="row__value row__value--empty">не указано</span>
                    )}
                  </div>
                )
              )}
            </div>
            <div className="field">
              <label>Статус</label>
              <select
                value={lead.status}
                onChange={(event) => changeLead(lead, { status: event.target.value })}
              >
                {Object.entries(LEAD_STATUS_NAMES).map(([value, title]) => (
                  <option key={value} value={value}>
                    {title}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Заметка</label>
              <input
                defaultValue={lead.fields.notes || ""}
                onBlur={(event) => {
                  const next = event.target.value;
                  if (next !== (lead.fields.notes || "")) changeLead(lead, { notes: next });
                }}
              />
            </div>
            <p className="meta" style={{ marginBottom: 0 }}>
              Обновлена {formatFull(lead.updated_at)}
            </p>
          </div>
        ))}
      </aside>
    </div>
  );
}

"use client";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { STATE_NAMES, api, formatListTime } from "../lib/api";

const FILTERS = [
  ["attention", "Требуют внимания"],
  ["human", "У вас"],
  ["bot", "У бота"],
  ["all", "Все"],
];

// Обновление поллингом: realtime в MVP не строим, раз в 5 секунд достаточно.
const POLL_MS = 5000;

/** Левая колонка со списком: она общая для всех разговоров. */
export default function ConversationsLayout({ children }) {
  const router = useRouter();
  const params = useParams();
  const activeId = params?.id ? Number(params.id) : null;
  const [filter, setFilter] = useState("all");
  const [items, setItems] = useState([]);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setItems((await api(`/conversations?filter=${filter}`)).items);
      setError("");
    } catch (problem) {
      if (problem.unauthorized) {
        router.push("/");
        return;
      }
      setError(problem.message);
    }
  }, [filter, router]);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <div className="workspace">
      <aside className="sidebar">
        <div className="filters">
          {FILTERS.map(([value, title]) => (
            <button
              key={value}
              className={`filter ${filter === value ? "filter--active" : ""}`}
              onClick={() => setFilter(value)}
            >
              {title}
            </button>
          ))}
        </div>
        {error ? <p className="error" style={{ padding: "0 16px" }}>{error}</p> : null}
        {items.map((item) => (
          <div
            key={item.id}
            className={`conv ${item.id === activeId ? "conv--active" : ""}`}
            onClick={() => router.push(`/conversations/${item.id}`)}
          >
            <div className="conv__name">
              <span className={`dot ${dotClass(item)}`} />
              <span>
                {item.client.first_name || "Без имени"}
                {item.client.username ? ` (@${item.client.username})` : ""}
              </span>
            </div>
            <div className="conv__meta">
              №{item.id} · {STATE_NAMES[item.state] || item.state}
              {item.unread ? " · непрочитано" : ""}
              {item.open_leads ? ` · заявок: ${item.open_leads}` : ""}
              {" · "}
              {formatListTime(item.last_message_at)}
            </div>
            <div className="conv__preview">{item.last_text}</div>
          </div>
        ))}
        {items.length === 0 && !error ? (
          <p className="meta" style={{ padding: "0 16px" }}>
            Пока пусто.
          </p>
        ) : null}
      </aside>
      {children}
    </div>
  );
}

/** Точка слева: жёлтая, если непрочитано, синяя, если разговор у владельца. */
function dotClass(item) {
  if (item.unread || item.needs_attention) return "dot--unread";
  if (item.state !== "AI_ACTIVE") return "dot--human";
  return "";
}

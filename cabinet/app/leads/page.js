"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { KIND_NAMES, LEAD_STATUS_NAMES, api, formatFull, patch } from "../lib/api";

const STATUSES = ["all", "new", "in_progress", "done", "spam"];
const POLL_MS = 5000;
const FIELD_LABELS = {
  goal: "цель",
  level: "уровень",
  format: "формат",
  timezone: "часовой пояс",
  preferred_time: "удобное время",
  team_size: "человек",
  sphere: "сфера",
  company: "компания",
};

export default function LeadsPage() {
  const router = useRouter();
  const [status, setStatus] = useState("all");
  const [items, setItems] = useState([]);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setItems((await api(`/leads?status=${status}`)).items);
      setError("");
    } catch (problem) {
      if (problem.unauthorized) {
        router.push("/");
        return;
      }
      setError(problem.message);
    }
  }, [status, router]);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  async function change(lead, changes) {
    try {
      await patch(`/leads/${lead.id}`, changes);
      await load();
    } catch (problem) {
      setError(problem.message);
    }
  }

  return (
    <div className="leads">
      <h1 style={{ fontSize: 24, margin: "0 0 16px" }}>Заявки</h1>
      <div className="filters" style={{ padding: 0, marginBottom: 16 }}>
        {STATUSES.map((value) => (
          <button
            key={value}
            className={`filter ${status === value ? "filter--active" : ""}`}
            onClick={() => setStatus(value)}
          >
            {value === "all" ? "Все" : LEAD_STATUS_NAMES[value]}
          </button>
        ))}
      </div>
      {error ? <p className="error">{error}</p> : null}

      {items.map((lead) => (
        <div className="lead-item" key={lead.id}>
          <div style={{ fontWeight: 600 }}>
            {lead.client.first_name || "Без имени"}
            {lead.client.username ? ` (@${lead.client.username})` : ""}
          </div>
          <div className="meta">
            {KIND_NAMES[lead.kind] || lead.kind} · {LEAD_STATUS_NAMES[lead.status]} ·{" "}
            {lead.is_qualified ? "собрана" : "неполная"} · обновлена {formatFull(lead.updated_at)}
            {lead.conversation_id ? (
              <>
                {" · "}
                <Link href={`/conversations/${lead.conversation_id}`} style={{ color: "#2563eb" }}>
                  разговор
                </Link>
              </>
            ) : null}
          </div>
          <div className="rows" style={{ marginTop: 10 }}>
            {Object.entries(FIELD_LABELS)
              .filter(([key]) => lead.fields[key])
              .map(([key, label]) => (
                <div className="row" key={key}>
                  <span className="row__label">{label}</span>
                  <span className="row__value">{String(lead.fields[key])}</span>
                </div>
              ))}
          </div>
          <div className="field">
            <label>Статус</label>
            <select
              value={lead.status}
              onChange={(event) => change(lead, { status: event.target.value })}
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
                if (next !== (lead.fields.notes || "")) change(lead, { notes: next });
              }}
            />
          </div>
        </div>
      ))}
      {items.length === 0 && !error ? <p className="meta">Заявок нет.</p> : null}
    </div>
  );
}

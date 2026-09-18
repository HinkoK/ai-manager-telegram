"use client";

/**
 * Кабинет ходит только в /api бота, в базу напрямую не смотрит.
 * Cookie сессии httpOnly, поэтому браузер посылает её сам, а JS её не видит.
 */
export async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (response.status === 401) {
    const error = new Error("нужен вход");
    error.unauthorized = true;
    throw error;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Ошибка ${response.status}`);
  }
  return data;
}

export const post = (path, body) =>
  api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });

export const patch = (path, body) =>
  api(path, { method: "PATCH", body: JSON.stringify(body) });

/** «16.09, 14:02» для списка разговоров. */
export function formatListTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return `${date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" })}, ${date.toLocaleTimeString(
    "ru-RU",
    { hour: "2-digit", minute: "2-digit" }
  )}`;
}

/** «14:02» для реплик в переписке. */
export function formatMessageTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

/** «14.09.2026» для профиля. */
export function formatDate(value) {
  if (!value) return "";
  return new Date(value).toLocaleDateString("ru-RU");
}

/** «16.09.2026, 14:02» для карточки заявки. */
export function formatFull(value) {
  if (!value) return "";
  return new Date(value).toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export const STATE_NAMES = {
  AI_ACTIVE: "отвечает бот",
  HUMAN_REQUESTED: "ждёт вас",
  HUMAN_ACTIVE: "у вас",
};

export const PROFILE_LABELS = {
  name: "имя",
  goal: "цель",
  level: "уровень",
  format_interest: "формат",
  timezone: "часовой пояс",
  preferred_time: "удобное время",
  is_teen: "подросток",
};

export const LEAD_STATUS_NAMES = {
  new: "новая",
  in_progress: "в работе",
  done: "закрыта",
  spam: "спам",
};

export const KIND_NAMES = {
  trial: "пробный урок",
  corporate: "корпоратив",
  other: "прочее",
};

-- Кабинет владельца: сессии и отметка о прочтении.
-- Логин и хеш пароля живут в .env, пользователей в базе нет: владелец один,
-- регистрации в MVP нет.
create table if not exists {{schema}}.owner_sessions (
  id         bigint generated always as identity primary key,
  -- В базе только хеш: утёкшая база не даёт войти в кабинет.
  token_hash text        not null unique,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  user_agent text
);

create index if not exists owner_sessions_expires_idx
  on {{schema}}.owner_sessions (expires_at);

-- Когда владелец последний раз открывал разговор в кабинете. По ней считаются
-- непрочитанные: last_message_at позже owner_seen_at.
alter table {{schema}}.conversations
  add column if not exists owner_seen_at timestamptz;

grant select, insert, update, delete on {{schema}}.owner_sessions to {{role}};
grant usage, select on all sequences in schema {{schema}} to {{role}};

alter table {{schema}}.owner_sessions enable row level security;
drop policy if exists bot_all on {{schema}}.owner_sessions;
create policy bot_all on {{schema}}.owner_sessions for all to {{role}} using (true) with check (true);

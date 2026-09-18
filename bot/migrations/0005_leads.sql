-- Заявки. Поля взяты из документов школы: trial.md (цель, удобное время с
-- часовым поясом, примерный уровень) и corporate.md (число людей, сфера, цель,
-- время). Телефон не собираем: общение идёт в этом же чате Telegram.
create table if not exists {{schema}}.leads (
  id                     bigint generated always as identity primary key,
  client_id              bigint      not null references {{schema}}.clients(id) on delete cascade,
  conversation_id        bigint      references {{schema}}.conversations(id) on delete set null,
  kind                   text        not null check (kind in ('trial', 'corporate', 'other')),
  status                 text        not null default 'new'
                           check (status in ('new', 'in_progress', 'done', 'spam')),
  goal                   text,
  level                  text,
  format                 text,
  timezone               text,
  preferred_time         text,
  company                text,
  team_size              int         check (team_size is null or team_size between 1 and 1000),
  sphere                 text,
  notes                  text,
  -- Данных достаточно, чтобы менеджер мог предложить окна.
  is_qualified           boolean     not null default false,
  -- Два уведомления на заявку: о новой и о том, что она собрана целиком.
  notified_at            timestamptz,
  qualified_notified_at  timestamptz,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

-- Одна активная заявка на клиента и вид: повторная просьба дополняет её,
-- а не плодит новые. Это гарантия базы, а не соглашение кода.
create unique index if not exists leads_one_active_per_kind
  on {{schema}}.leads (client_id, kind) where status in ('new', 'in_progress');

create index if not exists leads_status_idx on {{schema}}.leads (status, created_at desc);

grant select, insert, update, delete on {{schema}}.leads to {{role}};
grant usage, select on all sequences in schema {{schema}} to {{role}};

alter table {{schema}}.leads enable row level security;
drop policy if exists bot_all on {{schema}}.leads;
create policy bot_all on {{schema}}.leads for all to {{role}} using (true) with check (true);

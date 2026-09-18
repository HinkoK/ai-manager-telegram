-- Данные бота живут в своей схеме, отдельно от всего остального в этой базе.
-- Имя схемы подставляет применятель миграций из SCHOOL_SCHEMA: тесты гоняются
-- в school_test и до настоящих данных не дотягиваются.
create schema if not exists {{schema}};

-- Один на телеграм-пользователя. profile и summary это память между эпизодами,
-- их начнёт заполнять этап 4.
create table if not exists {{schema}}.clients (
  id               bigint generated always as identity primary key,
  telegram_user_id bigint      not null unique,
  username         text,
  first_name       text,
  language_code    text,
  profile          jsonb       not null default '{}'::jsonb,
  summary          text,
  is_blocked_bot   boolean     not null default false,
  is_banned        boolean     not null default false,
  created_at       timestamptz not null default now(),
  last_seen_at     timestamptz not null default now()
);

-- Эпизод разговора. Закрывается после суток тишины, следующее сообщение
-- открывает новый. state из CLAUDE.md, текстом с проверкой, а не перечислением:
-- добавить значение потом можно миграцией, без переделки типа.
create table if not exists {{schema}}.conversations (
  id              bigint generated always as identity primary key,
  client_id       bigint      not null references {{schema}}.clients(id) on delete cascade,
  status          text        not null default 'open'
                    check (status in ('open', 'closed')),
  state           text        not null default 'AI_ACTIVE'
                    check (state in ('AI_ACTIVE', 'HUMAN_REQUESTED', 'HUMAN_ACTIVE')),
  handoff_reason  text,
  handoff_at      timestamptz,
  returned_at     timestamptz,
  needs_attention boolean     not null default false,
  started_at      timestamptz not null default now(),
  last_message_at timestamptz not null default now(),
  closed_at       timestamptz
);

create index if not exists conversations_client_status_idx
  on {{schema}}.conversations (client_id, status);

-- Открытый эпизод у клиента ровно один. Это гарантия базы, а не соглашение кода.
create unique index if not exists conversations_one_open_per_client
  on {{schema}}.conversations (client_id) where status = 'open';

-- text допускает null: у голосового и фото текста нет, вид сообщения лежит в meta.
create table if not exists {{schema}}.messages (
  id                     bigint generated always as identity primary key,
  conversation_id        bigint      not null references {{schema}}.conversations(id) on delete cascade,
  client_id              bigint      not null references {{schema}}.clients(id) on delete cascade,
  role                   text        not null
                           check (role in ('client', 'bot', 'owner', 'system')),
  text                   text,
  telegram_message_id    bigint,
  owner_relay_message_id bigint,
  meta                   jsonb       not null default '{}'::jsonb,
  created_at             timestamptz not null default now()
);

create index if not exists messages_conversation_idx
  on {{schema}}.messages (conversation_id, id);

-- Журнал для кабинета и для разбора полётов.
create table if not exists {{schema}}.events (
  id              bigint generated always as identity primary key,
  conversation_id bigint references {{schema}}.conversations(id) on delete cascade,
  client_id       bigint references {{schema}}.clients(id) on delete cascade,
  type            text        not null,
  payload         jsonb       not null default '{}'::jsonb,
  created_at      timestamptz not null default now()
);

create index if not exists events_created_idx on {{schema}}.events (created_at desc);

-- Идемпотентность. Заявка ставится до обработки, завершение после ответа.
-- Незавершённая заявка старше порога считается брошенной и берётся снова:
-- клиент без ответа думает, что бот мёртв, это хуже повторного ответа.
create table if not exists {{schema}}.processed_updates (
  update_id    bigint primary key,
  claimed_at   timestamptz not null default now(),
  completed_at timestamptz
);

create index if not exists processed_updates_claimed_idx
  on {{schema}}.processed_updates (claimed_at);

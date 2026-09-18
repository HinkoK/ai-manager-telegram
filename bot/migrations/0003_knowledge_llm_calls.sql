-- База знаний и журнал вызовов модели.
-- {{embedding_dim}} подставляет применятель миграций из EMBEDDING_DIM.
--
-- pgvector в Supabase этого проекта установлен в схему public, а у роли бота
-- в пути поиска только своя схема. Поэтому тип, оператор и класс операторов
-- везде пишутся с явной схемой public, путь поиска роли не расширяем.
create extension if not exists vector with schema public;

-- Один файл из knowledge/. content_hash считается вместе с версией нарезки:
-- поменялась нарезка, файл перезагрузится, даже если текст тот же.
-- embedding_model нужен, чтобы смена модели перезагружала всю базу.
create table if not exists {{schema}}.knowledge_documents (
  id              bigint generated always as identity primary key,
  path            text        not null unique,
  title           text        not null,
  content_hash    text        not null,
  embedding_model text        not null,
  chunk_count     int         not null default 0,
  updated_at      timestamptz not null default now()
);

-- Кусок это раздел ## целиком, с префиксом «файл / раздел». Таблицы не режем.
create table if not exists {{schema}}.knowledge_chunks (
  id            bigint generated always as identity primary key,
  document_id   bigint      not null references {{schema}}.knowledge_documents(id) on delete cascade,
  chunk_index   int         not null,
  section_title text        not null,
  content       text        not null,
  embedding     public.vector({{embedding_dim}}) not null,
  unique (document_id, chunk_index)
);

-- На сотне кусков Postgres обойдётся и без индекса. Индекс нужен на рост базы.
-- HNSW в pgvector принимает vector размерностью до 2000.
create index if not exists knowledge_chunks_embedding_hnsw
  on {{schema}}.knowledge_chunks using hnsw (embedding public.vector_cosine_ops);

-- Расход модели: для дневного бюджета на этапе 8 и для отчёта.
-- Текста запросов и ответов здесь нет, только объёмы и время.
create table if not exists {{schema}}.llm_calls (
  id                bigint generated always as identity primary key,
  conversation_id   bigint      references {{schema}}.conversations(id) on delete set null,
  message_id        bigint      references {{schema}}.messages(id) on delete set null,
  purpose           text        not null check (purpose in ('answer', 'summary', 'embed')),
  model             text        not null,
  prompt_tokens     int,
  completion_tokens int,
  latency_ms        int         not null,
  -- Вид ошибки (timeout, http_429, invalid_json), без текста ответа провайдера.
  error             text,
  created_at        timestamptz not null default now()
);

create index if not exists llm_calls_created_idx on {{schema}}.llm_calls (created_at);

-- Права по умолчанию из 0002 уже покрывают новые таблицы, но явная выдача
-- не зависит от того, какой ролью применена 0002.
grant select, insert, update, delete on
  {{schema}}.knowledge_documents, {{schema}}.knowledge_chunks, {{schema}}.llm_calls
  to {{role}};
grant usage, select on all sequences in schema {{schema}} to {{role}};

alter table {{schema}}.knowledge_documents enable row level security;
alter table {{schema}}.knowledge_chunks    enable row level security;
alter table {{schema}}.llm_calls           enable row level security;

drop policy if exists bot_all on {{schema}}.knowledge_documents;
drop policy if exists bot_all on {{schema}}.knowledge_chunks;
drop policy if exists bot_all on {{schema}}.llm_calls;

create policy bot_all on {{schema}}.knowledge_documents for all to {{role}} using (true) with check (true);
create policy bot_all on {{schema}}.knowledge_chunks    for all to {{role}} using (true) with check (true);
create policy bot_all on {{schema}}.llm_calls           for all to {{role}} using (true) with check (true);

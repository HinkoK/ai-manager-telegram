-- Бот ходит в базу ролью {{role}}, у которой права только на свою схему.
-- Утёкшая строка подключения открывает школу и ничего больше.
-- Роль создаётся без логина; пароль ставит scripts/create_role.py и он же
-- пишет строку подключения в .env, в git пароль не попадает.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = '{{role}}') then
    create role {{role}} nologin;
  end if;
end $$;

grant usage on schema {{schema}} to {{role}};
grant select, insert, update, delete on all tables in schema {{schema}} to {{role}};
grant usage, select on all sequences in schema {{schema}} to {{role}};
alter default privileges in schema {{schema}}
  grant select, insert, update, delete on tables to {{role}};
alter default privileges in schema {{schema}}
  grant usage, select on sequences to {{role}};

-- Явный отказ на чужие схемы. Честная оговорка: схему public в Postgres видно
-- всем ролям, и отобрать это, не задев соседей по базе, нельзя. Защита работает
-- на уровне таблиц - права на них выданы другим ролям. Это проверяется тестом.
revoke all on schema public from {{role}};
revoke all on all tables in schema public from {{role}};
alter role {{role}} set search_path = {{schema}};

-- Если в этой же базе живут другие проекты, их схемы перечисляются ниже
-- (здесь примеры, замените на свои). Отказ снимает права только у {{role}},
-- чужие роли и их доступ не трогает. Схемы, которой нет, пропускаем:
-- локальный Postgres тестов про соседей не знает.
do $$
declare
  other text;
begin
  foreach other in array array['shop', 'blog'] loop
    if exists (select 1 from pg_namespace where nspname = other) then
      execute format('revoke all on schema %I from {{role}}', other);
      execute format('revoke all on all tables in schema %I from {{role}}', other);
    end if;
  end loop;
end $$;

-- RLS второй оградой: если схему когда-нибудь добавят в список доступных через
-- API Supabase, снаружи всё равно ничего не увидят. Владелец схемы (postgres,
-- он же применяет миграции) RLS обходит.
alter table {{schema}}.clients            enable row level security;
alter table {{schema}}.conversations      enable row level security;
alter table {{schema}}.messages           enable row level security;
alter table {{schema}}.events             enable row level security;
alter table {{schema}}.processed_updates  enable row level security;

drop policy if exists bot_all on {{schema}}.clients;
drop policy if exists bot_all on {{schema}}.conversations;
drop policy if exists bot_all on {{schema}}.messages;
drop policy if exists bot_all on {{schema}}.events;
drop policy if exists bot_all on {{schema}}.processed_updates;

create policy bot_all on {{schema}}.clients           for all to {{role}} using (true) with check (true);
create policy bot_all on {{schema}}.conversations     for all to {{role}} using (true) with check (true);
create policy bot_all on {{schema}}.messages          for all to {{role}} using (true) with check (true);
create policy bot_all on {{schema}}.events            for all to {{role}} using (true) with check (true);
create policy bot_all on {{schema}}.processed_updates for all to {{role}} using (true) with check (true);

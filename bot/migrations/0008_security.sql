-- Этап 8, безопасность.

-- Лимиты на клиента считаются по его сообщениям за минуту и за сутки.
create index if not exists messages_client_created_idx
  on {{schema}}.messages (client_id, created_at)
  where role = 'client';

-- Дневной бюджет токенов считается по llm_calls за последние сутки: индекс
-- по created_at уже есть с 0003.

-- Находка аудита: учёт миграций получил права бота через «все таблицы в схеме»
-- в 0002, хотя бот к нему отношения не имеет. Утёкшая строка бота не должна
-- уметь подделать историю миграций.
revoke all on {{schema}}.schema_migrations from {{role}};

-- Когда и почему клиент забанен: владелец видит это в кабинете.
alter table {{schema}}.clients
  add column if not exists banned_at timestamptz,
  add column if not exists ban_reason text;

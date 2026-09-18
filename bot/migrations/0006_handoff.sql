-- Передача разговора владельцу. Состояния, причина и время передачи лежат в
-- conversations с этапа 2, здесь добавляется отметка о напоминании и индексы.

-- Напоминание владельцу уходит один раз, отметка не даёт слать его в цикле.
alter table {{schema}}.conversations
  add column if not exists reminded_at timestamptz;

-- По id сообщения в чате владельца реплай находит разговор.
create index if not exists messages_owner_relay_idx
  on {{schema}}.messages (owner_relay_message_id)
  where owner_relay_message_id is not null;

-- Фоновая задача ищет разговоры, которые ждут владельца слишком долго.
create index if not exists conversations_state_idx
  on {{schema}}.conversations (state, handoff_at)
  where status = 'open';

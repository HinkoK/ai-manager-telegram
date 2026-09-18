-- Память между разговорами. Профиль и резюме уже есть в clients с этапа 2,
-- здесь появляется отметка, до какого сообщения резюме доведено: без неё
-- пришлось бы каждый раз пересчитывать резюме по всей переписке.
alter table {{schema}}.clients
  add column if not exists summary_message_id bigint
    references {{schema}}.messages(id) on delete set null,
  add column if not exists summary_updated_at timestamptz;

-- Появился третий вид вызова модели: переписывание вопроса перед поиском.
alter table {{schema}}.llm_calls drop constraint if exists llm_calls_purpose_check;
alter table {{schema}}.llm_calls add constraint llm_calls_purpose_check
  check (purpose in ('answer', 'summary', 'embed', 'rewrite'));

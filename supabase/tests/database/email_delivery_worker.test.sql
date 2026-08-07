begin;

create extension if not exists pgtap with schema extensions;

select extensions.plan(31);

insert into auth.users (id)
values ('10000000-0000-4000-8000-000000000001');

update public.profiles
set membership_status = 'active'::public.membership_status
where id = '10000000-0000-4000-8000-000000000001';

update public.notification_email_preferences
set is_enabled = true
where user_id = '10000000-0000-4000-8000-000000000001';

create temporary table email_worker_test_state (
  name text primary key,
  message_id uuid not null,
  locked_until timestamptz
) on commit drop;

with delivery_item as (
  insert into public.notification_delivery_items (
    id,
    user_id,
    channel,
    slot_id,
    facility_id,
    facility_name,
    available_date,
    start_time,
    end_time,
    matched_rule_ids,
    payload
  )
  values (
    '20000000-0000-4000-8000-000000000001',
    '10000000-0000-4000-8000-000000000001',
    'email',
    'worker-accepted-slot',
    'kamoike-prefectural',
    '鴨池県営テニスコート',
    date '2026-08-08',
    time '09:00',
    time '11:00',
    array['30000000-0000-4000-8000-000000000001'::uuid],
    '{"court_name":"Aコート","reservation_url":"https://example.invalid"}'
  )
  returning id, user_id, channel
),
message as (
  insert into public.notification_messages (
    id,
    user_id,
    channel
  )
  values (
    '40000000-0000-4000-8000-000000000001',
    '10000000-0000-4000-8000-000000000001',
    'email'
  )
  returning id, user_id, channel
)
insert into public.notification_message_items (
  message_id,
  delivery_item_id,
  user_id,
  channel
)
select message.id, delivery_item.id, message.user_id, message.channel
from message
cross join delivery_item;

insert into email_worker_test_state (name, message_id, locked_until)
select 'accepted', claimed.message_id, claimed.locked_until
from public.claim_email_messages(1) as claimed;

select extensions.is(
  (
    select pg_catalog.count(*)::integer
    from email_worker_test_state
    where name = 'accepted'
  ),
  1,
  'claim returns one delivery message'
);

select extensions.is(
  public.authorize_email_message_send(
    (
      select message_id
      from email_worker_test_state
      where name = 'accepted'
    ),
    (
      select locked_until
      from email_worker_test_state
      where name = 'accepted'
    ),
    pg_catalog.repeat('a', 64)
  ),
  'authorized',
  'authorization permits the current eligible lease'
);

select extensions.ok(
  (
    select
      provider_first_attempt_at is not null
      and provider_payload_fingerprint = pg_catalog.repeat('a', 64)
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000001'
  ),
  'authorization starts the provider window and stores only a fingerprint'
);

update public.notification_messages
set
  locked_at = (
    select locked_until - interval '4 minutes'
    from email_worker_test_state
    where name = 'accepted'
  ),
  locked_until = (
    select locked_until + interval '1 minute'
    from email_worker_test_state
    where name = 'accepted'
  )
where id = '40000000-0000-4000-8000-000000000001';

select extensions.is(
  public.record_email_message_accepted(
    '40000000-0000-4000-8000-000000000001',
    (
      select locked_until
      from email_worker_test_state
      where name = 'accepted'
    ),
    'resend_stale_result'
  ),
  false,
  'an older lease cannot record acceptance'
);

update email_worker_test_state
set locked_until = (
  select locked_until
  from public.notification_messages
  where id = '40000000-0000-4000-8000-000000000001'
)
where name = 'accepted';

select extensions.is(
  public.record_email_message_accepted(
    '40000000-0000-4000-8000-000000000001',
    (
      select locked_until
      from email_worker_test_state
      where name = 'accepted'
    ),
    'resend_accepted_1'
  ),
  true,
  'the current lease records acceptance'
);

select extensions.is(
  (
    select status::text
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000001'
  ),
  'accepted',
  'accepted result updates message status'
);

select extensions.is(
  (
    select provider_status
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000001'
  ),
  'accepted',
  'accepted result stores a normalized provider status'
);

select extensions.ok(
  (
    select locked_at is null and locked_until is null
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000001'
  ),
  'accepted result releases the lease'
);

with delivery_item as (
  insert into public.notification_delivery_items (
    id,
    user_id,
    channel,
    slot_id,
    facility_id,
    facility_name,
    available_date,
    start_time,
    end_time,
    matched_rule_ids,
    payload
  )
  values (
    '20000000-0000-4000-8000-000000000002',
    '10000000-0000-4000-8000-000000000001',
    'email',
    'worker-retry-slot',
    'sumizei',
    'SuMIzeiテニスコート',
    date '2026-08-09',
    time '10:00',
    time '12:00',
    array['30000000-0000-4000-8000-000000000002'::uuid],
    '{}'
  )
  returning id, user_id, channel
),
message as (
  insert into public.notification_messages (
    id,
    user_id,
    channel
  )
  values (
    '40000000-0000-4000-8000-000000000002',
    '10000000-0000-4000-8000-000000000001',
    'email'
  )
  returning id, user_id, channel
)
insert into public.notification_message_items (
  message_id,
  delivery_item_id,
  user_id,
  channel
)
select message.id, delivery_item.id, message.user_id, message.channel
from message
cross join delivery_item;

insert into email_worker_test_state (name, message_id, locked_until)
select 'retry', claimed.message_id, claimed.locked_until
from public.claim_email_messages(1) as claimed;

select extensions.is(
  (
    select pg_catalog.count(*)::integer
    from email_worker_test_state
    where name = 'retry'
  ),
  1,
  'a retry test message is claimed'
);

select extensions.is(
  public.authorize_email_message_send(
    (
      select message_id
      from email_worker_test_state
      where name = 'retry'
    ),
    (
      select locked_until
      from email_worker_test_state
      where name = 'retry'
    ),
    pg_catalog.repeat('b', 64)
  ),
  'authorized',
  'retry test message is authorized'
);

select extensions.is(
  public.record_email_message_failure(
    (
      select message_id
      from email_worker_test_state
      where name = 'retry'
    ),
    (
      select locked_until
      from email_worker_test_state
      where name = 'retry'
    ),
    'resend_network_error'
  ),
  'retry_wait',
  'network errors enter retry_wait'
);

select extensions.is(
  (
    select status::text
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000002'
  ),
  'retry_wait',
  'retryable failure persists retry_wait'
);

select extensions.is(
  (
    select last_error_code
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000002'
  ),
  'resend_network_error',
  'retryable failure stores only the normalized code'
);

select extensions.ok(
  (
    select
      next_attempt_at >= pg_catalog.now() + interval '60 seconds'
      and next_attempt_at <= pg_catalog.now() + interval '90 seconds'
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000002'
  ),
  'first retry uses bounded backoff and jitter'
);

update public.notification_messages
set next_attempt_at = pg_catalog.now()
where id = '40000000-0000-4000-8000-000000000002';

insert into email_worker_test_state (name, message_id, locked_until)
select 'retry-current', claimed.message_id, claimed.locked_until
from public.claim_email_messages(1) as claimed
where claimed.message_id = '40000000-0000-4000-8000-000000000002';

-- now() is transaction-stable in this pgTAP file. Move the simulated current
-- lease forward so the previous claim token is observably stale.
update public.notification_messages
set
  locked_until = locked_until + interval '1 minute'
where id = '40000000-0000-4000-8000-000000000002';

update email_worker_test_state
set locked_until = (
  select locked_until
  from public.notification_messages
  where id = '40000000-0000-4000-8000-000000000002'
)
where name = 'retry-current';

select extensions.is(
  public.record_email_message_failure(
    '40000000-0000-4000-8000-000000000002',
    (
      select locked_until
      from email_worker_test_state
      where name = 'retry'
    ),
    'resend_server_error'
  ),
  'stale',
  'an older lease cannot record failure'
);

select extensions.is(
  (
    select status::text
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000002'
  ),
  'processing',
  'stale failure leaves the current message status unchanged'
);

select extensions.ok(
  (
    select
      message.locked_at is not null
      and message.locked_until = state.locked_until
    from public.notification_messages as message
    inner join email_worker_test_state as state
      on state.message_id = message.id
      and state.name = 'retry-current'
    where message.id = '40000000-0000-4000-8000-000000000002'
  ),
  'stale failure leaves the current lease unchanged'
);

select extensions.ok(
  (
    select
      last_error_code = 'resend_network_error'
      and last_error_message is null
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000002'
  ),
  'stale failure leaves the current normalized error state unchanged'
);

select extensions.is(
  public.authorize_email_message_send(
    (
      select message_id
      from email_worker_test_state
      where name = 'retry-current'
    ),
    (
      select locked_until
      from email_worker_test_state
      where name = 'retry-current'
    ),
    pg_catalog.repeat('b', 64)
  ),
  'authorized',
  'same provider payload is authorized on retry'
);

select extensions.is(
  public.record_email_message_failure(
    (
      select message_id
      from email_worker_test_state
      where name = 'retry-current'
    ),
    (
      select locked_until
      from email_worker_test_state
      where name = 'retry-current'
    ),
    'resend_invalid_idempotent_request'
  ),
  'failed_permanent',
  'invalid idempotent request is permanent'
);

select extensions.is(
  (
    select status::text
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000002'
  ),
  'failed_permanent',
  'permanent failure updates message status'
);

select extensions.is(
  (
    select last_error_code
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000002'
  ),
  'resend_invalid_idempotent_request',
  'permanent failure stores only the normalized code'
);

with delivery_item as (
  insert into public.notification_delivery_items (
    id,
    user_id,
    channel,
    slot_id,
    facility_id,
    facility_name,
    available_date,
    start_time,
    end_time,
    matched_rule_ids,
    payload
  )
  values (
    '20000000-0000-4000-8000-000000000003',
    '10000000-0000-4000-8000-000000000001',
    'email',
    'worker-cancel-slot',
    'toukai-tennis',
    '東開庭球場',
    date '2026-08-10',
    time '13:00',
    time '15:00',
    array['30000000-0000-4000-8000-000000000003'::uuid],
    '{}'
  )
  returning id, user_id, channel
),
message as (
  insert into public.notification_messages (
    id,
    user_id,
    channel
  )
  values (
    '40000000-0000-4000-8000-000000000003',
    '10000000-0000-4000-8000-000000000001',
    'email'
  )
  returning id, user_id, channel
)
insert into public.notification_message_items (
  message_id,
  delivery_item_id,
  user_id,
  channel
)
select message.id, delivery_item.id, message.user_id, message.channel
from message
cross join delivery_item;

insert into email_worker_test_state (name, message_id, locked_until)
select 'cancel', claimed.message_id, claimed.locked_until
from public.claim_email_messages(1) as claimed;

update public.notification_email_preferences
set is_enabled = false
where user_id = '10000000-0000-4000-8000-000000000001';

select extensions.is(
  public.authorize_email_message_send(
    (
      select message_id
      from email_worker_test_state
      where name = 'cancel'
    ),
    (
      select locked_until
      from email_worker_test_state
      where name = 'cancel'
    ),
    pg_catalog.repeat('c', 64)
  ),
  'cancelled',
  'authorization cancels a user who became ineligible'
);

select extensions.is(
  (
    select status::text
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000003'
  ),
  'cancelled',
  'ineligible authorization persists cancellation'
);

select extensions.ok(
  (
    select locked_at is null and locked_until is null
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000003'
  ),
  'cancellation releases the lease'
);

update public.notification_email_preferences
set is_enabled = true
where user_id = '10000000-0000-4000-8000-000000000001';

insert into public.notification_messages (
  id,
  user_id,
  channel,
  attempt_count
)
values (
  '40000000-0000-4000-8000-000000000004',
  '10000000-0000-4000-8000-000000000001',
  'email',
  5
);

select extensions.is(
  (
    select pg_catalog.count(*)::integer
    from public.claim_email_messages(1)
    where message_id = '40000000-0000-4000-8000-000000000004'
  ),
  0,
  'attempt-exhausted messages are not claimed'
);

select extensions.is(
  (
    select last_error_code
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000004'
  ),
  'attempt_limit_exceeded',
  'claim marks attempt exhaustion as permanent'
);

insert into public.notification_messages (
  id,
  user_id,
  channel,
  status,
  attempt_count,
  provider_first_attempt_at,
  provider_payload_fingerprint
)
values (
  '40000000-0000-4000-8000-000000000005',
  '10000000-0000-4000-8000-000000000001',
  'email',
  'retry_wait',
  1,
  pg_catalog.now() - interval '23 hours',
  pg_catalog.repeat('d', 64)
);

select extensions.is(
  (
    select pg_catalog.count(*)::integer
    from public.claim_email_messages(1)
    where message_id = '40000000-0000-4000-8000-000000000005'
  ),
  0,
  'messages outside the provider safety window are not claimed'
);

select extensions.is(
  (
    select last_error_code
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000005'
  ),
  'idempotency_window_expired',
  'claim fails an expired provider window without another send'
);

insert into public.notification_messages (
  id,
  user_id,
  channel,
  status,
  attempt_count,
  locked_at,
  locked_until,
  provider_first_attempt_at,
  provider_payload_fingerprint
)
values (
  '40000000-0000-4000-8000-000000000006',
  '10000000-0000-4000-8000-000000000001',
  'email',
  'processing',
  2,
  pg_catalog.now(),
  pg_catalog.now() + interval '5 minutes',
  pg_catalog.now(),
  pg_catalog.repeat('e', 64)
);

select extensions.is(
  public.authorize_email_message_send(
    '40000000-0000-4000-8000-000000000006',
    (
      select locked_until
      from public.notification_messages
      where id = '40000000-0000-4000-8000-000000000006'
    ),
    pg_catalog.repeat('f', 64)
  ),
  'failed_permanent',
  'a changed provider payload is never sent with the same idempotency key'
);

select extensions.is(
  (
    select last_error_code
    from public.notification_messages
    where id = '40000000-0000-4000-8000-000000000006'
  ),
  'provider_payload_changed',
  'payload mismatch is recorded as a normalized permanent failure'
);

select extensions.finish();

rollback;

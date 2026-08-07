-- Phase 3.2 user email delivery worker lifecycle.
-- This forward migration intentionally leaves the Phase 3.1 queue migration
-- unchanged. Recipient addresses remain exclusively in Supabase Auth.

begin;

alter table public.notification_messages
  add column provider_first_attempt_at timestamptz,
  add column provider_payload_fingerprint text;

alter table public.notification_messages
  add constraint notification_messages_provider_attempt_pair
    check (
      (
        provider_first_attempt_at is null
        and provider_payload_fingerprint is null
      )
      or (
        provider_first_attempt_at is not null
        and provider_payload_fingerprint is not null
      )
    ),
  add constraint notification_messages_provider_payload_fingerprint_format
    check (
      provider_payload_fingerprint is null
      or provider_payload_fingerprint ~ '^[0-9a-f]{64}$'
    );

comment on column
  public.notification_messages.provider_first_attempt_at is
  'Start of the Resend idempotency safety window; no recipient data.';

comment on column
  public.notification_messages.provider_payload_fingerprint is
  'HMAC-SHA-256 of the exact provider payload; not the payload or recipient.';

create or replace function public.claim_email_messages(
  batch_size integer
)
returns table (
  message_id uuid,
  user_id uuid,
  channel public.notification_channel,
  attempt_count integer,
  locked_until timestamptz,
  items jsonb
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_max_batch_size constant pg_catalog.int4 := 100;
  v_max_attempts constant pg_catalog.int4 := 5;
  v_provider_safety_window constant interval := interval '23 hours';
begin
  if (
    batch_size is null
    or batch_size < 1
    or batch_size > v_max_batch_size
  ) then
    raise exception 'Email message claim batch size is invalid.'
      using errcode = '22023';
  end if;

  -- Cancellation takes priority over retry exhaustion. This also keeps
  -- permanently ineligible messages out of future claim scans.
  with ineligible_messages as materialized (
    select message.id
    from public.notification_messages as message
    where (
      message.status in (
        'pending'::public.notification_message_status,
        'retry_wait'::public.notification_message_status
      )
      or (
        message.status =
          'processing'::public.notification_message_status
        and message.locked_until <= pg_catalog.now()
      )
    )
    and not exists (
      select 1
      from public.profiles as profile
      inner join public.notification_email_preferences as preference
        on preference.user_id = profile.id
      where profile.id = message.user_id
        and profile.membership_status =
          'active'::public.membership_status
        and preference.is_enabled = true
        and preference.disabled_reason is null
    )
    order by
      message.next_attempt_at,
      message.created_at,
      message.id
    for update of message skip locked
    limit batch_size
  )
  update public.notification_messages as message
  set
    status = 'cancelled'::public.notification_message_status,
    locked_at = null,
    locked_until = null,
    last_error_code = null,
    last_error_message = null
  from ineligible_messages as ineligible
  where message.id = ineligible.id;

  -- A retry is never claimed once either the attempt cap or the conservative
  -- 23-hour provider idempotency window has been exhausted.
  with exhausted_messages as materialized (
    select message.id
    from public.notification_messages as message
    inner join public.profiles as profile
      on profile.id = message.user_id
      and profile.membership_status =
        'active'::public.membership_status
    inner join public.notification_email_preferences as preference
      on preference.user_id = message.user_id
      and preference.is_enabled = true
      and preference.disabled_reason is null
    where (
      message.status in (
        'pending'::public.notification_message_status,
        'retry_wait'::public.notification_message_status
      )
      or (
        message.status =
          'processing'::public.notification_message_status
        and message.locked_until <= pg_catalog.now()
      )
    )
    and (
      message.attempt_count >= v_max_attempts
      or (
        message.provider_first_attempt_at is not null
        and message.provider_first_attempt_at
          + v_provider_safety_window <= pg_catalog.now()
      )
    )
    order by
      message.next_attempt_at,
      message.created_at,
      message.id
    for update of message skip locked
    limit batch_size
  )
  update public.notification_messages as message
  set
    status = 'failed_permanent'::public.notification_message_status,
    locked_at = null,
    locked_until = null,
    failed_at = pg_catalog.now(),
    last_error_code = case
      when message.attempt_count >= v_max_attempts
        then 'attempt_limit_exceeded'
      else 'idempotency_window_expired'
    end,
    last_error_message = null
  from exhausted_messages as exhausted
  where message.id = exhausted.id;

  return query
  with claimable_messages as materialized (
    select message.id
    from public.notification_messages as message
    inner join public.profiles as profile
      on profile.id = message.user_id
      and profile.membership_status =
        'active'::public.membership_status
    inner join public.notification_email_preferences as preference
      on preference.user_id = message.user_id
      and preference.is_enabled = true
      and preference.disabled_reason is null
    where (
      (
        message.status in (
          'pending'::public.notification_message_status,
          'retry_wait'::public.notification_message_status
        )
        and message.next_attempt_at <= pg_catalog.now()
        and (
          message.locked_until is null
          or message.locked_until <= pg_catalog.now()
        )
      )
      or (
        message.status =
          'processing'::public.notification_message_status
        and message.locked_until <= pg_catalog.now()
      )
    )
    and message.attempt_count < v_max_attempts
    and (
      message.provider_first_attempt_at is null
      or message.provider_first_attempt_at
        + v_provider_safety_window > pg_catalog.now()
    )
    order by
      message.next_attempt_at,
      message.created_at,
      message.id
    for update of message skip locked
    limit batch_size
  ),
  claimed_messages as (
    update public.notification_messages as message
    set
      status = 'processing'::public.notification_message_status,
      attempt_count = message.attempt_count + 1,
      locked_at = pg_catalog.now(),
      locked_until = pg_catalog.now() + interval '5 minutes'
    from claimable_messages as claimable
    where message.id = claimable.id
    returning
      message.id,
      message.user_id,
      message.channel,
      message.attempt_count,
      message.locked_until
  )
  select
    claimed.id as message_id,
    claimed.user_id,
    claimed.channel,
    claimed.attempt_count,
    claimed.locked_until,
    pg_catalog.jsonb_agg(
      pg_catalog.jsonb_build_object(
        'facility_name',
        delivery_item.facility_name,
        'available_date',
        delivery_item.available_date,
        'start_time',
        delivery_item.start_time,
        'end_time',
        delivery_item.end_time,
        'payload',
        delivery_item.payload
      )
      order by
        delivery_item.available_date,
        delivery_item.start_time,
        delivery_item.end_time,
        delivery_item.facility_name,
        delivery_item.payload::pg_catalog.text,
        delivery_item.id
    ) as items
  from claimed_messages as claimed
  inner join public.notification_message_items as message_item
    on message_item.message_id = claimed.id
  inner join public.notification_delivery_items as delivery_item
    on delivery_item.id = message_item.delivery_item_id
  group by
    claimed.id,
    claimed.user_id,
    claimed.channel,
    claimed.attempt_count,
    claimed.locked_until
  order by claimed.locked_until, claimed.id;
end;
$$;

create function public.authorize_email_message_send(
  p_message_id uuid,
  p_locked_until timestamptz,
  p_provider_payload_fingerprint text
)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_message public.notification_messages%rowtype;
  v_max_attempts constant pg_catalog.int4 := 5;
  v_provider_safety_window constant interval := interval '23 hours';
begin
  if (
    p_message_id is null
    or p_locked_until is null
    or p_provider_payload_fingerprint is null
    or p_provider_payload_fingerprint !~ '^[0-9a-f]{64}$'
  ) then
    raise exception 'Email send authorization input is invalid.'
      using errcode = '22023';
  end if;

  select message.*
  into v_message
  from public.notification_messages as message
  where message.id = p_message_id
    and message.status =
      'processing'::public.notification_message_status
    and message.locked_until = p_locked_until
    and message.locked_until > pg_catalog.now()
  for update;

  if not found then
    return 'stale';
  end if;

  if not exists (
    select 1
    from public.profiles as profile
    inner join public.notification_email_preferences as preference
      on preference.user_id = profile.id
    where profile.id = v_message.user_id
      and profile.membership_status =
        'active'::public.membership_status
      and preference.is_enabled = true
      and preference.disabled_reason is null
  ) then
    update public.notification_messages as message
    set
      status = 'cancelled'::public.notification_message_status,
      locked_at = null,
      locked_until = null,
      last_error_code = null,
      last_error_message = null
    where message.id = v_message.id;

    return 'cancelled';
  end if;

  if v_message.attempt_count > v_max_attempts then
    update public.notification_messages as message
    set
      status = 'failed_permanent'::public.notification_message_status,
      locked_at = null,
      locked_until = null,
      failed_at = pg_catalog.now(),
      last_error_code = 'attempt_limit_exceeded',
      last_error_message = null
    where message.id = v_message.id;

    return 'failed_permanent';
  end if;

  if (
    v_message.provider_first_attempt_at is not null
    and v_message.provider_first_attempt_at
      + v_provider_safety_window <= pg_catalog.now()
  ) then
    update public.notification_messages as message
    set
      status = 'failed_permanent'::public.notification_message_status,
      locked_at = null,
      locked_until = null,
      failed_at = pg_catalog.now(),
      last_error_code = 'idempotency_window_expired',
      last_error_message = null
    where message.id = v_message.id;

    return 'failed_permanent';
  end if;

  if (
    v_message.provider_payload_fingerprint is not null
    and v_message.provider_payload_fingerprint
      <> p_provider_payload_fingerprint
  ) then
    update public.notification_messages as message
    set
      status = 'failed_permanent'::public.notification_message_status,
      locked_at = null,
      locked_until = null,
      failed_at = pg_catalog.now(),
      last_error_code = 'provider_payload_changed',
      last_error_message = null
    where message.id = v_message.id;

    return 'failed_permanent';
  end if;

  update public.notification_messages as message
  set
    provider_first_attempt_at = coalesce(
      message.provider_first_attempt_at,
      pg_catalog.now()
    ),
    provider_payload_fingerprint = coalesce(
      message.provider_payload_fingerprint,
      p_provider_payload_fingerprint
    )
  where message.id = v_message.id;

  return 'authorized';
end;
$$;

create function public.record_email_message_accepted(
  p_message_id uuid,
  p_locked_until timestamptz,
  p_provider_message_id text
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_updated_count pg_catalog.int4;
begin
  if (
    p_message_id is null
    or p_locked_until is null
    or p_provider_message_id is null
    or p_provider_message_id !~ '^[A-Za-z0-9_-]{1,255}$'
  ) then
    raise exception 'Accepted email result input is invalid.'
      using errcode = '22023';
  end if;

  update public.notification_messages as message
  set
    status = 'accepted'::public.notification_message_status,
    provider_message_id = p_provider_message_id,
    provider_status = 'accepted',
    accepted_at = pg_catalog.now(),
    failed_at = null,
    locked_at = null,
    locked_until = null,
    last_error_code = null,
    last_error_message = null
  where message.id = p_message_id
    and message.status =
      'processing'::public.notification_message_status
    and message.locked_until = p_locked_until
    and message.locked_until > pg_catalog.now()
    and message.provider_first_attempt_at is not null
    and message.provider_payload_fingerprint is not null;

  get diagnostics v_updated_count = row_count;
  return v_updated_count = 1;
end;
$$;

create function public.record_email_message_failure(
  p_message_id uuid,
  p_locked_until timestamptz,
  p_error_code text
)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_message public.notification_messages%rowtype;
  v_retryable pg_catalog.bool;
  v_delay_seconds pg_catalog.int4;
  v_retry_at timestamptz;
  v_final_error_code pg_catalog.text;
  v_max_attempts constant pg_catalog.int4 := 5;
  v_provider_safety_window constant interval := interval '23 hours';
begin
  if (
    p_message_id is null
    or p_locked_until is null
    or p_error_code is null
    or p_error_code <> all (
      array[
        'auth_lookup_error',
        'recipient_unavailable',
        'worker_internal_error',
        'resend_network_error',
        'resend_server_error',
        'resend_rate_limited',
        'resend_concurrent_request',
        'resend_unexpected_response',
        'resend_invalid_idempotency_key',
        'resend_invalid_idempotent_request',
        'resend_invalid_api_key',
        'resend_invalid_from',
        'resend_validation_error',
        'resend_quota_exceeded',
        'resend_security_error',
        'resend_client_error'
      ]::pg_catalog.text[]
    )
  ) then
    raise exception 'Email failure result input is invalid.'
      using errcode = '22023';
  end if;

  select message.*
  into v_message
  from public.notification_messages as message
  where message.id = p_message_id
    and message.status =
      'processing'::public.notification_message_status
    and message.locked_until = p_locked_until
    and message.locked_until > pg_catalog.now()
  for update;

  if not found then
    return 'stale';
  end if;

  v_retryable := p_error_code = any (
    array[
      'auth_lookup_error',
      'worker_internal_error',
      'resend_network_error',
      'resend_server_error',
      'resend_rate_limited',
      'resend_concurrent_request',
      'resend_unexpected_response'
    ]::pg_catalog.text[]
  );

  if v_retryable and v_message.attempt_count < v_max_attempts then
    v_delay_seconds := case v_message.attempt_count
      when 1 then 60
      when 2 then 120
      when 3 then 300
      else 900
    end + pg_catalog.floor(pg_catalog.random() * 31)::pg_catalog.int4;

    v_retry_at := pg_catalog.now()
      + pg_catalog.make_interval(secs => v_delay_seconds);

    if (
      v_message.provider_first_attempt_at is null
      or v_retry_at < v_message.provider_first_attempt_at
        + v_provider_safety_window
    ) then
      update public.notification_messages as message
      set
        status = 'retry_wait'::public.notification_message_status,
        next_attempt_at = v_retry_at,
        locked_at = null,
        locked_until = null,
        failed_at = null,
        last_error_code = p_error_code,
        last_error_message = null
      where message.id = v_message.id;

      return 'retry_wait';
    end if;
  end if;

  v_final_error_code := case
    when v_retryable
      and v_message.attempt_count >= v_max_attempts
      then 'attempt_limit_exceeded'
    when v_retryable
      and v_message.provider_first_attempt_at is not null
      and v_retry_at >= v_message.provider_first_attempt_at
        + v_provider_safety_window
      then 'idempotency_window_expired'
    else p_error_code
  end;

  update public.notification_messages as message
  set
    status = 'failed_permanent'::public.notification_message_status,
    locked_at = null,
    locked_until = null,
    failed_at = pg_catalog.now(),
    last_error_code = v_final_error_code,
    last_error_message = null
  where message.id = v_message.id;

  return 'failed_permanent';
end;
$$;

revoke execute on function public.claim_email_messages(integer)
from public, anon, authenticated;

grant execute on function public.claim_email_messages(integer)
to service_role;

revoke execute on function public.authorize_email_message_send(
  uuid,
  timestamptz,
  text
)
from public, anon, authenticated;

grant execute on function public.authorize_email_message_send(
  uuid,
  timestamptz,
  text
)
to service_role;

revoke execute on function public.record_email_message_accepted(
  uuid,
  timestamptz,
  text
)
from public, anon, authenticated;

grant execute on function public.record_email_message_accepted(
  uuid,
  timestamptz,
  text
)
to service_role;

revoke execute on function public.record_email_message_failure(
  uuid,
  timestamptz,
  text
)
from public, anon, authenticated;

grant execute on function public.record_email_message_failure(
  uuid,
  timestamptz,
  text
)
to service_role;

comment on function public.claim_email_messages(integer) is
  'Claims bounded email batches and enforces attempt/idempotency limits.';

comment on function public.authorize_email_message_send(
  uuid,
  timestamptz,
  text
) is
  'Rechecks eligibility and the exact current lease immediately before send.';

comment on function public.record_email_message_accepted(
  uuid,
  timestamptz,
  text
) is
  'Records a normalized Resend acceptance only for the exact current lease.';

comment on function public.record_email_message_failure(
  uuid,
  timestamptz,
  text
) is
  'Records allowlisted failure codes with bounded retry and no raw response.';

commit;

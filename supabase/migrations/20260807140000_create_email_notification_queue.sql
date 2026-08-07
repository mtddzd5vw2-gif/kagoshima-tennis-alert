-- Phase 3 user-specific email notification queue foundation.
-- Sending, Resend API calls, webhooks, and workflow integration are out of scope.

create type public.notification_channel as enum (
  'email'
);

create type public.notification_message_status as enum (
  'pending',
  'processing',
  'accepted',
  'delivered',
  'retry_wait',
  'failed_permanent',
  'bounced',
  'complained',
  'suppressed',
  'cancelled'
);

create function public.notification_email_payload_is_valid(
  p_payload jsonb
)
returns boolean
language sql
immutable
security invoker
set search_path = ''
as $$
  select
    pg_catalog.jsonb_typeof(p_payload) = 'object'
    and not exists (
      select 1
      from pg_catalog.jsonb_object_keys(
        case
          when pg_catalog.jsonb_typeof(p_payload) = 'object'
            then p_payload
          else '{}'::pg_catalog.jsonb
        end
      ) as payload_key(key)
      where payload_key.key <> all (
        array[
          'court_name',
          'reservation_url'
        ]::pg_catalog.text[]
      )
    )
    and (
      not (p_payload ? 'court_name')
      or (
        pg_catalog.jsonb_typeof(p_payload -> 'court_name') = 'string'
        and pg_catalog.btrim(p_payload ->> 'court_name') <> ''
        and pg_catalog.char_length(p_payload ->> 'court_name') <= 200
      )
    )
    and (
      not (p_payload ? 'reservation_url')
      or (
        pg_catalog.jsonb_typeof(p_payload -> 'reservation_url') = 'string'
        and pg_catalog.btrim(p_payload ->> 'reservation_url') <> ''
        and pg_catalog.char_length(
          p_payload ->> 'reservation_url'
        ) <= 2048
      )
    );
$$;

create table public.notification_email_preferences (
  user_id uuid primary key references auth.users(id) on delete cascade,
  is_enabled boolean not null default false,
  disabled_reason text,
  disabled_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint notification_email_preferences_disabled_reason_not_blank
    check (
      disabled_reason is null
      or pg_catalog.btrim(disabled_reason) <> ''
    ),
  constraint notification_email_preferences_disabled_reason_length
    check (
      disabled_reason is null
      or pg_catalog.char_length(disabled_reason) <= 80
    ),
  constraint notification_email_preferences_enabled_not_suppressed
    check (
      is_enabled = false
      or disabled_reason is null
    ),
  constraint notification_email_preferences_reason_has_timestamp
    check (
      disabled_reason is null
      or disabled_at is not null
    )
);

create table public.notification_delivery_items (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  channel public.notification_channel not null,
  slot_id text not null,
  facility_id text not null references public.facilities(id),
  facility_name text not null,
  available_date date not null,
  start_time time without time zone not null,
  end_time time without time zone not null,
  matched_rule_ids uuid[] not null,
  payload jsonb not null,
  created_at timestamptz not null default now(),
  constraint notification_delivery_items_slot_id_not_blank
    check (pg_catalog.btrim(slot_id) <> ''),
  constraint notification_delivery_items_slot_id_length
    check (pg_catalog.char_length(slot_id) <= 200),
  constraint notification_delivery_items_facility_name_not_blank
    check (pg_catalog.btrim(facility_name) <> ''),
  constraint notification_delivery_items_facility_name_length
    check (pg_catalog.char_length(facility_name) <= 200),
  constraint notification_delivery_items_time_order
    check (start_time < end_time),
  constraint notification_delivery_items_matched_rules_count
    check (pg_catalog.cardinality(matched_rule_ids) between 1 and 5),
  constraint notification_delivery_items_payload_is_object
    check (pg_catalog.jsonb_typeof(payload) = 'object'),
  constraint notification_delivery_items_payload_size
    check (pg_catalog.octet_length(payload::pg_catalog.text) <= 16384),
  constraint notification_delivery_items_payload_fields_valid
    check (
      public.notification_email_payload_is_valid(payload)
    ),
  constraint notification_delivery_items_user_channel_slot_key
    unique (user_id, channel, slot_id),
  constraint notification_delivery_items_id_user_channel_key
    unique (id, user_id, channel)
);

create index notification_delivery_items_user_created_at_idx
  on public.notification_delivery_items (user_id, created_at desc);

create index notification_delivery_items_created_at_idx
  on public.notification_delivery_items (created_at);

create table public.notification_messages (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  channel public.notification_channel not null,
  status public.notification_message_status not null default 'pending',
  attempt_count integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  locked_at timestamptz,
  locked_until timestamptz,
  provider_message_id text,
  provider_status text,
  last_error_code text,
  last_error_message text,
  accepted_at timestamptz,
  delivered_at timestamptz,
  failed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint notification_messages_attempt_count_nonnegative
    check (attempt_count >= 0),
  constraint notification_messages_lock_pair
    check (
      (locked_at is null and locked_until is null)
      or (
        locked_at is not null
        and locked_until is not null
        and locked_at < locked_until
      )
    ),
  constraint notification_messages_provider_message_id_not_blank
    check (
      provider_message_id is null
      or pg_catalog.btrim(provider_message_id) <> ''
    ),
  constraint notification_messages_provider_message_id_length
    check (
      provider_message_id is null
      or pg_catalog.char_length(provider_message_id) <= 255
    ),
  constraint notification_messages_provider_status_not_blank
    check (
      provider_status is null
      or pg_catalog.btrim(provider_status) <> ''
    ),
  constraint notification_messages_provider_status_length
    check (
      provider_status is null
      or pg_catalog.char_length(provider_status) <= 80
    ),
  constraint notification_messages_last_error_code_not_blank
    check (
      last_error_code is null
      or pg_catalog.btrim(last_error_code) <> ''
    ),
  constraint notification_messages_last_error_code_length
    check (
      last_error_code is null
      or pg_catalog.char_length(last_error_code) <= 80
    ),
  constraint notification_messages_last_error_message_length
    check (
      last_error_message is null
      or pg_catalog.char_length(last_error_message) <= 1000
    ),
  constraint notification_messages_id_user_channel_key
    unique (id, user_id, channel),
  constraint notification_messages_id_provider_message_key
    unique (id, provider_message_id)
);

create index notification_messages_claim_idx
  on public.notification_messages (
    status,
    next_attempt_at,
    locked_until,
    created_at
  )
  where status in ('pending', 'processing', 'retry_wait');

create index notification_messages_user_created_at_idx
  on public.notification_messages (user_id, created_at desc);

create index notification_messages_created_at_idx
  on public.notification_messages (created_at);

create unique index notification_messages_provider_message_id_key
  on public.notification_messages (provider_message_id)
  where provider_message_id is not null;

create table public.notification_message_items (
  message_id uuid not null,
  delivery_item_id uuid not null,
  user_id uuid not null,
  channel public.notification_channel not null,
  created_at timestamptz not null default now(),
  constraint notification_message_items_pkey
    primary key (message_id, delivery_item_id),
  constraint notification_message_items_delivery_item_key
    unique (delivery_item_id),
  constraint notification_message_items_message_owner_fkey
    foreign key (message_id, user_id, channel)
    references public.notification_messages(id, user_id, channel)
    on delete cascade,
  constraint notification_message_items_delivery_owner_fkey
    foreign key (delivery_item_id, user_id, channel)
    references public.notification_delivery_items(id, user_id, channel)
    on delete cascade
);

create index notification_message_items_user_id_idx
  on public.notification_message_items (user_id);

create table public.notification_provider_events (
  id uuid primary key default gen_random_uuid(),
  message_id uuid not null,
  provider text not null default 'resend',
  provider_event_id text not null,
  provider_message_id text not null,
  event_type text not null,
  provider_status text,
  occurred_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint notification_provider_events_provider_check
    check (provider = 'resend'),
  constraint notification_provider_events_event_id_not_blank
    check (pg_catalog.btrim(provider_event_id) <> ''),
  constraint notification_provider_events_event_id_length
    check (pg_catalog.char_length(provider_event_id) <= 255),
  constraint notification_provider_events_message_id_not_blank
    check (pg_catalog.btrim(provider_message_id) <> ''),
  constraint notification_provider_events_message_id_length
    check (pg_catalog.char_length(provider_message_id) <= 255),
  constraint notification_provider_events_event_type_not_blank
    check (pg_catalog.btrim(event_type) <> ''),
  constraint notification_provider_events_event_type_length
    check (pg_catalog.char_length(event_type) <= 80),
  constraint notification_provider_events_provider_status_not_blank
    check (
      provider_status is null
      or pg_catalog.btrim(provider_status) <> ''
    ),
  constraint notification_provider_events_provider_status_length
    check (
      provider_status is null
      or pg_catalog.char_length(provider_status) <= 80
    ),
  constraint notification_provider_events_provider_event_key
    unique (provider, provider_event_id),
  constraint notification_provider_events_message_provider_fkey
    foreign key (message_id, provider_message_id)
    references public.notification_messages(id, provider_message_id)
    on delete cascade
);

create index notification_provider_events_message_id_idx
  on public.notification_provider_events (message_id);

create index notification_provider_events_occurred_at_idx
  on public.notification_provider_events (occurred_at);

create function public.set_email_notification_preference_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if old.is_enabled = true and new.is_enabled = false
    and new.disabled_at is null then
    new.disabled_at := pg_catalog.now();
  elsif new.is_enabled = true and new.disabled_reason is null then
    new.disabled_at := null;
  elsif new.disabled_reason is not null
    and new.disabled_at is null then
    new.disabled_at := pg_catalog.now();
  end if;

  new.updated_at := pg_catalog.now();
  return new;
end;
$$;

create trigger set_notification_email_preferences_updated_at
before update on public.notification_email_preferences
for each row
execute function public.set_email_notification_preference_updated_at();

create function public.set_notification_message_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  new.updated_at := pg_catalog.now();
  return new;
end;
$$;

create trigger set_notification_messages_updated_at
before update on public.notification_messages
for each row
execute function public.set_notification_message_updated_at();

create function public.create_email_notification_preference_for_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.notification_email_preferences (user_id)
  values (new.id)
  on conflict (user_id) do nothing;

  return new;
end;
$$;

create trigger create_email_notification_preference_after_auth_user_insert
after insert on auth.users
for each row
execute function public.create_email_notification_preference_for_new_auth_user();

-- Existing users are explicitly backfilled with the default disabled state.
insert into public.notification_email_preferences (user_id)
select auth_user.id
from auth.users as auth_user
on conflict (user_id) do nothing;

create function public.enqueue_email_notification_candidates(
  p_candidates jsonb
)
returns table (
  candidate_count integer,
  inserted_delivery_item_count integer,
  inserted_message_count integer,
  linked_item_count integer
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_candidate pg_catalog.jsonb;
  v_rule_id_value pg_catalog.jsonb;
  v_candidate_count pg_catalog.int4;
  v_available_date pg_catalog.date;
  v_start_time pg_catalog.time;
  v_end_time pg_catalog.time;
  v_max_candidates constant pg_catalog.int4 := 500;
  v_max_matched_rules constant pg_catalog.int4 := 5;
  v_max_payload_bytes constant pg_catalog.int4 := 16384;
begin
  if (
    p_candidates is null
    or pg_catalog.jsonb_typeof(p_candidates) <> 'array'
  ) then
    raise exception 'Email notification candidates must be a JSON array.'
      using errcode = '22023';
  end if;

  v_candidate_count := pg_catalog.jsonb_array_length(p_candidates);

  if v_candidate_count > v_max_candidates then
    raise exception 'Email notification candidate batch exceeds 500 items.'
      using errcode = '22023';
  end if;

  for v_candidate in
    select candidate.value
    from pg_catalog.jsonb_array_elements(p_candidates) as candidate(value)
  loop
    if (
      pg_catalog.jsonb_typeof(v_candidate) <> 'object'
      or not (
        v_candidate ?& array[
          'user_id',
          'channel',
          'slot_id',
          'facility_id',
          'facility_name',
          'available_date',
          'start_time',
          'end_time',
          'matched_rule_ids',
          'payload'
        ]::pg_catalog.text[]
      )
      or (
        select pg_catalog.count(*)
        from pg_catalog.jsonb_object_keys(v_candidate)
      ) <> 10
      or pg_catalog.jsonb_typeof(v_candidate -> 'user_id') <> 'string'
      or pg_catalog.jsonb_typeof(v_candidate -> 'channel') <> 'string'
      or pg_catalog.jsonb_typeof(v_candidate -> 'slot_id') <> 'string'
      or pg_catalog.jsonb_typeof(v_candidate -> 'facility_id') <> 'string'
      or pg_catalog.jsonb_typeof(v_candidate -> 'facility_name') <> 'string'
      or pg_catalog.jsonb_typeof(v_candidate -> 'available_date') <> 'string'
      or pg_catalog.jsonb_typeof(v_candidate -> 'start_time') <> 'string'
      or pg_catalog.jsonb_typeof(v_candidate -> 'end_time') <> 'string'
      or pg_catalog.jsonb_typeof(v_candidate -> 'matched_rule_ids') <> 'array'
      or pg_catalog.jsonb_typeof(v_candidate -> 'payload') <> 'object'
    ) then
      raise exception 'Email notification candidate shape is invalid.'
        using errcode = '22023';
    end if;

    if (
      v_candidate ->> 'channel' <> 'email'
      or pg_catalog.btrim(v_candidate ->> 'slot_id') = ''
      or pg_catalog.char_length(v_candidate ->> 'slot_id') > 200
      or pg_catalog.btrim(v_candidate ->> 'facility_id') = ''
      or pg_catalog.char_length(v_candidate ->> 'facility_id') > 100
      or pg_catalog.btrim(v_candidate ->> 'facility_name') = ''
      or pg_catalog.char_length(v_candidate ->> 'facility_name') > 200
      or (v_candidate ->> 'available_date')
        !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
      or (v_candidate ->> 'start_time')
        !~ '^[0-2][0-9]:[0-5][0-9](:[0-5][0-9])?$'
      or (v_candidate ->> 'end_time')
        !~ '^[0-2][0-9]:[0-5][0-9](:[0-5][0-9])?$'
      or pg_catalog.jsonb_array_length(
        v_candidate -> 'matched_rule_ids'
      ) < 1
      or pg_catalog.jsonb_array_length(
        v_candidate -> 'matched_rule_ids'
      ) > v_max_matched_rules
      or pg_catalog.octet_length(
        (v_candidate -> 'payload')::pg_catalog.text
      ) > v_max_payload_bytes
      or not public.notification_email_payload_is_valid(
        v_candidate -> 'payload'
      )
    ) then
      raise exception 'Email notification candidate values are invalid.'
        using errcode = '22023';
    end if;

    begin
      perform (v_candidate ->> 'user_id')::pg_catalog.uuid;
      v_available_date :=
        (v_candidate ->> 'available_date')::pg_catalog.date;
      v_start_time := (v_candidate ->> 'start_time')::pg_catalog.time;
      v_end_time := (v_candidate ->> 'end_time')::pg_catalog.time;
    exception
      when invalid_text_representation or datetime_field_overflow then
        raise exception 'Email notification candidate types are invalid.'
          using errcode = '22023';
    end;

    if (
      v_available_date::pg_catalog.text
        <> v_candidate ->> 'available_date'
      or v_start_time >= v_end_time
    ) then
      raise exception 'Email notification candidate schedule is invalid.'
        using errcode = '22023';
    end if;

    for v_rule_id_value in
      select rule_id.value
      from pg_catalog.jsonb_array_elements(
        v_candidate -> 'matched_rule_ids'
      ) as rule_id(value)
    loop
      if pg_catalog.jsonb_typeof(v_rule_id_value) <> 'string' then
        raise exception 'Matched notification rule IDs must be UUID strings.'
          using errcode = '22023';
      end if;

      begin
        perform (v_rule_id_value #>> '{}')::pg_catalog.uuid;
      exception
        when invalid_text_representation then
          raise exception 'Matched notification rule IDs are invalid.'
            using errcode = '22023';
      end;
    end loop;

    if not exists (
      select 1
      from public.facilities as facility
      where facility.id = v_candidate ->> 'facility_id'
        and facility.name = pg_catalog.btrim(
          v_candidate ->> 'facility_name'
        )
        and facility.is_active = true
    ) then
      raise exception 'Email notification candidate facility is invalid.'
        using errcode = '22023';
    end if;
  end loop;

  if exists (
    with normalized_candidates as (
      select
        (candidate_input.candidate ->> 'user_id')::pg_catalog.uuid
          as user_id,
        (candidate_input.candidate ->> 'channel')::public.notification_channel
          as channel,
        pg_catalog.btrim(candidate_input.candidate ->> 'slot_id')
          as slot_id,
        pg_catalog.jsonb_build_object(
          'facility_id',
          candidate_input.candidate ->> 'facility_id',
          'facility_name',
          pg_catalog.btrim(
            candidate_input.candidate ->> 'facility_name'
          ),
          'available_date',
          (candidate_input.candidate ->> 'available_date')::pg_catalog.date,
          'start_time',
          (candidate_input.candidate ->> 'start_time')::pg_catalog.time,
          'end_time',
          (candidate_input.candidate ->> 'end_time')::pg_catalog.time,
          'payload',
          candidate_input.candidate -> 'payload'
        ) as snapshot
      from pg_catalog.jsonb_array_elements(p_candidates)
        as candidate_input(candidate)
    )
    select 1
    from normalized_candidates as candidate
    group by candidate.user_id, candidate.channel, candidate.slot_id
    having pg_catalog.count(distinct candidate.snapshot) > 1
  ) then
    raise exception
      'Duplicate email notification candidates have conflicting snapshots.'
      using errcode = '22023';
  end if;

  return query
  with parsed_candidates as materialized (
    select
      candidate_input.ordinality,
      (candidate_input.candidate ->> 'user_id')::pg_catalog.uuid
        as user_id,
      (candidate_input.candidate ->> 'channel')::public.notification_channel
        as channel,
      pg_catalog.btrim(candidate_input.candidate ->> 'slot_id')
        as slot_id,
      candidate_input.candidate ->> 'facility_id' as facility_id,
      pg_catalog.btrim(candidate_input.candidate ->> 'facility_name')
        as facility_name,
      (candidate_input.candidate ->> 'available_date')::pg_catalog.date
        as available_date,
      (candidate_input.candidate ->> 'start_time')::pg_catalog.time
        as start_time,
      (candidate_input.candidate ->> 'end_time')::pg_catalog.time
        as end_time,
      array(
        select distinct
          (rule_id.value #>> '{}')::pg_catalog.uuid
        from pg_catalog.jsonb_array_elements(
          candidate_input.candidate -> 'matched_rule_ids'
        ) as rule_id(value)
        order by (rule_id.value #>> '{}')::pg_catalog.uuid
      ) as matched_rule_ids,
      candidate_input.candidate -> 'payload' as payload
    from pg_catalog.jsonb_array_elements(p_candidates)
      with ordinality as candidate_input(candidate, ordinality)
  ),
  candidate_snapshots as materialized (
    select distinct on (
      parsed.user_id,
      parsed.channel,
      parsed.slot_id
    )
      parsed.user_id,
      parsed.channel,
      parsed.slot_id,
      parsed.facility_id,
      parsed.facility_name,
      parsed.available_date,
      parsed.start_time,
      parsed.end_time,
      parsed.payload
    from parsed_candidates as parsed
    order by
      parsed.user_id,
      parsed.channel,
      parsed.slot_id,
      parsed.ordinality
  ),
  aggregated_candidate_rules as materialized (
    select
      parsed.user_id,
      parsed.channel,
      parsed.slot_id,
      pg_catalog.array_agg(
        distinct matched_rule.rule_id
        order by matched_rule.rule_id
      ) as matched_rule_ids
    from parsed_candidates as parsed
    cross join lateral pg_catalog.unnest(
      parsed.matched_rule_ids
    ) as matched_rule(rule_id)
    group by parsed.user_id, parsed.channel, parsed.slot_id
  ),
  deduplicated_candidates as materialized (
    select
      snapshot.user_id,
      snapshot.channel,
      snapshot.slot_id,
      snapshot.facility_id,
      snapshot.facility_name,
      snapshot.available_date,
      snapshot.start_time,
      snapshot.end_time,
      matched_rules.matched_rule_ids,
      snapshot.payload
    from candidate_snapshots as snapshot
    inner join aggregated_candidate_rules as matched_rules
      on matched_rules.user_id = snapshot.user_id
      and matched_rules.channel = snapshot.channel
      and matched_rules.slot_id = snapshot.slot_id
  ),
  eligible_candidates as materialized (
    select
      candidate.user_id,
      candidate.channel,
      candidate.slot_id,
      candidate.facility_id,
      candidate.facility_name,
      candidate.available_date,
      candidate.start_time,
      candidate.end_time,
      enabled_rules.matched_rule_ids,
      candidate.payload
    from deduplicated_candidates as candidate
    inner join public.profiles as profile
      on profile.id = candidate.user_id
      and profile.membership_status =
        'active'::public.membership_status
    inner join public.notification_email_preferences as preference
      on preference.user_id = candidate.user_id
      and preference.is_enabled = true
      and preference.disabled_reason is null
    cross join lateral (
      select pg_catalog.array_agg(
        notification_rule.id
        order by notification_rule.id
      ) as matched_rule_ids
      from public.notification_rules as notification_rule
      where notification_rule.user_id = candidate.user_id
        and notification_rule.is_enabled = true
        and notification_rule.id = any (candidate.matched_rule_ids)
    ) as enabled_rules
    where pg_catalog.cardinality(
      enabled_rules.matched_rule_ids
    ) >= 1
  ),
  inserted_delivery_items as (
    insert into public.notification_delivery_items (
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
    select
      candidate.user_id,
      candidate.channel,
      candidate.slot_id,
      candidate.facility_id,
      candidate.facility_name,
      candidate.available_date,
      candidate.start_time,
      candidate.end_time,
      candidate.matched_rule_ids,
      candidate.payload
    from eligible_candidates as candidate
    on conflict (user_id, channel, slot_id) do nothing
    returning id, user_id, channel
  ),
  inserted_messages as (
    insert into public.notification_messages (
      user_id,
      channel,
      status,
      next_attempt_at
    )
    select
      delivery_item.user_id,
      delivery_item.channel,
      'pending'::public.notification_message_status,
      pg_catalog.now()
    from inserted_delivery_items as delivery_item
    group by delivery_item.user_id, delivery_item.channel
    returning id, user_id, channel
  ),
  inserted_links as (
    insert into public.notification_message_items (
      message_id,
      delivery_item_id,
      user_id,
      channel
    )
    select
      message.id,
      delivery_item.id,
      delivery_item.user_id,
      delivery_item.channel
    from inserted_delivery_items as delivery_item
    inner join inserted_messages as message
      on message.user_id = delivery_item.user_id
      and message.channel = delivery_item.channel
    returning message_id
  )
  select
    v_candidate_count,
    (
      select pg_catalog.count(*)::pg_catalog.int4
      from inserted_delivery_items
    ),
    (
      select pg_catalog.count(*)::pg_catalog.int4
      from inserted_messages
    ),
    (
      select pg_catalog.count(*)::pg_catalog.int4
      from inserted_links
    );
end;
$$;

create function public.claim_email_messages(
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
begin
  if (
    batch_size is null
    or batch_size < 1
    or batch_size > v_max_batch_size
  ) then
    raise exception 'Email message claim batch size must be 1 to 100.'
      using errcode = '22023';
  end if;

  return query
  with ineligible_messages as materialized (
    select message.id
    from public.notification_messages as message
    where (
      (
        message.status in (
          'pending'::public.notification_message_status,
          'retry_wait'::public.notification_message_status
        )
      ) or (
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
  ),
  cancelled_messages as (
    update public.notification_messages as message
    set
      status = 'cancelled'::public.notification_message_status,
      locked_at = null,
      locked_until = null
    from ineligible_messages as ineligible
    where message.id = ineligible.id
    returning message.id
  ),
  claimable_messages as materialized (
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
      and message.next_attempt_at <= pg_catalog.now()
      and (
        message.locked_until is null
        or message.locked_until <= pg_catalog.now()
      )
    ) or (
      message.status =
        'processing'::public.notification_message_status
      and message.locked_until <= pg_catalog.now()
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
        delivery_item.facility_name
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

alter table public.notification_email_preferences
  enable row level security;
alter table public.notification_delivery_items
  enable row level security;
alter table public.notification_messages
  enable row level security;
alter table public.notification_message_items
  enable row level security;
alter table public.notification_provider_events
  enable row level security;

create policy notification_email_preferences_select_own_active
on public.notification_email_preferences
for select
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status =
        'active'::public.membership_status
  )
);

create policy notification_email_preferences_update_own_active
on public.notification_email_preferences
for update
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status =
        'active'::public.membership_status
  )
)
with check (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status =
        'active'::public.membership_status
  )
);

revoke all privileges on table
  public.notification_email_preferences,
  public.notification_delivery_items,
  public.notification_messages,
  public.notification_message_items,
  public.notification_provider_events
from public, anon, authenticated;

grant select on table public.notification_email_preferences
to authenticated;

grant update (is_enabled)
on table public.notification_email_preferences
to authenticated;

revoke all on function public.notification_email_payload_is_valid(jsonb)
from public, anon, authenticated;

revoke all on function
  public.set_email_notification_preference_updated_at()
from public, anon, authenticated;

revoke all on function public.set_notification_message_updated_at()
from public, anon, authenticated;

revoke all on function
  public.create_email_notification_preference_for_new_auth_user()
from public, anon, authenticated;

revoke execute on function
  public.enqueue_email_notification_candidates(jsonb)
from public, anon, authenticated;

grant execute on function
  public.enqueue_email_notification_candidates(jsonb)
to service_role;

revoke execute on function public.claim_email_messages(integer)
from public, anon, authenticated;

grant execute on function public.claim_email_messages(integer)
to service_role;

comment on table public.notification_email_preferences is
  'Per-user email notification opt-in. Email addresses remain in Supabase Auth.';

comment on table public.notification_delivery_items is
  'Immutable deduplication ledger for user, channel, and stable slot ID.';

comment on table public.notification_messages is
  'Provider-agnostic email delivery attempts and retry state.';

comment on table public.notification_message_items is
  'One-time association between queued messages and delivery items.';

comment on table public.notification_provider_events is
  'Normalized provider events without raw webhook payloads or recipient data.';

comment on function public.enqueue_email_notification_candidates(jsonb) is
  'Validates and enqueues eligible email candidates, returning aggregate counts only.';

comment on function public.claim_email_messages(integer) is
  'Claims bounded email message batches with row locks and no recipient address.';

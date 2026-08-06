-- Save a complete Phase 2 notification rule in one transaction.
-- The function remains security invoker so every statement is subject to RLS.

create function public.save_notification_rule(
  p_rule_id uuid,
  p_name text,
  p_is_enabled boolean,
  p_date_from date,
  p_date_to date,
  p_start_time time without time zone,
  p_end_time time without time zone,
  p_minimum_duration_minutes smallint,
  p_facility_ids text[],
  p_weekdays smallint[]
)
returns uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_user_id uuid := auth.uid();
  v_rule_id uuid;
  v_facility_ids text[];
  v_weekdays smallint[];
begin
  if v_user_id is null then
    raise exception 'Authentication is required to save a notification rule.'
      using errcode = '42501';
  end if;

  if (
    p_name is null
    or pg_catalog.btrim(p_name) = ''
    or pg_catalog.char_length(pg_catalog.btrim(p_name)) > 80
  ) then
    raise exception 'Notification rule name must be 1 to 80 characters.'
      using errcode = '22023';
  end if;

  if p_is_enabled is null then
    raise exception 'Notification rule enabled state is required.'
      using errcode = '22023';
  end if;

  if (
    p_start_time is null
    or p_end_time is null
    or p_start_time >= p_end_time
  ) then
    raise exception 'Notification rule start time must be before end time.'
      using errcode = '22023';
  end if;

  if (
    p_date_from is not null
    and p_date_to is not null
    and p_date_from > p_date_to
  ) then
    raise exception 'Notification rule start date must not be after end date.'
      using errcode = '22023';
  end if;

  if (
    p_minimum_duration_minutes is null
    or p_minimum_duration_minutes < 30
    or p_minimum_duration_minutes > 720
    or p_minimum_duration_minutes % 30 <> 0
  ) then
    raise exception 'Minimum duration must be 30 to 720 minutes in 30 minute steps.'
      using errcode = '22023';
  end if;

  if (
    p_facility_ids is null
    or pg_catalog.cardinality(p_facility_ids) < 1
  ) then
    raise exception 'At least one facility is required.'
      using errcode = '22023';
  end if;

  if exists (
    select 1
    from pg_catalog.unnest(p_facility_ids) as facility_input(facility_id)
    where facility_input.facility_id is null
      or pg_catalog.btrim(facility_input.facility_id) = ''
  ) then
    raise exception 'Facility IDs must not be null or blank.'
      using errcode = '22023';
  end if;

  select pg_catalog.array_agg(
    distinct facility_input.facility_id
    order by facility_input.facility_id
  )
  into v_facility_ids
  from pg_catalog.unnest(p_facility_ids) as facility_input(facility_id);

  if (
    select pg_catalog.count(*)
    from public.facilities as facility
    where facility.id = any (v_facility_ids)
      and facility.is_active = true
  ) <> pg_catalog.cardinality(v_facility_ids) then
    raise exception 'Every facility must exist and be active.'
      using errcode = '22023';
  end if;

  if p_weekdays is null or pg_catalog.cardinality(p_weekdays) < 1 then
    raise exception 'At least one weekday is required.'
      using errcode = '22023';
  end if;

  if exists (
    select 1
    from pg_catalog.unnest(p_weekdays) as weekday_input(weekday)
    where weekday_input.weekday is null
      or weekday_input.weekday < 1
      or weekday_input.weekday > 7
  ) then
    raise exception 'Weekdays must use ISO values 1 through 7.'
      using errcode = '22023';
  end if;

  select pg_catalog.array_agg(
    distinct weekday_input.weekday
    order by weekday_input.weekday
  )
  into v_weekdays
  from pg_catalog.unnest(p_weekdays) as weekday_input(weekday);

  if p_rule_id is null then
    insert into public.notification_rules (
      user_id,
      name,
      is_enabled,
      date_from,
      date_to,
      start_time,
      end_time,
      minimum_duration_minutes
    )
    values (
      v_user_id,
      pg_catalog.btrim(p_name),
      p_is_enabled,
      p_date_from,
      p_date_to,
      p_start_time,
      p_end_time,
      p_minimum_duration_minutes
    )
    returning id into v_rule_id;
  else
    update public.notification_rules as rule
    set
      name = pg_catalog.btrim(p_name),
      is_enabled = p_is_enabled,
      date_from = p_date_from,
      date_to = p_date_to,
      start_time = p_start_time,
      end_time = p_end_time,
      minimum_duration_minutes = p_minimum_duration_minutes
    where rule.id = p_rule_id
      and rule.user_id = v_user_id
    returning rule.id into v_rule_id;

    if not found then
      raise exception 'Notification rule was not found for the current user.'
        using errcode = '42501';
    end if;
  end if;

  delete from public.notification_rule_facilities as selected_facility
  where selected_facility.rule_id = v_rule_id
    and selected_facility.user_id = v_user_id;

  delete from public.notification_rule_weekdays as selected_weekday
  where selected_weekday.rule_id = v_rule_id
    and selected_weekday.user_id = v_user_id;

  insert into public.notification_rule_facilities (
    rule_id,
    user_id,
    facility_id
  )
  select
    v_rule_id,
    v_user_id,
    facility_input.facility_id
  from pg_catalog.unnest(v_facility_ids) as facility_input(facility_id);

  insert into public.notification_rule_weekdays (
    rule_id,
    user_id,
    weekday
  )
  select
    v_rule_id,
    v_user_id,
    weekday_input.weekday
  from pg_catalog.unnest(v_weekdays) as weekday_input(weekday);

  return v_rule_id;
end;
$$;

revoke all on function public.save_notification_rule(
  uuid,
  text,
  boolean,
  date,
  date,
  time without time zone,
  time without time zone,
  smallint,
  text[],
  smallint[]
)
from public, anon, authenticated;

grant execute on function public.save_notification_rule(
  uuid,
  text,
  boolean,
  date,
  date,
  time without time zone,
  time without time zone,
  smallint,
  text[],
  smallint[]
)
to authenticated;

comment on function public.save_notification_rule(
  uuid,
  text,
  boolean,
  date,
  date,
  time without time zone,
  time without time zone,
  smallint,
  text[],
  smallint[]
) is
  'Atomically creates or updates one complete notification rule for auth.uid().';

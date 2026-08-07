-- Read complete enabled notification rules for trusted Phase 2 matching.
-- This RPC is service-role only and remains subject to the caller's privileges.

create function public.list_notification_rules_for_matching()
returns table (
  rule_id uuid,
  user_id uuid,
  date_from date,
  date_to date,
  start_time time without time zone,
  end_time time without time zone,
  minimum_duration_minutes smallint,
  facility_ids text[],
  weekdays smallint[]
)
language sql
security invoker
stable
set search_path = ''
as $$
  select
    rule.id as rule_id,
    rule.user_id,
    rule.date_from,
    rule.date_to,
    rule.start_time,
    rule.end_time,
    rule.minimum_duration_minutes,
    pg_catalog.array_agg(
      distinct selected_facility.facility_id
      order by selected_facility.facility_id
    ) as facility_ids,
    pg_catalog.array_agg(
      distinct selected_weekday.weekday
      order by selected_weekday.weekday
    ) as weekdays
  from public.notification_rules as rule
  inner join public.profiles as profile
    on profile.id = rule.user_id
    and profile.membership_status = 'active'::public.membership_status
  inner join public.notification_rule_facilities as selected_facility
    on selected_facility.rule_id = rule.id
    and selected_facility.user_id = rule.user_id
  inner join public.notification_rule_weekdays as selected_weekday
    on selected_weekday.rule_id = rule.id
    and selected_weekday.user_id = rule.user_id
  where rule.is_enabled = true
  group by
    rule.id,
    rule.user_id,
    rule.date_from,
    rule.date_to,
    rule.start_time,
    rule.end_time,
    rule.minimum_duration_minutes
  having
    pg_catalog.count(distinct selected_facility.facility_id) >= 1
    and pg_catalog.count(distinct selected_weekday.weekday) >= 1
  order by rule.user_id, rule.id;
$$;

revoke execute on function public.list_notification_rules_for_matching()
from public, anon, authenticated;

grant execute on function public.list_notification_rules_for_matching()
to service_role;

comment on function public.list_notification_rules_for_matching() is
  'Returns active members enabled notification rules to trusted matching jobs.';

-- Limit each user to five notification rules, including paused rules.
-- Delivery remains a Phase 3 responsibility.

begin;

lock table public.notification_rules in share row exclusive mode;

do language plpgsql $migration_check$
declare
  v_max_notification_rules constant pg_catalog.int4 := 5;
begin
  if exists (
    select 1
    from public.notification_rules as notification_rule
    group by notification_rule.user_id
    having pg_catalog.count(*) > v_max_notification_rules
  ) then
    raise exception
      'Cannot apply notification rule limit because existing data exceeds 5 rules per user.'
      using errcode = '23514';
  end if;
end;
$migration_check$;

create function public.enforce_notification_rule_limit()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_max_notification_rules constant pg_catalog.int4 := 5;
  v_existing_rule_count pg_catalog.int8;
  v_lock_key pg_catalog.int8;
begin
  if tg_op = 'UPDATE' and new.user_id is not distinct from old.user_id then
    return new;
  end if;

  v_lock_key := pg_catalog.hashtextextended(
    new.user_id::pg_catalog.text,
    0::pg_catalog.int8
  );
  perform pg_catalog.pg_advisory_xact_lock(v_lock_key);

  if tg_op = 'INSERT' then
    select pg_catalog.count(*)
    into v_existing_rule_count
    from public.notification_rules as notification_rule
    where notification_rule.user_id = new.user_id;
  else
    select pg_catalog.count(*)
    into v_existing_rule_count
    from public.notification_rules as notification_rule
    where notification_rule.user_id = new.user_id
      and notification_rule.id <> old.id;
  end if;

  if v_existing_rule_count >= v_max_notification_rules then
    raise exception 'Notification rule limit of 5 has been reached.'
      using errcode = '23514';
  end if;

  return new;
end;
$$;

create trigger enforce_notification_rules_per_user_limit
before insert or update of user_id on public.notification_rules
for each row
execute function public.enforce_notification_rule_limit();

revoke all on function public.enforce_notification_rule_limit()
from public, anon, authenticated;

comment on function public.enforce_notification_rule_limit() is
  'Enforces a five-rule limit per user, including enabled and paused rules, after serializing inserts by user with a transaction advisory lock.';

comment on trigger enforce_notification_rules_per_user_limit
on public.notification_rules is
  'Rejects inserts and owner changes that would exceed five notification rules for one user.';

commit;

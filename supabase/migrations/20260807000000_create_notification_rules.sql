-- Tennis Court Watcher Phase 2 notification rule data model.
-- This migration stores user settings only. Delivery is a Phase 3 responsibility.

create table public.regions (
  id text primary key,
  country_code text not null,
  prefecture_code text not null,
  municipality_code text not null,
  name text not null,
  timezone text not null,
  is_active boolean not null default true,
  sort_order integer not null default 0,
  created_at timestamptz not null default now(),
  constraint regions_id_not_blank check (btrim(id) <> ''),
  constraint regions_country_code_not_blank check (btrim(country_code) <> ''),
  constraint regions_prefecture_code_not_blank
    check (btrim(prefecture_code) <> ''),
  constraint regions_municipality_code_not_blank
    check (btrim(municipality_code) <> ''),
  constraint regions_name_not_blank check (btrim(name) <> ''),
  constraint regions_timezone_not_blank check (btrim(timezone) <> '')
);

create table public.facility_types (
  id text primary key,
  name text not null,
  is_active boolean not null default true,
  sort_order integer not null default 0,
  created_at timestamptz not null default now(),
  constraint facility_types_id_not_blank check (btrim(id) <> ''),
  constraint facility_types_name_not_blank check (btrim(name) <> '')
);

create table public.facilities (
  id text primary key,
  region_id text not null references public.regions(id),
  facility_type_id text not null references public.facility_types(id),
  name text not null,
  is_active boolean not null default true,
  sort_order integer not null default 0,
  created_at timestamptz not null default now(),
  constraint facilities_id_not_blank check (btrim(id) <> ''),
  constraint facilities_name_not_blank check (btrim(name) <> '')
);

create table public.notification_rules (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  is_enabled boolean not null default false,
  date_from date,
  date_to date,
  start_time time not null,
  end_time time not null,
  minimum_duration_minutes smallint not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint notification_rules_name_not_blank check (btrim(name) <> ''),
  constraint notification_rules_name_length check (char_length(name) <= 80),
  constraint notification_rules_time_order check (start_time < end_time),
  constraint notification_rules_date_range check (
    date_from is null
    or date_to is null
    or date_from <= date_to
  ),
  constraint notification_rules_minimum_duration_range check (
    minimum_duration_minutes between 30 and 720
  ),
  constraint notification_rules_minimum_duration_step check (
    minimum_duration_minutes % 30 = 0
  ),
  constraint notification_rules_id_user_id_key unique (id, user_id)
);

create index notification_rules_user_id_idx
  on public.notification_rules (user_id);

create table public.notification_rule_facilities (
  rule_id uuid not null,
  user_id uuid not null,
  facility_id text not null references public.facilities(id),
  created_at timestamptz not null default now(),
  constraint notification_rule_facilities_pkey
    primary key (rule_id, facility_id),
  constraint notification_rule_facilities_rule_owner_fkey
    foreign key (rule_id, user_id)
    references public.notification_rules(id, user_id)
    on delete cascade
);

create index notification_rule_facilities_user_id_idx
  on public.notification_rule_facilities (user_id);

create index notification_rule_facilities_facility_id_idx
  on public.notification_rule_facilities (facility_id);

create table public.notification_rule_weekdays (
  rule_id uuid not null,
  user_id uuid not null,
  weekday smallint not null,
  created_at timestamptz not null default now(),
  constraint notification_rule_weekdays_pkey primary key (rule_id, weekday),
  constraint notification_rule_weekdays_rule_owner_fkey
    foreign key (rule_id, user_id)
    references public.notification_rules(id, user_id)
    on delete cascade,
  constraint notification_rule_weekdays_iso_check
    check (weekday between 1 and 7)
);

create index notification_rule_weekdays_user_id_idx
  on public.notification_rule_weekdays (user_id);

insert into public.regions (
  id,
  country_code,
  prefecture_code,
  municipality_code,
  name,
  timezone,
  sort_order
)
values (
  'jp-kagoshima-kagoshima-city',
  'JP',
  '46',
  '46201',
  '鹿児島市',
  'Asia/Tokyo',
  10
);

insert into public.facility_types (
  id,
  name,
  sort_order
)
values (
  'tennis-court',
  'テニスコート',
  10
);

insert into public.facilities (
  id,
  region_id,
  facility_type_id,
  name,
  sort_order
)
values
  (
    'kamoike-prefectural',
    'jp-kagoshima-kagoshima-city',
    'tennis-court',
    '鴨池県営テニスコート',
    10
  ),
  (
    'sumizei',
    'jp-kagoshima-kagoshima-city',
    'tennis-court',
    'SuMIzeiテニスコート',
    20
  ),
  (
    'toukai-tennis',
    'jp-kagoshima-kagoshima-city',
    'tennis-court',
    '東開庭球場',
    30
  );

create function public.set_notification_rule_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := pg_catalog.now();
  return new;
end;
$$;

create trigger set_notification_rules_updated_at
before update on public.notification_rules
for each row
execute function public.set_notification_rule_updated_at();

alter table public.regions enable row level security;
alter table public.facility_types enable row level security;
alter table public.facilities enable row level security;
alter table public.notification_rules enable row level security;
alter table public.notification_rule_facilities enable row level security;
alter table public.notification_rule_weekdays enable row level security;

create policy regions_select_authenticated
on public.regions
for select
to authenticated
using ((select auth.uid()) is not null);

create policy facility_types_select_authenticated
on public.facility_types
for select
to authenticated
using ((select auth.uid()) is not null);

create policy facilities_select_authenticated
on public.facilities
for select
to authenticated
using ((select auth.uid()) is not null);

create policy notification_rules_select_own_active
on public.notification_rules
for select
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

create policy notification_rules_insert_own_active
on public.notification_rules
for insert
to authenticated
with check (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

create policy notification_rules_update_own_active
on public.notification_rules
for update
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
)
with check (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

create policy notification_rules_delete_own_active
on public.notification_rules
for delete
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

create policy notification_rule_facilities_select_own_active
on public.notification_rule_facilities
for select
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

create policy notification_rule_facilities_insert_own_active
on public.notification_rule_facilities
for insert
to authenticated
with check (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

create policy notification_rule_facilities_update_own_active
on public.notification_rule_facilities
for update
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
)
with check (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

create policy notification_rule_facilities_delete_own_active
on public.notification_rule_facilities
for delete
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

create policy notification_rule_weekdays_select_own_active
on public.notification_rule_weekdays
for select
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

create policy notification_rule_weekdays_insert_own_active
on public.notification_rule_weekdays
for insert
to authenticated
with check (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

create policy notification_rule_weekdays_update_own_active
on public.notification_rule_weekdays
for update
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
)
with check (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

create policy notification_rule_weekdays_delete_own_active
on public.notification_rule_weekdays
for delete
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
  and exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.membership_status = 'active'
  )
);

revoke all privileges on table
  public.regions,
  public.facility_types,
  public.facilities,
  public.notification_rules,
  public.notification_rule_facilities,
  public.notification_rule_weekdays
from public, anon, authenticated;

grant select on table
  public.regions,
  public.facility_types,
  public.facilities
to authenticated;

grant select, insert, update, delete on table
  public.notification_rules,
  public.notification_rule_facilities,
  public.notification_rule_weekdays
to authenticated;

revoke all on function public.set_notification_rule_updated_at()
from public, anon, authenticated;

comment on table public.regions is
  'Region master for Phase 2 notification settings and future expansion.';

comment on table public.facility_types is
  'Facility type master for Phase 2 notification settings.';

comment on table public.facilities is
  'Facility master. IDs match facility_id values in data/availability.json.';

comment on table public.notification_rules is
  'Phase 2 user notification settings. Actual delivery is implemented in Phase 3.';

comment on table public.notification_rule_facilities is
  'Facilities selected by a Phase 2 notification rule.';

comment on table public.notification_rule_weekdays is
  'Weekdays selected by a Phase 2 notification rule.';

comment on column public.notification_rule_weekdays.weekday is
  'ISO 8601 weekday: 1=Monday through 7=Sunday.';

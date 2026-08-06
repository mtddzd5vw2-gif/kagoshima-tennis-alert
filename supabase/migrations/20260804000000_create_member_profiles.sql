-- Tennis Court Watcher Phase 1 member profiles and terms acceptance.
-- Apply this migration to a new Supabase project after Auth is available.

create type public.membership_status as enum (
  'pending_terms',
  'active',
  'withdrawal_pending',
  'suspended'
);

create table public.legal_document_versions (
  document_type text not null,
  version text not null,
  effective_at timestamptz not null,
  is_current boolean not null default false,
  created_at timestamptz not null default now(),
  constraint legal_document_versions_pkey primary key (document_type, version),
  constraint legal_document_versions_document_type_check
    check (document_type in ('terms')),
  constraint legal_document_versions_version_not_blank
    check (btrim(version) <> '')
);

create unique index legal_document_versions_one_current_per_type
  on public.legal_document_versions (document_type)
  where is_current;

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  membership_status public.membership_status not null default 'pending_terms',
  latest_terms_version text,
  latest_terms_accepted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint profiles_terms_acceptance_pair_check check (
    (latest_terms_version is null and latest_terms_accepted_at is null)
    or
    (latest_terms_version is not null and latest_terms_accepted_at is not null)
  )
);

create table public.terms_acceptances (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  document_type text not null,
  version text not null,
  accepted_at timestamptz not null default now(),
  source text not null default 'web',
  constraint terms_acceptances_document_version_fkey
    foreign key (document_type, version)
    references public.legal_document_versions(document_type, version),
  constraint terms_acceptances_source_check check (source = 'web'),
  constraint terms_acceptances_user_document_version_key
    unique (user_id, document_type, version)
);

comment on table public.terms_acceptances is
  'Append-only terms acceptance audit history. Browser roles have SELECT only.';

insert into public.legal_document_versions (
  document_type,
  version,
  effective_at,
  is_current
)
values (
  'terms',
  '2026-08-04-draft',
  timestamptz '2026-08-04 00:00:00+09',
  true
);

create function public.set_profile_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger set_profiles_updated_at
before update on public.profiles
for each row
execute function public.set_profile_updated_at();

create function public.create_profile_for_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id)
  values (new.id);
  return new;
end;
$$;

create trigger create_profile_after_auth_user_insert
after insert on auth.users
for each row
execute function public.create_profile_for_new_auth_user();

-- Existing Auth users start without inferred consent or active membership.
insert into public.profiles (id)
select users.id
from auth.users as users
on conflict (id) do nothing;

create function public.accept_current_terms()
returns table (
  version text,
  accepted_at timestamptz
)
language plpgsql
security definer
set search_path = ''
as $$
declare
  current_user_id uuid := auth.uid();
  current_terms_version text;
  recorded_accepted_at timestamptz;
begin
  if current_user_id is null then
    raise exception 'authentication required'
      using errcode = '42501';
  end if;

  select document.version
    into current_terms_version
  from public.legal_document_versions as document
  where document.document_type = 'terms'
    and document.is_current;

  if current_terms_version is null then
    raise exception 'current terms are not configured'
      using errcode = '55000';
  end if;

  insert into public.terms_acceptances (
    user_id,
    document_type,
    version
  )
  values (
    current_user_id,
    'terms',
    current_terms_version
  )
  on conflict (user_id, document_type, version) do nothing;

  select acceptance.accepted_at
    into recorded_accepted_at
  from public.terms_acceptances as acceptance
  where acceptance.user_id = current_user_id
    and acceptance.document_type = 'terms'
    and acceptance.version = current_terms_version;

  update public.profiles as profile
  set
    membership_status = case
      when profile.membership_status = 'pending_terms'
        then 'active'::public.membership_status
      else profile.membership_status
    end,
    latest_terms_version = current_terms_version,
    latest_terms_accepted_at = recorded_accepted_at
  where profile.id = current_user_id;

  if not found then
    raise exception 'member profile is unavailable'
      using errcode = '55000';
  end if;

  return query
  select current_terms_version, recorded_accepted_at;
end;
$$;

alter table public.legal_document_versions enable row level security;
alter table public.profiles enable row level security;
alter table public.terms_acceptances enable row level security;

create policy legal_document_versions_select_current_terms
on public.legal_document_versions
for select
to authenticated
using (
  (select auth.uid()) is not null
  and document_type = 'terms'
  and is_current
);

create policy profiles_select_own
on public.profiles
for select
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = id
);

create policy terms_acceptances_select_own
on public.terms_acceptances
for select
to authenticated
using (
  (select auth.uid()) is not null
  and (select auth.uid()) = user_id
);

revoke all privileges on table
  public.legal_document_versions,
  public.profiles,
  public.terms_acceptances
from public, anon, authenticated;

revoke all privileges on sequence public.terms_acceptances_id_seq
from public, anon, authenticated;

grant select on table
  public.legal_document_versions,
  public.profiles,
  public.terms_acceptances
to authenticated;

revoke all on function public.set_profile_updated_at()
from public, anon, authenticated;

revoke all on function public.create_profile_for_new_auth_user()
from public, anon, authenticated;

revoke all on function public.accept_current_terms()
from public, anon, authenticated;

grant execute on function public.accept_current_terms()
to authenticated;

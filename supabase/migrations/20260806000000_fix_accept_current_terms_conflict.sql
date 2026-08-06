-- Replace accept_current_terms() after the initial migration has been applied.
-- Naming the unique constraint avoids ambiguity with the RETURNS TABLE version
-- output parameter in PL/pgSQL.

create or replace function public.accept_current_terms()
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
  on conflict on constraint terms_acceptances_user_document_version_key
  do nothing;

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

revoke execute on function public.accept_current_terms()
from public, anon, authenticated;

grant execute on function public.accept_current_terms()
to authenticated;

-- Ejecuta este script en Supabase > SQL Editor.
-- Crea las cuentas desde Authentication > Users usando un email con formato:
-- nombredeusuario@montajespc.com (por ejemplo, nicknamed@montajespc.com).
-- Añade en User Metadata el campo username, con el nombre visible deseado.
-- Desactiva la confirmación por email si estas cuentas no tienen buzón real.

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  username text not null unique,
  created_at timestamptz not null default now()
);

create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles (id, username)
  values (new.id, coalesce(new.raw_user_meta_data->>'username', split_part(new.email, '@', 1)))
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users
for each row execute procedure public.handle_new_user();

-- También crea perfiles para cuentas que ya existían antes de instalar el trigger.
insert into public.profiles (id, username)
select
  u.id,
  coalesce(nullif(u.raw_user_meta_data->>'username', ''), split_part(u.email, '@', 1))
from auth.users u
where u.email is not null
on conflict (id) do nothing;

create table if not exists public.builds (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  parts jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.build_access (
  user_id uuid not null references public.profiles(id) on delete cascade,
  build_id uuid not null references public.builds(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (user_id, build_id)
);

create or replace function public.is_pc_admin()
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (select 1 from public.profiles p where p.id = (select auth.uid()) and lower(p.username) = 'nicknamed');
$$;
revoke all on function public.is_pc_admin() from public;
grant execute on function public.is_pc_admin() to authenticated;

alter table public.profiles enable row level security;
alter table public.builds enable row level security;
alter table public.build_access enable row level security;

drop policy if exists "profile self or admin read" on public.profiles;
create policy "profile self or admin read" on public.profiles for select to authenticated
using (id = (select auth.uid()) or (select public.is_pc_admin()));

drop policy if exists "admin manages builds" on public.builds;
create policy "admin manages builds" on public.builds for all to authenticated
using ((select public.is_pc_admin())) with check ((select public.is_pc_admin()));
drop policy if exists "viewer reads assigned builds" on public.builds;
create policy "viewer reads assigned builds" on public.builds for select to authenticated
using (exists (select 1 from public.build_access a where a.build_id = id and a.user_id = (select auth.uid())));

drop policy if exists "admin manages build access" on public.build_access;
create policy "admin manages build access" on public.build_access for all to authenticated
using ((select public.is_pc_admin())) with check ((select public.is_pc_admin()));
drop policy if exists "viewer reads own build access" on public.build_access;
create policy "viewer reads own build access" on public.build_access for select to authenticated
using (user_id = (select auth.uid()));

grant select on public.profiles to authenticated;
grant select, insert, update, delete on public.builds to authenticated;
grant select, insert, delete on public.build_access to authenticated;

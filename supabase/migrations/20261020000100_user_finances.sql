-- Run once as the database administrator in the hosted PostgreSQL SQL editor.
-- Replace the placeholder password and keep this app role credential private.
create role buy_wait_app login password 'REPLACE_WITH_A_LONG_RANDOM_PASSWORD'
  nosuperuser nocreatedb nocreaterole noinherit nobypassrls;
grant connect on database postgres to buy_wait_app;

create table if not exists public.user_profiles (
  user_id text primary key,
  currency char(3) not null check (currency ~ '^[A-Z]{3}$'),
  balance numeric(16,2) not null check (balance >= 0),
  reserve numeric(16,2) not null check (reserve >= 0 and reserve <= balance),
  updated_at date not null default current_date
);

create table if not exists public.user_cashflows (
  id bigint generated always as identity primary key,
  user_id text not null references public.user_profiles(user_id) on delete cascade,
  label varchar(120) not null,
  direction text not null check (direction in ('income','expense')),
  amount numeric(16,2) not null check (amount > 0),
  frequency text not null check (frequency in ('once','weekly','biweekly','monthly','quarterly')),
  next_date date not null
);

create index if not exists user_cashflows_by_owner on public.user_cashflows(user_id, next_date);

alter table public.user_profiles enable row level security;
alter table public.user_profiles force row level security;
alter table public.user_cashflows enable row level security;
alter table public.user_cashflows force row level security;

drop policy if exists profile_owner_only on public.user_profiles;
create policy profile_owner_only on public.user_profiles
  to buy_wait_app
  using (user_id = nullif(current_setting('app.user_id', true), ''))
  with check (user_id = nullif(current_setting('app.user_id', true), ''));

drop policy if exists cashflow_owner_only on public.user_cashflows;
create policy cashflow_owner_only on public.user_cashflows
  to buy_wait_app
  using (user_id = nullif(current_setting('app.user_id', true), ''))
  with check (user_id = nullif(current_setting('app.user_id', true), ''));

revoke all on public.user_profiles, public.user_cashflows from public, anon, authenticated, service_role;
grant usage on schema public to buy_wait_app;
revoke create on schema public from buy_wait_app;
grant select, insert, update, delete on public.user_profiles, public.user_cashflows to buy_wait_app;
revoke all on sequence public.user_cashflows_id_seq from public, anon, authenticated, service_role;
grant usage, select on sequence public.user_cashflows_id_seq to buy_wait_app;

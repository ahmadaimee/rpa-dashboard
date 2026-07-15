-- ============================================================
-- Multi-tenancy: company-scoped isolation.
--   Staff admins carry app_metadata.company = <slug> and can only see /
--   touch their own company's workers, scenarios, tasks, schedules and
--   pairing codes (RLS — other rows are invisible, not just blocked).
--   Super admins (no company in app_metadata) see everything and get a
--   company switcher in the dashboard.
-- ============================================================

create table public.companies (
  slug       text primary key,
  name       text not null,
  created_at timestamptz not null default now()
);
alter table public.companies enable row level security;

create or replace function public.admin_company() returns text
language sql stable as $$
  select auth.jwt() -> 'app_metadata' ->> 'company'
$$;

create policy companies_admin_read on public.companies
  for select using (public.is_admin());
create policy companies_super_write on public.companies
  for all using (public.is_admin() and public.admin_company() is null)
  with check (public.is_admin() and public.admin_company() is null);
create policy companies_worker_read on public.companies
  for select using (public.is_worker());

-- ── company columns ─────────────────────────────────────────
alter table public.workers       add column company text references public.companies(slug);
alter table public.scenarios     add column company text references public.companies(slug);
alter table public.tasks         add column company text;
alter table public.schedules     add column company text references public.companies(slug);
alter table public.pairing_codes add column company text references public.companies(slug);

-- scenario names are now unique per company (two companies can both
-- have a "DailyReport"); nulls-not-distinct keeps legacy rows unique too
alter table public.scenarios drop constraint scenarios_name_key;
alter table public.scenarios add constraint scenarios_company_name_key
  unique nulls not distinct (company, name);

-- tasks inherit their worker's company automatically
create or replace function public.tasks_set_company() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.company is null then
    select company into new.company from public.workers where id = new.worker_id;
  end if;
  return new;
end $$;
create trigger tasks_company_trg before insert on public.tasks
  for each row execute function public.tasks_set_company();

-- ── tenant-scoped admin policies (replace the old admin-all) ─
create or replace function public.admin_can(p_company text) returns boolean
language sql stable as $$
  select public.is_admin()
     and (public.admin_company() is null or p_company = public.admin_company())
$$;

drop policy workers_admin_all on public.workers;
create policy workers_admin_all on public.workers
  for all using (public.admin_can(company)) with check (public.admin_can(company));

drop policy scenarios_admin_all on public.scenarios;
create policy scenarios_admin_all on public.scenarios
  for all using (public.admin_can(company)) with check (public.admin_can(company));

drop policy tasks_admin_all on public.tasks;
create policy tasks_admin_all on public.tasks
  for all using (public.admin_can(company)) with check (public.admin_can(company));

drop policy schedules_admin_all on public.schedules;
create policy schedules_admin_all on public.schedules
  for all using (public.admin_can(company)) with check (public.admin_can(company));

drop policy pairing_admin_all on public.pairing_codes;
create policy pairing_admin_all on public.pairing_codes
  for all using (public.admin_can(company)) with check (public.admin_can(company));

drop policy commands_admin_all on public.commands;
create policy commands_admin_all on public.commands
  for all using (public.is_admin() and exists (
    select 1 from public.workers w
    where w.id = worker_id and (public.admin_company() is null
                                or w.company = public.admin_company())))
  with check (public.is_admin() and exists (
    select 1 from public.workers w
    where w.id = worker_id and (public.admin_company() is null
                                or w.company = public.admin_company())));

drop policy logs_admin_all on public.task_logs;
create policy logs_admin_all on public.task_logs
  for all using (public.is_admin() and exists (
    select 1 from public.tasks t
    where t.id = task_id and (public.admin_company() is null
                              or t.company = public.admin_company())))
  with check (public.is_admin());

drop policy win_tasks_admin_all on public.win_tasks;
create policy win_tasks_admin_all on public.win_tasks
  for all using (public.is_admin() and exists (
    select 1 from public.workers w
    where w.id = worker_id and (public.admin_company() is null
                                or w.company = public.admin_company())))
  with check (public.is_admin());

-- ── company-aware dispatch ──────────────────────────────────
drop function public.dispatch_run_all(text, uuid);
create function public.dispatch_run_all(
  p_source      text default 'manual',
  p_schedule_id uuid default null,
  p_company     text default null
) returns int
language plpgsql security definer set search_path = public as $$
declare
  v_workers uuid[];
  v_batch   uuid := gen_random_uuid();
  v_count   int  := 0;
  v_i       int  := 0;
  sc        record;
begin
  -- security definer bypasses RLS — enforce tenant scope explicitly
  if public.admin_company() is not null
     and p_company is distinct from public.admin_company() then
    raise exception 'not allowed for this company';
  end if;

  select coalesce(array_agg(id), '{}') into v_workers
  from (
    select id from public.workers
    where enabled and last_seen > now() - interval '30 seconds'
      and (p_company is null or company = p_company)
    order by username
  ) w;

  if array_length(v_workers, 1) is null then
    select coalesce(array_agg(id), '{}') into v_workers
    from (select id from public.workers
          where enabled and (p_company is null or company = p_company)
          order by username) w;
  end if;

  if array_length(v_workers, 1) is null then
    return 0;
  end if;

  for sc in select name, path from public.scenarios
            where (p_company is null or company = p_company)
            order by name loop
    insert into public.tasks (scenario_name, scenario_path, worker_id, status,
                              source, batch_id, schedule_id, company)
    values (sc.name, sc.path,
            v_workers[(v_i % array_length(v_workers, 1)) + 1],
            'pending', p_source, v_batch, p_schedule_id, p_company);
    v_i := v_i + 1;
    v_count := v_count + 1;
  end loop;

  return v_count;
end $$;
revoke execute on function public.dispatch_run_all from public, anon;
grant  execute on function public.dispatch_run_all to authenticated;

-- schedules dispatch within their own company
create or replace function public.check_schedules() returns void
language plpgsql security definer set search_path = public as $$
declare
  s        record;
  local_ts timestamp;
  v_n      int;
  v_wid    uuid;
  v_path   text;
begin
  for s in select * from public.schedules where enabled loop
    local_ts := now() at time zone s.timezone;

    if extract(dow from local_ts)::int = any (s.days)
       and to_char(local_ts, 'HH24:MI') = to_char(s.run_time, 'HH24:MI')
       and (s.last_fired_at is null or s.last_fired_at < now() - interval '2 minutes')
    then
      if s.kind = 'scenario' and s.scenario_name is not null then
        v_wid := s.worker_id;
        if v_wid is null then
          select id into v_wid from public.workers
          where enabled and last_seen > now() - interval '30 seconds'
            and (s.company is null or company = s.company)
          order by username limit 1;
        end if;
        if v_wid is not null then
          select path into v_path from public.scenarios
          where name = s.scenario_name
            and (s.company is null or company = s.company);
          insert into public.tasks (scenario_name, scenario_path, worker_id,
                                    status, source, schedule_id, company)
          values (s.scenario_name, v_path, v_wid, 'pending', 'schedule',
                  s.id, s.company);
          v_n := 1;
        else
          v_n := 0;
        end if;
      else
        v_n := public.dispatch_run_all('schedule', s.id, s.company);
      end if;

      update public.schedules
      set last_fired_at = now(), last_count = v_n
      where id = s.id;
    end if;
  end loop;
end $$;

alter publication supabase_realtime add table public.companies;

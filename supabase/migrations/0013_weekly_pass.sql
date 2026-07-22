-- ============================================================
-- Weekly Pass — a built-in maintenance script (firewall.py) that runs
-- on every PC of a company, ONE PC AT A TIME, roughly every N days.
--
--   * company-wide switch: companies.weekly_pass_enabled
--   * a PC becomes "due" when its last Weekly Pass ended more than
--     weekly_pass_interval_days ago
--   * only ONE Weekly Pass task exists company-wide at any moment — the
--     next PC is queued only after the previous one finished, so the pass
--     walks the PCs sequentially
--   * a PC is only picked while it is genuinely idle (online, enabled,
--     status='idle', Keyence not running, no queued/running task) and no
--     schedule fires within ±30 minutes
--   * manual: weekly_pass_run_all() (sequential over all PCs, ignores the
--     interval and the quiet window) or a plain task insert for one PC
--
-- Tasks carry source='weekly_pass'; the worker then runs the bundled
-- script instead of resolving a .rks scenario.
-- ============================================================

alter table public.companies
  add column if not exists weekly_pass_enabled       boolean not null default false,
  add column if not exists weekly_pass_interval_days int     not null default 7,
  add column if not exists weekly_pass_force_started timestamptz;

comment on column public.companies.weekly_pass_force_started is
  'Set by weekly_pass_run_all() — PCs whose last pass predates it are re-run once, interval ignored.';

create index if not exists tasks_weekly_pass_idx
  on public.tasks (company, worker_id, created_at desc)
  where source = 'weekly_pass';

-- ── best-effort text → timestamp (win_tasks.next_run is locale text) ──
create or replace function public.try_ts(p text) returns timestamptz
language plpgsql immutable as $$
begin
  if p is null or btrim(p) = '' then return null; end if;
  return p::timestamptz;
exception when others then
  return null;
end $$;

-- ── is any schedule firing within ±p_mins for this company? ──────────
create or replace function public.schedule_due_soon(
  p_company text,
  p_mins    int default 30
) returns boolean
language plpgsql stable security definer set search_path = public as $$
declare
  s        record;
  local_ts timestamp;
  i        int;
begin
  for s in select * from public.schedules
           where enabled and (company = p_company or company is null) loop
    -- fired very recently → the PCs may still be busy with it
    if s.last_fired_at is not null
       and s.last_fired_at > now() - make_interval(mins => p_mins) then
      return true;
    end if;
    local_ts := now() at time zone s.timezone;
    for i in 0 .. p_mins loop
      if extract(dow from local_ts + make_interval(mins => i))::int = any (s.days)
         and to_char(local_ts + make_interval(mins => i), 'HH24:MI')
             = to_char(s.run_time, 'HH24:MI') then
        return true;
      end if;
    end loop;
  end loop;
  return false;
end $$;

-- ── the sequencer (pg_cron, every 5 minutes) ─────────────────────────
create or replace function public.dispatch_weekly_pass() returns int
language plpgsql security definer set search_path = public as $$
declare
  c        record;
  v_wid    uuid;
  v_force  boolean;
  v_total  int := 0;
begin
  -- A PC that never picked its task up (went offline right after) must not
  -- block the chain forever.
  update public.tasks
  set status = 'failed',
      error = 'Weekly Pass skipped — PC never picked the task up',
      finished_at = now()
  where source = 'weekly_pass'
    and status = 'pending'
    and created_at < now() - interval '2 hours';

  for c in select * from public.companies
           where weekly_pass_enabled
              or (weekly_pass_force_started is not null
                  and weekly_pass_force_started > now() - interval '2 days') loop

    v_force := c.weekly_pass_force_started is not null
           and c.weekly_pass_force_started > now() - interval '2 days';

    -- one at a time, company-wide
    if exists (select 1 from public.tasks
               where company = c.slug and source = 'weekly_pass'
                 and status in ('pending', 'running')) then
      continue;
    end if;

    -- keep out of the way of scheduled work (skipped for a manual run-all)
    if not v_force and public.schedule_due_soon(c.slug, 30) then
      continue;
    end if;

    -- next due PC: idle right now, nothing queued on it, longest overdue first
    select w.id into v_wid
    from public.workers w
    left join lateral (
      select max(coalesce(t.finished_at, t.created_at)) as last_pass
      from public.tasks t
      where t.worker_id = w.id and t.source = 'weekly_pass'
        and t.status in ('success', 'failed', 'stopped')
    ) lp on true
    where w.company = c.slug
      and w.enabled
      and w.status = 'idle'
      and not w.rk_running
      and w.last_seen > now() - interval '60 seconds'
      and not exists (select 1 from public.tasks t2
                      where t2.worker_id = w.id
                        and t2.status in ('pending', 'running'))
      -- a Windows scheduled task of ours about to fire on this PC
      and (v_force or not exists (
            select 1 from public.win_tasks wt
            where wt.worker_id = w.id and wt.is_ours
              and public.try_ts(wt.next_run)
                  between now() and now() + interval '30 minutes'))
      and (
        case when v_force
             then lp.last_pass is null or lp.last_pass < c.weekly_pass_force_started
             else lp.last_pass is null
                  or lp.last_pass < now()
                     - make_interval(days => greatest(c.weekly_pass_interval_days, 1))
        end
      )
    order by lp.last_pass nulls first, w.username
    limit 1;

    if v_wid is null then
      -- force pass finished (nobody left to run) → clear the flag
      if v_force and not exists (
        select 1 from public.workers w
        left join lateral (
          select max(coalesce(t.finished_at, t.created_at)) as last_pass
          from public.tasks t
          where t.worker_id = w.id and t.source = 'weekly_pass'
            and t.status in ('success', 'failed', 'stopped')
        ) lp on true
        where w.company = c.slug and w.enabled
          and (lp.last_pass is null or lp.last_pass < c.weekly_pass_force_started)
      ) then
        update public.companies set weekly_pass_force_started = null
        where slug = c.slug;
      end if;
      continue;
    end if;

    insert into public.tasks (scenario_name, worker_id, status, source, company)
    values ('Weekly Pass', v_wid, 'pending', 'weekly_pass', c.slug);
    v_total := v_total + 1;
  end loop;

  return v_total;
end $$;

revoke execute on function public.dispatch_weekly_pass from public, anon;
grant  execute on function public.dispatch_weekly_pass to authenticated;

-- ── dashboard RPCs ───────────────────────────────────────────────────
create or replace function public.set_weekly_pass(
  p_company  text,
  p_enabled  boolean,
  p_interval int default 7
) returns void
language plpgsql security definer set search_path = public as $$
begin
  if not public.admin_can(p_company) then
    raise exception 'not allowed for this company';
  end if;
  update public.companies
  set weekly_pass_enabled = coalesce(p_enabled, false),
      weekly_pass_interval_days = greatest(coalesce(p_interval, 7), 1)
  where slug = p_company;
end $$;

-- Manual: run the pass on every PC of the company, still one at a time.
create or replace function public.weekly_pass_run_all(p_company text)
returns int
language plpgsql security definer set search_path = public as $$
begin
  if not public.admin_can(p_company) then
    raise exception 'not allowed for this company';
  end if;
  update public.companies set weekly_pass_force_started = now()
  where slug = p_company;
  return public.dispatch_weekly_pass();
end $$;

-- Manual: run it right now on ONE PC (queued like any other task).
create or replace function public.weekly_pass_run_one(p_worker uuid)
returns uuid
language plpgsql security definer set search_path = public as $$
declare v_co text; v_id uuid;
begin
  select company into v_co from public.workers where id = p_worker;
  if not public.admin_can(v_co) then
    raise exception 'not allowed for this company';
  end if;
  insert into public.tasks (scenario_name, worker_id, status, source, company)
  values ('Weekly Pass', p_worker, 'pending', 'weekly_pass', v_co)
  returning id into v_id;
  return v_id;
end $$;

-- Stop a force-run mid-way (drops the PCs that have not run yet).
create or replace function public.weekly_pass_cancel(p_company text)
returns void
language plpgsql security definer set search_path = public as $$
begin
  if not public.admin_can(p_company) then
    raise exception 'not allowed for this company';
  end if;
  update public.companies set weekly_pass_force_started = null
  where slug = p_company;
  update public.tasks
  set status = 'stopped', error = 'Weekly Pass cancelled from dashboard',
      finished_at = now()
  where company = p_company and source = 'weekly_pass' and status = 'pending';
end $$;

revoke execute on function public.set_weekly_pass       from public, anon;
revoke execute on function public.weekly_pass_run_all   from public, anon;
revoke execute on function public.weekly_pass_run_one   from public, anon;
revoke execute on function public.weekly_pass_cancel    from public, anon;
grant  execute on function public.set_weekly_pass       to authenticated;
grant  execute on function public.weekly_pass_run_all   to authenticated;
grant  execute on function public.weekly_pass_run_one   to authenticated;
grant  execute on function public.weekly_pass_cancel    to authenticated;

-- ── cron ─────────────────────────────────────────────────────────────
select cron.schedule('rpa-weekly-pass', '*/5 * * * *',
                     $$select public.dispatch_weekly_pass()$$);

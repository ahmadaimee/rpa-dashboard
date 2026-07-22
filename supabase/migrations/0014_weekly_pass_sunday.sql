-- ============================================================
-- Weekly Pass — fixed weekly cadence instead of a rolling interval.
--
-- Every PC of the company becomes due once per week, at the start of
-- SUNDAY (America/New_York). The sequencer still walks the PCs one at a
-- time and still waits for each PC to be idle with no schedule within
-- ±30 min, so a PC that is busy or offline on Sunday simply gets its turn
-- as soon as it is free — it can never skip a whole week.
-- ============================================================

alter table public.companies drop column if exists weekly_pass_interval_days;

-- Start of the most recent Sunday, in the given timezone.
create or replace function public.last_sunday(p_tz text default 'America/New_York')
returns timestamptz
language sql stable as $$
  select (date_trunc('week', (now() at time zone p_tz) + interval '1 day')
          - interval '1 day') at time zone p_tz
$$;

create or replace function public.dispatch_weekly_pass() returns int
language plpgsql security definer set search_path = public as $$
declare
  c        record;
  v_wid    uuid;
  v_force  boolean;
  v_since  timestamptz := public.last_sunday();
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
             else lp.last_pass is null or lp.last_pass < v_since
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

-- interval argument is gone — the cadence is fixed to Sunday
drop function if exists public.set_weekly_pass(text, boolean, int);

create or replace function public.set_weekly_pass(
  p_company text,
  p_enabled boolean
) returns void
language plpgsql security definer set search_path = public as $$
begin
  if not public.admin_can(p_company) then
    raise exception 'not allowed for this company';
  end if;
  update public.companies
  set weekly_pass_enabled = coalesce(p_enabled, false)
  where slug = p_company;
end $$;

revoke execute on function public.set_weekly_pass from public, anon;
grant  execute on function public.set_weekly_pass to authenticated;

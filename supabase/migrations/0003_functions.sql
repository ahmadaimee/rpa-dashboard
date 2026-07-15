-- ============================================================
-- Server-side dispatch, schedule evaluation, maintenance
-- ============================================================

-- Round-robin every scenario across online+enabled workers.
-- Falls back to all enabled workers if nobody is online (tasks queue up).
-- Replaces the old "first worker claims runAllRequest" race.
create or replace function public.dispatch_run_all(
  p_source      text default 'manual',
  p_schedule_id uuid default null
) returns int
language plpgsql security definer set search_path = public as $$
declare
  v_workers   uuid[];
  v_batch     uuid := gen_random_uuid();
  v_count     int  := 0;
  v_i         int  := 0;
  sc          record;
begin
  select coalesce(array_agg(id), '{}') into v_workers
  from (
    select id from public.workers
    where enabled
      and last_seen is not null
      and last_seen > now() - interval '30 seconds'
    order by username
  ) w;

  if array_length(v_workers, 1) is null then
    select coalesce(array_agg(id), '{}') into v_workers
    from (select id from public.workers where enabled order by username) w;
  end if;

  if array_length(v_workers, 1) is null then
    return 0;
  end if;

  for sc in select name, path from public.scenarios order by name loop
    insert into public.tasks (scenario_name, scenario_path, worker_id, status,
                              source, batch_id, schedule_id)
    values (sc.name, sc.path,
            v_workers[(v_i % array_length(v_workers, 1)) + 1],
            'pending', p_source, v_batch, p_schedule_id);
    v_i := v_i + 1;
    v_count := v_count + 1;
  end loop;

  return v_count;
end $$;

-- Only dashboard users may call it through the API.
revoke execute on function public.dispatch_run_all from public, anon;
grant  execute on function public.dispatch_run_all to authenticated;

-- ── Schedule evaluation (pg_cron, every minute) ─────────────
-- days uses JS convention: 0=Sun .. 6=Sat (extract(dow) matches).
create or replace function public.check_schedules() returns void
language plpgsql security definer set search_path = public as $$
declare
  s        record;
  local_ts timestamp;   -- wall-clock time in the schedule's timezone
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
          order by username limit 1;
        end if;
        if v_wid is not null then
          select path into v_path from public.scenarios where name = s.scenario_name;
          insert into public.tasks (scenario_name, scenario_path, worker_id,
                                    status, source, schedule_id)
          values (s.scenario_name, v_path, v_wid, 'pending', 'schedule', s.id);
          v_n := 1;
        else
          v_n := 0;
        end if;
      else
        v_n := public.dispatch_run_all('schedule', s.id);
      end if;

      update public.schedules
      set last_fired_at = now(), last_count = v_n
      where id = s.id;
    end if;
  end loop;
end $$;

-- ── Stale-task recovery ─────────────────────────────────────
-- Tasks stuck 'running' whose worker stopped heartbeating → failed.
create or replace function public.recover_stale_tasks() returns int
language plpgsql security definer set search_path = public as $$
declare v_n int;
begin
  with dead as (
    update public.tasks t
    set status = 'failed',
        error = 'Worker went offline mid-execution (stale task recovery)',
        finished_at = now()
    from public.workers w
    where t.worker_id = w.id
      and t.status = 'running'
      and t.started_at < now() - interval '10 minutes'
      and (w.last_seen is null or w.last_seen < now() - interval '10 minutes')
    returning t.id
  )
  select count(*) into v_n from dead;
  return v_n;
end $$;

-- ── Log retention ───────────────────────────────────────────
create or replace function public.purge_old_logs() returns void
language sql security definer set search_path = public as $$
  delete from public.task_logs where ts < now() - interval '14 days';
$$;

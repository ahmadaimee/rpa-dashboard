-- ============================================================
-- Cloud schedules: kind='multi' — run a chosen SET of scenarios.
--   scenario_names holds the picked names; they are distributed
--   round-robin across the company's online PCs (or all go to the
--   pinned PC when worker_id is set).
-- ============================================================

alter table public.schedules add column if not exists scenario_names text[];

create or replace function public.check_schedules() returns void
language plpgsql security definer set search_path = public as $$
declare
  s         record;
  local_ts  timestamp;   -- wall-clock time in the schedule's timezone
  v_n       int;
  v_wid     uuid;
  v_path    text;
  v_workers uuid[];
  v_name    text;
  v_i       int;
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

      elsif s.kind = 'multi' then
        v_n := 0;
        if coalesce(array_length(s.scenario_names, 1), 0) > 0 then
          if s.worker_id is not null then
            v_workers := array[s.worker_id];
          else
            select coalesce(array_agg(id), '{}') into v_workers
            from (select id from public.workers
                  where enabled and last_seen > now() - interval '30 seconds'
                    and (s.company is null or company = s.company)
                  order by username) w;
          end if;
          if coalesce(array_length(v_workers, 1), 0) > 0 then
            v_i := 0;
            foreach v_name in array s.scenario_names loop
              select path into v_path from public.scenarios
              where name = v_name
                and (s.company is null or company = s.company);
              insert into public.tasks (scenario_name, scenario_path, worker_id,
                                        status, source, schedule_id, company)
              values (v_name, v_path,
                      v_workers[(v_i % array_length(v_workers, 1)) + 1],
                      'pending', 'schedule', s.id, s.company);
              v_i := v_i + 1;
              v_n := v_n + 1;
            end loop;
          end if;
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

-- ============================================================
-- pg_cron jobs (requires pg_cron extension — enabled in 0001)
-- ============================================================

select cron.schedule('rpa-check-schedules',    '* * * * *',   $$select public.check_schedules()$$);
select cron.schedule('rpa-stale-task-recovery','*/5 * * * *', $$select public.recover_stale_tasks()$$);
select cron.schedule('rpa-purge-logs',         '15 3 * * *',  $$select public.purge_old_logs()$$);

-- Real Keyence activity: rk_running now means "scenario actually executing";
-- rk_open = app merely open; rk_scenario names the running scenario
-- (including runs a user starts directly on the PC).
alter table public.workers add column rk_open boolean not null default false;
alter table public.workers add column rk_scenario text;

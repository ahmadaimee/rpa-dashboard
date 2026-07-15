-- ============================================================
-- Cloud RPA Orchestrator — core schema
-- ============================================================

create extension if not exists pg_cron;

-- ── Workers (one row per registered PC) ─────────────────────
create table public.workers (
  id            uuid primary key default gen_random_uuid(),
  auth_user_id  uuid unique references auth.users(id) on delete set null,
  username      text not null,             -- Windows %USERNAME% (used for {USERNAME} path substitution)
  display_name  text,
  hostname      text,
  status        text not null default 'idle',   -- idle | running | offline
  rk_running    boolean not null default false, -- RkScenarioManager.exe seen in tasklist
  last_seen     timestamptz,
  registered_at timestamptz not null default now(),
  app_version   text,
  enabled       boolean not null default true
);

-- ── Scenario library (reported by folder scans + manual adds) ──
create table public.scenarios (
  id           uuid primary key default gen_random_uuid(),
  name         text not null unique,        -- bare name → <ScenariosFolder>\<name>.rks
  path         text,                        -- optional custom path ({USERNAME} substituted per PC)
  reported_by  uuid references public.workers(id) on delete set null,
  last_seen_at timestamptz,                 -- last time a scan saw this file on disk
  created_at   timestamptz not null default now()
);

-- ── Tasks (one scenario run on one worker) ──────────────────
create table public.tasks (
  id            uuid primary key default gen_random_uuid(),
  scenario_name text not null,
  scenario_path text,                       -- custom path override, else resolved on worker
  worker_id     uuid not null references public.workers(id) on delete cascade,
  status        text not null default 'pending',
    -- pending | running | success | failed | stopped
  source        text not null default 'manual',   -- manual | run_all | schedule
  batch_id      uuid,                       -- groups a run_all dispatch
  schedule_id   uuid,
  resolved_path text,
  exit_code     int,
  error         text,
  created_at    timestamptz not null default now(),
  started_at    timestamptz,
  finished_at   timestamptz
);
create index tasks_worker_status_idx on public.tasks (worker_id, status);
create index tasks_created_idx       on public.tasks (created_at desc);
create index tasks_batch_idx         on public.tasks (batch_id) where batch_id is not null;

-- ── Live task logs (batched inserts from workers) ───────────
create table public.task_logs (
  id      bigint generated always as identity primary key,
  task_id uuid not null references public.tasks(id) on delete cascade,
  seq     int not null,
  line    text not null,
  ts      timestamptz not null default now()
);
create index task_logs_task_idx on public.task_logs (task_id, seq);

-- ── Commands (dashboard → worker) ───────────────────────────
create table public.commands (
  id         uuid primary key default gen_random_uuid(),
  worker_id  uuid not null references public.workers(id) on delete cascade,
  type       text not null,                 -- stop_task | scan | shutdown | restart | update
  payload    jsonb not null default '{}',   -- e.g. {"task_id": "..."}
  status     text not null default 'pending',  -- pending | acked | done | failed
  result     jsonb,
  created_by uuid,                          -- dashboard auth.uid()
  created_at timestamptz not null default now(),
  acked_at   timestamptz,
  finished_at timestamptz
);
create index commands_worker_status_idx on public.commands (worker_id, status);

-- ── Schedules (evaluated by pg_cron every minute) ───────────
create table public.schedules (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  kind          text not null default 'run_all',   -- run_all | scenario
  scenario_name text,                              -- when kind = 'scenario'
  worker_id     uuid references public.workers(id) on delete set null,  -- optional pin
  days          int[] not null,                    -- 0=Sun .. 6=Sat (JS convention, same as old UI)
  run_time      time not null,
  timezone      text not null default 'America/New_York',
  enabled       boolean not null default true,
  last_fired_at timestamptz,
  last_count    int,
  created_at    timestamptz not null default now()
);

-- ── Pairing codes (installer registration) ──────────────────
create table public.pairing_codes (
  code       text primary key,
  created_by uuid,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  used_by    uuid references public.workers(id) on delete set null,
  used_at    timestamptz
);

-- ── Worker releases (optional auto-update, phase 5) ─────────
create table public.worker_releases (
  version      text primary key,
  storage_path text not null,
  sha256       text not null,
  released_at  timestamptz not null default now()
);

-- ── Online view helper ──────────────────────────────────────
create view public.workers_online as
  select w.*, (w.last_seen is not null and w.last_seen > now() - interval '30 seconds') as online
  from public.workers w;
-- Run with the caller's permissions so RLS applies through the view
alter view public.workers_online set (security_invoker = true);

-- ── Realtime publication ────────────────────────────────────
alter publication supabase_realtime add table public.workers;
alter publication supabase_realtime add table public.tasks;
alter publication supabase_realtime add table public.commands;
alter publication supabase_realtime add table public.task_logs;
alter publication supabase_realtime add table public.scenarios;

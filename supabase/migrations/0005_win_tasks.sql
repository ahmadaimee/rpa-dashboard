-- ============================================================
-- Windows Task Scheduler integration
--   win_tasks: snapshot of each worker's Windows scheduled tasks
--   (non-Microsoft), refreshed by the win_sched_list command.
-- ============================================================

create table public.win_tasks (
  id          uuid primary key default gen_random_uuid(),
  worker_id   uuid not null references public.workers(id) on delete cascade,
  task_name   text not null,             -- full path, e.g. \OrchardRPA-Nightly
  next_run    text,
  last_run    text,
  last_result text,
  status      text,
  schedule    text,                      -- human-readable schedule info
  task_to_run text,
  is_ours     boolean not null default false,  -- created via this dashboard
  updated_at  timestamptz not null default now(),
  unique (worker_id, task_name)
);

alter table public.win_tasks enable row level security;

create policy win_tasks_admin_all on public.win_tasks
  for all using (public.is_admin()) with check (public.is_admin());
create policy win_tasks_worker_all on public.win_tasks
  for all using (public.is_worker() and worker_id = public.my_worker_id())
  with check (public.is_worker() and worker_id = public.my_worker_id());

alter publication supabase_realtime add table public.win_tasks;

-- Workers need to insert tasks for themselves (--enqueue from a Windows
-- scheduled task drops a normal cloud task into their own queue).
create policy tasks_worker_insert on public.tasks
  for insert with check (public.is_worker() and worker_id = public.my_worker_id());

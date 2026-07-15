-- ============================================================
-- RLS: two principals distinguished by JWT app_metadata.role
--   worker → created by the register-device edge function
--   admin  → any other authenticated (dashboard) user
-- ============================================================

create or replace function public.is_worker() returns boolean
language sql stable as $$
  select coalesce(auth.jwt() -> 'app_metadata' ->> 'role', '') = 'worker'
$$;

create or replace function public.is_admin() returns boolean
language sql stable as $$
  select auth.role() = 'authenticated' and not public.is_worker()
$$;

create or replace function public.my_worker_id() returns uuid
language sql stable security definer set search_path = public as $$
  select id from public.workers where auth_user_id = auth.uid()
$$;

alter table public.workers         enable row level security;
alter table public.scenarios       enable row level security;
alter table public.tasks           enable row level security;
alter table public.task_logs       enable row level security;
alter table public.commands        enable row level security;
alter table public.schedules       enable row level security;
alter table public.pairing_codes   enable row level security;
alter table public.worker_releases enable row level security;

-- ── workers ─────────────────────────────────────────────────
create policy workers_admin_all on public.workers
  for all using (public.is_admin()) with check (public.is_admin());
create policy workers_self_select on public.workers
  for select using (public.is_worker() and auth_user_id = auth.uid());
create policy workers_self_update on public.workers
  for update using (public.is_worker() and auth_user_id = auth.uid())
  with check (public.is_worker() and auth_user_id = auth.uid());

-- ── scenarios ───────────────────────────────────────────────
create policy scenarios_admin_all on public.scenarios
  for all using (public.is_admin()) with check (public.is_admin());
create policy scenarios_worker_select on public.scenarios
  for select using (public.is_worker());
create policy scenarios_worker_insert on public.scenarios
  for insert with check (public.is_worker());
create policy scenarios_worker_update on public.scenarios
  for update using (public.is_worker()) with check (public.is_worker());

-- ── tasks ───────────────────────────────────────────────────
create policy tasks_admin_all on public.tasks
  for all using (public.is_admin()) with check (public.is_admin());
create policy tasks_worker_select on public.tasks
  for select using (public.is_worker() and worker_id = public.my_worker_id());
create policy tasks_worker_update on public.tasks
  for update using (public.is_worker() and worker_id = public.my_worker_id())
  with check (public.is_worker() and worker_id = public.my_worker_id());

-- ── task_logs ───────────────────────────────────────────────
create policy logs_admin_all on public.task_logs
  for all using (public.is_admin()) with check (public.is_admin());
create policy logs_worker_insert on public.task_logs
  for insert with check (
    public.is_worker() and exists (
      select 1 from public.tasks t
      where t.id = task_id and t.worker_id = public.my_worker_id()
    )
  );
create policy logs_worker_select on public.task_logs
  for select using (
    public.is_worker() and exists (
      select 1 from public.tasks t
      where t.id = task_id and t.worker_id = public.my_worker_id()
    )
  );

-- ── commands ────────────────────────────────────────────────
create policy commands_admin_all on public.commands
  for all using (public.is_admin()) with check (public.is_admin());
create policy commands_worker_select on public.commands
  for select using (public.is_worker() and worker_id = public.my_worker_id());
create policy commands_worker_update on public.commands
  for update using (public.is_worker() and worker_id = public.my_worker_id())
  with check (public.is_worker() and worker_id = public.my_worker_id());

-- ── schedules: admin only (pg_cron runs as superuser) ───────
create policy schedules_admin_all on public.schedules
  for all using (public.is_admin()) with check (public.is_admin());

-- ── pairing_codes: admin only (edge fn uses service role) ───
create policy pairing_admin_all on public.pairing_codes
  for all using (public.is_admin()) with check (public.is_admin());

-- ── worker_releases ─────────────────────────────────────────
create policy releases_admin_all on public.worker_releases
  for all using (public.is_admin()) with check (public.is_admin());
create policy releases_worker_select on public.worker_releases
  for select using (public.is_worker());

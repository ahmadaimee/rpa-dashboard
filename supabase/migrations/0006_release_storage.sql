-- ============================================================
-- Auto-update: private storage bucket for worker releases.
-- Admins upload from the dashboard; workers download to self-update.
-- (worker_releases metadata table already exists from 0001.)
-- ============================================================

insert into storage.buckets (id, name, public)
values ('worker-releases', 'worker-releases', false)
on conflict (id) do nothing;

create policy releases_read on storage.objects for select
  using (bucket_id = 'worker-releases' and (public.is_admin() or public.is_worker()));
create policy releases_admin_insert on storage.objects for insert
  with check (bucket_id = 'worker-releases' and public.is_admin());
create policy releases_admin_update on storage.objects for update
  using (bucket_id = 'worker-releases' and public.is_admin());
create policy releases_admin_delete on storage.objects for delete
  using (bucket_id = 'worker-releases' and public.is_admin());

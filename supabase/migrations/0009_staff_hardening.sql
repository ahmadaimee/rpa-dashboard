-- Staff admins must not publish worker releases (global software) —
-- restrict release writes and the storage bucket to super admins.

drop policy releases_admin_all on public.worker_releases;
create policy releases_admin_read on public.worker_releases
  for select using (public.is_admin());
create policy releases_super_write on public.worker_releases
  for all using (public.is_admin() and public.admin_company() is null)
  with check (public.is_admin() and public.admin_company() is null);

drop policy releases_admin_insert on storage.objects;
create policy releases_admin_insert on storage.objects for insert
  with check (bucket_id = 'worker-releases' and public.is_admin()
              and public.admin_company() is null);
drop policy releases_admin_update on storage.objects;
create policy releases_admin_update on storage.objects for update
  using (bucket_id = 'worker-releases' and public.is_admin()
         and public.admin_company() is null);
drop policy releases_admin_delete on storage.objects;
create policy releases_admin_delete on storage.objects for delete
  using (bucket_id = 'worker-releases' and public.is_admin()
         and public.admin_company() is null);

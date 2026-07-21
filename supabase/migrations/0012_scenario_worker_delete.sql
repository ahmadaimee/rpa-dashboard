-- ============================================================
-- Workers may delete scenario rows: the folder scan removes
-- library entries for .rks files that now live only inside an
-- Archive / Archived / Archives subfolder.
-- ============================================================
create policy scenarios_worker_delete on public.scenarios
  for delete using (public.is_worker());

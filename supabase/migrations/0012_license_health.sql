-- Keyence license health reported by workers (v1.8.8+).
-- Run this BEFORE publishing the worker release — heartbeats from a new
-- worker fail if these columns are missing.
alter table public.workers
  add column if not exists license_status text,          -- ok | warning | error
  add column if not exists license_last_verified timestamptz,
  add column if not exists license_error text;

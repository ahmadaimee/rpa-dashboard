# Orchard RPA Orchestrator — Cloud Edition

Fully-online orchestration for Keyence RK-10 RPA across all org PCs.
No admin-PC server, no LAN dependency: workers and the dashboard both talk to Supabase.

```
Supabase (Postgres + Realtime + Auth + pg_cron + register-device edge function)
   ▲ worker exe (polls/heartbeats)              ▲ supabase-js
Worker PCs (OrchardRPAWorker.exe,       Dashboard (static site on
 .rks scenarios via OneDrive sync)       Netlify/Vercel, email login)
```

## One-time cloud setup (Phase 0)

1. **Create a Supabase project** at https://supabase.com (free tier is fine).
2. **Auth settings** → disable public signups. Add admin users manually
   (Authentication → Users → Add user) with email + password.
3. **Run the migrations** in order in the SQL Editor (or `supabase db push` with the CLI):
   - `supabase/migrations/0001_schema.sql`
   - `supabase/migrations/0002_rls.sql`
   - `supabase/migrations/0003_functions.sql`
   - `supabase/migrations/0004_cron.sql`  *(enable the `pg_cron` extension first: Database → Extensions)*
4. **Deploy the edge function**:
   ```
   supabase functions deploy register-device --project-ref <your-ref>
   ```
   (Uses the service role key automatically; no extra secrets needed.)

## Dashboard (Phase 2)

1. Edit `dashboard/config.js` with your project URL + anon key
   (Project Settings → API).
2. Deploy the `dashboard/` folder as a static site (Netlify drop, Vercel,
   or any static host). For a quick local test: `python -m http.server 8080`
   inside `dashboard/` and open http://localhost:8080.
3. Sign in with an admin user created in step 2 above.

## Worker (Phases 1 & 4)

1. Edit `worker/embedded.py` — paste your Supabase URL + anon key.
2. Build the exe (needs Python 3.11+ on the build machine only):
   ```powershell
   cd worker
   .\build.ps1              # console build — use while testing
   .\build.ps1 -NoConsole   # hidden build for rollout
   ```
3. On each worker PC:
   - Dashboard → Settings → **Generate Pairing Code**
   - Copy `dist\OrchardRPAWorker.exe` to the PC and double-click it
   - Enter the pairing code → done. The worker installs a Task Scheduler
     logon task, runs hidden, and appears online in the dashboard.
4. Remove from a PC: `OrchardRPAWorker.exe --uninstall`, then Remove in
   dashboard Settings (revokes access).

Worker files live in `%LOCALAPPDATA%\OrchardRPA\` (config.json, worker.log).

### Dev run without building
```
cd worker
pip install -r requirements.txt
python worker.py                # interactive install
python worker.py --background   # run the loop in this console
```

### Testing without Keyence
Point `rk_exe` in `%LOCALAPPDATA%\OrchardRPA\config.json` at a stub batch
file that sleeps and exits 0/1, and `scenarios_folder` at a folder with
dummy `.rks` files.

## How it works

| Concern | Mechanism |
|---|---|
| Run one scenario | dashboard inserts a `tasks` row → worker claims it (atomic `pending→running` update) |
| Run all | `dispatch_run_all()` SQL RPC round-robins scenarios across online workers |
| Stop | `commands` row (`stop_task`) → worker sends CTRL+ALT+P + terminate + taskkill |
| Status | success/failed from `RkScenarioManager.exe` exit code (0 = success) |
| Live view | heartbeat every 10 s (`last_seen`, `rk_running`); online = seen < 30 s ago |
| Logs | stdout batched (15 lines / 3 s, cap 500) into `task_logs`; 14-day retention |
| Schedules | pg_cron runs `check_schedules()` every minute — fires with dashboards closed |
| Stale tasks | pg_cron marks `running` tasks `failed` if their worker stopped heartbeating >10 min |
| Security | Supabase Auth: admins = email/password; workers = per-device auth user (`app_metadata.role='worker'`) minted by the `register-device` edge function against a one-time pairing code. RLS confines each worker to its own rows. |

## Rollout from the old system

The old LAN system (`RPA-Orchestrator/`) and this one don't conflict.
Pilot one PC on the cloud worker, verify, then migrate PCs one at a time
(`python worker.py --uninstall` removes the old scheduled task).

// register-device — pairs a new worker PC with the org.
// POST { code, username, hostname, display_name, app_version }
// → { worker_id, email, password }
// The installer calls this with the anon key; we validate the pairing
// code and mint a dedicated auth user (app_metadata.role = 'worker').

import { createClient } from "npm:@supabase/supabase-js@2";

const admin = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
);

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type, apikey, x-client-info",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  let body: Record<string, string>;
  try {
    body = await req.json();
  } catch {
    return json({ error: "Invalid JSON" }, 400);
  }

  const code = (body.code ?? "").trim().toUpperCase();
  const username = (body.username ?? "").trim();
  if (!code || !username) return json({ error: "code and username are required" }, 400);

  // Validate pairing code
  const { data: pc, error: pcErr } = await admin
    .from("pairing_codes")
    .select("*")
    .eq("code", code)
    .maybeSingle();
  if (pcErr) return json({ error: pcErr.message }, 500);
  if (!pc) return json({ error: "Invalid pairing code" }, 403);
  if (pc.used_at) return json({ error: "Pairing code already used" }, 403);
  if (new Date(pc.expires_at) < new Date()) return json({ error: "Pairing code expired" }, 403);

  // Mint the worker auth user
  const password = crypto.randomUUID() + crypto.randomUUID();
  const email = `worker-${crypto.randomUUID()}@workers.internal`;
  const { data: user, error: userErr } = await admin.auth.admin.createUser({
    email,
    password,
    email_confirm: true,
    app_metadata: { role: "worker" },
  });
  if (userErr || !user?.user) return json({ error: userErr?.message ?? "createUser failed" }, 500);

  // Register the worker row
  const { data: worker, error: wErr } = await admin
    .from("workers")
    .insert({
      auth_user_id: user.user.id,
      username,
      display_name: body.display_name || username,
      hostname: body.hostname || null,
      app_version: body.app_version || null,
      last_seen: new Date().toISOString(),
    })
    .select("id")
    .single();
  if (wErr) {
    await admin.auth.admin.deleteUser(user.user.id);
    return json({ error: wErr.message }, 500);
  }

  // Burn the code
  await admin
    .from("pairing_codes")
    .update({ used_by: worker.id, used_at: new Date().toISOString() })
    .eq("code", code);

  return json({ worker_id: worker.id, email, password });
});

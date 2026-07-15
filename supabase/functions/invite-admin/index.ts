// invite-admin — the super admin creates a company-scoped staff login.
// POST { email, company } with the caller's user JWT.
// Only a super admin (authenticated, no company, not a worker) may call.
// Returns { email, password } — a temp password shown once in the dashboard.

import { createClient } from "npm:@supabase/supabase-js@2";

const service = createClient(
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

  const jwt = (req.headers.get("Authorization") ?? "").replace("Bearer ", "");
  const { data: caller } = await service.auth.getUser(jwt);
  const meta = caller?.user?.app_metadata ?? {};
  if (!caller?.user || meta.role === "worker" || meta.company) {
    return json({ error: "Only the super admin can create staff logins" }, 403);
  }

  let body: Record<string, string>;
  try {
    body = await req.json();
  } catch {
    return json({ error: "Invalid JSON" }, 400);
  }
  const email = (body.email ?? "").trim().toLowerCase();
  const company = (body.company ?? "").trim();
  const superAdmin = body.super === true || body.super === "true";
  if (!email) return json({ error: "email is required" }, 400);
  if (!company && !superAdmin) return json({ error: "company is required" }, 400);

  if (!superAdmin) {
    const { data: co } = await service.from("companies")
      .select("slug").eq("slug", company).maybeSingle();
    if (!co) return json({ error: `Unknown company: ${company}` }, 400);
  }

  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789";
  const password = Array.from(crypto.getRandomValues(new Uint8Array(14)),
    (b) => alphabet[b % alphabet.length]).join("");

  const { error } = await service.auth.admin.createUser({
    email,
    password,
    email_confirm: true,
    app_metadata: superAdmin ? {} : { company },
  });
  if (error) return json({ error: error.message }, 500);

  return json({ email, password, company: superAdmin ? null : company });
});

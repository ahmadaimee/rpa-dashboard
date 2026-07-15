"""
Baked into the .exe at build time so the installer only asks for a
pairing code. Fill these in before running build.ps1 (or set the
ORCHARD_SUPABASE_URL / ORCHARD_SUPABASE_ANON_KEY env vars, which win).
The anon key is safe to embed — RLS is the security boundary.
"""
import os

SUPABASE_URL      = os.environ.get("ORCHARD_SUPABASE_URL",      "https://YOUR-PROJECT.supabase.co")
SUPABASE_ANON_KEY = os.environ.get("ORCHARD_SUPABASE_ANON_KEY", "YOUR-ANON-KEY")

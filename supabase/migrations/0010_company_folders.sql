-- Per-company default scenarios folder ({USERNAME} is substituted on each
-- worker PC). Workers read their company's folder for default scans and
-- bare-name scenario resolution.
alter table public.companies add column scenarios_folder text;

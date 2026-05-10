-- Product research persistence for Clip Creator.
-- Run this in the Supabase SQL editor for the project used by this app.

create extension if not exists "pgcrypto";

insert into storage.buckets (id, name, public)
values ('product-context', 'product-context', false)
on conflict (id) do nothing;

create table if not exists public.product_research_projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  product_name text not null,
  product_url text,
  audience text,
  product_context text,
  plan jsonb not null default '{}'::jsonb,
  sources jsonb not null default '[]'::jsonb,
  uploads jsonb not null default '[]'::jsonb,
  model text,
  usage jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.product_research_files (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references public.product_research_projects(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  bucket text not null default 'product-context',
  storage_path text not null,
  file_name text not null,
  content_type text,
  file_size bigint,
  extracted_text text,
  created_at timestamptz not null default now()
);

create index if not exists idx_product_research_projects_user_created
  on public.product_research_projects(user_id, created_at desc);

create index if not exists idx_product_research_files_project
  on public.product_research_files(project_id);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists set_product_research_projects_updated_at on public.product_research_projects;
create trigger set_product_research_projects_updated_at
  before update on public.product_research_projects
  for each row
  execute function public.set_updated_at();

alter table public.product_research_projects enable row level security;
alter table public.product_research_files enable row level security;

drop policy if exists "Users can view their product research projects" on public.product_research_projects;
create policy "Users can view their product research projects"
  on public.product_research_projects for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert their product research projects" on public.product_research_projects;
create policy "Users can insert their product research projects"
  on public.product_research_projects for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can update their product research projects" on public.product_research_projects;
create policy "Users can update their product research projects"
  on public.product_research_projects for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "Users can delete their product research projects" on public.product_research_projects;
create policy "Users can delete their product research projects"
  on public.product_research_projects for delete
  using (auth.uid() = user_id);

drop policy if exists "Users can view their product research files" on public.product_research_files;
create policy "Users can view their product research files"
  on public.product_research_files for select
  using (auth.uid() = user_id);

drop policy if exists "Users can insert their product research files" on public.product_research_files;
create policy "Users can insert their product research files"
  on public.product_research_files for insert
  with check (auth.uid() = user_id);

drop policy if exists "Users can delete their product research files" on public.product_research_files;
create policy "Users can delete their product research files"
  on public.product_research_files for delete
  using (auth.uid() = user_id);

drop policy if exists "Users can read their product context files" on storage.objects;
create policy "Users can read their product context files"
  on storage.objects for select
  using (
    bucket_id = 'product-context'
    and auth.uid()::text = (storage.foldername(name))[1]
  );

drop policy if exists "Users can upload their product context files" on storage.objects;
create policy "Users can upload their product context files"
  on storage.objects for insert
  with check (
    bucket_id = 'product-context'
    and auth.uid()::text = (storage.foldername(name))[1]
  );

drop policy if exists "Users can update their product context files" on storage.objects;
create policy "Users can update their product context files"
  on storage.objects for update
  using (
    bucket_id = 'product-context'
    and auth.uid()::text = (storage.foldername(name))[1]
  )
  with check (
    bucket_id = 'product-context'
    and auth.uid()::text = (storage.foldername(name))[1]
  );

drop policy if exists "Users can delete their product context files" on storage.objects;
create policy "Users can delete their product context files"
  on storage.objects for delete
  using (
    bucket_id = 'product-context'
    and auth.uid()::text = (storage.foldername(name))[1]
  );

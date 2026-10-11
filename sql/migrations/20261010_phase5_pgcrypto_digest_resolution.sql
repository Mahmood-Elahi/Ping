-- Phase 5 forward repair: pgcrypto is installed in Supabase's extensions
-- schema, while the SECURITY DEFINER finalizer intentionally uses a fixed
-- search path. Keep the function body, signature, owner, and grants unchanged.

begin;

alter function public.finalize_student_job_source_sync(uuid)
    set search_path to public, extensions, pg_temp;

commit;

-- Apply only after the staged-source-sync application code is deployed.
-- Keeping this separate makes the production rollout backward compatible:
-- the new RPCs can be installed before old runners lose their entry point.

drop function if exists public.sync_student_job_source(
    uuid, text, jsonb, boolean, integer, timestamptz
);

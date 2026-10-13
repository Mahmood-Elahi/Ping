# Codex instructions

Ping is the user-facing product name and uses the tagline **“New roles. Less
noise.”** Keep stable technical identifiers unchanged unless a task explicitly
authorizes a compatibility migration: Python/package/module names, database
objects, environment variables, workflow identifiers, user agents, artifact
names, and the GitHub repository may still contain `role-radar` or `job-radar`.

Before changing this repository:

1. Run `git status --short` and preserve all unrelated or uncommitted work.
2. Read `README.md` plus the relevant authoritative document:
   - `docs/ARCHITECTURE.md`
   - `docs/SOURCE_CONTRACTS.md`
   - `docs/OPERATIONS.md`
   - `docs/ROADMAP.md`
3. Treat `config/target_companies.yaml` as production. Candidate, canary,
   rollback, proposed, historical, and validated fixtures are not production
   inputs unless the loader explicitly says otherwise.

Mandatory source rules:

- Fetch success is not scan completeness; bounds are never terminal evidence.
- Preserve exact employer identity, source scope, stable job identity,
  inventory accounting, mutation detection, and central finalization.
- Failed, incomplete, capped, discovery-only, manual, unsupported, or
  cooldown-skipped runs have no absence authority.
- Keep the two-complete-absence lifecycle rule and exact-only cross-source
  equivalence. Registry removal is not retirement.
- `PASS_COMPLETE` is the only generally activation-eligible canary result.
  Never activate from deterministic fixtures alone.
- Every source replacement needs an exact rollback record. Do not weaken a
  contract to make a source pass.

Do not run live scans, full-company scans, provider probes, canaries, production
audits, database migrations, Supabase/Sheets mutations, retention execution,
scope retirement, or GitHub workflows without explicit task authorization.
Prefer offline validators and focused deterministic fixtures. Never commit,
push, merge, deploy, or change branches unless explicitly requested.

For documentation-only work, verify relative links, stale references, branding
exceptions, registry counts, and `git diff --check`; do not use a live scan as
documentation validation. Preserve recovery configs, fixtures, evidence,
archives, migrations, and historical validation records.

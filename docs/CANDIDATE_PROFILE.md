# Candidate Profile v1

Candidate Profile v1 is an optional deterministic preference layer for the student-job workflow. It runs after student eligibility and role relevance, and before final ranking.

The default profile is [`config/candidate_profile.yaml`](../config/candidate_profile.yaml). It targets internships starting in Summer 2027 (preferably May), favors 4-month terms without rejecting longer terms, gives Canada a modest preference, allows US and international roles, and scores seven broad technical domain clusters. Work arrangement and industry have no positive weight.

## Separation of concerns

- **Role Relevance** asks whether the posting is actually a relevant student role. Its score remains available as `role_relevance_score`.
- **Candidate Profile** asks whether that relevant role aligns with configured term, technical-domain, duration, and geography preferences. It does not assess whether the candidate already has the required skills.
- **Resume Match** is not part of v1. Qualification and resume evidence belong to a future, separate layer.

When enabled, final ranking is a bounded blend of role relevance and `candidate_preference_score`. An explicit non-target start term receives an additional deterministic penalty. Domain matching counts each configured cluster once and caps the cross-domain bonus, so repeated keywords cannot inflate the result.

Defense is a hard exclusion. The profile contains an explicit direct-contractor list, decisive weapons/military-system patterns, and corroborated defense-context patterns. A single generic company-overview or EEO reference is not enough to exclude a role.

Missing start date, duration, location detail, or work arrangement is treated as unknown rather than rejected. International roles are retained by the profile-enabled student workflow and annotated when work authorization would be required.

## Output fields

Profile-enabled ranked JSON and CSV include:

- `role_relevance_score` and `role_relevance_band`
- `candidate_preference_score`
- `ranking_score` (also exposed through the existing `score` / `relevance_score` fields)
- `term_match` and `term_evidence`
- `duration_months` and `duration_match`
- `geography_match` and `work_arrangement`
- `matched_domain_clusters`, `domain_match_score`, and `multi_domain_bonus`
- `candidate_preference_reasons`
- `international_work_authorization_required`

## Run and verify

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_candidate_profile
.\.venv\Scripts\python.exe scripts\replay_candidate_profile.py
.\.venv\Scripts\python.exe scripts\run_student_job_scan.py --dry-run
```

The student scan and dashboard use `config/candidate_profile.yaml` by default. Supply another profile with `--candidate-profile PATH`, or verify legacy behavior with `--no-candidate-profile`.

The replay uses the frozen 327-job artifact at `jobs/company_careers_ranked_20260829_164545.json` and writes a machine-readable comparison plus a concise Markdown report under `output/`.

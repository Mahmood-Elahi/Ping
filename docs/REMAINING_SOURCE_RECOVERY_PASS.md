# 1. GOAL

Close out SGS and ServiceNow from operator-provided `PASS_COMPLETE` evidence.
ServiceNow is the sole newly recovered member of the original 37-source queue;
the other 36 sources remain unchanged. No live provider work is part of this
closeout.

# 2. INFORMATION

The repository started at `b2303253aa5a57731c16cb4b45d2dd49b5225b4d`
(`b230325 Complete pagination completeness recovery batch 4`). The starting
worktree contained only the protected artifacts listed in item 2 below.

The machine-readable evidence, all 37 source rows, status counts, provider
gates, request derivations, and parked-source list are in
`config/remaining_source_recovery_pass.yaml`.

# 3. IMPLEMENTATION

SGS production still exactly matches the live-tested Batch 4 candidate.
ServiceNow production now exactly matches the isolated live-tested
SmartRecruiters candidate, which is retained as historical validation evidence.
No adapter logic changed. Focused tests prove the live 697-row shape completes
in seven listing requests and fails closed on overflow, mutation, duplicate
IDs, and identity mismatch.

# 4. REPORT

1. **Current HEAD:** `b2303253aa5a57731c16cb4b45d2dd49b5225b4d`
   (`b230325`). No branch or HEAD operation was performed.

2. **Git status at start:** modified protected
   `docs/COMPANY_CAREERS_RECOVERY_BATCH9.md`; untracked protected
   `batch9_extended_results.zip`, `batch9_probe_results.zip`,
   `batch9_xml_results.zip`, and `config/job_radar_run_history.json`.

3. **SGS closeout:** operator evidence is recorded in
   `config/pagination_completeness_recovery_batch4.yaml`. Production did not
   match before closeout, so only the exact tested row was promoted. It now
   uses `^SGS$`, 100 rows/page, 50 pages, 5,000 jobs, 3,000,000 bytes, and no
   detail enrichment. Status is `RECOVERED`, `LIVE_VALIDATED`,
   `PASS_COMPLETE`. The 4,646 total leaves 354 jobs of configured capacity.

4. **ServiceNow closeout:** `RECOVERED`, `LIVE_VALIDATED`, `PASS_COMPLETE`.
   The authoritative total was 697; all 697 jobs were collected in seven of
   eight allowed requests. The 800-job ceiling leaves 103 jobs of headroom.

5. **Remaining unresolved after ServiceNow closeout:** 36. The original queue
   immediately before ServiceNow live validation contained 37 sources.

6. **Exact starting classification:** 18 `NEEDS_SOURCE_SPECIFIC_REPAIR`, 11
   `INSUFFICIENT_EVIDENCE`, seven `FAIL_IDENTITY`, and one `FAIL_CONTRACT`,
   exactly as enumerated in `config/remaining_source_recovery_pass.yaml`.

7. **Deep investigations:** ServiceNow; all seven identity sources; Meta,
   Qualcomm, Rivian, Goldman Sachs, Micron, and GlobalFoundries. SGS was also
   deeply checked for closeout, outside the 37.

8. **Stopped early:** the other 23 sources, because the repository contained
   no new authority beyond their exact prior blocker. The exact list is in the
   evidence YAML.

9. **Shared repairs implemented:** none. The existing generic
   SmartRecruiters inventory-only repair was sufficient.

10. **ServiceNow:** the existing shared SmartRecruiters inventory-only repair
    was reused; SGS already used the same repair and is separately
    live-validated. The historical ServiceNow attempt had an authoritative total and no
    observed mutation, but per-job hydration exhausted its 52-request budget
    without completing inventory. The Bosch/SGS listing-only
    pattern applies because listings carry exact company identity, numeric ID,
    title, location, release date, department/function, and exact company-scoped
    detail reference; canonical/apply URLs are deterministically constructed
    and validated. The successful inventory-only run used
    `selective_detail_enrichment: false`, collected 697/697 jobs in seven
    listing requests, and returned `PASS_COMPLETE`. Final status: `RECOVERED`.

11. **Identity track:** Samsung Electronics (`workday`, same Workday lead), GE
    Vernova (`workday`, same board plus Phase 4 subdivision), and Capital One
    (`workday`, same board) still lack exact runtime employer/site identity.
    General Motors (`static_html` -> `generalmotors/careers_gm`), Caterpillar
    (`static_html` -> `cat/CaterpillarCareers`), and Zalando (`static_html` ->
    `zalando/ZalandoSiteWD`) still lack exact first-party binding and production
    scope equivalence; GM subsidiary scope and Progress Rail separation remain
    explicit. Cameco's static source remains terminal-unproven and its Jobvite
    lead remains unbound. All seven remain `FAIL_IDENTITY`.

12. **First-party inventory track:** Meta, Qualcomm, and Rivian retain their
    known sitemap leads but gained no active-only and complete-index authority.
    Goldman Sachs retains `GetRoles`, but no replay fixture/public stability or
    exact EARLY_CAREER/PROFESSIONAL union proof appeared. Micron and
    GlobalFoundries retain their Workday leads without first-party tenant/scope
    binding. No candidate was created; all six remain
    `INSUFFICIENT_EVIDENCE`.

13. **Oracle track:** JPMorgan Chase, Emerson, Honeywell, Oracle, and Fortinet
    remain `NEEDS_SOURCE_SPECIFIC_REPAIR`. No new authoritative finite facet
    universe or proof that all jobs belong to it exists.

14. **Eightfold track:** Microsoft, Eaton, and HP Inc. retain mutable PCSx
    traversal; HP's 16/911 retained shape remains especially incomplete. No
    immutable partition or equivalent first-party inventory appeared. All
    remain `NEEDS_SOURCE_SPECIFIC_REPAIR`.

15. **SuccessFactors track:** CPKC, Scotiabank, Bombardier, CN Rail, Seagate,
    and Kinross Gold retain mutation/repeated-page contradictions. No exact
    active-only complete sitemap/RSS contract appeared. All remain
    `NEEDS_SOURCE_SPECIFIC_REPAIR`.

16. **Remaining source-specific track:** Atlassian retains its canonical URL
    contract failure; Stantec lacks a scope-local terminal; Espressif Systems,
    ecobee, and Athabasca Oil lack a terminal; Nordic Semiconductor lacks a
    retained exact target response; AstraZeneca has mutation plus unvalidated
    identity; Nutrien lacks exact SuccessFactors identity/scope/terminal; and
    Coupang repeats page one. Their statuses remain as shown in item 28.

17. **jobseek code reused/adapted:** none in this pass. Previously reviewed
    mechanics informed rejection gates only.

18. **freehire code reused/adapted:** none in this pass. Previously reviewed
    sitemap mechanics did not supply source authority.

19. **JobSpy mechanics used:** none.

20. **Sibling approaches rejected:** Oracle partitioning lacks an exhaustive
    facet universe; sitemap/RSS leads lack active-only completeness; Workday
    leads lack exact binding/scope equivalence; Eightfold watermarks cannot
    authorize absence; third-party aggregation is not authority.

21. **New adapters/config variants/registries:** no adapter; the isolated
    registry, `config/target_companies.remaining_source_recovery_canaries.yaml`,
    is retained as historical live-validation evidence; the evidence record is
    `config/remaining_source_recovery_pass.yaml`.

22. **Production config changes:** SGS and ServiceNow only across the two
    operator-evidence closeouts. ServiceNow's old `static_html` row was replaced
    only by the exact tested SmartRecruiters candidate.

23. **Inventory accounting:** SGS records
    `4646 = 4646 + 0 + 0 + 0`; ServiceNow records
    `697 = 697 + 0 + 0 + 0`. Any unexplained difference blocks
    `PASS_COMPLETE`.

24. **Terminal evidence:** no adapter change. SGS and ServiceNow record
    operator-validated authoritative totals. Bounds are never terminals.

25. **Mutation safety:** unchanged and tested. Moving totals and overlap fail
    closed after at most one bounded restart.

26. **Identity validation:** SGS production narrowed to `^SGS$`. ServiceNow
    uses exact slug, endpoint path, full-match company name, numeric ID,
    company-scoped reference, and sole apply host.

27. **Validated request accounting:** ServiceNow used
    `ceil(697 / 100) = 7` listing requests from its budget of eight. No detail
    requests were made. A future authoritative total above 800 must fail
    incomplete rather than truncate or authorize absence.

28. **Final original-37-source table:** one recovered row plus the unchanged
    36-source unresolved queue.

| Company | Current platform | Primary blocker | Best known alternate source | New evidence this pass | Final status | Future canary required |
|---|---|---|---|---|---|---:|
| Atlassian | atlassian | canonical URL contract | none | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| JPMorgan Chase | oracle_recruiting | mutable offsets; no facet universe | Oracle partitions | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Emerson | oracle_recruiting | mutable offsets; no facet universe | Oracle partitions | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Honeywell | oracle_recruiting | mutable offsets; no facet universe | Oracle partitions | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Microsoft | eightfold | mutable PCSx | none proven | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Eaton | eightfold | mutable PCSx | none proven | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Oracle | oracle_recruiting | mutable offsets; no facet universe | Oracle partitions | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Stantec | browser_rendered_html | no scope-local terminal | rejected global sitemap | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| CPKC | sap_successfactors | mutation | unproved sitemap/RSS | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Scotiabank | sap_successfactors | mutation | unproved sitemap/RSS | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Bombardier | sap_successfactors | repeated page | unproved sitemap/RSS | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Fortinet | oracle_recruiting | mutable offsets; no facet universe | Oracle partitions | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| ServiceNow | smartrecruiters | resolved detail-heavy request budgeting | activated SmartRecruiters `ServiceNow` | operator live 697/697 in seven listing requests | RECOVERED | No |
| CN Rail | sap_successfactors | mutation despite reaching total | unproved sitemap/RSS | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| HP Inc. | eightfold | mutable 16/911 traversal | none proven | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Seagate | sap_successfactors | repeated page at 97/284 | unproved sitemap/RSS | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| AstraZeneca | workday | mutation and identity | Phase 4 subdivision | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Kinross Gold | sap_successfactors | mutation despite reaching total | unproved sitemap/RSS | none | NEEDS_SOURCE_SPECIFIC_REPAIR | No |
| Meta | meta | sitemap activity/completeness | Meta sitemap | none | INSUFFICIENT_EVIDENCE | No |
| Qualcomm | eightfold | sitemap activity/job semantics | Qualcomm sitemap | none | INSUFFICIENT_EVIDENCE | No |
| Micron | eightfold | Workday binding/scope | Micron Workday | none | INSUFFICIENT_EVIDENCE | No |
| Rivian | icims | sitemap activity/scope/ID parity | Rivian sitemap | none | INSUFFICIENT_EVIDENCE | No |
| Goldman Sachs | browser_rendered_html | GraphQL union/public replay | GetRoles GraphQL | none | INSUFFICIENT_EVIDENCE | No |
| GlobalFoundries | eightfold | Workday binding/scope | GlobalFoundries Workday | none | INSUFFICIENT_EVIDENCE | No |
| Espressif Systems | static_html | no terminal | none | none | INSUFFICIENT_EVIDENCE | No |
| ecobee | static_html | no terminal | none | none | INSUFFICIENT_EVIDENCE | No |
| Nordic Semiconductor | teamtailor | no exact retained target terminal | same Teamtailor source | generic mechanic only | INSUFFICIENT_EVIDENCE | No |
| Athabasca Oil | static_html | positive cards only | none | none | INSUFFICIENT_EVIDENCE | No |
| Nutrien | browser_rendered_html | endpoint/scope/identity/terminal | SuccessFactors RMK | none | INSUFFICIENT_EVIDENCE | No |
| Samsung Electronics | workday | exact identity absent | same Workday board | none | FAIL_IDENTITY | No |
| GE Vernova | workday | identity absent; over direct cap | Workday subdivision | none | FAIL_IDENTITY | No |
| General Motors | static_html | parent/subsidiary scope | GM Workday | none | FAIL_IDENTITY | No |
| Caterpillar | static_html | parent/Progress Rail scope | Caterpillar Workday | none | FAIL_IDENTITY | No |
| Zalando | static_html | board identity/scope | Zalando Workday | none | FAIL_IDENTITY | No |
| Capital One | workday | exact identity absent | same Workday board | none | FAIL_IDENTITY | No |
| Cameco | static_html | terminal and alternate binding | rejected Jobvite | none | FAIL_IDENTITY | No |
| Coupang | static_html | page parameter repeats page one | unproved alternate boards | none | FAIL_CONTRACT | No |

29. `RECOVERED`: **1**.

30. `READY_FOR_CANARY`: **0**.

31. `RECOVERED_BY_SHARED_FIX`: **0**.

32. `NEEDS_SOURCE_SPECIFIC_REPAIR`: **17**.

33. `INSUFFICIENT_EVIDENCE`: **11**.

34. `FAIL_IDENTITY`: **7**.

35. `FAIL_CONTRACT`: **1**.

36. `NO_SAFE_REPAIR / other`: **0**.

37. **Exact newly recovered list:** ServiceNow. The ready-for-canary list is
    empty.

38. **Live action:** none during this closeout. The operator-supplied canary
    evidence was used as authoritative input and ServiceNow was not rerun.

39. **PASS_COMPLETE requirements:** exact provider-family gates for
    SmartRecruiters, sitemap, GraphQL union, Workday, Oracle, SuccessFactors,
    and static/browser sources are recorded under `report.pass_complete_requirements`
    in the evidence YAML. ServiceNow satisfied every SmartRecruiters gate.

40. **Rollback:** SGS rollback is restoring only its prior 20-row/one-page
    production row if release review rejects that evidence closeout. ServiceNow
    rollback is restoring only its prior static production row; the isolated
    candidate remains historical evidence. No database rollback exists because
    nothing was persisted.

41. **Focused tests:** 36 passed, four subtests passed for the new recovery,
    Batch 4, inventory/hydration, and SmartRecruiters contract set. The three
    stale SGS historical assertions then passed as 21 tests/45 subtests after
    closeout updates. Source-integrity/terminal/retention grouping passed as
    135 tests/56 subtests.

42. **Batch 1-4 regressions:** 36 passed.

43. **Phase 3-7 regressions:** 229 passed.

44. **Retention tests:** 26 passed.

45. **Full pytest:** 2,557 passed, 909 subtests passed, one unrelated
    LangChain deprecation warning; zero failures.

46. **`git diff --check`:** passed. Git emitted only existing line-ending
    normalization warnings for two test files.

47. **Changed/new files:** 23 pass-owned files: five existing config files,
    two new config files, two existing docs, one new report, 12 existing
    tests, and one new focused test. The exact list is in the handoff response.

48. **Final git status:** reported verbatim in the handoff response.

49. **Safe to stage later:** only the implementation/evidence/test files listed
    as this pass's changes in the handoff response.

50. **Must remain unstaged:** `docs/COMPANY_CAREERS_RECOVERY_BATCH9.md`, the
    three `batch9_*_results.zip` files, `config/job_radar_run_history.json`, and
    any temporary `remaining37_audit.txt` or `remaining37_history.txt` files.

51. **Prohibited actions confirmation:** no live source scan, live canary,
    provider/company network request, browser/proxy request, Supabase mutation,
    migration execution, GitHub Action, commit, push, merge, checkout, reset,
    rebase, or branch change occurred.

52. **Updated unresolved production-source count:** 36. ServiceNow is recovered
    from operator-provided `PASS_COMPLETE` evidence.

53. **Best next manual action:** none is prescribed by this closeout.

54. **Parked sources:** the other 36 sources in item 28. Resume one only when
    new first-party provider identity, scope, active-only, terminal, or finite
    partition evidence appears.

55. **Broad prompt decision:** another broad recovery prompt is not justified.
    Move the parked queue to individual or provider-family maintenance driven
    by genuinely new evidence.

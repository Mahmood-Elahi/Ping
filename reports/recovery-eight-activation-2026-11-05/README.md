# Eight successful recoveries — activation proposal

Ready for review against production commit `f873fb54572df4fc2ebe1a5a85c8dc49c801b3e5`. No activation, new collection or production data write has been performed.

| Company | Retained PASS_COMPLETE jobs | Physical requests |
| --- | ---: | ---: |
| ENMAX | 7 | 1 |
| Rockwell Automation | 441 | 23 |
| Thomson Reuters | 496 | 25 |
| Canadian Tire | 128 | 7 |
| EXFO | 78 | 4 |
| Ontario Teachers' Pension Plan | 34 | 2 |
| RBC | 816 | 41 |
| Target Tech | 110 | 6 |
| Total | 2,110 | 109 |

The production registry changes only these eight rows: exact reviewed Workday identity bindings, plus board-base search URLs for ENMAX, EXFO and Ontario Teachers'. Production preprocessing produces the exact tested targets. Tenants, boards, facets, scopes, job identity algorithms and effective bounds are unchanged. The other 525 rows and all reliability overlays remain unchanged.

Collection, mutation, cooldown and completion code matches the canary engine. Relevant model and central finalizer ASTs match. The two-complete-absence lifecycle rule and exact-only equivalence remain unchanged. The five incomplete companies are excluded.

`evidence.json.gz.b64` contains retained certificate fields, original evidence hashes, independently checked identity-accounting digests and exact rollback records. Raw provider payloads and individual job identities remain in the private local evidence directories; they are not published here. The offline validator checks bundle integrity, complete-source acceptance, exact production materialization and engine applicability. These are historical certificates, not claims of current live freshness.

The older ten-source validator first validates this package, restores the exact predecessor registry in memory and verifies its bytes against the original immutable lock. No historical certificate or lock was regenerated.

Validation: `tests/test_recovery_eight_activation.py` and `tests/test_successfactors_production_configuration.py`, with network blocking. See `validation.txt` for the final result. The package validator imports no persistence clients and makes no provider requests.

After separate approval: merge/deploy this focused change through the normal production process. Do not dispatch workflows or rescan as part of PR preparation. If rollback is required, restore only the affected company's exact raw row from the bundle's rollback record; preserve all jobs and existing scope history. Registry rollback/removal is not retirement and must not close jobs.

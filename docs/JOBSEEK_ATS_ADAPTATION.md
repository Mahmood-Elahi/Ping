# Jobseek SmartRecruiters adaptation

Source review: colophon-group/jobseek commit
`ac3a4e434183fd7bdac6d6b83912371388521e4b`,
`apps/crawler/go/smartrecruiters-monitor/inventory.go` and `detail.go`, and
the corresponding Python monitor/scraper. Upstream is MIT licensed, copyright
2025 Viktor Shcherbakov. The experiment's upstream license copy and evidence
are retained in the enclosing pilot evaluation workspace.

The adaptation keeps Ping's Python runtime and its existing identity,
inventory/hydration, mutation restart, scope, finalization and lifecycle
interfaces. Before retaining a page, its entire raw cardinality must fit the
stable advertised total. A result limit cannot conceal surplus rows. A changed
total still takes the existing bounded snapshot restart path first.

Hydrated descriptions retain all section bodies, including unknown sections,
while preserving Ping's existing normalized plain-text shape. Provider section
titles are not inserted because they would change material content and
description-based matching without adding body content. Optional employment
type, experience level, language and an unambiguous workplace classification
remain in `raw_metadata`; the job schema and provider-native publication
identity remain stable. SmartRecruiters documents remote, hybrid and onsite as
distinct Posting API location types, so contradictory `remote=true` and
`hybrid=true` flags do not receive a guessed canonical type. Missing
descriptions stay missing. Hydration is not enabled by this change.

Provider references:

- https://developers.smartrecruiters.com/reference/v1listpostings
- https://developers.smartrecruiters.com/changelog/jobs-api-posting-api-support-hybrid

`tests/test_jobseek_smartrecruiters_adaptation.py` covers raw overshoot, tail
accounting, mutation contraction, metadata, descriptions, malformed and failed
responses, bounds, empty collections, and actual adapter → scanner → lifecycle
behavior. Two failed contradictory scans cannot advance absence. Normal
complete absences still use the existing two-observation rule.

No company definition is added or activated. ASML1 returned 17 publications
and empty sampled detail sections in the bounded pilot. This is board-level
enumeration evidence, not proof of complete current ASML hiring coverage.

## Upstream license notice

MIT License

Copyright (c) 2025 Viktor Shcherbakov

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

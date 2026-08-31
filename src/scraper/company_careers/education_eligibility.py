"""Deterministic undergraduate eligibility classification for student roles."""

from __future__ import annotations

import html
import re
from typing import Any, Iterable, Mapping

from src.scraper.company_careers.models import JobRecord


UNDERGRADUATE_ALLOWED = "undergraduate_allowed"
GRADUATE_ONLY = "graduate_only"
AMBIGUOUS = "ambiguous"
NOT_STATED = "not_stated"

_EDUCATION_KEYS = {
    "education", "educationlevel", "educationrequirements", "minimumeducation",
    "minimumeducationlevel", "degree", "degreelevel", "degreerequirements",
}
_QUALIFICATION_KEYS = {
    "basicqualifications", "candidateprofile", "eligibility", "minimumqualifications",
    "preferredqualifications", "qualifications", "requiredqualifications", "requirements",
}
_DESCRIPTION_KEYS = {"description", "fulldescription", "jobdescription", "jobdetails"}

_UNDERGRADUATE = (
    r"(?:undergraduate(?:s|\s+students?|\s+candidates?)?|"
    r"bachelor(?:['\u2019]s|s)?(?:\s+degree|\s+students?|\s+candidates?)?|"
    r"b\.?\s?s\.?\s+(?:degree|students?|candidates?)|"
    r"b\.?\s?a\.?\s+(?:degree|students?|candidates?)|"
    r"bsc(?:\s+degree|\s+students?|\s+candidates?)?)"
)
_MASTERS = (
    r"(?:\bmaster(?:['\u2019]s|s)(?:\s+(?:degree|program|students?))?\b|"
    r"\bmaster(?:\s+degree|\s+of\s+[A-Za-z]+)\b|"
    r"\bm\.?\s?s\.?(?:\s+(?:degree|program|students?))\b)"
)
_PHD = r"(?:\bph\.?\s?d\.?\b|\bdoctor(?:al|ate)(?:\s+degree|\s+students?)?\b)"
_MBA = r"(?<![A-Za-z])m\.?\s?b\.?\s?a\.?(?![A-Za-z])"
_JD = r"(?<![A-Za-z])j\.?\s?d\.?(?![A-Za-z])"
_MD = r"(?<![A-Za-z])m\.?\s?d\.?(?![A-Za-z])"
_PROFESSIONAL = rf"(?:{_MBA}|{_JD}|{_MD})"
_GRADUATE = (
    rf"(?:{_MASTERS}|{_PHD}|{_PROFESSIONAL}|graduate\s+(?:degree|school|program|students?|candidates?)|"
    r"post\s*[- ]?doctor(?:al|ate)|postdocs?|graduate[- ]level|graduate\s+fellowship|medical\s+residen(?:cy|t))"
)
_ELIGIBILITY_CONTEXT = (
    r"(?:accept|allow|candidate|eligible|enroll|minimum|must|"
    r"plus|prefer|program|pursu|qualif|require|student|study)"
)
_PHD_ONLY_TITLE = (
    rf"{_PHD}\s+(?:candidate\s+)?(?:intern(?:ship)?|student\s+researcher|research\s+intern)\b"
)


def classify_education_eligibility(job: JobRecord) -> dict[str, str]:
    """Classify degree-level eligibility using sentence- and clause-local evidence."""
    undergraduate: list[str] = []
    graduate_only: list[tuple[str, str]] = []
    ambiguous: list[str] = []

    for source_kind, sentence, clause in _evidence_units(job):
        normalized = _normalize_text(clause)
        if not normalized:
            continue
        has_undergraduate = bool(re.search(_UNDERGRADUATE, normalized, re.IGNORECASE))
        has_graduate = bool(re.search(_GRADUATE, normalized, re.IGNORECASE))
        if _allows_undergraduates(normalized, source_kind, has_undergraduate):
            undergraduate.append(sentence)
        if _removes_graduate_requirement(normalized):
            undergraduate.append(sentence)
        reason = _graduate_only_reason(normalized, source_kind, has_graduate)
        if reason:
            graduate_only.append((sentence, reason))
        elif has_graduate and _is_ambiguous_eligibility_mention(normalized, source_kind):
            ambiguous.append(sentence)

    undergraduate = _unique(undergraduate)
    graduate_only = _unique_pairs(graduate_only)
    ambiguous = _unique(ambiguous)
    if undergraduate and graduate_only:
        unavailable = [item for item in graduate_only if item[1] == "undergraduates_explicitly_unavailable"]
        if unavailable:
            return _result(AMBIGUOUS, unavailable[0][0])
        return _result(UNDERGRADUATE_ALLOWED, undergraduate[0])
    if undergraduate:
        return _result(UNDERGRADUATE_ALLOWED, undergraduate[0])
    if graduate_only:
        return _result(GRADUATE_ONLY, graduate_only[0][0], exclusion_reason="graduate_only")
    if ambiguous:
        return _result(AMBIGUOUS, ambiguous[0])
    return _result(NOT_STATED, "")


def annotate_education_eligibility(job: JobRecord) -> dict[str, str]:
    """Attach normalized education metadata without changing the storage schema."""
    classification = classify_education_eligibility(job)
    job.raw_metadata["education_eligibility"] = classification["education_eligibility"]
    job.raw_metadata["education_evidence"] = classification["education_evidence"]
    if classification["education_eligibility"] == GRADUATE_ONLY:
        job.raw_metadata["exclusion_reason"] = "graduate_only"
    elif job.raw_metadata.get("exclusion_reason") == "graduate_only":
        job.raw_metadata.pop("exclusion_reason")
    return classification


def _allows_undergraduates(text: str, source_kind: str, has_undergraduate: bool) -> bool:
    if not has_undergraduate:
        return False
    if re.search(
        rf"(?:not\s+(?:available|open|eligible)|ineligible|unavailable)[^.;:]{{0,60}}{_UNDERGRADUATE}",
        text, re.IGNORECASE,
    ):
        return False
    if source_kind == "title":
        return True
    if source_kind in {"education", "qualification"}:
        return not re.search(rf"\b(?:not|ineligible|unavailable)\b[^.;:]*{_UNDERGRADUATE}", text, re.IGNORECASE)
    if re.search(rf"^graduat(?:e|ing)\s+from[^.;:]{{0,80}}{_UNDERGRADUATE}", text, re.IGNORECASE):
        return True
    return bool(re.search(
        rf"(?:{_UNDERGRADUATE}[^.;:]{{0,100}}(?:accept|allow|candidates?|eligible|enroll|may\s+apply|minimum|must|pursu|qualif|require|students?)|"
        rf"(?:accept|allow|eligible|enroll|look(?:ing)?|minimum|must|pursu|qualif|require|seek(?:ing)?)[^.;:]{{0,140}}{_UNDERGRADUATE})",
        text, re.IGNORECASE,
    ))


def _removes_graduate_requirement(text: str) -> bool:
    return bool(re.search(
        rf"(?:not\s+required|do(?:es)?\s+not\s+(?:need|require)|no\s+requirement)[^.;:]{{0,100}}{_GRADUATE}|"
        rf"{_GRADUATE}[^.;:]{{0,80}}(?:not\s+required|optional)",
        text, re.IGNORECASE,
    ))


def _graduate_only_reason(text: str, source_kind: str, has_graduate: bool) -> str | None:
    if re.search(
        rf"(?:not\s+(?:available|open|eligible)|ineligible|unavailable)\s+(?:to|for)\s+{_UNDERGRADUATE}|"
        rf"{_UNDERGRADUATE}[^.;:]{{0,60}}(?:may\s+not|cannot|can't|not\s+eligible)",
        text, re.IGNORECASE,
    ):
        return "undergraduates_explicitly_unavailable"
    if not has_graduate or _is_negated(text):
        return None
    if re.search(r"\b(?:preferred|preference|nice\s+to\s+have|desired|a\s+plus)\b", text, re.IGNORECASE) and not re.search(
        r"\b(?:current(?:ly)?|enroll(?:ed|ment)?|mandatory|must|only|required?)\b",
        text, re.IGNORECASE,
    ):
        return None
    if re.search(rf"(?:{_GRADUATE}[^.;:]{{0,45}}\bonly\b|\bonly\b[^.;:]{{0,45}}{_GRADUATE})", text, re.IGNORECASE):
        return "graduate_students_only"
    if re.search(rf"\bcurrent(?:ly)?\b[^.;:]{{0,30}}{_GRADUATE}", text, re.IGNORECASE):
        return "graduate_enrollment_required"
    if re.search(
        rf"(?:currently\s+)?(?:enrolled|pursuing|studying)[^.;:]{{0,80}}{_GRADUATE}|"
        rf"{_GRADUATE}[^.;:]{{0,80}}(?:enrollment|currently\s+enrolled)",
        text, re.IGNORECASE,
    ):
        return "graduate_enrollment_required"
    if re.search(
        rf"(?:minimum\s+(?:education|degree)|minimum\s+education\s+is)[^.;:]{{0,80}}{_GRADUATE}|"
        rf"(?:requires?|required|must\s+(?:have|hold|be)|needs?)\s*:?[^.;:]{{0,80}}{_GRADUATE}|"
        rf"{_GRADUATE}[^.;:]{{0,60}}(?:required|mandatory|minimum|must\s+have|is\s+required)",
        text, re.IGNORECASE,
    ):
        return "graduate_degree_required"
    if re.search(
        rf"expected\s+(?:to\s+)?graduat(?:e|ion)[^.;:]{{0,120}}(?:from|in|with)[^.;:]{{0,50}}{_GRADUATE}",
        text, re.IGNORECASE,
    ):
        return "graduate_program_graduation_required"
    if re.search(r"\b(?:graduate\s+fellowship|post\s*[- ]?doctor(?:al|ate)|postdocs?|medical\s+residen(?:cy|t))\b", text, re.IGNORECASE):
        return "graduate_program_type"
    if re.search(
        rf"{_MBA}[^.;:]{{0,50}}(?:leadership\s+development\s+program|internship|fellowship)",
        text, re.IGNORECASE,
    ):
        return "graduate_program_type"
    if source_kind == "education":
        return "graduate_education_field"
    if source_kind == "title":
        parenthetical_phd_program = (
            bool(re.search(rf"\(\s*{_PHD}\s*\)", text, re.IGNORECASE))
            and bool(re.search(r"\b(?:intern(?:ship)?|students?|researchers?)\b", text, re.IGNORECASE))
        )
        if parenthetical_phd_program or re.search(_PHD_ONLY_TITLE, text, re.IGNORECASE):
            return "graduate_only_title"
        academic_graduate = rf"(?:{_MASTERS}|graduate\s+(?:students?|candidates?|fellowship)|post\s*[- ]?doctor(?:al|ate)|postdocs?)"
        if re.search(
            rf"{academic_graduate}[^.;:]{{0,60}}(?:intern(?:ship)?|fellow(?:ship)?|student|co\s*[-/]?\s*op)|"
            rf"(?:intern(?:ship)?|fellow(?:ship)?|student)[^.;:]{{0,60}}{academic_graduate}",
            text, re.IGNORECASE,
        ):
            return "graduate_only_title"
        if re.search(rf"{_MBA}[^.;:]{{0,50}}(?:intern(?:ship)?|fellow(?:ship)?|program)", text, re.IGNORECASE):
            return "graduate_only_title"
        if re.search(rf"{_JD}\s+(?:candidate\s+)?(?:intern|legal|law|student|fellow)", text, re.IGNORECASE):
            return "graduate_only_title"
        if re.search(rf"{_MD}\s+(?:candidate\s+)?(?:intern|clinical|medical|student|resident|fellow)", text, re.IGNORECASE):
            return "graduate_only_title"
    return None


def _is_negated(text: str) -> bool:
    return bool(re.search(
        r"\b(?:not|isn't|is\s+not|aren't|are\s+not|no)\b[^.;:]{0,50}"
        r"\b(?:required|mandatory|must|need|enrolled|enrollment)\b",
        text, re.IGNORECASE,
    ))


def _is_ambiguous_eligibility_mention(text: str, source_kind: str) -> bool:
    if source_kind in {"education", "qualification"}:
        return True
    if source_kind == "title":
        if re.search(rf"(?:{_MASTERS}|{_PHD}|{_MBA}|graduate\s+(?:degree|school|program|students?|candidates?)|postdocs?)", text, re.IGNORECASE):
            return True
        return bool(re.search(
            rf"(?:{_JD}[^.;:]{{0,30}}(?:degree|legal|law|student|candidate)|"
            rf"{_MD}[^.;:]{{0,30}}(?:degree|clinical|medical|student|candidate|resident))",
            text, re.IGNORECASE,
        ))
    return bool(re.search(_ELIGIBILITY_CONTEXT, text, re.IGNORECASE))


def _evidence_units(job: JobRecord) -> Iterable[tuple[str, str, str]]:
    yield "title", job.title, job.title
    seen = {("title", _normalize_text(job.title))}
    sources: list[tuple[str, str]] = []
    if job.description:
        sources.append(("description", job.description))
    for path, value in _metadata_items(job.raw_metadata):
        key = _normalize_key(re.sub(r"\[\d+\]$", "", path.rsplit(".", 1)[-1]))
        values = (
            [str(item) for item in value if isinstance(item, (str, int, float))]
            if isinstance(value, (list, tuple))
            else [str(value)] if isinstance(value, (str, int, float)) else []
        )
        if not values:
            continue
        if key in _EDUCATION_KEYS:
            sources.extend(("education", item) for item in values)
        elif key in _QUALIFICATION_KEYS:
            sources.extend(("qualification", item) for item in values)
        elif key in _DESCRIPTION_KEYS:
            sources.extend(("description", item) for item in values)
    for source_kind, value in sources:
        for sentence in _sentences(value):
            marker = (source_kind, _normalize_text(sentence))
            if marker in seen:
                continue
            seen.add(marker)
            clauses = [part.strip() for part in re.split(r"\s*;\s*", sentence) if part.strip()]
            for clause in clauses or [sentence]:
                yield source_kind, sentence, clause


def _sentences(value: str) -> list[str]:
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?i)<\s*br\s*/?\s*>|<\s*/\s*(?:p|li|div|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text).replace("\r", "\n")
    parts = re.split(r"(?:\n+|(?<=[.!?])\s+(?=[A-Z0-9]))", text)
    return [re.sub(r"^[\s\-\u2022*]+|\s+$", "", re.sub(r"\s+", " ", part)) for part in parts if part.strip()]


def _metadata_items(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, item
            yield from _metadata_items(item, path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _metadata_items(item, f"{prefix}[{index}]")


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _result(eligibility: str, evidence: str, *, exclusion_reason: str = "") -> dict[str, str]:
    result = {"education_eligibility": eligibility, "education_evidence": evidence}
    if exclusion_reason:
        result["exclusion_reason"] = exclusion_reason
    return result


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _unique_pairs(values: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    return list(dict.fromkeys(values))

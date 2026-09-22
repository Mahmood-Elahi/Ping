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

_BACHELORS = r"\bbachelor(?:['\u2019]s|s)?(?:\s+(?:degree|program|students?|candidates?))?\b"
_BS = r"(?<![A-Za-z])b\.?\s?s\.?(?![A-Za-z])"
_BA = r"(?<![A-Za-z])b\.?\s?a\.?(?![A-Za-z])"
_BSC = r"(?<![A-Za-z])b\.?\s?sc\.?(?![A-Za-z])"
_UNDERGRADUATE = (
    rf"(?:\bundergraduate(?:s|\s+students?|\s+candidates?)?\b|{_BACHELORS}|"
    rf"{_BS}|{_BA}|{_BSC})"
)
_UNDERGRADUATE_STANDING = r"\b(?:juniors?|seniors?|third[- ]year|fourth[- ]year)\b"
_MASTERS = (
    r"(?:\bmaster(?:['\u2019]s|s)(?:\s+(?:degree|program|students?))?\b|"
    r"\bmaster(?:\s+degree|\s+of\s+[A-Za-z]+)\b|"
    r"(?<![A-Za-z])m\.?\s?s\.?(?![A-Za-z]))"
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
_STRONG_UNDERGRADUATE_REASONS = {
    "inclusive_degree_levels",
    "undergraduate_academic_standing",
    "undergraduate_enrollment_allowed",
    "undergraduates_explicitly_eligible",
    "graduate_requirement_not_required",
}


def classify_education_eligibility(job: JobRecord) -> dict[str, str]:
    """Classify degree-level eligibility using sentence- and clause-local evidence."""
    undergraduate: list[tuple[str, str]] = []
    graduate_only: list[tuple[str, str]] = []
    ambiguous: list[tuple[str, str]] = []

    for source_kind, sentence, clause in _evidence_units(job):
        normalized = _normalize_text(clause)
        sentence_context = _normalize_text(sentence)
        if not normalized:
            continue
        has_undergraduate = bool(re.search(_UNDERGRADUATE, normalized, re.IGNORECASE))
        has_graduate = bool(re.search(_GRADUATE, normalized, re.IGNORECASE))
        undergraduate_reason = _undergraduate_allowed_reason(
            normalized,
            sentence_context,
            source_kind,
            has_undergraduate,
            has_graduate,
        )
        if undergraduate_reason:
            undergraduate.append((sentence, undergraduate_reason))
        if _removes_graduate_requirement(normalized):
            undergraduate.append((sentence, "graduate_requirement_not_required"))
        reason = _graduate_only_reason(
            normalized,
            sentence_context,
            source_kind,
            has_graduate,
        )
        if reason:
            graduate_only.append((sentence, reason))
        elif has_graduate:
            ambiguous_reason = _ambiguous_eligibility_reason(
                normalized,
                sentence_context,
                source_kind,
            )
            if ambiguous_reason:
                ambiguous.append((sentence, ambiguous_reason))

    undergraduate = _unique_pairs(undergraduate)
    graduate_only = _unique_pairs(graduate_only)
    ambiguous = _unique_pairs(ambiguous)
    if undergraduate and graduate_only:
        unavailable = [item for item in graduate_only if item[1] == "undergraduates_explicitly_unavailable"]
        if unavailable:
            return _result(AMBIGUOUS, unavailable[0][0], "contradictory_explicit_eligibility")
        decisive_undergraduate = next(
            (item for item in undergraduate if item[1] in _STRONG_UNDERGRADUATE_REASONS),
            None,
        )
        if decisive_undergraduate:
            return _result(UNDERGRADUATE_ALLOWED, *decisive_undergraduate)
        return _result(GRADUATE_ONLY, *graduate_only[0], exclusion_reason="graduate_only")
    if undergraduate:
        return _result(UNDERGRADUATE_ALLOWED, *undergraduate[0])
    if graduate_only:
        return _result(GRADUATE_ONLY, *graduate_only[0], exclusion_reason="graduate_only")
    if ambiguous:
        return _result(AMBIGUOUS, *ambiguous[0])
    return _result(NOT_STATED, "", "no_graduate_restriction_detected")


def annotate_education_eligibility(job: JobRecord) -> dict[str, str]:
    """Attach normalized education metadata without changing the storage schema."""
    classification = classify_education_eligibility(job)
    job.raw_metadata["education_eligibility"] = classification["education_eligibility"]
    job.raw_metadata["education_evidence"] = classification["education_evidence"]
    job.raw_metadata["education_reason"] = classification["education_reason"]
    if classification["education_eligibility"] == GRADUATE_ONLY:
        job.raw_metadata["exclusion_reason"] = "graduate_only"
    elif job.raw_metadata.get("exclusion_reason") == "graduate_only":
        job.raw_metadata.pop("exclusion_reason")
    return classification


def _undergraduate_allowed_reason(
    text: str,
    sentence_context: str,
    source_kind: str,
    has_undergraduate: bool,
    has_graduate: bool,
) -> str | None:
    if re.search(
        rf"(?:not\s+(?:available|open|eligible)|ineligible|unavailable)[^.;:]{{0,60}}{_UNDERGRADUATE}",
        text, re.IGNORECASE,
    ):
        return None
    if re.search(_UNDERGRADUATE_STANDING, text, re.IGNORECASE):
        return "undergraduate_academic_standing"
    if not has_undergraduate:
        return None
    if re.search(
        rf"\b(?:completed|earned|obtained|holds?|holding|already\s+has|have)\b[^.;:]{{0,45}}{_UNDERGRADUATE}",
        sentence_context,
        re.IGNORECASE,
    ):
        return None
    if _is_preference_only(sentence_context):
        return None
    if has_graduate and _has_inclusive_degree_list(text):
        return "inclusive_degree_levels"
    if re.search(rf"^graduat(?:e|ing)\s+from[^.;:]{{0,80}}{_UNDERGRADUATE}", text, re.IGNORECASE):
        return "undergraduate_enrollment_allowed"
    if re.search(
        rf"(?:{_UNDERGRADUATE}[^.;:]{{0,100}}(?:accept(?:ed)?|allow(?:ed)?|candidates?|eligible|may\s+apply|students?)|"
        rf"(?:accept(?:ed)?|allow(?:ed)?|eligible|open\s+to)[^.;:]{{0,140}}{_UNDERGRADUATE})",
        text, re.IGNORECASE,
    ):
        return "undergraduates_explicitly_eligible"
    if re.search(
        rf"(?:currently\s+)?(?:enrolled|pursuing|studying(?:\s+towards?)?)[^.;:]{{0,100}}{_UNDERGRADUATE}|"
        rf"{_UNDERGRADUATE}[^.;:]{{0,80}}(?:students?|candidates?)",
        text,
        re.IGNORECASE,
    ):
        return "undergraduate_enrollment_allowed"
    if source_kind == "title":
        return "undergraduate_enrollment_allowed"
    if re.search(
        rf"(?:minimum|requires?|required|must\s+(?:have|hold)|qualifications?)\s*:?[^.;:]{{0,100}}{_UNDERGRADUATE}|"
        rf"{_UNDERGRADUATE}[^.;:]{{0,60}}(?:required|minimum|qualifications?)",
        text,
        re.IGNORECASE,
    ):
        return "bachelors_degree_sufficient"
    if source_kind in {"education", "qualification"}:
        return "bachelors_degree_sufficient"
    return None


def _has_inclusive_degree_list(text: str) -> bool:
    undergraduate_matches = list(re.finditer(_UNDERGRADUATE, text, re.IGNORECASE))
    graduate_matches = list(re.finditer(_GRADUATE, text, re.IGNORECASE))
    for undergraduate in undergraduate_matches:
        for graduate in graduate_matches:
            left, right = sorted((undergraduate, graduate), key=lambda match: match.start())
            between = text[left.end():right.start()]
            if len(between) > 50:
                continue
            if re.search(
                r"\b(?:completed|earned|obtained|preferred|required|currently|pursuing|enrolled|"
                r"must|followed\s+by|in\s+progress)\b",
                between,
                re.IGNORECASE,
            ):
                continue
            if re.search(r"(?:[,/]|\b(?:or|and)\b)", between, re.IGNORECASE):
                return True
    return False


def _is_preference_only(text: str) -> bool:
    return bool(
        re.search(r"\b(?:preferred|preference|nice\s+to\s+have|desired|a\s+plus)\b", text, re.IGNORECASE)
        and not re.search(
            r"\b(?:accept(?:ed)?|allow(?:ed)?|eligible|enrolled|minimum|mandatory|must|"
            r"pursuing|required?)\b",
            text,
            re.IGNORECASE,
        )
    )


def _graduate_mentions_are_preferred(text: str) -> bool:
    preferred_graduate = re.search(
        rf"(?:{_GRADUATE}[^.;:]{{0,35}}(?:preferred|preference|desired|a\s+plus)|"
        rf"(?:preferred|preference|desired)[^.;:]{{0,35}}{_GRADUATE})",
        text,
        re.IGNORECASE,
    )
    graduate_requirement = re.search(
        rf"(?:{_GRADUATE}[^.;:]{{0,35}}(?:required|mandatory|must)|"
        rf"(?:currently\s+)?(?:enrolled|pursuing|studying)[^.;:]{{0,60}}{_GRADUATE})",
        text,
        re.IGNORECASE,
    )
    return bool(preferred_graduate and not graduate_requirement)


def _removes_graduate_requirement(text: str) -> bool:
    return bool(re.search(
        rf"(?:not\s+required|do(?:es)?\s+not\s+(?:need|require)|no\s+requirement)[^.;:]{{0,100}}{_GRADUATE}|"
        rf"{_GRADUATE}[^.;:]{{0,80}}(?:not\s+required|optional)",
        text, re.IGNORECASE,
    ))


def _has_nonexclusive_graduate_eligibility(text: str) -> bool:
    if re.search(r"\bonly\b", text, re.IGNORECASE):
        return False
    return bool(re.search(
        rf"(?:{_GRADUATE}[^.;:]{{0,55}}\balso\s+(?:are\s+)?eligible\b|"
        rf"\balso\s+eligible\b[^.;:]{{0,55}}{_GRADUATE})",
        text,
        re.IGNORECASE,
    ))


def _has_broadening_exception(text: str) -> bool:
    return bool(re.search(
        r"\b(?:(?:exceptional\s+)?candidates?\s+from\s+other\s+backgrounds?\s+"
        r"(?:are\s+)?(?:welcome|encouraged\s+to\s+apply)|"
        r"(?:we\s+)?welcome\s+(?:exceptional\s+)?candidates?\s+from\s+other\s+backgrounds?)\b",
        text,
        re.IGNORECASE,
    ))


def _has_generic_student_alternative(text: str) -> bool:
    graduate_alternative = re.compile(
        r"(?:post\s*[- ]?doctor(?:al|ate)(?:\s+fellows?)?|postdocs?|recent\s+graduates?)",
        re.IGNORECASE,
    )
    for student in re.finditer(r"\bstudents?\b", text, re.IGNORECASE):
        prefix = text[max(0, student.start() - 45):student.start()]
        if re.search(
            rf"(?:{_UNDERGRADUATE}|{_MASTERS}|{_PHD}|{_PROFESSIONAL}|\bgraduate)\s*$",
            prefix,
            re.IGNORECASE,
        ):
            continue
        suffix = text[student.end():student.end() + 100]
        if re.search(r"(?:\bor\b|\band\b|,)", suffix, re.IGNORECASE) and graduate_alternative.search(suffix):
            return True
    return False


def _graduate_only_reason(
    text: str,
    sentence_context: str,
    source_kind: str,
    has_graduate: bool,
) -> str | None:
    if re.search(
        rf"(?:not\s+(?:available|open|eligible)|ineligible|unavailable)\s+(?:to|for)\s+{_UNDERGRADUATE}|"
        rf"{_UNDERGRADUATE}[^.;:]{{0,60}}(?:may\s+not|cannot|can't|not\s+eligible)",
        text, re.IGNORECASE,
    ):
        return "undergraduates_explicitly_unavailable"
    if not has_graduate or _is_negated(text):
        return None
    if _has_broadening_exception(sentence_context):
        return None
    if _is_preference_only(text) or _graduate_mentions_are_preferred(text):
        return None
    if re.search(rf"(?:{_GRADUATE}[^.;:]{{0,45}}\bonly\b|\bonly\b[^.;:]{{0,45}}{_GRADUATE})", text, re.IGNORECASE):
        return "graduate_students_only"
    if re.search(rf"\bcurrent(?:ly)?\b[^.;:]{{0,30}}{_GRADUATE}", text, re.IGNORECASE):
        return "graduate_enrollment_required"
    if re.search(
        rf"(?:currently\s+)?(?:enrolled|pursuing|studying)[^.;:]{{0,80}}{_GRADUATE}|"
        rf"{_GRADUATE}[^.;:]{{0,80}}(?:enrollment|currently\s+enrolled|in\s+progress)",
        text, re.IGNORECASE,
    ):
        return "graduate_enrollment_required"
    nonexclusive_eligibility = _has_nonexclusive_graduate_eligibility(text)
    generic_student_alternative = _has_generic_student_alternative(text)
    if not nonexclusive_eligibility and (
        re.search(
            rf"{_PHD}[^.;:]{{0,20}}(?:students?|candidates?)\b",
            text,
            re.IGNORECASE,
        )
        or re.search(
            rf"(?:{_MASTERS}|{_PROFESSIONAL})[^.;:]{{0,30}}(?:or|and)[^.;:]{{0,30}}"
            rf"(?:{_MASTERS}|{_PHD}|{_PROFESSIONAL})[^.;:]{{0,25}}(?:students?|candidates?)\b",
            text,
            re.IGNORECASE,
        )
        or re.search(
            r"\bgraduate\s+students?\b[^.;:]{0,60}(?:\bor\b|\band\b|,)"
            r"[^.;:]{0,40}(?:post\s*[- ]?doctor(?:al|ate)(?:\s+fellows?)?|postdocs?|recent\s+graduates?)",
            text,
            re.IGNORECASE,
        )
        or (
            re.fullmatch(rf"\s*graduate\s+(?:students?|candidates?)\s*\.?\s*", text, re.IGNORECASE)
            or re.search(
                r"\b(?:applicants?\s+must\s+be|eligible|open\s+to|seeking|internship\s+is\s+for)"
                r"[^.;:]{0,35}\bgraduate\s+(?:students?|candidates?)\b",
                text,
                re.IGNORECASE,
            )
        )
    ):
        return "graduate_student_targeting"
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
    if not nonexclusive_eligibility and not generic_student_alternative and re.search(
        r"\b(?:graduate\s+fellowship|post\s*[- ]?doctor(?:al|ate)|postdocs?|medical\s+residen(?:cy|t))\b",
        text,
        re.IGNORECASE,
    ):
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
        phd_before_role = re.search(
            rf"{_PHD}[^.;:]{{0,80}}\bintern(?:ship)?\b",
            text,
            re.IGNORECASE,
        )
        role_before_phd = re.search(
            rf"(?:intern(?:ship)?|student\s+researcher|research\s+intern)\s*(?:[,(/-]\s*|\s+){_PHD}",
            text,
            re.IGNORECASE,
        )
        if parenthetical_phd_program or phd_before_role or role_before_phd:
            return "graduate_only_title"
        academic_graduate = rf"(?:{_MASTERS}|graduate\s+(?:students?|candidates?|fellowship)|post\s*[- ]?doctor(?:al|ate)|postdocs?)"
        if re.search(
            rf"{academic_graduate}[^.;:]{{0,60}}(?:intern(?:ship)?|fellow(?:ship)?|student|co\s*[-/]?\s*op)|"
            rf"(?:intern(?:ship)?|fellow(?:ship)?|student)[^.;:]{{0,60}}{academic_graduate}",
            text, re.IGNORECASE,
        ):
            return "graduate_only_title"
        if re.search(
            rf"{_MBA}[^.;:]{{0,60}}(?:intern(?:ship)?|fellow(?:ship)?|program)|"
            rf"\bintern(?:ship)?\b\s*(?:[,(/-]\s*|\s+){_MBA}",
            text,
            re.IGNORECASE,
        ):
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


def _ambiguous_eligibility_reason(
    text: str,
    sentence_context: str,
    source_kind: str,
) -> str | None:
    if _has_broadening_exception(sentence_context):
        return "graduate_requirement_broadened_by_exception"
    if _has_nonexclusive_graduate_eligibility(text):
        return "graduate_additional_eligibility"
    if _has_generic_student_alternative(text):
        return "mixed_student_graduate_categories"
    if _is_preference_only(text):
        return "graduate_preference_only"
    if source_kind in {"education", "qualification"}:
        return "graduate_terminology_ambiguous"
    if source_kind == "title":
        if re.search(rf"(?:{_MASTERS}|{_PHD}|{_MBA}|graduate\s+(?:degree|school|program|students?|candidates?)|postdocs?)", text, re.IGNORECASE):
            return "graduate_title_ambiguous"
        if re.search(
            rf"(?:{_JD}[^.;:]{{0,30}}(?:degree|legal|law|student|candidate)|"
            rf"{_MD}[^.;:]{{0,30}}(?:degree|clinical|medical|student|candidate|resident))",
            text, re.IGNORECASE,
        ):
            return "graduate_title_ambiguous"
        return None
    if re.search(_ELIGIBILITY_CONTEXT, text, re.IGNORECASE):
        return "graduate_terminology_ambiguous"
    return None


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
        sentences = _sentences(value)
        for sentence_index, sentence in enumerate(sentences):
            marker = (source_kind, _normalize_text(sentence))
            if marker in seen:
                continue
            seen.add(marker)
            evidence = sentence
            if sentence_index + 1 < len(sentences) and _has_broadening_exception(sentences[sentence_index + 1]):
                evidence = f"{sentence} {sentences[sentence_index + 1]}"
            clauses = [part.strip() for part in re.split(r"\s*;\s*", sentence) if part.strip()]
            for clause in clauses or [sentence]:
                yield source_kind, evidence, clause


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


def _result(
    eligibility: str,
    evidence: str,
    reason: str,
    *,
    exclusion_reason: str = "",
) -> dict[str, str]:
    result = {
        "education_eligibility": eligibility,
        "education_evidence": evidence,
        "education_reason": reason,
    }
    if exclusion_reason:
        result["exclusion_reason"] = exclusion_reason
    return result


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _unique_pairs(values: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    return list(dict.fromkeys(values))

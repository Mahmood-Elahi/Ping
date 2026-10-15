"""Fail-closed lexical comparison for the deployed source-sync finalizer.

The read-only verifier emits pg_get_functiondef. Save that definition to a
file and pass it as --deployed. A token-identical result proves that any MD5
difference is limited to whitespace, comments, or unquoted identifier casing.
Any token difference remains a production blocker and is never allowlisted by
this tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = (
    REPO_ROOT / "sql" / "migrations" / "20261010_phase5_scope_local_finalizer.sql"
)


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    offset: int


def extract_finalizer_body(text: str) -> str:
    """Extract prosrc-equivalent text from one CREATE FUNCTION definition."""

    header = re.search(
        r"create\s+or\s+replace\s+function\s+"
        r"public\.finalize_student_job_source_sync\s*\(\s*p_sync_id\s+uuid\s*\)",
        text,
        re.IGNORECASE,
    )
    if header is None:
        raise ValueError("finalize_student_job_source_sync(uuid) definition not found")

    opener = re.search(r"\bas\s+(\$[A-Za-z_0-9]*\$)", text[header.end() :], re.IGNORECASE)
    if opener is None:
        raise ValueError("function body dollar-quote opener not found")
    delimiter = opener.group(1)
    body_start = header.end() + opener.end()
    body_end = text.find(delimiter, body_start)
    if body_end < 0:
        raise ValueError("function body dollar-quote terminator not found")
    return text[body_start:body_end].strip()


def normalized_body_md5(body: str) -> str:
    normalized = re.sub(r"\s+", " ", body).strip()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _quoted_token(body: str, start: int, quote: str) -> tuple[str, int]:
    index = start + 1
    while index < len(body):
        if body[index] == "\\" and index + 1 < len(body):
            index += 2
            continue
        if body[index] == quote:
            if index + 1 < len(body) and body[index + 1] == quote:
                index += 2
                continue
            return body[start : index + 1], index + 1
        index += 1
    raise ValueError(f"unterminated {quote} quote at offset {start}")


def tokenize_plpgsql(body: str) -> tuple[Token, ...]:
    """Return a conservative token stream, discarding only comments/spacing."""

    tokens: list[Token] = []
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char.isspace():
            index += 1
            continue
        if body.startswith("--", index):
            newline = body.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if body.startswith("/*", index):
            depth = 1
            cursor = index + 2
            while cursor < length and depth:
                if body.startswith("/*", cursor):
                    depth += 1
                    cursor += 2
                elif body.startswith("*/", cursor):
                    depth -= 1
                    cursor += 2
                else:
                    cursor += 1
            if depth:
                raise ValueError(f"unterminated block comment at offset {index}")
            index = cursor
            continue
        if char in {"'", '"'}:
            value, index_after = _quoted_token(body, index, char)
            tokens.append(Token("string" if char == "'" else "identifier", value, index))
            index = index_after
            continue
        if char == "$":
            delimiter_match = re.match(r"\$[A-Za-z_0-9]*\$", body[index:])
            if delimiter_match is not None:
                delimiter = delimiter_match.group(0)
                closing = body.find(delimiter, index + len(delimiter))
                if closing < 0:
                    raise ValueError(f"unterminated dollar quote at offset {index}")
                end = closing + len(delimiter)
                tokens.append(Token("dollar_string", body[index:end], index))
                index = end
                continue
        if char.isalpha() or char == "_":
            match = re.match(r"[A-Za-z_][A-Za-z_0-9$]*", body[index:])
            assert match is not None
            value = match.group(0).casefold()
            tokens.append(Token("word", value, index))
            index += len(match.group(0))
            continue
        if char.isdigit():
            match = re.match(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", body[index:])
            assert match is not None
            tokens.append(Token("number", match.group(0), index))
            index += len(match.group(0))
            continue

        operator = next(
            (
                candidate
                for candidate in (
                    "#>>",
                    "->>",
                    "::",
                    ":=",
                    "<=",
                    ">=",
                    "<>",
                    "!=",
                    "||",
                    "->",
                    "#>",
                )
                if body.startswith(candidate, index)
            ),
            char,
        )
        tokens.append(Token("symbol", operator, index))
        index += len(operator)
    return tuple(tokens)


def token_sha256(tokens: Sequence[Token]) -> str:
    canonical = "\x1f".join(f"{token.kind}:{token.value}" for token in tokens)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compare_bodies(baseline: str, deployed: str) -> dict[str, object]:
    baseline_tokens = tokenize_plpgsql(baseline)
    deployed_tokens = tokenize_plpgsql(deployed)
    equivalent = [
        (token.kind, token.value) for token in baseline_tokens
    ] == [(token.kind, token.value) for token in deployed_tokens]
    result: dict[str, object] = {
        "result": "TOKEN_EQUIVALENT" if equivalent else "TOKEN_DIFFERENCE_STOP",
        "baseline_normalized_body_md5": normalized_body_md5(baseline),
        "deployed_normalized_body_md5": normalized_body_md5(deployed),
        "baseline_token_sha256": token_sha256(baseline_tokens),
        "deployed_token_sha256": token_sha256(deployed_tokens),
        "baseline_token_count": len(baseline_tokens),
        "deployed_token_count": len(deployed_tokens),
    }
    if not equivalent:
        mismatch = next(
            (
                index
                for index, pair in enumerate(zip(baseline_tokens, deployed_tokens))
                if (pair[0].kind, pair[0].value) != (pair[1].kind, pair[1].value)
            ),
            min(len(baseline_tokens), len(deployed_tokens)),
        )
        result["first_mismatch_token"] = mismatch
        result["baseline_context"] = [
            (token.kind, token.value)
            for token in baseline_tokens[max(0, mismatch - 3) : mismatch + 4]
        ]
        result["deployed_context"] = [
            (token.kind, token.value)
            for token in deployed_tokens[max(0, mismatch - 3) : mismatch + 4]
        ]
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployed", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--deployed-is-body",
        action="store_true",
        help="Treat --deployed as raw pg_proc.prosrc instead of CREATE FUNCTION SQL.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    baseline_body = extract_finalizer_body(args.baseline.read_text(encoding="utf-8"))
    deployed_text = args.deployed.read_text(encoding="utf-8")
    deployed_body = (
        deployed_text.strip()
        if args.deployed_is_body
        else extract_finalizer_body(deployed_text)
    )
    result = compare_bodies(baseline_body, deployed_body)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["result"] == "TOKEN_EQUIVALENT" else 1


if __name__ == "__main__":
    raise SystemExit(main())

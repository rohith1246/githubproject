#!/usr/bin/env python3
"""redact.py -- the single definition of what counts as source-derived detail.

Everything that decides "may this text leave the machine" lives here, and only here. An earlier
design had two filters with two independently-tuned pattern sets -- one in the census, one at the
output boundary -- and they disagreed constantly: clean English describing a codebase was rejected
because it contained the word "from", while a class name slipped past. Two filters means two
opinions and no answer.

The two entry points share one pattern set:

    redact(text)         -> (cleaned, removed)   used where model text enters
    contains_leak(text)  -> bool                 used at the output boundary, as an assertion

Because the second runs over already-redacted text, it firing means `redact` has a bug rather than
that the content was unacceptable. That is the point: a guarantee you can only satisfy by manual
patching is not a guarantee.

**Redaction, not rejection.** A description containing one filename is worth keeping without the
filename. Discarding it loses the finding and keeps nothing.
"""
from __future__ import annotations

import re

# Nothing longer than this leaves, purely to bound a runaway response. Truncation happens on a
# word boundary and is never a reason to discard -- an earlier version dropped on length and
# silently destroyed every example on a real repository, all of them clean.
HARD_CHAR_LIMIT = 2000

# --- patterns, ordered most specific first -------------------------------------------------
# Each is (compiled pattern, replacement, human label). Replacement keeps the sentence readable.
_RULES: list[tuple[re.Pattern, str, str]] = [
    # Paths, with or without a filename.
    (re.compile(r"\b(?:[\w.\-]+[/\\])+[\w.\-]+\b"), "[path]", "a filesystem path"),
    # Source filenames on their own.
    (re.compile(r"\b[\w\-]+\.(?:py|js|jsx|mjs|cjs|ts|tsx|go|java|rb|php|cs|rs|kt|kts|swift|"
                r"scala|c|cc|cpp|h|hpp|m|mm|sh|sql|yml|yaml|toml|json)\b"), "[file]", "a filename"),
    # Object hashes.
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "[revision]", "an object hash"),
    # Function calls, with or without arguments.
    (re.compile(r"\b\w+\([^)]{0,80}\)"), "[function]", "a function call"),
    # Real declarations. Case-sensitive on purpose: lower-case `import x` is code, while
    # "Import duties are computed per jurisdiction" is prose and must survive.
    (re.compile(r"(?:^|\n)[ \t]*(?:def|class)\s+\w+\s*[(:].*"), "[declaration]", "a declaration"),
    (re.compile(r"(?:^|\n)[ \t]*import\s+[\w.]+.*"), "[import]", "an import statement"),
    (re.compile(r"(?:^|\n)[ \t]*from\s+[\w.]+\s+import\b.*"), "[import]", "an import statement"),
    (re.compile(r"\bfunction\s+\w+\s*\([^)]*\)"), "[function]", "a function declaration"),
    # Code punctuation. A bare semicolon is ordinary prose and is deliberately absent.
    (re.compile(r"[{}]|=>|->|::|`"), "", "code punctuation"),
    # snake_case and camelCase identifiers.
    (re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"), "[identifier]", "a snake_case identifier"),
    (re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]*)+\b"), "[identifier]", "a camelCase identifier"),
]

# PascalCase tokens and standalone TitleCase names can carry class, product, company, or project
# identity. Naming public technologies is legitimate, so preserve only an explicit allowlist.
# Public because emit_allowlist's sentence rule asks the same question -- "is this capitalised
# word a technology or somebody's product?" -- and two answers to it is one too many.
_PASCAL = re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]*)+\b")
_SINGLE_TITLE = re.compile(r"[A-Z][a-z]{2,}")
TECH = {
    "PostgreSQL", "MySQL", "MariaDB", "SQLite", "MongoDB", "DynamoDB", "ClickHouse", "BigQuery",
    "RedShift", "Snowflake", "ElasticSearch", "OpenSearch", "RabbitMQ", "GraphQL", "OpenAPI",
    "JavaScript", "TypeScript", "WebSocket", "WebAssembly", "PowerShell", "NoSQL", "OAuth",
    "GitHub", "GitLab", "BitBucket", "DevOps", "Kubernetes", "TensorFlow", "PyTorch", "NumPy",
    "SciPy", "DataDog", "NewRelic", "PagerDuty", "PayPal", "QuickBooks", "SalesForce", "Shopify",
    "Stripe", "Twilio", "SendGrid", "MacOS", "AppStore", "PlayStore", "AirFlow", "SpringBoot",
    "DjangoREST", "FastAPI", "NextJS", "NuxtJS", "SvelteKit", "DotNet", "AspNet",
    "Python", "Java", "Kotlin", "Swift", "Scala", "Rust", "Ruby", "React", "Angular",
    "Django", "Flask", "Fastapi", "Terraform", "Ansible", "Docker", "Maven", "Gradle",
}

_WS = re.compile(r"\s{2,}")


def _truncate(text: str, limit: int = HARD_CHAR_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."


def redact(text: str) -> tuple[str, list[str]]:
    """Strip source-derived detail, keep the prose. Returns (cleaned, what_was_removed)."""
    if not text:
        return "", []
    out = " ".join(str(text).split())
    removed: list[str] = []

    for pattern, replacement, label in _RULES:
        if pattern.search(out):
            removed.append(label)
            out = pattern.sub(replacement, out)

    def _pascal(m: re.Match) -> str:
        if m.group(0) in TECH:
            return m.group(0)
        removed.append("a class name")
        return "[identifier]"

    out = _PASCAL.sub(_pascal, out)
    if _SINGLE_TITLE.fullmatch(out) and out not in TECH:
        removed.append("a proper name")
        out = "[identifier]"
    out = _WS.sub(" ", out).strip()
    # Collapse runs of placeholders left by adjacent redactions.
    out = re.sub(r"(\[\w+\])(\s+\1)+", r"\1", out)
    return _truncate(out), sorted(set(removed))


def contains_leak(text: str) -> str | None:
    """The output-boundary assertion. Returns a human label, or None when clean.

    Runs over text that redact() has already cleaned, so a non-None result is a bug in redact()
    and not a judgement about the content.
    """
    if not text:
        return None
    for pattern, _, label in _RULES:
        if pattern.search(text):
            return label
    for tok in _PASCAL.findall(text):
        if tok not in TECH:
            return "a class name"
    if _SINGLE_TITLE.fullmatch(text) and text not in TECH:
        return "a proper name"
    return None

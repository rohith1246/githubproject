#!/usr/bin/env python3
"""emit_allowlist.py -- the emission boundary for the EXTERNAL measure skill.

Library module, not a CLI. `measure.py` calls it immediately before it writes anything.

    from emit_allowlist import EmissionRefused, enforce, review, validate_sentence

    row         = enforce("codebase_repos", row)
    measurement = enforce("measurement", measurement)
    mining      = enforce("codebase_repo_mining", mining)
    print(review({"measurement.json": measurement, ...}, full=False))

Arguments:
    enforce(document, payload)      document is one of "codebase_repos", "measurement",
                                    "codebase_repo_mining" -- the three schemas declared in
                                    SPEC below. Returns a NEW payload; never mutates its input.
    review(documents, full)         documents maps a display name to an already-enforced
                                    payload. `full=True` enumerates every field; `full=False`
                                    prints the per-kind counts, every emitted sentence verbatim,
                                    and everything that was deliberately not collected.
    validate_sentence(text)         Returns a plain-English reason the text may not leave, or
                                    None when it may.

Environment variables:
    None. This module reads no configuration, no credentials, and no files.

WHY AN ALLOWLIST
----------------
The previous boundary was a denylist: eight key names to drop plus a regex scrub over string
leaves. A denylist answers "is this one of the eight things we thought of", which means every
field a collector grows later leaves the machine by default, and every dict KEY left as it was
found because the scrub only looked at values. This module inverts that. Every leaf that may be
emitted is declared here with the kind of thing it is allowed to be, and anything not declared
stops the run before a byte is written.

Four kinds of thing may leave a repository owner's machine:

    1. numbers
    2. booleans and closed-vocabulary enums
    3. the content digest and the display handle derived from it
    4. ONE sentence per mined idea, describing the SHAPE of a piece of engineering work

Everything else is either declared NOT_COLLECTED -- emitted as null or empty, with a reason a
reader can see in `--review` -- or it is undeclared, and undeclared is a write-time error.

Dynamic maps (`loc_by_language`, `iac_loc_by_type`, the framework maps) are declared with the
closed vocabulary their KEYS must come from. A key outside that vocabulary is folded into
"other" rather than emitted, because a map key is as much a string from the repository as a
map value is.

`redact.py` and `measure.py`'s leak audit still run behind this. They are a backstop now, not
the boundary: they can only catch what they were taught to recognise, and this module is what
makes "we did not emit it" true rather than "we tried to scrub it".
"""
from __future__ import annotations

import math
import re

from redact import TECH

__all__ = [
    "EmissionRefused", "enforce", "review", "validate_sentence",
    "CLOSED_VOCABULARY_KEYS", "DECLARED_KEYS", "MAX_SENTENCE_WORDS", "SPEC",
]


class EmissionRefused(RuntimeError):
    """Raised at the write boundary. The run fails; nothing is written."""


# ---------------------------------------------------------------------------
# Sentence validation -- the only free text that leaves
# ---------------------------------------------------------------------------

MAX_SENTENCE_WORDS = 40

# Ordered most specific first. Each entry is (pattern, the reason a reader would accept).
# A bare semicolon is ordinary English and is deliberately absent, as it is in redact.py.
_PROSE_REJECTIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[/\\]"), "it contains a path separator"),
    (re.compile(r"https?:|\bwww\."), "it contains a URL"),
    (re.compile(r"@"), "it contains an address or handle"),
    (re.compile(r"[A-Za-z_]\w*\.[A-Za-z_]"), "it contains a dotted identifier or a file name"),
    (re.compile(r"(?:^|\s)\.[A-Za-z0-9]{1,6}\b"), "it names a file extension"),
    (re.compile(r"[A-Za-z0-9]+_[A-Za-z0-9]+"), "it contains a snake_case identifier"),
    (re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b"), "it contains a camelCase identifier"),
    (re.compile(r"[\"`]|'[^']{1,80}'"), "it contains a quoted string"),
    # Square brackets matter as much as braces here: they are what redact() leaves behind, so
    # this is also the rule that stops a half-scrubbed "[identifier] handles [path]" being
    # emitted as though it said something.
    (re.compile(r"[{}\[\]<>=|*#$]|::|->|=>"), "it contains code punctuation"),
    (re.compile(r"\b[0-9a-f]{7,}\b"), "it contains an object hash"),
    (re.compile(r"\b[A-Z]{2,}[A-Z0-9]*\b"), "it contains an acronym or a constant name"),
]

# Round brackets are rejected in model-written text and permitted in ours. The risk in a
# parenthesis is what a model put inside it, and every identifier-shaped thing it could put
# there is caught by the rules above; our own status notes are parenthetical by habit, and
# dropping every one of them would leave a failed lane with no explanation at all.
_PARENTHESES = (re.compile(r"[()]"), "it contains code punctuation")

_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")


def _check_prose(text: str, max_words: int, allow: frozenset[str] = frozenset()) -> str | None:
    """Shared checker behind validate_sentence(). Returns a rejection reason, or None.

    `allow` is this tool's OWN vocabulary -- the enum literals and flag names our own notes are
    written from. Those tokens are neutralised before the patterns run, so "census failed
    (rate_limited)" is not rejected for containing a snake_case identifier that we wrote, and
    passing a non-empty `allow` is what marks the text as ours rather than a model's.
    """
    if not isinstance(text, str) or not text.strip():
        return "it is empty"
    probe = " ".join(text.split())
    words = probe.split(" ")
    if len(words) > max_words:
        return f"it is {len(words)} words long, over the limit of {max_words}"
    for token in sorted(allow, key=len, reverse=True):
        probe = probe.replace(token, "ok")
    rules = _PROSE_REJECTIONS if allow else [*_PROSE_REJECTIONS, _PARENTHESES]
    for pattern, reason in rules:
        if pattern.search(probe):
            return reason
    for match in _WORD.finditer(probe):
        token = match.group(0)
        if not token[:1].isupper() or token in TECH:
            continue
        # A capital is only ordinary English at the start of a sentence. Anywhere else it is a
        # product, a company, a service or a class, and we cannot tell which without a dictionary
        # we are not going to ship -- so anywhere else it is a rejection.
        before = probe[:match.start()].rstrip()
        if not before or before[-1] in ".!?":
            continue
        return f"it contains the capitalised name {token!r}"
    return None


def validate_sentence(text: str) -> str | None:
    """Why this one-line description may not leave, or None when it may.

    A description is allowed to say what SHAPE the work has -- "add cursor pagination to a list
    endpoint backed by a relational store". It is not allowed to name a file, a path, a symbol,
    a product or a company, because a sentence that names one of those describes the repository
    rather than the engineering, and the repository is not ours to describe.

    Rejection is total. The caller drops the description and keeps the count: a half-redacted
    sentence with a placeholder where the interesting noun was is worse than no sentence, and
    it hides the fact that something was removed.
    """
    return _check_prose(text, MAX_SENTENCE_WORDS)


# The words this tool writes its own notes with. Neutralised before prose validation so our own
# closed vocabulary does not read as a repository identifier.
_OWN_WORDS = frozenset({
    "--no-llm", "--no-mine", "--build", "--review", "--model", "--provider",
    "claude", "codex", "gemini", "CLI", "JSON", "API", "PATH", "LOC", "KB", "OK",
    "llm_disabled", "mine_disabled", "rate_limited", "model_unavailable", "cli_missing",
    "no_json", "logic_depth", "self_contained", "material_census", "git_history",
    "code_structure", "mined_task_summaries", "task_type_counts", "measurement.json",
    "codebase_repos.json", "codebase_repos.csv", "codebase_repo_mining.json",
    "REPO_INTRINSIC", "ENVIRONMENT", "TIMEOUT", "UNCLASSIFIED", "NONE",
    # Exception names the collectors interpolate with type(e).__name__. They are the Python
    # runtime's vocabulary, not the repository's, and without them "parser unavailable
    # (ImportError)" is dropped and a support question has no answer in the artifact.
    "ImportError", "ModuleNotFoundError", "OSError", "FileNotFoundError", "PermissionError",
    "TimeoutExpired", "JSONDecodeError", "ValueError", "RuntimeError", "MemoryError",
    "UnicodeDecodeError", "NotADirectoryError", "IsADirectoryError",
})


# ---------------------------------------------------------------------------
# Kinds -- what one declared leaf is allowed to be
# ---------------------------------------------------------------------------

_DROP = object()   # returned by a kind that wants its key removed entirely


class _Kind:
    """One permitted shape. `apply` returns the value to emit or raises EmissionRefused.

    None is acceptable everywhere: a field with nothing to say is null, per the contract's
    null-vs-0 rule. Refusing null would force collectors to invent a measurement.
    """

    label = "value"

    def apply(self, value, where: str):
        raise NotImplementedError


class _Number(_Kind):
    label = "number"

    def apply(self, value, where):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmissionRefused(_wrong(where, "a number", value))
        if isinstance(value, float) and not math.isfinite(value):
            raise EmissionRefused(f"refusing to write: {where} is {value!r}, which is not JSON")
        return value


class _Boolean(_Kind):
    label = "boolean"

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, bool):
            raise EmissionRefused(_wrong(where, "a boolean", value))
        return value


class _Enum(_Kind):
    label = "enum"

    def __init__(self, name: str, vocabulary, unknown: str = "reject"):
        self.name = name
        self.vocabulary = frozenset(vocabulary)
        self.unknown = unknown

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, str):
            raise EmissionRefused(_wrong(where, f"one of the {self.name} vocabulary", value))
        if value in self.vocabulary:
            return value
        if self.unknown == "other":
            return "other"
        raise EmissionRefused(
            f"refusing to write: {where} is {value[:80]!r}, which is not in the closed "
            f"{self.name} vocabulary. Either it belongs there -- add it -- or it is a string "
            f"from the repository, which may not leave."
        )


class _Token(_Kind):
    label = "token"

    def __init__(self, name: str, pattern: str):
        self.name = name
        self.pattern = re.compile(pattern)

    def apply(self, value, where):
        if value is None or value == "":
            return None                       # an empty token is nothing to say, not a value
        if not isinstance(value, str) or not self.pattern.fullmatch(value):
            raise EmissionRefused(_wrong(where, f"a {self.name}", value))
        return value


class _Prose(_Kind):
    label = "sentence"

    def __init__(self, max_words: int, allow: frozenset[str] = frozenset(), label: str = "sentence"):
        self.max_words = max_words
        self.allow = allow
        self.label = label

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, str):
            raise EmissionRefused(_wrong(where, "a sentence", value))
        if not value.strip():
            return ""
        return "" if _check_prose(value, self.max_words, self.allow) else " ".join(value.split())


class _TaggedSentence(_Kind):
    """One mined idea: "<task_type>: <sentence>".

    The tag is what lets a reader line the sentences up against the per-type counts -- the count
    for a type is the number of sentences carrying that tag, so the number can be audited by
    reading the evidence for it. The tag is validated against the closed task-type vocabulary and
    the sentence after it is held to the ordinary sentence rule, tag excluded: the tag is ours,
    the sentence is the model's.
    """

    label = "sentence"

    def __init__(self, vocabulary):
        self.vocabulary = frozenset(vocabulary)

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, str):
            raise EmissionRefused(_wrong(where, "a tagged sentence", value))
        tag, _, sentence = value.partition(": ")
        if tag not in self.vocabulary:
            raise EmissionRefused(
                f"refusing to write: {where} is tagged {tag[:40]!r}, which is not one of the "
                f"declared task types {sorted(self.vocabulary)}."
            )
        return "" if _check_prose(sentence, MAX_SENTENCE_WORDS) else f"{tag}: {sentence}"


class _NotCollected(_Kind):
    label = "not collected"

    def __init__(self, shape, reason: str):
        self.shape = shape          # None, [], {} -- or _DROP to remove the key outright
        self.reason = reason

    def apply(self, value, where):
        if self.shape is _DROP:
            return _DROP
        return type(self.shape)() if isinstance(self.shape, (list, dict)) else None


class _ListOf(_Kind):
    label = "list"

    def __init__(self, item: _Kind):
        self.item = item

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, list):
            raise EmissionRefused(_wrong(where, "a list", value))
        out = []
        for i, v in enumerate(value):
            emitted = self.item.apply(v, f"{where}[{i}]")
            # A rejected sentence is dropped from the list, never emitted half-scrubbed.
            if emitted is _DROP or (emitted == "" and isinstance(self.item, (_Prose, _TaggedSentence))):
                continue
            out.append(emitted)
        return out


class _MapOf(_Kind):
    label = "map"

    def __init__(self, name: str, vocabulary, value_kind: _Kind):
        self.name = name
        self.vocabulary = frozenset(vocabulary)
        self.value_kind = value_kind

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise EmissionRefused(_wrong(where, "a map", value))
        out: dict = {}
        for k, v in value.items():
            key = k if isinstance(k, str) and k in self.vocabulary else "other"
            emitted = self.value_kind.apply(v, f"{where}.{key}")
            if key in out and isinstance(emitted, (int, float)) and not isinstance(emitted, bool):
                out[key] = out[key] + emitted          # unknown keys fold together
            else:
                out[key] = emitted
        return out


class _Object(_Kind):
    label = "object"

    def __init__(self, fields: dict):
        self.fields = fields

    def apply(self, value, where):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise EmissionRefused(_wrong(where, "an object", value))
        out: dict = {}
        for k, v in value.items():
            kind = self.fields.get(k)
            if kind is None:
                raise EmissionRefused(
                    f"refusing to write: {where}.{k} is not declared in emit_allowlist.SPEC. "
                    f"Nothing undeclared leaves this machine. If the field is safe, declare it "
                    f"with the kind of thing it is allowed to be; if it carries anything from "
                    f"the repository, it does not belong in an external artifact."
                )
            emitted = kind.apply(v, f"{where}.{k}" if where else k)
            if emitted is not _DROP:
                out[k] = emitted
        return out


def _wrong(where: str, expected: str, value) -> str:
    return (f"refusing to write: {where} was declared as {expected} but is "
            f"{type(value).__name__} {str(value)[:60]!r}")


# The task-type census. Each type has a count column and each mined sentence is tagged with the
# type it belongs to, so the counts can be audited by reading the sentences behind them.
#
# This is a CLOSED vocabulary and it is the external variant's, which is smaller than the internal
# variant's. A type that is not declared here cannot be written: neither as an `n_<type>` column
# (MINING_COUNTS is derived from this tuple) nor as a sentence tag (TAGGED_SENTENCE is built from
# it, and an undeclared tag stops the run). That is deliberate. The external artifact is read by
# the companies it is produced for, so the vocabulary it uses may only contain task shapes we are
# willing to name to them; a type this variant does not mine has no column here to leak through.
TASK_TYPES = (
    "net_new", "agentic", "bug_repair", "repo_evolution",
)

NUMBER = _Number()
BOOL = _Boolean()
SENTENCE = _Prose(MAX_SENTENCE_WORDS)
TAGGED_SENTENCE = _TaggedSentence(TASK_TYPES)
SUMMARY = _Prose(120, label="summary")
OWN_PROSE = _Prose(120, allow=_OWN_WORDS, label="tool note")
DIGEST = _Token("content digest", r"[0-9a-f]{64}")
HANDLE = _Token("display handle", r"repo-[0-9a-f]{12}")
TIMESTAMP = _Token("timestamp", r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
                                r"(?:Z|[+-]\d{2}:?\d{2})?")
RATIO = _Token("ratio", r"\d+:\d+|0 tests")
VERSION = _Token("version string", r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,80}")
MODEL_ID = _Token("model id", r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,80}")
# `owner/name`, or a nested group path on hosts that allow one. Bounded to four segments and to
# the characters a repository name may hold, so a remote that is really a local filesystem path
# cannot ride through this as a name -- an absolute path is exactly what must not leave. A token
# accepts None, which is the honest value when there is no remote to read.
REPO_FULL_NAME = _Token(
    "repository name", r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99}){0,3}"
)


def NOT_COLLECTED(shape, reason: str) -> _NotCollected:      # noqa: N802 -- reads as a kind
    """Declared, permanently empty, and visible as such in `--review`."""
    return _NotCollected(shape, reason)


def OMITTED(reason: str) -> _NotCollected:                   # noqa: N802
    """Declared and removed outright: emitting a placeholder would imply we had the answer."""
    return _NotCollected(_DROP, reason)


# ---------------------------------------------------------------------------
# Closed vocabularies
#
# Imported from the collectors that own them wherever one exists, so the boundary cannot drift
# away from the thing it is guarding. These are OUR tables of public technology names -- none of
# them is derived from the repository being measured.
# ---------------------------------------------------------------------------

import classify_repo                                          # noqa: E402
import repo_stats                                             # noqa: E402

_IAC_TYPES = frozenset({
    "Dockerfile", "Terraform", "CloudFormation", "Helm", "Docker Compose", "Kubernetes",
    "Ansible",
})
LANGUAGES = frozenset(repo_stats.LANGUAGE_BY_EXT.values()) | _IAC_TYPES | {"Unknown"}

# analyze_frameworks() adds these to the marker table's own names from manifest contents.
_DEP_FRAMEWORKS = frozenset({
    "FastAPI", "Flask", "SQLAlchemy", "Pydantic", "React", "Express", "NestJS", "Hono",
    "Elysia", "Koa", "Fastify", "tRPC", "Prisma", "Drizzle ORM", "TypeORM", "Zod",
    "Tailwind CSS",
})
FRAMEWORKS = frozenset(name for _, name in repo_stats.FRAMEWORK_MARKERS) | _DEP_FRAMEWORKS
PACKAGE_MANAGERS = frozenset(repo_stats.MANIFEST_TO_PM.values())
MANIFESTS = frozenset(repo_stats.MANIFEST_TO_PM) | {"setup.py", "setup.cfg"}
LOCKFILES = frozenset(repo_stats.LOCKFILE_PATTERNS)
LOCKFILES_EXPECTED = frozenset({
    "package-lock.json or yarn.lock or pnpm-lock.yaml",
    "poetry.lock / Pipfile.lock / uv.lock / requirements.txt (pinned)",
    "go.sum", "Cargo.lock", "Gemfile.lock", "composer.lock",
})
CI_SYSTEMS = frozenset(name for _, name in repo_stats.CI_CONFIGS)
LINTERS = frozenset(repo_stats.LINT_CONFIGS.values()) | {
    "ruff", "black", "pylint", "flake8", "mypy", "pyright",
}
LINT_CONFIG_FILES = frozenset(repo_stats.LINT_CONFIGS)
TEST_FRAMEWORKS = frozenset(repo_stats.TEST_FRAMEWORK_SIGNALS.values()) | {
    "Karma", "Mocha", "cargo test",
}
TEST_CONFIG_FILES = frozenset({
    "vitest.config.ts", "vitest.config.js", "vitest.config.mjs", "jest.config.ts",
    "jest.config.js", "jest.config.cjs", "jest.config.json", "playwright.config.ts",
    "playwright.config.js", "cypress.config.ts", "cypress.config.js", "karma.conf.js",
    ".mocharc.js", ".mocharc.json", ".mocharc.yaml", "pytest.ini", "pyproject.toml",
    "tox.ini", "setup.cfg",
})
COVERAGE_TOOLING = frozenset({"c8/v8", "NYC/Istanbul", "lcov", "pytest-cov"}) | {
    f"configured in {name}" for name in ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
}
LOGGING_FRAMEWORKS = frozenset(
    lib for libs in repo_stats.LOGGING_FRAMEWORKS.values() for lib in libs
)
ERROR_TRACKING = frozenset(repo_stats.ERROR_TRACKING_LIBS)
VALIDATION_LIBS = frozenset({
    "zod", "pydantic", "joi", "yup", "class-validator", "valibot", "jsonschema",
})
DEP_KEYWORD_GROUPS = frozenset(repo_stats.CLASS_DEP_KEYWORDS)
DEP_KEYWORDS = frozenset(repo_stats.dep_keyword_tokens())
README_SECTIONS = frozenset({
    "install", "setup", "getting started", "run", "test", "environment", "contributing",
    "architecture", "usage", "api",
})
README_NAMES = frozenset({"README.md", "README.rst", "README.txt", "README", "readme.md"})
CHANGELOG_NAMES = frozenset({
    "CHANGELOG.md", "CHANGELOG.rst", "CHANGELOG", "CHANGES.md", "HISTORY.md", "RELEASES.md",
})
CONTRIBUTING_NAMES = frozenset({"CONTRIBUTING.md", "CONTRIBUTING.rst", "CONTRIBUTING"})
PROJECT_TYPES = frozenset({
    "library", "web app", "API service", "mobile", "infrastructure", "CLI / service", "CLI",
    "unknown",
})
REPO_CLASSES = frozenset(classify_repo.ATOMIC_CLASSES)
DEMO_SIGNALS = (
    "name_lexicon_hit", "strong_name_hit", "known_demo_app", "template_readme",
    "scaffold_fingerprint", "boilerplate_readme", "authoritative_demo", "static_name_family",
    "static_content_family",
)
PROVIDERS = frozenset({"claude", "codex", "gemini"})
TOOLCHAINS = frozenset({
    "node-npm", "node-yarn", "node-pnpm", "python", "python-venv", "go-modules", "maven",
    "gradle", "cargo", "bundler", "composer", "dotnet",
})
# The language runtimes a repository can declare a version for. Closed, and it is `runtime.LANES`
# -- named here rather than imported so this module keeps its one job, which is to be the single
# written-down statement of what may leave the machine.
RUNTIME_LANES = frozenset({"node", "python", "go", "rust", "ruby", "java", "dotnet"})
CENSUS_FAILURE_KINDS = frozenset({
    "rate_limited", "auth", "model_unavailable", "timeout", "no_json", "crashed",
    "cli_missing", "unknown",
})
REDACTION_LABELS = frozenset({
    "a filesystem path", "a filename", "an object hash", "a function call", "a declaration",
    "an import statement", "a function declaration", "code punctuation",
    "a snake_case identifier", "a camelCase identifier", "a class name", "a proper name",
})
MATERIAL_CATEGORIES = (
    "complex_logic", "substantial_features", "defect_repairs", "work_units",
    "net_new_capabilities", "repo_evolutions", "performance_work", "hardening_work",
    "integration_work",
)
MINING_COUNTS = tuple(f"n_{task_type}" for task_type in TASK_TYPES)

# Reasons, written once, so the same words appear in the artifact review and in the code.
_NO_SCAN = ("no credential scan is performed anywhere in this skill: it never greps a "
            "repository for its own secrets and never records where one lives")
_NO_PATHS = "file and directory names are not collected"
_NO_ENV_NAMES = "environment variable names are not collected"
_NO_IDENTITY = "author names, addresses and repository names are not collected"
_NO_HASHES = "commit and object hashes are not collected"
_NO_REF_NAMES = "branch and tag names are not collected, they routinely carry product identity"
_NO_COMMANDS = "the exact commands run inside the repository are not collected"
_NO_COMMIT_PROSE = "commit subjects and messages are not collected"


# ---------------------------------------------------------------------------
# The declaration table
# ---------------------------------------------------------------------------

_TREE = _Object({
    "schema_version": VERSION,
    "repo_path": OMITTED(_NO_IDENTITY),
    "repo_name": OMITTED(_NO_IDENTITY),
    "primary_language": _Enum("language", LANGUAGES, unknown="other"),
    "secondary_languages": _ListOf(_Enum("language", LANGUAGES, unknown="other")),
    "loc_by_language": _MapOf("language", LANGUAGES, NUMBER),
    "file_count_by_language": _MapOf("language", LANGUAGES, NUMBER),
    "jvm_dotnet_loc_share": NUMBER,
    "detected_frameworks": _ListOf(_Enum("framework", FRAMEWORKS, unknown="other")),
    "project_type": _Enum("project type", PROJECT_TYPES, unknown="other"),
    "total_loc": NUMBER,
    "total_source_files": NUMBER,
    "median_file_size_loc": NUMBER,
    "p90_file_size_loc": NUMBER,
    "god_files_over_500_loc": NUMBER,
    "god_files_over_1000_loc": NUMBER,
    "top_largest_files": _ListOf(_Object({
        "path": NOT_COLLECTED(None, _NO_PATHS),
        "loc": NUMBER,
    })),
    "generated_files_excluded": NUMBER,
    "iac_loc": NUMBER,
    "iac_file_count": NUMBER,
    "iac_loc_by_type": _MapOf("infrastructure type", _IAC_TYPES, NUMBER),
    "test_spec_files": NUMBER,
    "test_fixture_files": NUMBER,
    "test_source_ratio": RATIO,
    "test_spec_sample": NOT_COLLECTED([], _NO_PATHS),
    "test_framework": _ListOf(_Enum("test framework", TEST_FRAMEWORKS, unknown="other")),
    "test_config_files": _ListOf(_Enum("test config", TEST_CONFIG_FILES, unknown="other")),
    "coverage_tooling": _Enum("coverage tool", COVERAGE_TOOLING, unknown="other"),
    "coverage_threshold": NUMBER,
    "package_managers": _ListOf(_Enum("package manager", PACKAGE_MANAGERS, unknown="other")),
    "manifests_found": _ListOf(_Enum("manifest", MANIFESTS, unknown="other")),
    "lockfiles_found": _ListOf(_Enum("lockfile", LOCKFILES, unknown="other")),
    "lockfiles_expected": _ListOf(_Enum("lockfile", LOCKFILES_EXPECTED, unknown="other")),
    "direct_runtime_deps": NUMBER,
    "direct_dev_deps": NUMBER,
    "total_transitive_deps": NUMBER,
    "dep_update_tooling": _Enum("dependency bot", {"none", "Dependabot", "Renovate"}),
    "ci_systems": _ListOf(_Enum("ci system", CI_SYSTEMS, unknown="other")),
    "ci_config_files": NOT_COLLECTED([], _NO_PATHS),
    "ci_runs_tests": BOOL,
    "ci_runs_lint": BOOL,
    "ci_runs_typecheck": BOOL,
    "ci_has_deploy": BOOL,
    "ci_present": BOOL,
    # Provenance for the three ci_runs_* flags: a null flag means the config could not be
    # parsed, not that the pipeline runs nothing.
    "ci_analysis_method": _Enum("ci analysis method",
                                frozenset({"parsed", "parser_unavailable", "no_ci"})),
    "hardcoded_secret_hits": NOT_COLLECTED(None, _NO_SCAN),
    "secret_hit_details": NOT_COLLECTED([], _NO_SCAN),
    "env_files_committed": NOT_COLLECTED([], _NO_PATHS),
    "dep_audit_in_ci": BOOL,
    "input_validation_patterns": _ListOf(_Enum("validation library", VALIDATION_LIBS,
                                               unknown="other")),
    "linters_and_formatters": _MapOf("linter", LINTERS,
                                     _Enum("lint config", LINT_CONFIG_FILES, unknown="other")),
    "has_lint_config": BOOL,
    "readme": _Enum("readme name", README_NAMES, unknown="other"),
    "readme_loc": NUMBER,
    "readme_sections": _ListOf(_Enum("readme section", README_SECTIONS, unknown="other")),
    "changelog": _Enum("changelog name", CHANGELOG_NAMES, unknown="other"),
    "contributing_guide": _Enum("contributing name", CONTRIBUTING_NAMES, unknown="other"),
    "has_pr_template": BOOL,
    "has_issue_template": BOOL,
    "demo_signals": _Object({name: BOOL for name in DEMO_SIGNALS}),
    "has_dockerfile": BOOL,
    "has_docker_compose": BOOL,
    "has_devcontainer": BOOL,
    "has_nix": BOOL,
    "env_example_file": NOT_COLLECTED(None, _NO_ENV_NAMES),
    "env_vars_referenced_in_source": NOT_COLLECTED([], _NO_ENV_NAMES),
    "env_vars_in_example": NOT_COLLECTED([], _NO_ENV_NAMES),
    "env_vars_missing_from_example": NOT_COLLECTED([], _NO_ENV_NAMES),
    "logging_framework": _Enum("logging library", LOGGING_FRAMEWORKS, unknown="other"),
    "error_tracking": _Enum("error tracker", ERROR_TRACKING, unknown="other"),
    "has_health_endpoint": BOOL,
    "has_metrics": BOOL,
    "class_signals": _Object({
        "dep_keyword_hits": _Object({
            group: _ListOf(_Enum("dependency keyword", DEP_KEYWORDS, unknown="other"))
            for group in DEP_KEYWORD_GROUPS
        }),
        "notebook_count": NUMBER,
        "terraform_file_count": NUMBER,
        "terraform_present": BOOL,
        "terraform_loc": NUMBER,
        "k8s_manifest_count": NUMBER,
        "k8s_loc": NUMBER,
        "helm_present": BOOL,
        "helm_file_count": NUMBER,
        "helm_loc": NUMBER,
        "pulumi_present": BOOL,
        "ansible_present": BOOL,
        "ansible_file_count": NUMBER,
        "ansible_loc": NUMBER,
        "cloudformation_file_count": NUMBER,
        "cloudformation_loc": NUMBER,
        "docker_compose_file_count": NUMBER,
        "docker_compose_loc": NUMBER,
        "dockerfile_count": NUMBER,
        "dockerfile_loc": NUMBER,
        "ui_component_file_count": NUMBER,
        "sql_file_count": NUMBER,
        "sql_loc": NUMBER,
        "css_loc": NUMBER,
        "css_loc_ratio": NUMBER,
        "data_file_count": NUMBER,
        "iac_loc": NUMBER,
        "iac_file_count": NUMBER,
        "iac_loc_by_type": _MapOf("infrastructure type", _IAC_TYPES, NUMBER),
    }),
})

_GIT = _Object({
    "head_sha": OMITTED(_NO_HASHES),
    "effective_tip_sha": OMITTED(_NO_HASHES),
    "anonymizer_commit": OMITTED(_NO_HASHES),
    "latest_tag": OMITTED(_NO_REF_NAMES),
    "top_authors": OMITTED(_NO_IDENTITY),
    "anonymizer_commit_detected": BOOL,
    "anonymizer_commit_excluded": BOOL,
    "total_commits": NUMBER,
    "total_commits_including_anonymizer": NUMBER,
    "first_commit": TIMESTAMP,
    "last_commit": TIMESTAMP,
    "span_days": NUMBER,
    "recency_days": NUMBER,
    "human_authors": NUMBER,
    "bot_authors": NUMBER,
    "bot_commit_count": NUMBER,
    "bot_commit_ratio": NUMBER,
    "conventional_rate_last_200": NUMBER,
    "tag_count": NUMBER,
    "semver_tag_count": NUMBER,
    "merge_commit_count": NUMBER,
    "looks_like_burst_copy": BOOL,
    "class_a_count": NUMBER,
    "class_b_count": NUMBER,
    "class_c_pre_count": NUMBER,
    "class_d_bug_count": NUMBER,
    "confirmed_candidate_count": NUMBER,
    "provisional_candidate_count": NUMBER,
    "analyzed_commits": NUMBER,
    "full_history_scanned": BOOL,
    "commits_by_month": _ListOf(NUMBER),
    "active_days": NUMBER,
    # The per-class commit lists carry subjects and hashes. build_git_block() never copies them;
    # declaring them here means a change of mind about that fails the run instead of shipping.
    "class_a_commits": OMITTED(_NO_COMMIT_PROSE),
    "class_b_commits": OMITTED(_NO_COMMIT_PROSE),
    "class_c_pre_commits": OMITTED(_NO_COMMIT_PROSE),
    "class_d_bug_commits": OMITTED(_NO_COMMIT_PROSE),
    "scanned_commits": OMITTED(_NO_COMMIT_PROSE),
    "candidates": OMITTED(_NO_COMMIT_PROSE),
    "commit_subjects": OMITTED(_NO_COMMIT_PROSE),
})

_STRUCTURE = _Object({
    "probe": _Enum("probe name", {"code_structure"}),
    "ok": BOOL,
    "error": OWN_PROSE,
    "note": OWN_PROSE,
    "bounds": OWN_PROSE,
    "prod_loc": NUMBER,
    "source_files": NUMBER,
    "test_files": NUMBER,
    "prod_loc_deterministic": NUMBER,
    "source_files_deterministic": NUMBER,
    "test_files_deterministic": NUMBER,
    "n_functions_seen": NUMBER,
    "n_files_parsed": NUMBER,
    "n_files_parse_failed": NUMBER,
    "source_files_unparsed": NUMBER,
    "decisions_gini_top1pct": NUMBER,
    "error_handling_per_kloc": NUMBER,
    "structure_probe_mode": _Enum("probe mode", {
        "deterministic", "agentic_unavailable", "agentic_confirmed", "agentic_contradicted"}),
    "structure_unknown_files": NUMBER,
    "structure_unknown_exts": NOT_COLLECTED(None, _NO_PATHS),
    "structure_agentic_languages": NOT_COLLECTED(None, _NO_PATHS),
    "structure_agentic_note": SENTENCE,
})

_HISTORY = _Object({
    "probe": _Enum("probe name", {"git_history"}),
    "ok": BOOL,
    "error": OWN_PROSE,
    "ref_analysed": NOT_COLLECTED(None, _NO_REF_NAMES),
    "commit_sha": NOT_COLLECTED(None, _NO_HASHES),
    "ref_commits": NUMBER,
    "ref_candidates_considered": NUMBER,
    "ref_choice_reason": OWN_PROSE,
    "ref_choice_overrode_deepest": BOOL,
    "anonymizer_tip_detected": BOOL,
    "development_substance": NUMBER,
    "development_substance_note": SENTENCE,
    "development_substance_error": OWN_PROSE,
    "development_sampled_commits": NUMBER,
    "development_real_in_sample": NUMBER,
    "real_commits": NUMBER,
    "mineable_commits": NUMBER,
    "human_authors": NUMBER,
    "history_probe_mode": _Enum("probe mode", {"deterministic", "agentic"}),
    "history_model": MODEL_ID,
})

_BUILD = _Object({
    "probe": _Enum("probe name", {"build"}),
    "ok": BOOL,
    "error": OWN_PROSE,
    "note": OWN_PROSE,
    "build_skipped": BOOL,
    # WHICH LEVEL ACTUALLY RAN, and which one was asked for. Additive, and load-bearing: without
    # the pair, a `discover` record that a vendor chose is indistinguishable from a `full` attempt
    # this run could not finish, and only the second one leaves `observed_runnability` unscored for
    # a reason the operator can act on (raise the budget).
    "build_level": _Enum("build level", {"none", "discover", "full"}),
    "build_level_requested": _Enum("build level", {"none", "discover", "full"}),
    "build_level_fallback_reason": OWN_PROSE,
    "run_budget_exhausted": BOOL,
    "build_probe_mode": _Enum("probe mode", {"deterministic", "agentic"}),
    "build_probe_model": MODEL_ID,
    "agentic_fallback_reason": OWN_PROSE,
    "toolchain": _Enum("toolchain", TOOLCHAINS, unknown="other"),
    "build_attempted": BOOL,
    "build_commands_tried": NOT_COLLECTED([], _NO_COMMANDS),
    "install_ok": BOOL,
    "build_ok": BOOL,
    # The executed verdict: build_ok + tests_ran + (n_passed > 0) + (coverage_pct > 0), 0..4.
    # A small integer with no repository detail in it, and the one number in this block we earned
    # by actually running the project rather than reading its manifests.
    "observed_runnability": NUMBER,
    # Why that index is null, and who owns the absence. A null with no reason beside it is the
    # thing a reader downgrades a repository over.
    "observed_runnability_reason": OWN_PROSE,
    # The DISCOVER-LEVEL companion: install_ok + build_ok + tests_discovered, 0..3. Every term
    # executed, none of them needing a suite, which is what makes a fallen-back run gradeable.
    "discover_runnability": NUMBER,
    "tests_discovered": BOOL,
    "build_and_tests_ran": BOOL,
    "failure_class": _Enum("failure class", {
        "NONE", "REPO_INTRINSIC", "ENVIRONMENT", "TIMEOUT", "UNCLASSIFIED"}),
    "repo_intrinsic_failure": BOOL,
    "timed_out": BOOL,
    "coverage_pct": NUMBER,
    "coverage_method": OWN_PROSE,
    "coverage_unsupported_reason": OWN_PROSE,
    "build_remediation_effort": _Enum("remediation effort", {
        "none", "trivial", "moderate", "substantial", "infeasible", "unknown"}),
    "build_remediation_notes": OWN_PROSE,
    # WHICH RUNTIME THE TREE ASKED FOR AND WHICH ONE IT GOT. Additive, and the reason it is here
    # rather than left in the (stripped) per-project evidence: `wrong_runtime` was 27 of the
    # 417-repo corpus, and without these five fields a reader cannot tell a repository that does
    # not build from one we built with the wrong interpreter. The lanes are a closed vocabulary; the
    # versions are VERSION tokens, which is why `runtime.py` normalises a range to `3.8-3.13`
    # rather than emitting the `>=`/`<` the manifest actually wrote.
    "runtime_lanes_declared": _ListOf(_Enum("runtime lane", RUNTIME_LANES)),
    "runtime_lanes_unsatisfied": _ListOf(_Enum("runtime lane", RUNTIME_LANES)),
    "runtime_requested": _ListOf(VERSION),
    "runtime_used": _ListOf(VERSION),
    "runtime_resolution_note": OWN_PROSE,
    # THE REPAIR PASS. Counts and one boolean -- nothing here carries a command, a log line, a
    # package name or a path, because what an agent had to install to make a tree build is a fact
    # about the tree that we did not need in order to score it. `repair_offered` separates "not
    # asked for" from "asked for and nothing needed it", which would otherwise both read as zero.
    "repair_offered": BOOL,
    "repair_candidates_n": NUMBER,
    "repair_attempted_n": NUMBER,
    "repair_succeeded_n": NUMBER,
    "repair_refused_n": NUMBER,
    # A turn that edited tracked source is a failed repair, and the count is emitted so a reader
    # can see the guard firing rather than trusting that it exists.
    "repair_rejected_source_edit_n": NUMBER,
    "repair_seconds": NUMBER,
})

_MATERIAL = _Object({
    "probe": _Enum("probe name", {"material_census"}),
    "ok": BOOL,
    "scored": BOOL,
    "provider": _Enum("provider", PROVIDERS),
    "model": MODEL_ID,
    "error": OWN_PROSE,
    "skip_reason": _Enum("skip reason", {"llm_disabled"}),
    "logic_depth_unavailable_reason": OWN_PROSE,
    "census_platform_note": OWN_PROSE,
    "census_failure_kind": _Enum("failure kind", CENSUS_FAILURE_KINDS),
    "census_retryable": BOOL,
    "census_attempts": NUMBER,
    "census_error_detail": SENTENCE,
    "census_hit_cap": BOOL,
    "timed_out": BOOL,
    "themes": _ListOf(SENTENCE),
    "self_contained": NUMBER,
    "logic_depth": NUMBER,
    "minable_ideas_total": NUMBER,
    "n_minable_ideas": NUMBER,
    "defect_repairs_with_regression_test": NUMBER,
    **{f"n_{cat}": NUMBER for cat in MATERIAL_CATEGORIES},
    "minable_ideas": _Object({cat: _ListOf(SENTENCE) for cat in MATERIAL_CATEGORIES}),
    "material_summary": SUMMARY,
    "what_i_could_not_assess": SENTENCE,
    "redacted_from_examples": _ListOf(_Enum("redaction label", REDACTION_LABELS,
                                            unknown="other")),
    "task_type_counts": _Object({
        "total_candidates": NUMBER,
        **{k: NUMBER for k in MINING_COUNTS},
    }),
})

_MEASUREMENT = _Object({
    "measurer_version": VERSION,
    "measured_at": TIMESTAMP,
    "repo_digest": DIGEST,
    # Declared so the platform can map a measurement to the repository it describes;
    # `repo_digest` identifies the tree, not the repository. Null when there is no remote.
    "real_repo_name": REPO_FULL_NAME,
    "variant": _Enum("variant", {"ext"}),
    "capacity": NUMBER,
    "tree": _TREE,
    "git": _GIT,
    "classification": _Object({
        "primary_class": _Enum("repo class", REPO_CLASSES),
        "class_confidence": _MapOf("repo class", REPO_CLASSES, NUMBER),
        "is_monorepo": BOOL,
        "is_likely_demo": BOOL,
        "demo_reasoning": _ListOf(_Enum("demo signal", DEMO_SIGNALS)),
    }),
    "ext_signals": _Object({
        "structure": _STRUCTURE,
        "history": _HISTORY,
        "build": _BUILD,
    }),
    "material": _MATERIAL,
})

_CODEBASE_REPOS = _Object({
    "id": NOT_COLLECTED(None, "assigned by the platform on ingest"),
    "codebase_id": NOT_COLLECTED(None, "assigned by the platform on ingest"),
    "service_id": NOT_COLLECTED(None, "assigned by the platform on ingest"),
    "repo_digest": DIGEST,
    "real_repo_name": NOT_COLLECTED(None, _NO_IDENTITY),
    "fake_repo_name": HANDLE,
    # `partial` is not decoration. A row that says `measured` while a lane died claims a
    # completeness the run did not have, and the reason then only exists in a nested block nobody
    # queries. skip_reason names WHICH kind of gap it was; the lane's own block says which lane.
    "status": _Enum("status", {"measured", "partial"}),
    "skip_reason": _Enum("skip reason", {"llm_disabled", "mine_disabled",
                                         "lanes_unavailable", "lanes_timed_out"}),
    "measured_at": TIMESTAMP,
    "loc": NUMBER,
    "zip_bytes": NUMBER,
    "excluded_loc": NUMBER,
    "languages": _MapOf("language", LANGUAGES, NUMBER),
    "primary_language": _Enum("language", LANGUAGES, unknown="other"),
    "frontend_pct": NUMBER,
    "backend_pct": NUMBER,
    "test_loc": NUMBER,
    "test_code_files": NUMBER,
    "test_spec_files": NUMBER,
    "test_ratio": NUMBER,
    "test_source_ratio": RATIO,
    "test_framework": _ListOf(_Enum("test framework", TEST_FRAMEWORKS, unknown="other")),
    "has_ci": BOOL,
    "ci_present": BOOL,
    "ci_runs_tests": BOOL,
    "detected_frameworks": _ListOf(_Enum("framework", FRAMEWORKS, unknown="other")),
    "commit_count": NUMBER,
    "author_count": NUMBER,
    "first_commit_at": TIMESTAMP,
    "last_commit_at": TIMESTAMP,
    "active_days": NUMBER,
    "span_days": NUMBER,
    "commits_by_month": _ListOf(NUMBER),
    "pr_count": NUMBER,
    "issue_count": NUMBER,
    "repo_class": _Enum("repo class", REPO_CLASSES),
    "is_likely_demo": BOOL,
    "quality_score": NUMBER,
    "build_ok": BOOL,
    "testable_at_head": BOOL,
    "capacity": NUMBER,
})

_MINING = _Object({
    "repo_id": HANDLE,
    "measurer_version": VERSION,
    "provider": _Enum("provider", PROVIDERS),
    "model": MODEL_ID,
    "mined_at": TIMESTAMP,
    "total_candidates": NUMBER,
    **{k: NUMBER for k in MINING_COUNTS},
    "scored": BOOL,
    "skip_reason": _Enum("skip reason", {"llm_disabled", "mine_disabled"}),
    "mine_unavailable_reason": OWN_PROSE,
    "mined_task_summaries": _ListOf(TAGGED_SENTENCE),
})

SPEC: dict[str, _Object] = {
    "codebase_repos": _CODEBASE_REPOS,
    "measurement": _MEASUREMENT,
    "codebase_repo_mining": _MINING,
}


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------

def enforce(document: str, payload: dict) -> dict:
    """Return the payload the allowlist permits, or raise EmissionRefused.

    Every leaf is validated against its declared kind and every dict key against the set of
    declared field names -- or, inside a dynamic map, against that map's closed vocabulary.
    Nothing undeclared is scrubbed, truncated or passed with a warning: the run stops.
    """
    spec = SPEC.get(document)
    if spec is None:
        raise EmissionRefused(
            f"refusing to write: no schema is declared for {document!r}. "
            f"Declared documents are {sorted(SPEC)}."
        )
    return spec.apply(payload, document)


def _collect_keys(kind: _Kind, fields: set[str], vocabulary: set[str]) -> None:
    if isinstance(kind, _Object):
        fields.update(kind.fields)
        for child in kind.fields.values():
            _collect_keys(child, fields, vocabulary)
    elif isinstance(kind, _ListOf):
        _collect_keys(kind.item, fields, vocabulary)
    elif isinstance(kind, _MapOf):
        vocabulary.update(kind.vocabulary)
        vocabulary.add("other")
        _collect_keys(kind.value_kind, fields, vocabulary)
    elif isinstance(kind, _Enum):
        vocabulary.update(kind.vocabulary)
        vocabulary.add("other")


def _derive_key_sets() -> tuple[frozenset[str], frozenset[str]]:
    fields: set[str] = set()
    vocabulary: set[str] = set()
    for spec in SPEC.values():
        _collect_keys(spec, fields, vocabulary)
    return frozenset(fields), frozenset(fields | vocabulary)


def _closed_vocabulary_keys() -> frozenset[str]:
    """Field names whose values this module has already proved come from a closed vocabulary.

    `measure.py` exempts these from the backstop scrub. Running a regex over a value that was
    matched against a fixed table cannot make it safer, and it does corrupt it -- the scrub
    reads "Next.js" as a filename and "GitHub Actions" as a class name.
    """
    found: set[str] = set()

    def walk(kind: _Kind) -> None:
        if isinstance(kind, _Object):
            for name, child in kind.fields.items():
                if _is_closed(child):
                    found.add(name)
                walk(child)
        elif isinstance(kind, _ListOf):
            walk(kind.item)
        elif isinstance(kind, _MapOf):
            walk(kind.value_kind)

    def _is_closed(kind: _Kind) -> bool:
        # A tagged sentence counts: its tag is a closed-vocabulary token and the sentence after
        # it has already passed a rule far stricter than the backstop scrub, which would only
        # mangle the tag into a placeholder.
        if isinstance(kind, (_Enum, _Token, _MapOf, _TaggedSentence)):
            return True
        if isinstance(kind, _ListOf):
            return _is_closed(kind.item)
        return False

    for spec in SPEC.values():
        walk(spec)
    return frozenset(found)


DECLARED_FIELDS, DECLARED_KEYS = _derive_key_sets()
CLOSED_VOCABULARY_KEYS = _closed_vocabulary_keys()


# ---------------------------------------------------------------------------
# The review the repository owner reads before deciding to send anything
# ---------------------------------------------------------------------------

_GROUPS = (
    ("numbers", "NUMBERS"),
    ("booleans", "BOOLEANS"),
    ("enums", "CLOSED-VOCABULARY VALUES"),
    ("identity", "CONTENT DIGEST AND DISPLAY HANDLE"),
    ("timestamps", "TIMESTAMPS"),
    ("task_ideas", "TASK IDEAS MINED FROM YOUR CODE (one line each, shown in full)"),
    ("sentences", "ONE-LINE DESCRIPTIONS (the only other free text, shown in full)"),
    ("notes", "THIS TOOL'S OWN STATUS NOTES"),
    ("unmeasured", "EMITTED WITH NO VALUE (the field ships, the value was not collected)"),
)

# What a reader sees in place of a value the run never produced. "not collected" is itself
# information they are entitled to: the field leaves their machine either way.
_NOT_COLLECTED = "not collected"
_EMPTY = "not collected (empty)"

# Printed directly under the mined-ideas heading. A reader who has just been shown a list of
# things that could be done to their codebase is entitled to be told, in the same place, that
# nothing is going to be.
_TASK_IDEA_NOTE = (
    "    Each line is the SHAPE of an exercise a coding model could be asked to perform against",
    "    a sandboxed copy of a codebase like this one. This tool never writes to your repository,",
    "    applies a change, or acts on any of these ideas. They are shapes of work, recorded as",
    "    text.",
)


def _group_of(kind: _Kind, name: str) -> str:
    if isinstance(kind, _Number):
        return "numbers"
    if isinstance(kind, _Boolean):
        return "booleans"
    if isinstance(kind, _Enum):
        return "enums"
    if kind is DIGEST or kind is HANDLE:
        return "identity"
    if kind is TIMESTAMP:
        return "timestamps"
    if isinstance(kind, _TaggedSentence):
        # A mined task idea is the one thing in the artifact that is derived from the owner's
        # code rather than counted off it, so it gets its own heading instead of being filed
        # among the census notes where a reader has to know the field names to find it.
        return "task_ideas"
    if isinstance(kind, _Prose):
        return "notes" if kind.allow else "sentences"
    return "enums"


def _declared_withheld(kind: _Kind, name: str, into: dict) -> None:
    """Every declared field this skill refuses to populate, and why.

    Read off the declaration table rather than off the payload: "we do not collect commit
    messages" is a statement about the tool, and a reader must see it whether or not the run
    happened to have a place to put one.
    """
    if isinstance(kind, _NotCollected):
        into.setdefault(name, kind.reason)
    elif isinstance(kind, _Object):
        for field, child in kind.fields.items():
            _declared_withheld(child, field, into)
    elif isinstance(kind, _ListOf):
        _declared_withheld(kind.item, name, into)
    elif isinstance(kind, _MapOf):
        _declared_withheld(kind.value_kind, name, into)


def _review_children(kind: _Kind, value, path: str) -> list[tuple[_Kind, object, str]]:
    """The (kind, value, path) triples one container expands into. Empty when it holds nothing."""
    if isinstance(kind, _Object) and isinstance(value, dict):
        return [(kind.fields[k], v, f"{path}.{k}") for k, v in value.items()
                if k in kind.fields]
    if isinstance(kind, _ListOf) and isinstance(value, list):
        return [(kind.item, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(kind, _MapOf) and isinstance(value, dict):
        return [(kind.value_kind, v, f"{path}.{k}") for k, v in value.items()]
    return []


def _walk_review(kind: _Kind, value, path: str, found: dict) -> None:
    """Record every emitted field, INCLUDING the ones that carry no value.

    An owner deciding whether to send the artifact needs the whole field list, not the subset
    that happened to be populated: `quality_score = not collected` is a fact about the run they
    are entitled to before it leaves their machine.
    """
    if isinstance(kind, _NotCollected):
        return
    if isinstance(kind, (_Object, _ListOf, _MapOf)):
        children = _review_children(kind, value, path)
        if children:
            for child_kind, child_value, child_path in children:
                _walk_review(child_kind, child_value, child_path, found)
        else:
            found.setdefault("unmeasured", []).append(
                (path, _NOT_COLLECTED if value is None else _EMPTY))
        return
    if value is None:
        found.setdefault("unmeasured", []).append((path, _NOT_COLLECTED))
        return
    if value == "":
        found.setdefault("unmeasured", []).append((path, _EMPTY))
        return
    found.setdefault(_group_of(kind, path), []).append((path, value))


def review(documents: dict[str, dict], full: bool = False) -> str:
    """The plain-language account of what is in the artifact. Nothing has left yet.

    `full=True` names every emitted field and its value, including the fields that ship with no
    value at all. `full=False` gives the per-kind counts, every one-line description verbatim,
    and the list of things this skill does not collect -- which is the part a reader most needs
    and the part a field-by-field dump buries.
    """
    found: dict[str, list] = {}
    withheld: dict[str, str] = {}
    for display, payload in documents.items():
        document = display.split(".json")[0].split(".csv")[0]
        spec = SPEC.get(document)
        if spec is None:
            continue
        _walk_review(spec, payload, display, found)
        _declared_withheld(spec, document, withheld)

    lines = [
        "",
        "REVIEW -- this is everything the four output files contain. No file has been sent",
        "anywhere. You own the repository and you own these files: read this, then decide.",
        "",
        "Separately, and not undone by declining to send them: if the model lanes ran, the",
        "provider CLI read your source and your development history during this run, under",
        "your own account. That disclosure has already happened. It is the only copy -- the",
        "session was not persisted, so no transcript was written outside this directory, and",
        "credential-shaped files were withheld from the history the model was shown.",
        "references/client-runbook.md sections 3 and 5.",
        "",
    ]
    for key, heading in _GROUPS:
        entries = found.get(key, [])
        if not entries:
            continue
        show = key in ("task_ideas", "sentences", "identity", "notes") or full
        lines.append(f"{heading} -- {len(entries)}")
        if key == "task_ideas":
            # Read cold, "introduce a subtle defect ..." looks like a plan for someone's
            # production code rather than what it is. Say what it is, next to it.
            lines.extend(_TASK_IDEA_NOTE)
        if show:
            for path, value in entries:
                lines.append(f"    {path} = {value}")
        elif key == "enums":
            distinct = sorted({str(v) for _, v in entries})
            lines.append(f"    {', '.join(distinct[:40])}")
        lines.append("")

    if withheld:
        lines.append(f"NOT COLLECTED BY POLICY -- {len(withheld)} declared fields, never filled in")
        for name, reason in sorted(withheld.items()):
            lines.append(f"    {name}: {reason}")
        lines.append("")

    lines.append("Nothing in this skill searches your source for secret-shaped strings. No author")
    lines.append("name or address, no commit message, no branch or tag name and no file path is")
    lines.append("emitted. Every value above was checked against a declared allowlist before it was")
    lines.append("written. Two disclosed exceptions sit beside that, both described in")
    lines.append("references/client-runbook.md section 4: the content digest reads every file's")
    lines.append("bytes, and a --build authentication failure checks the checkout's own registry")
    lines.append("configuration. Neither retains anything it read.")
    lines.append("")
    return "\n".join(lines)

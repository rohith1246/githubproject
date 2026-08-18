#!/usr/bin/env python3
"""
repo_stats.py — static codebase metadata collector for the repo-quality-score skill.

Usage:
    python repo_stats.py <repo-path> [--top-files N]

Positional arguments:
    repo-path           Path to the repository (or sub-directory) to analyze.

Options:
    --top-files N       Number of largest source files to report (default 10).

Environment variables: none. This script reads no secrets and no credentials. It
only opens files read-only with bounded reads, never executes code, never installs
dependencies, and never touches the network.

Emits JSON to stdout. Works on any language/framework by pattern-matching well-known
file structures.

Covers:
- Language detection and LOC breakdown
- Framework detection
- File size distribution (median, p90, top-N largest)
- Code-file and test-file counts and test:source ratio
- Dependency count from manifests and lockfiles
- CI/CD config detection
- Dependency-audit and input-validation hygiene. NOT secret scanning: this collector holds no
  credential patterns, never searches for a secret-shaped string, and never opens a `.env`. The
  scanner that once lived here is deleted; `hardcoded_secret_hits` and `secret_hit_details` are
  permanently null and empty, meaning "not collected by policy" and never "measured zero".
- Linting/formatting config detection
- Documentation signals (README, CHANGELOG, CONTRIBUTING)
- Observability signals
- Coverage tooling detection
- Class signals (frontend / backend / ML / AI-research / data-eng / security / infra)
  used by classify_repo.py to assign the repo to one or more classes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

try:
    # CI configuration is YAML and every CI system requires it to parse, so we can read the
    # jobs, the steps and the `if:` guards instead of grepping the file for the word "test".
    # Optional so that a checkout of this collector still runs without it -- but when it is
    # missing the CI flags are reported UNMEASURED, never guessed.
    import yaml
except ImportError:
    yaml = None

# ---------------------------------------------------------------------------
# Language detection by file extension
# ---------------------------------------------------------------------------

LANGUAGE_BY_EXT: dict[str, str] = {
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".py": "Python",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".c": "C",
    ".h": "C/C++",
    ".swift": "Swift",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hrl": "Erlang",
    ".scala": "Scala",
    ".clj": "Clojure",
    ".hs": "Haskell",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".sql": "SQL",
    ".css": "CSS",
    ".scss": "CSS",
    ".sass": "CSS",
    ".less": "CSS",
    ".html": "HTML",
    ".htm": "HTML",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".tf": "Terraform",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".json": "JSON",
    ".toml": "TOML",
    ".md": "Markdown",
    ".mdx": "Markdown",
}

# Extensions that are code (contribute to LOC, file counts, etc.)
CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".go", ".rs", ".java", ".kt", ".kts",
    ".rb", ".php", ".cs", ".cpp", ".cc", ".cxx", ".c", ".h",
    ".swift", ".dart", ".ex", ".exs", ".erl", ".hrl",
    ".scala", ".clj", ".hs", ".sh", ".bash", ".zsh",
    ".sql", ".vue", ".svelte",
}

# Infrastructure-as-Code (IaC). Kept SEPARATE from CODE_EXTENSIONS so it never
# double-counts with analyze_languages: .tf/.yaml/etc. are NOT code extensions, so
# they are invisible to LOC/file-size accounting unless analyze_iac claims them.
# YAML/JSON is only counted when POSITIVELY identified as infra (content-sniffed),
# never as raw config/data. HCL/Terraform files are always IaC.
IAC_HCL_EXTENSIONS = {".tf", ".tfvars", ".hcl"}
IAC_YAML_EXTENSIONS = {".yaml", ".yml"}
# Directory prefixes whose YAML is CI config, not IaC (already scored via H/CI-CD).
_CI_DIR_PREFIXES = (".github/", ".gitlab", ".circleci/", ".gitlab-ci")
# Max bytes to sniff from a candidate manifest head.
_IAC_SNIFF_BYTES = 4096
# Safety backstop on how many YAML/JSON heads we content-sniff, so a pathological
# tree can't stall the scan. Set high because SKIP_DIRS already prunes vendored trees
# (node_modules, vendor, dist, …) and each sniff reads only a 4 KB head — a real repo's
# manifest count is far below this, so it should never starve genuine IaC files.
_IAC_MAX_SNIFF = 20_000

# Directories to always skip
SKIP_DIRS = {
    "node_modules", ".git", "vendor", "dist", "build", "out",
    ".cache", "__pycache__", ".pytest_cache", ".mypy_cache",
    "venv", ".venv", "env", ".env", "target",  # Rust target
    ".next", ".nuxt", ".output", ".turbo",
    "coverage", ".nyc_output", "htmlcov",
    "migrations",  # usually generated
    "generated", "gen", "__generated__",
    ".tox", "eggs", "*.egg-info",
}

# Files/patterns to exclude from "source" counts (generated / vendored)
GENERATED_PATTERNS = [
    re.compile(r"// Code generated", re.I),
    re.compile(r"# DO NOT EDIT", re.I),
    re.compile(r"# This file is auto-generated", re.I),
    re.compile(r"// AUTO-GENERATED", re.I),
    re.compile(r"/* eslint-disable \*/"),
]

# Test file patterns
TEST_PATTERNS = [
    re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mjs)$", re.I),
    re.compile(r"(^|/)test_[^/]+\.(py)$", re.I),
    re.compile(r"(^|/)[^/]+_test\.(go|py|rb)$", re.I),
    re.compile(r"(^|/)__tests__/", re.I),
    # Universal test-directory trees (align with git_stats.py): any file under a
    # test/, tests/, or spec/ directory counts as a test. Fixtures within them are
    # still split out by is_fixture_file before being counted as specs.
    re.compile(r"(^|/)tests?/", re.I),
    re.compile(r"(^|/)spec/", re.I),
    re.compile(r"[A-Z][^/]*Test[s]?\.(java|kt|cs)$"),
    re.compile(r"\.(test|spec)\.rs$", re.I),
]

FIXTURE_PATTERNS = [
    re.compile(r"(^|/)tests?/fixtures?/", re.I),
    re.compile(r"(^|/)__snapshots__/", re.I),
    re.compile(r"(^|/)__mocks__/", re.I),
    re.compile(r"\.snap$", re.I),
    re.compile(r"\.fixture\.(json|ts|js|yaml)$", re.I),
]

# ---------------------------------------------------------------------------
# Framework detection (by file presence)
# ---------------------------------------------------------------------------

FRAMEWORK_MARKERS: list[tuple[str, str]] = [
    # File pattern → framework name
    ("next.config.js", "Next.js"),
    ("next.config.ts", "Next.js"),
    ("next.config.mjs", "Next.js"),
    ("nuxt.config.ts", "Nuxt"),
    ("nuxt.config.js", "Nuxt"),
    ("svelte.config.js", "SvelteKit"),
    ("svelte.config.ts", "SvelteKit"),
    ("angular.json", "Angular"),
    ("vite.config.ts", "Vite"),
    ("vite.config.js", "Vite"),
    ("remix.config.js", "Remix"),
    ("remix.config.ts", "Remix"),
    ("astro.config.mjs", "Astro"),
    ("astro.config.ts", "Astro"),
    ("gatsby-config.js", "Gatsby"),
    ("gatsby-config.ts", "Gatsby"),
    ("vue.config.js", "Vue CLI"),
    ("manage.py", "Django"),
    ("settings.py", "Django"),  # usually inside a package
    ("wsgi.py", "WSGI (Flask/Django)"),
    ("asgi.py", "ASGI (FastAPI/Django Channels)"),
    ("Cargo.toml", "Rust (Cargo)"),
    ("go.mod", "Go Modules"),
    ("pom.xml", "Maven (Java)"),
    ("build.gradle", "Gradle"),
    ("build.gradle.kts", "Gradle (Kotlin DSL)"),
    ("Gemfile", "Ruby (Bundler)"),
    ("composer.json", "PHP (Composer)"),
    ("pubspec.yaml", "Flutter/Dart"),
    ("mix.exs", "Elixir (Mix)"),
    ("rebar.config", "Erlang (Rebar)"),
    ("project.clj", "Clojure (Leiningen)"),
    ("stack.yaml", "Haskell (Stack)"),
    ("cabal.project", "Haskell (Cabal)"),
    ("flake.nix", "Nix"),
    ("Makefile", "Make"),
    ("CMakeLists.txt", "CMake"),
    ("terraform.tfstate", "Terraform"),
    ("main.tf", "Terraform"),
    ("serverless.yml", "Serverless Framework"),
    ("serverless.yaml", "Serverless Framework"),
    ("amplify.yml", "AWS Amplify"),
    ("vercel.json", "Vercel"),
    ("netlify.toml", "Netlify"),
    ("fly.toml", "Fly.io"),
    ("helm/Chart.yaml", "Helm"),
    ("Chart.yaml", "Helm"),
    ("docker-compose.yml", "Docker Compose"),
    ("docker-compose.yaml", "Docker Compose"),
    ("Dockerfile", "Docker"),
    (".devcontainer/devcontainer.json", "Dev Container"),
    ("devcontainer.json", "Dev Container"),
]

# Dep manifest → package manager name
MANIFEST_TO_PM: dict[str, str] = {
    "package.json": "npm/yarn/pnpm",
    "pyproject.toml": "pip/poetry/uv",
    "requirements.txt": "pip",
    "Pipfile": "pipenv",
    "go.mod": "Go modules",
    "Cargo.toml": "Cargo",
    "Gemfile": "Bundler",
    "composer.json": "Composer",
    "pom.xml": "Maven",
    "build.gradle": "Gradle",
    "build.gradle.kts": "Gradle",
    "pubspec.yaml": "pub (Dart)",
    "mix.exs": "Mix (Elixir)",
    "Package.swift": "Swift Package Manager",
}

LOCKFILE_PATTERNS = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "uv.lock",
    "go.sum", "Cargo.lock", "Gemfile.lock",
    "composer.lock", "packages.lock.json", "gradle.lockfile",
    "pubspec.lock",
]

# CI config patterns
CI_CONFIGS: list[tuple[str, str]] = [
    (".github/workflows", "GitHub Actions"),
    (".circleci/config.yml", "CircleCI"),
    (".gitlab-ci.yml", "GitLab CI"),
    ("Jenkinsfile", "Jenkins"),
    ("azure-pipelines.yml", "Azure Pipelines"),
    (".travis.yml", "Travis CI"),
    ("bitbucket-pipelines.yml", "Bitbucket Pipelines"),
    (".buildkite/pipeline.yml", "Buildkite"),
    ("cloudbuild.yaml", "GCP Cloud Build"),
    (".woodpecker.yml", "Woodpecker CI"),
    (".drone.yml", "Drone CI"),
]

# Linting/formatting configs
LINT_CONFIGS = {
    ".eslintrc.js": "ESLint", ".eslintrc.cjs": "ESLint", ".eslintrc.ts": "ESLint",
    ".eslintrc.json": "ESLint", ".eslintrc.yaml": "ESLint", "eslint.config.js": "ESLint",
    "eslint.config.mjs": "ESLint", "eslint.config.ts": "ESLint",
    ".prettierrc": "Prettier", ".prettierrc.js": "Prettier", ".prettierrc.json": "Prettier",
    ".prettierrc.yaml": "Prettier",
    "ruff.toml": "Ruff", ".ruff.toml": "Ruff",
    ".pylintrc": "Pylint", "pylintrc": "Pylint",
    "pyproject.toml": "Ruff/Black (check content)",  # may contain [tool.ruff] etc.
    ".flake8": "Flake8", "setup.cfg": "Flake8 (check content)",
    "golangci-lint.yml": "golangci-lint", ".golangci.yml": "golangci-lint",
    ".golangci.yaml": "golangci-lint",
    ".rubocop.yml": "RuboCop",
    "phpcs.xml": "PHP_CodeSniffer",
    ".editorconfig": "EditorConfig",
    "biome.json": "Biome", "biome.jsonc": "Biome",
    ".oxlintrc": "Oxlint",
}

# Test framework detection (by dep name and config file)
TEST_FRAMEWORK_SIGNALS = {
    "vitest": "vitest", "jest": "jest", "mocha": "mocha", "jasmine": "jasmine",
    "ava": "ava", "tape": "tape", "qunit": "qunit", "cypress": "Cypress",
    "playwright": "Playwright", "@playwright/test": "Playwright",
    "pytest": "pytest", "unittest": "unittest (stdlib)", "nose2": "nose2",
    "rspec": "RSpec", "minitest": "Minitest",
    "go test": "go test", "testing": "go test",
    "JUnit": "JUnit", "TestNG": "TestNG",
    "RustTest": "rust built-in tests",
    "PHPUnit": "PHPUnit",
    "NUnit": "NUnit", "xUnit": "xUnit",
}

# No credential scanning happens anywhere in this file. Grepping a repository for its own
# secrets is not a task-yield signal, it is a liability: measure-ext runs on the vendor's own
# machine, and a tool that reads their source looking for their credentials has no defensible
# reason to exist. `hardcoded_secret_hits` and `secret_hit_details` are retained as schema keys
# (null / empty) so existing consumers keep working -- see analyze_hygiene().

# Observability signal patterns (grep in source)
LOGGING_FRAMEWORKS = {
    "js": ["pino", "winston", "bunyan", "log4js", "loglevel", "@nestjs/common"],
    "py": ["structlog", "loguru", "logging.getLogger", "logzero"],
    "go": ["zerolog", "zap", "logrus", "slog"],
    "java": ["slf4j", "log4j", "logback"],
    "ruby": ["Rails.logger", "Logger.new"],
}

ERROR_TRACKING_LIBS = ["@sentry/", "sentry-sdk", "sentry_sdk", "rollbar", "honeybadger", "bugsnag", "airbrake"]

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def count_lines(path: Path, max_lines: int = 50_000) -> int:
    """Count non-empty lines in a file. Returns 0 on binary/error."""
    try:
        count = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    return max_lines
                if line.strip():
                    count += 1
        return count
    except (OSError, PermissionError):
        return 0


def is_generated(path: Path) -> bool:
    """Heuristic: is this a generated file?"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(500)
        return any(p.search(head) for p in GENERATED_PATTERNS)
    except (OSError, PermissionError):
        return False


def is_test_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in TEST_PATTERNS)


def is_fixture_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in FIXTURE_PATTERNS)


def should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".") and name not in {".github", ".gitlab", ".circleci", ".devcontainer"}


def walk_source_files(root: Path) -> list[tuple[Path, str]]:
    """Yield (abs_path, rel_path) for every non-skipped source file. rel_path uses
    forward slashes on every OS (via as_posix) because the test/fixture/source
    regexes throughout this module assume `/` separators — without this, layouts like
    `tests\\foo_test.py` would not match on Windows."""
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
        for fname in filenames:
            abs_path = Path(dirpath) / fname
            rel_path = abs_path.relative_to(root).as_posix()
            results.append((abs_path, rel_path))
    return results


def grep_in_file(path: Path, patterns: list[re.Pattern]) -> list[str]:
    """Return lines matching any of the patterns."""
    hits = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if any(p.search(line) for p in patterns):
                    hits.append(line.rstrip())
    except (OSError, PermissionError):
        pass
    return hits


def file_exists(root: Path, *parts: str) -> bool:
    return (root / Path(*parts)).exists()


def read_file_safe(path: Path, max_bytes: int = 16_384) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(max_bytes)
    except (OSError, PermissionError):
        return ""


# No Groovy: LANGUAGE_BY_EXT has no Groovy entry, so a .groovy file is never counted in
# loc_by_language and naming it here would imply a measurement we do not make.
JVM_DOTNET_LANGUAGES = ("Java", "Kotlin", "Scala", "C#")


def jvm_dotnet_loc_share(loc_by_language: dict[str, int], total_loc: int) -> float | None:
    """Share of counted LOC written in the JVM/.NET family, 0.0-1.0.

    A share rather than a per-language key because a rubric gate cannot read
    `loc_by_language.Java`: an absent language key resolves as UNMEASURED rather than zero, so
    every repository without Java would land in a PARTIAL gate verdict and the scores would stop
    being comparable. This scalar is always emitted, so it has no such hole.

    None only when nothing was counted at all -- a repository with no source is a question for
    the zero_code_files screen, not something to report a 0.0 share for.
    """
    if not total_loc:
        return None
    family = sum(int(loc_by_language.get(lang) or 0) for lang in JVM_DOTNET_LANGUAGES)
    return round(family / total_loc, 4)


# ---------------------------------------------------------------------------
# Core analysis functions
# ---------------------------------------------------------------------------

def analyze_languages(all_files: list[tuple[Path, str]]) -> dict:
    loc_by_lang: dict[str, int] = defaultdict(int)
    file_count_by_lang: dict[str, int] = defaultdict(int)

    for abs_path, rel_path in all_files:
        ext = abs_path.suffix.lower()
        if ext not in CODE_EXTENSIONS:
            continue
        if is_generated(abs_path):
            continue
        lang = LANGUAGE_BY_EXT.get(ext, "Other")
        lines = count_lines(abs_path)
        loc_by_lang[lang] += lines
        file_count_by_lang[lang] += 1

    total_loc = sum(loc_by_lang.values())
    sorted_langs = sorted(loc_by_lang.items(), key=lambda x: x[1], reverse=True)

    primary = sorted_langs[0][0] if sorted_langs else "Unknown"
    secondary = [lang for lang, _ in sorted_langs[1:4]] if len(sorted_langs) > 1 else []

    return {
        "primary_language": primary,
        "secondary_languages": secondary,
        "total_loc": total_loc,
        "loc_by_language": dict(sorted_langs),
        "file_count_by_language": dict(file_count_by_lang),
    }


def analyze_file_sizes(
    all_files: list[tuple[Path, str]],
    top_n: int = 10,
    extra_files: list[tuple[int, str]] | None = None,
) -> dict:
    """File-size distribution over source files. `extra_files` is a list of
    already-counted (loc, rel_path) entries (e.g. IaC manifests from analyze_iac)
    to fold into the same distribution so infra repos aren't reported as 0 files."""
    sizes: list[tuple[int, str]] = []
    generated_excluded = 0

    for abs_path, rel_path in all_files:
        ext = abs_path.suffix.lower()
        if ext not in CODE_EXTENSIONS:
            continue
        if is_generated(abs_path):
            generated_excluded += 1
            continue
        loc = count_lines(abs_path)
        if loc > 0:
            sizes.append((loc, rel_path))

    if extra_files:
        sizes.extend((loc, rel) for loc, rel in extra_files if loc > 0)

    if not sizes:
        return {"total_source_files": 0, "total_non_test_source_files": 0,
                "median_loc": 0, "p90_loc": 0,
                "largest_files": [], "generated_excluded": 0}

    sizes.sort(key=lambda x: x[0], reverse=True)
    locs = sorted([s[0] for s in sizes])
    n = len(locs)
    median = locs[n // 2]
    p90 = locs[int(n * 0.9)]

    return {
        "total_source_files": n,
        # Non-test source files (same inclusion criteria as total_source_files,
        # minus test/spec paths) — used for an accurate test:source ratio.
        "total_non_test_source_files": sum(1 for _, r in sizes if not is_test_file(r)),
        "median_loc": median,
        "p90_loc": p90,
        "largest_files": [{"path": p, "loc": l} for l, p in sizes[:top_n]],
        "god_files_over_500": sum(1 for l, _ in sizes if l > 500),
        "god_files_over_1000": sum(1 for l, _ in sizes if l > 1000),
        "generated_excluded": generated_excluded,
    }


def _is_ci_config_path(rel_path: str) -> bool:
    low = rel_path.lower()
    return any(low.startswith(p) or ("/" + p) in ("/" + low) for p in _CI_DIR_PREFIXES)


# Kubernetes `kind:` values that mark a real cluster manifest (guards against a
# random YAML that merely happens to contain the words apiVersion/kind).
_K8S_KINDS = {
    "pod", "deployment", "service", "statefulset", "daemonset", "replicaset",
    "replicationcontroller", "job", "cronjob", "configmap", "secret", "ingress",
    "ingressclass", "persistentvolume", "persistentvolumeclaim", "storageclass",
    "namespace", "serviceaccount", "role", "rolebinding", "clusterrole",
    "clusterrolebinding", "horizontalpodautoscaler", "verticalpodautoscaler",
    "networkpolicy", "poddisruptionbudget", "customresourcedefinition",
    "endpoints", "endpointslice", "limitrange", "resourcequota", "list",
    "kustomization", "helmrelease", "gitrepository", "kustomize",
    "deploymentconfig", "route", "rollout", "sealedsecret", "certificate",
}


def _detect_iac_type(abs_path: Path, rel_path: str, chart_dirs: set[str]) -> str | None:
    """Return an IaC category for a file, or None. Positive identification only."""
    name = abs_path.name.lower()
    ext = abs_path.suffix.lower()

    if name == "dockerfile" or name.startswith("dockerfile.") or name.endswith(".dockerfile"):
        return "Dockerfile"
    if ext in IAC_HCL_EXTENSIONS:
        return "Terraform"
    if ext not in IAC_YAML_EXTENSIONS and ext != ".json":
        return None
    if _is_ci_config_path(rel_path):
        return None

    head = read_file_safe(abs_path, max_bytes=_IAC_SNIFF_BYTES)
    low = head.lower()

    if ext == ".json":
        if "awstemplateformatversion" in low or '"aws::' in low:
            return "CloudFormation"
        return None

    # ---- YAML ----
    parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
    # Chart.yaml is definitive Helm. Otherwise a file is Helm only when it is tied to a
    # SPECIFIC chart dir (a dir that holds a Chart.yaml — "" means the repo root):
    #   - a values*.yaml sitting directly IN a chart dir (sibling of Chart.yaml), or
    #   - a templates/ file whose enclosing chart dir (the part before "/templates/")
    #     is a chart dir.
    # This must NOT treat a root Chart.yaml as covering every nested templates//values
    # in the tree (that over-classified unrelated dirs as Helm).
    if name in ("chart.yaml", "chart.yml"):
        return "Helm"
    if name.startswith("values") and parent in chart_dirs:
        return "Helm"
    slashed = "/" + rel_path
    if "/templates/" in slashed:
        chart_part = slashed.split("/templates/", 1)[0].lstrip("/")  # "" if top-level templates/
        if chart_part in chart_dirs:
            return "Helm"

    # Canonical compose filenames only (docker-compose*.y*ml, compose.y*ml,
    # compose.<override>.y*ml). NOT a bare `compose*` prefix — that false-matched
    # composer.yaml / composition.yml. Oddly-named real compose files are still
    # caught by the content check (top-level services: + image/build) below.
    if name.startswith("docker-compose") or name.startswith("compose."):
        return "Docker Compose"

    if "apiversion:" in low and "kind:" in low:
        # Confirm the declared kind is a real cluster object.
        for line in head.splitlines():
            s = line.strip().lower()
            if s.startswith("kind:"):
                kind = s.split(":", 1)[1].strip().strip('"\'')
                if kind in _K8S_KINDS:
                    return "Kubernetes"
                break
        # apiVersion+kind but unrecognized kind: still likely a manifest/CRD.
        return "Kubernetes"

    if "awstemplateformatversion" in low or ("resources:" in low and "aws::" in low):
        return "CloudFormation"

    # docker-compose without the conventional name: top-level services: + image/build.
    if head.startswith("services:") or "\nservices:" in head:
        if "image:" in low or "build:" in low or "container_name:" in low:
            return "Docker Compose"

    # Ansible: require CONTENT markers, not just a dir name — `tasks/`/`handlers/`
    # are common in non-Ansible repos and a bare path match wrongly inflated iac_loc.
    #  - strong: playbook `hosts:`, or unmistakable ansible-isms anywhere in the head;
    #  - or a role/playbook PATH corroborated by a task-list shape (`- name:`/`tasks:`).
    slashed = "/" + rel_path
    ansible_path = "/roles/" in slashed or "/playbooks/" in slashed or parent.endswith("playbooks")
    strong = (
        head.startswith("hosts:") or "\nhosts:" in head
        or "ansible.builtin." in low or "\nbecome:" in low
        or "gather_facts:" in low or "\nvars_files:" in low
    )
    if strong or (ansible_path and ("\n- name:" in head or head.startswith("- name:") or "\ntasks:" in low)):
        return "Ansible"

    return None


def analyze_iac(all_files: list[tuple[Path, str]], root: Path) -> dict:
    """Detect Infrastructure-as-Code files, count their LOC, and expose per-type
    signals. IaC LOC is otherwise invisible (YAML/HCL are not CODE_EXTENSIONS),
    which is why an infra repo of pure manifests would report 0 LOC."""
    # Dirs that contain a Chart.yaml — used to attribute templates/*.yaml to Helm.
    chart_dirs: set[str] = set()
    for _abs, rel in all_files:
        base = rel.rsplit("/", 1)[-1].lower()
        if base in ("chart.yaml", "chart.yml"):
            chart_dirs.add(rel.rsplit("/", 1)[0] if "/" in rel else "")

    loc_by_type: dict[str, int] = defaultdict(int)
    files_by_type: dict[str, int] = defaultdict(int)
    sized_files: list[tuple[int, str]] = []
    scanned = 0

    for abs_path, rel_path in all_files:
        ext = abs_path.suffix.lower()
        if ext not in IAC_HCL_EXTENSIONS and ext not in IAC_YAML_EXTENSIONS \
                and ext != ".json" and not abs_path.name.lower().startswith("dockerfile") \
                and not abs_path.name.lower().endswith(".dockerfile"):
            continue
        if is_test_file(rel_path) or is_fixture_file(rel_path) or is_generated(abs_path):
            continue
        # Bound YAML/JSON content sniffing so a pathological tree stays fast. Skip only
        # the over-cap YAML/JSON candidates (they need a content read to identify) — do
        # NOT break, or later cheap-to-detect .tf/.hcl/Dockerfile files would be
        # dropped. The cap is a high backstop (see _IAC_MAX_SNIFF) so real repos never
        # starve genuine manifests past the limit.
        if ext in IAC_YAML_EXTENSIONS or ext == ".json":
            scanned += 1
            if scanned > _IAC_MAX_SNIFF:
                continue
        iac_type = _detect_iac_type(abs_path, rel_path, chart_dirs)
        if not iac_type:
            continue
        loc = count_lines(abs_path)
        if loc <= 0:
            continue
        loc_by_type[iac_type] += loc
        files_by_type[iac_type] += 1
        sized_files.append((loc, rel_path))

    iac_loc = sum(loc_by_type.values())
    iac_file_count = sum(files_by_type.values())
    return {
        "iac_loc": iac_loc,
        "iac_file_count": iac_file_count,
        "iac_loc_by_type": dict(sorted(loc_by_type.items(), key=lambda x: -x[1])),
        "iac_file_count_by_type": dict(files_by_type),
        "_sized_files": sized_files,
        # Signals merged into class_signals (authoritative counts for the classifier).
        "terraform_file_count": files_by_type.get("Terraform", 0),
        "terraform_present": files_by_type.get("Terraform", 0) > 0,
        "terraform_loc": loc_by_type.get("Terraform", 0),
        "k8s_manifest_count": files_by_type.get("Kubernetes", 0),
        "k8s_loc": loc_by_type.get("Kubernetes", 0),
        "docker_compose_file_count": files_by_type.get("Docker Compose", 0),
        "docker_compose_loc": loc_by_type.get("Docker Compose", 0),
        "helm_file_count": files_by_type.get("Helm", 0),
        "helm_loc": loc_by_type.get("Helm", 0),
        "ansible_file_count": files_by_type.get("Ansible", 0),
        "ansible_loc": loc_by_type.get("Ansible", 0),
        "cloudformation_file_count": files_by_type.get("CloudFormation", 0),
        "cloudformation_loc": loc_by_type.get("CloudFormation", 0),
        "dockerfile_loc": loc_by_type.get("Dockerfile", 0),
    }


def analyze_tests(all_files: list[tuple[Path, str]]) -> dict:
    spec_files: list[str] = []
    fixture_files: list[str] = []

    for abs_path, rel_path in all_files:
        if not is_test_file(rel_path):
            continue
        loc = count_lines(abs_path)
        if is_fixture_file(rel_path) or loc > 5000:
            fixture_files.append(rel_path)
        else:
            spec_files.append(rel_path)

    return {
        "spec_files": len(spec_files),
        "fixture_and_snapshot_files": len(fixture_files),
        "total_test_files": len(spec_files) + len(fixture_files),
        "spec_file_paths_sample": spec_files[:20],
    }


def analyze_frameworks(root: Path) -> list[str]:
    detected = []
    for marker, framework in FRAMEWORK_MARKERS:
        if "/" in marker:
            if (root / marker).exists():
                detected.append(framework)
        else:
            # Check at root and one level deep
            if (root / marker).exists():
                detected.append(framework)

    # Python frameworks, from DECLARED DEPENDENCY NAMES rather than from manifest text. The
    # grep this replaces read `requirements.txt` + `pyproject.toml` as one raw string, so a
    # description reading "migrating legacy Flask apps to modern stacks" and a comment reading
    # "# TODO: drop the old fastapi shim" produced `['FastAPI', 'Flask']` and, downstream,
    # `project_type = 'API service'` for a repository whose only dependency is click. This is
    # the same defect the classifier's dep-name parser was written for; reuse it rather than
    # keep a second, worse copy. It also reads Pipfile, setup.py, setup.cfg and environment.yml,
    # which the grep never looked at.
    pypi = set(collect_dependency_names(root).get("pypi", []))
    for name, framework in (("fastapi", "FastAPI"), ("flask", "Flask"),
                            ("sqlalchemy", "SQLAlchemy"), ("pydantic", "Pydantic")):
        if name in pypi:
            detected.append(framework)

    # Check for React (not Next.js/Remix already detected)
    pkg_content = read_file_safe(root / "package.json")
    if pkg_content:
        try:
            pkg = json.loads(pkg_content)
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "react" in all_deps and "Next.js" not in detected and "Remix" not in detected and "Gatsby" not in detected:
                detected.append("React")
            if "express" in all_deps:
                detected.append("Express")
            if "@nestjs/core" in all_deps:
                detected.append("NestJS")
            if "hono" in all_deps:
                detected.append("Hono")
            if "elysia" in all_deps:
                detected.append("Elysia")
            if "koa" in all_deps:
                detected.append("Koa")
            if "fastify" in all_deps:
                detected.append("Fastify")
            if "trpc" in str(all_deps) or "@trpc/server" in all_deps:
                detected.append("tRPC")
            if "prisma" in all_deps or "@prisma/client" in all_deps:
                detected.append("Prisma")
            if "drizzle-orm" in all_deps:
                detected.append("Drizzle ORM")
            if "typeorm" in all_deps:
                detected.append("TypeORM")
            if "zod" in all_deps:
                detected.append("Zod")
            if "tailwindcss" in all_deps:
                detected.append("Tailwind CSS")
        except (json.JSONDecodeError, KeyError):
            pass

    return list(dict.fromkeys(detected))  # deduplicate preserving order


def analyze_dependencies(root: Path) -> dict:
    result: dict = {
        "package_managers": [],
        "manifests_found": [],
        "lockfiles_found": [],
        "lockfiles_expected": [],
        "direct_runtime_deps": 0,
        "direct_dev_deps": 0,
        "total_transitive_deps": 0,
        "dep_update_tooling": "none",
        "external_services_detected": [],
    }

    # npm/yarn/pnpm
    pkg_path = root / "package.json"
    if pkg_path.exists():
        result["manifests_found"].append("package.json")
        result["package_managers"].append("npm/yarn/pnpm")
        result["lockfiles_expected"].append("package-lock.json or yarn.lock or pnpm-lock.yaml")
        try:
            pkg = json.loads(read_file_safe(pkg_path))
            result["direct_runtime_deps"] += len(pkg.get("dependencies", {}))
            result["direct_dev_deps"] += len(pkg.get("devDependencies", {}))
        except (json.JSONDecodeError, ValueError):
            pass

    # Python
    for pymanifest in ["pyproject.toml", "requirements.txt", "Pipfile", "setup.py", "setup.cfg"]:
        if (root / pymanifest).exists():
            result["manifests_found"].append(pymanifest)
            if "pip" not in str(result["package_managers"]):
                result["package_managers"].append("pip/poetry/uv")
            result["lockfiles_expected"].append("poetry.lock / Pipfile.lock / uv.lock / requirements.txt (pinned)")
            break

    # Go
    if (root / "go.mod").exists():
        result["manifests_found"].append("go.mod")
        result["package_managers"].append("Go modules")
        result["lockfiles_expected"].append("go.sum")
        go_mod_content = read_file_safe(root / "go.mod")
        # Count "require" entries
        in_require = False
        req_count = 0
        for line in go_mod_content.splitlines():
            line = line.strip()
            if line.startswith("require ("):
                in_require = True
            elif in_require and line == ")":
                in_require = False
            elif in_require and line and not line.startswith("//"):
                req_count += 1
            elif line.startswith("require ") and not line.startswith("require ("):
                req_count += 1
        result["direct_runtime_deps"] += req_count

    # Rust
    if (root / "Cargo.toml").exists():
        result["manifests_found"].append("Cargo.toml")
        result["package_managers"].append("Cargo")
        result["lockfiles_expected"].append("Cargo.lock")

    # Ruby
    if (root / "Gemfile").exists():
        result["manifests_found"].append("Gemfile")
        result["package_managers"].append("Bundler")
        result["lockfiles_expected"].append("Gemfile.lock")

    # PHP
    if (root / "composer.json").exists():
        result["manifests_found"].append("composer.json")
        result["package_managers"].append("Composer")
        result["lockfiles_expected"].append("composer.lock")

    # Detect lockfiles
    for lf in LOCKFILE_PATTERNS:
        for candidate in root.rglob(lf):
            rel = str(candidate.relative_to(root))
            if "node_modules" not in rel and ".git" not in rel:
                result["lockfiles_found"].append(rel)
                break

    # Count transitive deps from lockfiles
    for lf in result["lockfiles_found"]:
        lf_path = root / lf
        if not lf_path.exists():
            continue
        content = read_file_safe(lf_path, max_bytes=1_000_000)
        if lf.endswith("package-lock.json"):
            try:
                data = json.loads(content)
                # v3 lockfile uses "packages"
                pkgs = data.get("packages", data.get("dependencies", {}))
                result["total_transitive_deps"] += len(pkgs)
            except (json.JSONDecodeError, ValueError):
                pass
        elif lf.endswith("yarn.lock"):
            result["total_transitive_deps"] += content.count("\n\n")
        elif lf.endswith("pnpm-lock.yaml"):
            result["total_transitive_deps"] += content.count("\n  /")
        elif lf.endswith("poetry.lock"):
            result["total_transitive_deps"] += content.count("[[package]]")
        elif lf.endswith("go.sum"):
            result["total_transitive_deps"] += content.count("\n") // 2
        elif lf.endswith("Cargo.lock"):
            result["total_transitive_deps"] += content.count("[[package]]")
        elif lf.endswith("Gemfile.lock"):
            # GEM specs section
            in_specs = False
            for line in content.splitlines():
                if line.strip() == "specs:":
                    in_specs = True
                elif in_specs and line.startswith("  ") and not line.startswith("    "):
                    result["total_transitive_deps"] += 1

    # Dep update tooling
    if (root / ".github/dependabot.yml").exists() or (root / ".github/dependabot.yaml").exists():
        result["dep_update_tooling"] = "Dependabot"
    elif (root / "renovate.json").exists() or (root / ".renovaterc").exists() or (root / ".renovaterc.json").exists():
        result["dep_update_tooling"] = "Renovate"

    return result


# --- CI: what the pipeline RUNS, read off the parsed config ------------------
#
# What this replaces: one regex, `\btest\b|\bvitest\b|\bjest\b|\bpytest\b|\bgo test\b|\brspec\b`,
# applied three times to the raw text of every CI file. It was wrong in both directions and the
# false positives were the dangerous ones, because a repository with no suite scored as if it
# had one. Verified on eleven realistic workflow bodies: false NEGATIVE on `./gradlew build`,
# `mvn -B verify`, `tox -e py312`, `nox -s unit`, `make check` and `cargo nextest run`, none of
# which contain the word "test" as a token; false POSITIVE on a lint-only job merely NAMED
# "test", and on a `pytest` step guarded by `if: false`, which by definition never runs.
#
# So: parse the document, walk to the shell commands, drop the subtrees that are switched off,
# follow the indirections into the repository's OWN Makefile / package.json / scripts, and match
# each command against a table of test runners.

# Keys whose value is a shell command (or a list of them) across the CI systems in CI_CONFIGS:
# GitHub Actions and CircleCI use `run`, GitLab/Travis/Bitbucket/Azure use `script`,
# Drone/Woodpecker use `commands`, Cloud Build uses `args` under a step's `entrypoint`.
_CI_COMMAND_KEYS = frozenset({
    "run", "script", "before_script", "after_script", "commands", "command",
})
# A guard that is literally off. GitHub Actions expressions that depend on the event are not
# evaluated here -- only a constant false is, because only a constant is knowable statically.
_CI_DISABLED = frozenset({"false", "${{ false }}", "${{false}}", "0", "no", "off"})
# Prefixes that wrap a command without being one: runners, elevation, and env assignments.
_CI_WRAPPER = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:sudo\s+|time\s+|nice\s+|exec\s+|xvfb-run(?:\s+-\S+)*\s+|env\s+(?:\S+=\S*\s+)*|"
    r"(?:poetry|pipenv|uv|hatch|pdm|rye)\s+run\s+(?:--\S+\s+)*|"
    r"bundle\s+exec\s+|npx\s+(?:--\S+\s+)*|pnpm\s+(?:exec|dlx)\s+|yarn\s+dlx\s+)*",
    re.I,
)
# `mvn -DskipTests package` and `gradlew build -x test` build without testing. The word "test"
# is present in both; the suite is not run in either.
_CI_TESTS_SKIPPED = re.compile(r"-DskipTests|-Dmaven\.test\.skip|(^|\s)-x\s+test\b|--no-run\b",
                               re.I)
_CI_TEST_COMMAND = re.compile(
    r"^(?:"
    r"pytest|py\.test|tox|nox|behave|robot|"
    r"python[0-9.]*\s+-m\s+(?:pytest|unittest|nose2|tox|nox)|"
    r"python[0-9.]*\s+setup\.py\s+test|"
    r"jest|vitest|mocha|ava|karma|jasmine|tap|"
    r"(?:npm|yarn|pnpm|bun)\s+(?:-\S+\s+)*(?:run\s+)?(?:test|tests)\b|"
    r"go\s+test|gotestsum|"
    r"cargo\s+(?:test|nextest\s+run)|"
    r"(?:\./)?gradlew?(?:\.bat)?\s+.*\b(?:test|check|build)\b|"
    r"(?:\./)?mvnw?\s+.*\b(?:test|verify|install)\b|mvn\s+.*\b(?:test|verify|install)\b|"
    r"make\s+.*\b(?:test|tests|check|coverage)\b|"
    r"just\s+.*\b(?:test|check)\b|"
    r"rspec|rake\s+(?:test|spec)|minitest|"
    r"phpunit|\S*vendor/bin/phpunit|composer\s+(?:run-script\s+)?test|"
    r"dotnet\s+test|ctest|bazel\s+test|ninja\s+test|swift\s+test|"
    r"playwright\s+test|cypress\s+run|"
    r"coverage\s+run|pytest-\S+"
    r")\b",
    re.I,
)
_CI_LINT_COMMAND = re.compile(
    r"^(?:eslint|pylint|ruff|flake8|golangci-lint|clippy|cargo\s+clippy|rubocop|"
    r"black|isort|prettier|shellcheck|stylelint|standardrb|credo|swiftlint|ktlint|"
    r"(?:npm|yarn|pnpm|bun)\s+(?:-\S+\s+)*(?:run\s+)?lint|"
    r"pre-commit\s+run|make\s+.*\blint\b|tox\s+.*\blint\b|nox\s+.*\blint\b)\b",
    re.I,
)
_CI_TYPECHECK_COMMAND = re.compile(
    r"^(?:tsc|mypy|pyright|pyre|flow\s+check|"
    r"(?:npm|yarn|pnpm|bun)\s+(?:-\S+\s+)*(?:run\s+)?type[-:]?check|"
    r"make\s+.*\btype.?check\b|tox\s+.*\b(?:mypy|type)\b|"
    r"python[0-9.]*\s+-m\s+(?:mypy|pyright|pyre|ty)\b)\b",
    re.I,
)
# Deploy stays on the text search on purpose: deployment shows up as a job name, an
# `environment:` block or a marketplace action, not as a command with a recognisable verb, so a
# command table would report a false negative on most real pipelines. It is unconsumed by every
# shipped rubric; see the audit note recommending its removal rather than its repair.
_CI_DEPLOY_TEXT = re.compile(r"\bdeploy\b|\brelease\b|\bpublish\b|\bpush.*ecr\b|"
                             r"\bpush.*registry\b", re.I)
# A GitHub Actions expression substitution, `${{ env.CARGO }}`.
_GHA_EXPR = re.compile(r"\$\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")
# Jenkinsfiles are Groovy, not YAML. Their shell steps are `sh 'cmd'` / `sh "cmd"` / `sh '''…'''`.
_JENKINS_SH = re.compile(r"\b(?:sh|bat|powershell)\s*(?:\(\s*)?"
                         r"(?:'''(.*?)'''|\"\"\"(.*?)\"\"\"|'([^']*)'|\"([^\"]*)\")",
                         re.S)
# Marketplace actions that ARE the check, with no `run:` line to read. Observed on
# gin-gonic/gin, whose entire lint job is `uses: golangci/golangci-lint-action@v9`.
_CI_USES_LINT = re.compile(r"^(?:golangci/golangci-lint-action|reviewdog/action-|"
                           r"github/super-linter|super-linter/super-linter|psf/black|"
                           r"astral-sh/ruff-action|chartboost/ruff-action|pre-commit/action|"
                           r"rubocop/|wearerequired/lint-action)", re.I)
_CI_USES_TYPECHECK = re.compile(r"^(?:jakebailey/pyright-action|tsuyoshicho/action-mypy|"
                                r"jpetrucciani/mypy-check)", re.I)

# Indirections a CI line delegates to. Following them reads the repository's own files, which
# is an observation; guessing what `make ci` does is not. Two levels deep is enough for the
# shapes seen in the wild (`make ci` -> `pytest`; `bash scripts/test-cov.sh` ->
# `bash scripts/test.sh` -> `pytest`) and terminates without needing a cycle check.
_CI_INDIRECTION_DEPTH = 2
_MAKE_CALL = re.compile(r"^make(?:\s+-\S+|\s+\S+=\S*)*\s+([A-Za-z0-9_./-]+)", re.I)
_NPM_SCRIPT_CALL = re.compile(r"^(?:npm|yarn|pnpm|bun)\s+(?:-\S+\s+)*(?:run(?:-script)?\s+)?"
                              r"([A-Za-z0-9:_-]+)", re.I)
_SCRIPT_CALL = re.compile(r"^(?:(?:ba|z|k|da)?sh\s+(?:-\S+\s+)*)?"
                          r"(\.?/?[A-Za-z0-9_./-]+\.(?:sh|bash))\b")


def _make_recipe(root: Path, target: str) -> list[str]:
    """The recipe lines of one Makefile target, read from the repository's own Makefile."""
    for name in ("Makefile", "makefile", "GNUmakefile"):
        text = read_file_safe(root / name, max_bytes=262_144)
        if not text:
            continue
        lines: list[str] = []
        collecting = False
        for line in text.splitlines():
            if re.match(rf"^{re.escape(target)}\s*:(?!=)", line):
                collecting = True
                continue
            if collecting:
                if line.startswith(("\t", " " * 4)):
                    lines.append(line.strip().lstrip("@-+").strip())
                elif line.strip() and not line.startswith("#"):
                    break
        if lines:
            return lines
    return []


def _npm_script_body(root: Path, script: str) -> list[str]:
    """The body of one package.json script, so `npm run test` is read rather than assumed."""
    try:
        scripts = json.loads(read_file_safe(root / "package.json")).get("scripts") or {}
    except (json.JSONDecodeError, ValueError, AttributeError):
        return []
    body = scripts.get(script)
    return [body] if isinstance(body, str) else []


def _ci_indirection(root: Path, command: str) -> list[str]:
    """What this command delegates to, resolved against files in this repository."""
    match = _MAKE_CALL.match(command)
    if match:
        return _make_recipe(root, match.group(1))
    match = _NPM_SCRIPT_CALL.match(command)
    if match:
        return _npm_script_body(root, match.group(1))
    match = _SCRIPT_CALL.match(command)
    if match:
        candidate = (root / match.group(1).lstrip("./")).resolve()
        if root.resolve() in candidate.parents and candidate.is_file():
            return read_file_safe(candidate, max_bytes=131_072).splitlines()
    return []


def _expand_indirections(root: Path, commands: list[str], depth: int) -> list[str]:
    """Replace `make ci` / `npm run test` / `bash scripts/test.sh` with what they actually run.

    The body REPLACES the surface command whenever it settles the question, so an `npm test`
    script whose body is `eslint .` is a lint step and not a test step. When the body settles
    nothing -- a Makefile recipe of `$(PYTHON) -m pytest`, say, where the runner hides behind a
    variable -- the surface command is kept as well, because dropping it would turn a true
    positive into a false negative.
    """
    if depth <= 0:
        return commands
    expanded: list[str] = []
    for command in commands:
        body = _expand_indirections(root, _split_commands(_ci_indirection(root, command)),
                                    depth - 1)
        if body and any(pattern.match(line) for line in body
                        for pattern in (_CI_TEST_COMMAND, _CI_LINT_COMMAND,
                                        _CI_TYPECHECK_COMMAND)):
            expanded.extend(body)
        else:
            expanded.append(command)
            expanded.extend(body)
    return expanded


def _ci_disabled(guard) -> bool:
    """Is this `if:` a constant false? Only a constant can be settled without running CI."""
    if guard is False:
        return True
    return isinstance(guard, str) and guard.strip().lower() in _CI_DISABLED


def _command_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _command_strings(item)]
    if isinstance(value, dict):
        # CircleCI's expanded form: `run: {name: …, command: …}`.
        return _command_strings(value.get("command"))
    return []


def _yaml_env(node, found: dict[str, str]) -> None:
    """Every `env:` mapping declared in the document, flattened.

    A workflow that runs `${{ env.CARGO }} test` has told us what CARGO is a few lines up;
    reading it is observation. Not reading it is why BurntSushi/ripgrep, whose entire test
    matrix is `${{ env.CARGO }} test --verbose --workspace`, looked like it ran no tests.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "env" and isinstance(value, dict):
                found.update({str(k): str(v) for k, v in value.items()
                              if isinstance(v, (str, int, float, bool))})
            else:
                _yaml_env(value, found)
    elif isinstance(node, list):
        for item in node:
            _yaml_env(item, found)


def _substitute(command: str, env: dict[str, str]) -> str:
    """Resolve `${{ env.NAME }}` against the document's own env; drop what we cannot resolve."""
    def replace(match: re.Match) -> str:
        key = match.group(1)
        name = key.split(".", 1)[1] if key.lower().startswith("env.") else key
        return env.get(name, "")
    return _GHA_EXPR.sub(replace, command)


def _yaml_steps(node, commands: list[str], actions: list[str]) -> None:
    """Every shell command and marketplace action reachable in a parsed CI document.

    Subtrees whose own `if:`/`when:` is a constant false are skipped: a step guarded by
    `if: false` does not run, and counting it is how a switched-off suite read as a live one.
    """
    if isinstance(node, dict):
        if _ci_disabled(node.get("if")) or _ci_disabled(node.get("when")):
            return
        for key, value in node.items():
            if not isinstance(key, str):
                _yaml_steps(value, commands, actions)
            elif key.lower() in _CI_COMMAND_KEYS:
                commands.extend(_command_strings(value))
            elif key.lower() == "uses" and isinstance(value, str):
                actions.append(value)
            else:
                _yaml_steps(value, commands, actions)
    elif isinstance(node, list):
        for item in node:
            _yaml_steps(item, commands, actions)


def _split_commands(blocks: list[str], env: dict[str, str] | None = None) -> list[str]:
    lines: list[str] = []
    for block in blocks:
        for raw in re.split(r"[\n;]|&&|\|\|", block):
            line = _substitute(raw, env).strip() if env else raw.strip()
            line = _CI_WRAPPER.sub("", line, count=1).strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def _ci_steps(rel_path: str, content: str) -> tuple[list[str], list[str]] | None:
    """(commands, actions) for one CI file, or None if the file could not be read."""
    blocks: list[str] = []
    actions: list[str] = []
    env: dict[str, str] = {}
    if rel_path.split("/")[-1].lower().startswith("jenkinsfile"):
        blocks = ["".join(g for g in m.groups() if g) for m in _JENKINS_SH.finditer(content)]
    elif yaml is None:
        return None
    else:
        try:
            document = yaml.safe_load(content)
        except yaml.YAMLError:
            return None
        if document is not None:
            _yaml_steps(document, blocks, actions)
            _yaml_env(document, env)
    return _split_commands(blocks, env), actions


def analyze_ci(root: Path) -> dict:
    ci_systems: list[str] = []
    ci_configs: list[str] = []
    configs: list[tuple[str, str]] = []          # (relative path, content)

    def add_config(path: Path) -> None:
        rel = str(path.relative_to(root))
        if rel not in ci_configs:
            ci_configs.append(rel)
            configs.append((rel, read_file_safe(path)))

    for config_path, system in CI_CONFIGS:
        full = root / config_path
        if config_path.endswith("/"):            # directory
            if full.is_dir():
                ci_systems.append(system)
                for f in sorted(full.glob("*.yml")):
                    add_config(f)
        elif full.is_file():
            # Directory entries (e.g. ".github/workflows") must NOT be read as a file — they
            # are handled by the dedicated GitHub Actions block below.
            ci_systems.append(system)
            add_config(full)

    gha_dir = root / ".github" / "workflows"
    if gha_dir.is_dir():
        if "GitHub Actions" not in ci_systems:
            ci_systems.append("GitHub Actions")
        for f in sorted(list(gha_dir.glob("*.yml")) + list(gha_dir.glob("*.yaml"))):
            add_config(f)

    commands: list[str] = []
    actions: list[str] = []
    parsed = unreadable = 0
    for rel, content in configs:
        steps = _ci_steps(rel, content)
        if steps is None:
            unreadable += 1
            continue
        parsed += 1
        commands.extend(steps[0])
        actions.extend(steps[1])

    # `make ci`, `npm run test:unit` and `bash scripts/test.sh` say nothing on their own; the
    # repository's Makefile, package.json and scripts say what they do. Follow them.
    commands = _expand_indirections(root, commands, _CI_INDIRECTION_DEPTH)

    def runs(pattern: re.Pattern, uses: re.Pattern | None = None,
             commands: list[str] = commands) -> bool | None:
        # A file we could not parse is not evidence of absence. If nothing parsed, say so.
        if not parsed and unreadable:
            return None
        return (any(pattern.match(c) for c in commands)
                or bool(uses and any(uses.match(a) for a in actions)))

    # A build that explicitly skips the suite is not a test run, whatever it is called.
    executed = [c for c in commands if not _CI_TESTS_SKIPPED.search(c)]

    return {
        "ci_systems": list(dict.fromkeys(ci_systems)),
        "ci_configs": ci_configs,
        "runs_tests": runs(_CI_TEST_COMMAND, commands=executed),
        "runs_lint": runs(_CI_LINT_COMMAND, _CI_USES_LINT),
        "runs_typecheck": runs(_CI_TYPECHECK_COMMAND, _CI_USES_TYPECHECK),
        "has_deploy_pipeline": any(_CI_DEPLOY_TEXT.search(text) for _, text in configs),
        "ci_present": len(ci_systems) > 0,
        "ci_analysis_method": ("no_ci" if not configs
                               else "parsed" if parsed
                               else "parser_unavailable"),
    }


def analyze_hygiene(root: Path, source_files: list[tuple[Path, str]]) -> dict:
    """Dependency-audit and input-validation hygiene. Reads no credentials and no .env file.

    This deliberately does NOT scan for secrets, does not open `.env` files, and does not record
    which paths hold them. The two secret keys are kept in the returned dict so the emitted schema
    is unchanged for existing consumers, and are permanently null/empty: `None` means "not
    collected by policy", never "measured zero".

    What remains is genuine, non-sensitive repository hygiene:
      * `dep_audit_in_ci` -- does CI run a dependency auditor (bool, from CI config text).
      * `input_validation_patterns` -- which validation LIBRARIES appear (closed vocabulary of
        public package names, not repo-derived symbols).
    """
    dep_audit_in_ci = False
    input_validation_patterns: list[str] = []

    scan_files = sorted(
        ((p, r) for p, r in source_files
         if p.suffix.lower() in CODE_EXTENSIONS
         and not is_test_file(r)
         and ".env" not in r),
        key=lambda pr: pr[1],
    )[:500]

    # Check dep audit in CI
    for config_path, _ in CI_CONFIGS:
        full = root / config_path
        if full.exists():
            content = read_file_safe(full)
            if re.search(r'\baudit\b|\bsnyk\b|\btrivy\b|\bgrype\b|\bsafety\b|\bpip.audit\b', content, re.I):
                dep_audit_in_ci = True
                break
    # Also check .github/workflows (both .yml and .yaml; same full pattern set as above)
    gha = root / ".github" / "workflows"
    if gha.is_dir():
        for f in list(gha.glob("*.yml")) + list(gha.glob("*.yaml")):
            if re.search(r'\baudit\b|\bsnyk\b|\btrivy\b|\bgrype\b|\bsafety\b|\bpip.audit\b', read_file_safe(f), re.I):
                dep_audit_in_ci = True

    # Input validation signals -- public library names only.
    all_code = []
    for abs_path, rel_path in scan_files[:100]:
        content = read_file_safe(abs_path, max_bytes=4096)
        all_code.append(content)
    combined = "\n".join(all_code)
    for probe, name in (
        (r'\bzod\b|z\.object|z\.string', "zod"),
        (r'\bpydantic\b|BaseModel', "pydantic"),
        (r'\bjoi\b|Joi\.object', "joi"),
        (r'\byup\b', "yup"),
        (r'\bclass-validator\b|@IsString|@IsNotEmpty', "class-validator"),
        (r'\bvalibot\b', "valibot"),
        (r'\bjsonschema\b|jsonschema\.validate', "jsonschema"),
    ):
        if re.search(probe, combined):
            input_validation_patterns.append(name)

    return {
        # Retained schema keys, permanently not collected. null != 0.
        "hardcoded_secret_hits": None,
        "secret_hit_details": [],
        "env_files_committed": [],
        "dep_audit_in_ci": dep_audit_in_ci,
        "input_validation_patterns": input_validation_patterns,
    }


def analyze_lint_config(root: Path) -> dict:
    detected: dict[str, str] = {}
    for filename, tool in LINT_CONFIGS.items():
        if (root / filename).exists():
            # For pyproject.toml, check if it actually contains lint config
            if filename == "pyproject.toml":
                content = read_file_safe(root / filename)
                for linter in ["ruff", "black", "pylint", "flake8", "mypy", "pyright"]:
                    if f"[tool.{linter}]" in content:
                        detected[linter] = filename
            elif filename == "setup.cfg":
                content = read_file_safe(root / filename)
                if "[flake8]" in content:
                    detected["flake8"] = filename
            else:
                detected[tool] = filename
    return {"linters_and_formatters": detected, "has_lint_config": len(detected) > 0}


def analyze_test_framework(root: Path) -> dict:
    frameworks: list[str] = []
    config_files: list[str] = []
    coverage_tooling = None
    coverage_threshold = None

    # JS/TS
    for config in ["vitest.config.ts", "vitest.config.js", "vitest.config.mjs",
                   "jest.config.ts", "jest.config.js", "jest.config.cjs", "jest.config.json",
                   "playwright.config.ts", "playwright.config.js",
                   "cypress.config.ts", "cypress.config.js",
                   "karma.conf.js", ".mocharc.js", ".mocharc.json", ".mocharc.yaml"]:
        if (root / config).exists():
            config_files.append(config)
            if "vitest" in config:
                frameworks.append("vitest")
            elif "jest" in config:
                frameworks.append("jest")
            elif "playwright" in config:
                frameworks.append("Playwright")
            elif "cypress" in config:
                frameworks.append("Cypress")
            elif "karma" in config:
                frameworks.append("Karma")
            elif "mocha" in config:
                frameworks.append("Mocha")

    # Python
    for config in ["pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg"]:
        if (root / config).exists():
            content = read_file_safe(root / config)
            if "[tool.pytest" in content or "[pytest]" in content or "pytest" in content:
                if "pytest" not in frameworks:
                    frameworks.append("pytest")
                    config_files.append(config)

    # Go
    if (root / "go.mod").exists():
        frameworks.append("go test")

    # Rust
    if (root / "Cargo.toml").exists():
        frameworks.append("cargo test")

    # Ruby
    if (root / ".rspec").exists() or (root / "spec").is_dir():
        frameworks.append("RSpec")
    if (root / "test").is_dir() and (root / "Gemfile").exists():
        frameworks.append("Minitest")

    # PHP
    if (root / "phpunit.xml").exists() or (root / "phpunit.xml.dist").exists():
        frameworks.append("PHPUnit")

    # Coverage tooling
    pkg_content = read_file_safe(root / "package.json")
    if "c8" in pkg_content or '"coverage"' in pkg_content:
        coverage_tooling = "c8/v8"
    elif "nyc" in pkg_content or "istanbul" in pkg_content:
        coverage_tooling = "NYC/Istanbul"
    elif "lcov" in pkg_content:
        coverage_tooling = "lcov"

    for config_file in ["vitest.config.ts", "vitest.config.js", "jest.config.ts", "jest.config.js"]:
        content = read_file_safe(root / config_file)
        if "coverage" in content:
            if not coverage_tooling:
                coverage_tooling = "configured in " + config_file
            m = re.search(r'threshold.*?lines.*?:.*?(\d+)', content, re.S)
            if m:
                coverage_threshold = int(m.group(1))

    pyproject = read_file_safe(root / "pyproject.toml")
    if "coverage" in pyproject or "pytest-cov" in pyproject:
        coverage_tooling = coverage_tooling or "pytest-cov"

    return {
        "frameworks": list(dict.fromkeys(frameworks)),
        "config_files": config_files,
        "coverage_tooling": coverage_tooling,
        "coverage_threshold": coverage_threshold,
    }


def analyze_documentation(root: Path) -> dict:
    readme = None
    for name in ["README.md", "README.rst", "README.txt", "README", "readme.md"]:
        if (root / name).exists():
            readme = name
            break

    readme_sections = []
    readme_loc = 0
    if readme:
        content = read_file_safe(root / readme)
        readme_loc = len([l for l in content.splitlines() if l.strip()])
        for section in ["install", "setup", "getting started", "run", "test", "environment", "contributing", "architecture", "usage", "api"]:
            if re.search(section, content, re.I):
                readme_sections.append(section)

    changelog = next((n for n in ["CHANGELOG.md", "CHANGELOG.rst", "CHANGELOG", "CHANGES.md", "HISTORY.md", "RELEASES.md"] if (root / n).exists()), None)
    contributing = next((n for n in ["CONTRIBUTING.md", "CONTRIBUTING.rst", "CONTRIBUTING"] if (root / n).exists()), None)
    has_pr_template = (root / ".github" / "PULL_REQUEST_TEMPLATE.md").exists() or (root / ".github" / "pull_request_template.md").exists()
    has_issue_template = (root / ".github" / "ISSUE_TEMPLATE").is_dir() or (root / ".github" / "issue_template.md").exists()

    return {
        "readme": readme,
        "readme_loc": readme_loc,
        "readme_sections_detected": readme_sections,
        "changelog": changelog,
        "contributing_guide": contributing,
        "has_pr_template": has_pr_template,
        "has_issue_template": has_issue_template,
    }


# --- Demo / template / exercise-copy detection (static signals) --------------
# Names that mark a scaffold/exercise/demo rather than original product work.
_DEMO_NAME_RE = re.compile(
    r"(?i)(^|[_\-/])("
    r"demo|sample|examples?|starter|boilerplate|scaffold|playground|"
    r"hello[-_]?world|tutorials?|workshops?|poc|proof[-_]?of[-_]?concept|"
    r"sandbox|kata|exercises?|todo|todoapp|test[-_]?ci"
    r")([_\-/]|$)"
)
# Unambiguous scaffold words that on their own mark a demo/template (no corroboration
# needed). Weaker tokens above (todo/sandbox/kata/test-ci) still need a 2nd family.
_STRONG_DEMO_NAME_RE = re.compile(
    r"(?i)(^|[_\-/])("
    r"demo|sample|examples?|boilerplate|scaffold|starter|"
    r"hello[-_]?world|playground|poc|proof[-_]?of[-_]?concept|todomvc"
    r")([_\-/]|$)"
)
# Well-known demo/reference applications (name OR README).
_KNOWN_DEMO_RE = re.compile(
    r"(?i)\b(sock[-_ ]?shop|weaveworks|realworld|petclinic|todomvc|"
    r"guestbook|2048|nodegoat|juice[-_ ]?shop)\b"
)
# README phrases that are dead giveaways of a scaffold/demo.
_TEMPLATE_README_RE = re.compile(
    r"(?i)("
    r"bootstrapped with create react app|create-react-app|"
    r"weaveworks|sock shop|realworld example|"
    r"this (is a|project is a|repo(sitory)? is a) (demo|sample|example|template|boilerplate|starter)|"
    r"(sample|example|demo) (app|application|project)|"
    r"getting started template|starter (kit|template)|scaffold(ing)? for"
    r")"
)


def analyze_demo_signals(root: Path, repo_name: str | None, docs: dict) -> dict:
    """Static signals that a repo is a demo/template/exercise copy rather than
    original product work. Combined with git 'burst-copy' history (git_stats.py) in
    score.py to set is_likely_demo. README and scaffold-file based.

    `repo_name` is the repository's OWN name and must come from an authoritative source.
    `collect` passes None, because the only name available to it is the local directory
    the operator happened to clone into — which is ours, not the repository's. The backtest
    clones every repository to `/tmp/bt-<repo>-<uuid>/src`, so that heuristic was reading
    the constant string "src" 417 times; run by hand it made `poc-billing-engine` and
    `acme-sample-parser` conclusive demos. With no name, the two name signals are None
    (not measured), never False, and `authoritative_demo` rests on content alone.
    """
    name_hit = bool(_DEMO_NAME_RE.search(repo_name)) if repo_name else None
    strong_name = bool(_STRONG_DEMO_NAME_RE.search(repo_name)) if repo_name else None

    readme = docs.get("readme")
    readme_text = read_file_safe(root / readme, max_bytes=4096) if readme else ""
    known_demo = bool((repo_name and _KNOWN_DEMO_RE.search(repo_name))
                      or _KNOWN_DEMO_RE.search(readme_text))
    template_readme = bool(_TEMPLATE_README_RE.search(readme_text))
    boilerplate_readme = (not readme) or (docs.get("readme_loc", 0) < 12)

    # Default-scaffold fingerprints (Create React App / Vite / Next starters).
    def ex(*parts): return (root / Path(*parts)).exists()
    scaffold = (
        (ex("src", "App.js") or ex("src", "App.tsx")) and ex("src", "logo.svg")
    ) or (ex("src", "App.vue") and ex("public", "favicon.ico") and not ex("src", "router")) \
        or ex("pages", "index.js") and ex("pages", "api", "hello.js")

    # Signal families: name-based, content-based (README/scaffold). History is
    # added later. `authoritative` = a README phrase that on its own is conclusive.
    return {
        "name_lexicon_hit": name_hit,
        "strong_name_hit": strong_name,
        "known_demo_app": known_demo,
        "template_readme": template_readme,
        "scaffold_fingerprint": bool(scaffold),
        "boilerplate_readme": bool(boilerplate_readme),
        # Conclusive on its own: a named demo app or a template README. Both are CONTENT.
        # `strong_name_hit` used to be here and is not any more -- "poc", "sample",
        # "playground" and "starter" are ordinary words in product repository names, and the
        # name we had was the operator's directory rather than the repository's.
        "authoritative_demo": bool(template_readme or known_demo),
        # convenience: whether any static family fired
        "static_name_family": bool(name_hit) or known_demo,
        "static_content_family": template_readme or bool(scaffold),
    }


def analyze_reproducibility(root: Path, source_files: list[tuple[Path, str]]) -> dict:
    has_dockerfile = (root / "Dockerfile").exists() or any((root / f"Dockerfile.{s}").exists() for s in ["dev", "development", "local"])
    has_compose = (root / "docker-compose.yml").exists() or (root / "docker-compose.yaml").exists()
    has_devcontainer = (root / ".devcontainer").is_dir() or (root / "devcontainer.json").exists()
    has_nix = (root / "flake.nix").exists() or (root / "shell.nix").exists()

    # .env.example
    env_example = next((n for n in [".env.example", ".env.template", ".env.sample", ".env.test.example"] if (root / n).exists()), None)

    # Environment-variable NAMES are not collected. They are repository-derived identifiers that
    # routinely encode product, vendor and service names, they feed no rubric anywhere in this
    # pipeline, and reading source to harvest them is exactly the behaviour a vendor should refuse.
    # The three keys are retained (empty) so the emitted schema is unchanged.

    return {
        "has_dockerfile": has_dockerfile,
        "has_docker_compose": has_compose,
        "has_devcontainer": has_devcontainer,
        "has_nix": has_nix,
        "env_example_file": env_example,
        "env_vars_referenced_in_source": [],
        "env_vars_in_example": [],
        "env_vars_missing_from_example": [],
    }


def analyze_observability(root: Path, source_files: list[tuple[Path, str]]) -> dict:
    logging_framework = None
    error_tracking = None
    has_health_endpoint = False
    has_metrics = False

    pkg_content = read_file_safe(root / "package.json")
    for lib in ["pino", "winston", "bunyan", "log4js"]:
        if lib in pkg_content:
            logging_framework = lib
            break

    req_content = read_file_safe(root / "requirements.txt") + read_file_safe(root / "pyproject.toml")
    if "structlog" in req_content:
        logging_framework = "structlog"
    elif "loguru" in req_content:
        logging_framework = "loguru"

    for lib in ERROR_TRACKING_LIBS:
        if lib in pkg_content or lib.replace("-", "_") in req_content:
            error_tracking = lib
            break

    # Scan source for health/metrics
    scan_count = 0
    for abs_path, rel_path in source_files:
        if abs_path.suffix.lower() not in CODE_EXTENSIONS or is_test_file(rel_path):
            continue
        content = read_file_safe(abs_path, max_bytes=4096)
        if re.search(r'["\'/](health|ping|readiness|liveness)["\']', content, re.I):
            has_health_endpoint = True
        if re.search(r'prometheus|prom-client|metrics|statsd|datadog', content, re.I):
            has_metrics = True
        scan_count += 1
        if scan_count > 200:
            break

    # Check go.mod for logging
    go_mod_content = read_file_safe(root / "go.mod")
    if "zerolog" in go_mod_content:
        logging_framework = "zerolog"
    elif "zap" in go_mod_content:
        logging_framework = "zap"
    elif "logrus" in go_mod_content:
        logging_framework = "logrus"

    return {
        "logging_framework": logging_framework,
        "error_tracking": error_tracking,
        "has_health_endpoint": has_health_endpoint,
        "has_metrics": has_metrics,
    }


# ---------------------------------------------------------------------------
# Dependency names for class detection
# ---------------------------------------------------------------------------
# Class keywords are matched against PARSED DEPENDENCY NAMES, never against raw
# manifest text. Scanning text made ordinary English match: pytest's pyproject.toml
# carries the towncrier heading "Deprecations (removal in next major release)", the
# frontend keyword `next` fired on the word "next", and a Python test framework was
# classified — and described to a customer — as a frontend web app.

_PEP508_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_QUOTED = re.compile(r"""['"]([^'"\n]+)['"]""")
_GEMFILE_GEM = re.compile(r"""^\s*gem\s+['"]([^'"]+)['"]""", re.MULTILINE)
_POM_COORD = re.compile(r"<(groupId|artifactId)>\s*([^<\s]+)\s*</\1>")
_GRADLE_COORD = re.compile(r"""['"]([A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+)(?::[^'"]*)?['"]""")
_GO_MODULE = re.compile(
    r"^\s*(?:require\s+)?([a-z0-9][a-z0-9.-]*\.[a-z]{2,}/[^\s]+)\s+v\d", re.MULTILINE
)
_SETUP_PY_REQUIRES = re.compile(
    r"(?:install_requires|extras_require|setup_requires|tests_require)\s*=\s*[\[{](.*?)[\]}]",
    re.DOTALL,
)


def _normalize_dep_name(name: str) -> str:
    """Lowercase, de-quote, and PEP 503-normalize a dependency name.

    Keeps `/`, `:` and `.` so npm scopes (`@angular/core`), go module paths
    (`go.mongodb.org/mongo-driver`) and maven coordinates (`group:artifact`) stay
    segmented — the matcher anchors on those separators.
    """
    cleaned = name.strip().strip("'\"").lower().replace("_", "-").rstrip("/")
    return cleaned if re.fullmatch(r"[@a-z0-9][a-z0-9./:@+-]*", cleaned or "") else ""


def _table_values(value) -> list:
    """Values of a TOML/JSON table, or nothing if the manifest put something else there."""
    return list(value.values()) if isinstance(value, dict) else []


def _read_toml(path: Path) -> dict:
    # TOMLDecodeError and JSONDecodeError are both ValueError subclasses.
    try:
        return tomllib.loads(read_file_safe(path, max_bytes=262_144))
    except ValueError:
        return {}


def _read_json(path: Path) -> dict:
    try:
        parsed = json.loads(read_file_safe(path, max_bytes=262_144))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _requirement_name(spec: str) -> str:
    """Name from one PEP 508 requirement (`torch>=2.1 ; extra == "gpu"` -> `torch`)."""
    spec = spec.strip().strip("'\"")
    if not spec or spec.startswith(("-", "#", "http", "git+", "file:", ".", "/")):
        return ""
    match = _PEP508_NAME.match(spec)
    return match.group(0) if match else ""


def _python_requirement_names(specs) -> list[str]:
    if not isinstance(specs, list):
        return []
    return [_requirement_name(s) for s in specs if isinstance(s, str)]


def _yaml_block_keys(text: str, sections: tuple[str, ...]) -> list[str]:
    """Keys nested one level under any of `sections` in a simple YAML mapping.

    Deliberately naive — pubspec/conda dependency blocks are flat, and a real YAML
    parser is not worth a dependency here.
    """
    names: list[str] = []
    section_indent = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if section_indent is not None and indent <= section_indent:
            section_indent = None
        if stripped.rstrip(":") in sections and stripped.endswith(":"):
            section_indent = indent
            continue
        if section_indent is None or indent <= section_indent:
            continue
        entry = stripped.lstrip("- ").split("#", 1)[0]
        names.append(re.split(r"[=<>!~\s:]", entry, maxsplit=1)[0])
    return names


def _setup_cfg_requirement_names(text: str) -> list[str]:
    names: list[str] = []
    in_block = False
    for line in text.splitlines():
        if re.match(r"^\s*(install_requires|setup_requires)\s*=", line):
            in_block = True
            names.append(_requirement_name(line.split("=", 1)[1]))
            continue
        if in_block:
            if line.strip() and not line[:1].isspace():
                in_block = False
                continue
            names.append(_requirement_name(line))
    return names


def _npm_dep_names(manifest: dict) -> list[str]:
    fields = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
    names: list[str] = []
    for field in fields:
        block = manifest.get(field)
        if isinstance(block, dict):
            names.extend(block)
    return names


def _pyproject_dep_names(data: dict) -> list[str]:
    names: list[str] = []
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    names.extend(_python_requirement_names(project.get("dependencies")))
    for extra in _table_values(project.get("optional-dependencies")):
        names.extend(_python_requirement_names(extra))
    for group in _table_values(data.get("dependency-groups")):
        names.extend(_python_requirement_names(group))
    build = data.get("build-system") if isinstance(data.get("build-system"), dict) else {}
    names.extend(_python_requirement_names(build.get("requires")))
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
    for field in ("dependencies", "dev-dependencies"):
        if isinstance(poetry.get(field), dict):
            names.extend(poetry[field])
    for group in _table_values(poetry.get("group")):
        if isinstance(group, dict) and isinstance(group.get("dependencies"), dict):
            names.extend(group["dependencies"])
    return names


def collect_dependency_names(root: Path) -> dict[str, list[str]]:
    """Direct dependency names from the root manifests, keyed by ecosystem.

    Ecosystem keying is what stops a keyword from one language matching a package
    name from another (PyPI ships `lit`, `astro` and `solid`; none of them mean the
    repo is a frontend).
    """
    names: dict[str, set[str]] = defaultdict(set)

    def add(ecosystem: str, raw_names) -> None:
        for raw in raw_names:
            normalized = _normalize_dep_name(raw)
            if normalized:
                names[ecosystem].add(normalized)

    if (root / "package.json").exists():
        add("npm", _npm_dep_names(_read_json(root / "package.json")))

    for req in ("requirements.txt", "requirements-dev.txt"):
        if (root / req).exists():
            text = read_file_safe(root / req, max_bytes=131_072)
            add("pypi", [_requirement_name(line.split("#", 1)[0]) for line in text.splitlines()])

    if (root / "pyproject.toml").exists():
        add("pypi", _pyproject_dep_names(_read_toml(root / "pyproject.toml")))

    if (root / "Pipfile").exists():
        pipfile = _read_toml(root / "Pipfile")
        for field in ("packages", "dev-packages"):
            if isinstance(pipfile.get(field), dict):
                add("pypi", pipfile[field])

    if (root / "setup.py").exists():
        text = read_file_safe(root / "setup.py", max_bytes=131_072)
        for block in _SETUP_PY_REQUIRES.findall(text):
            add("pypi", [_requirement_name(s) for s in _QUOTED.findall(block)])

    if (root / "setup.cfg").exists():
        text = read_file_safe(root / "setup.cfg", max_bytes=131_072)
        add("pypi", _setup_cfg_requirement_names(text))

    for env in ("environment.yml", "environment.yaml"):
        if (root / env).exists():
            text = read_file_safe(root / env, max_bytes=131_072)
            add("pypi", _yaml_block_keys(text, ("dependencies", "pip")))

    if (root / "go.mod").exists():
        text = read_file_safe(root / "go.mod", max_bytes=131_072)
        add("go", _GO_MODULE.findall(text))

    if (root / "Cargo.toml").exists():
        cargo = _read_toml(root / "Cargo.toml")
        for field in ("dependencies", "dev-dependencies", "build-dependencies"):
            if isinstance(cargo.get(field), dict):
                add("cargo", cargo[field])
        workspace = cargo.get("workspace") if isinstance(cargo.get("workspace"), dict) else {}
        if isinstance(workspace.get("dependencies"), dict):
            add("cargo", workspace["dependencies"])

    if (root / "Gemfile").exists():
        add("gem", _GEMFILE_GEM.findall(read_file_safe(root / "Gemfile", max_bytes=131_072)))

    if (root / "composer.json").exists():
        composer = _read_json(root / "composer.json")
        for field in ("require", "require-dev"):
            if isinstance(composer.get(field), dict):
                add("composer", composer[field])

    if (root / "pom.xml").exists():
        text = read_file_safe(root / "pom.xml", max_bytes=262_144)
        add("maven", [value for _, value in _POM_COORD.findall(text)])

    for gradle in ("build.gradle", "build.gradle.kts"):
        if (root / gradle).exists():
            text = read_file_safe(root / gradle, max_bytes=131_072)
            add("maven", _GRADLE_COORD.findall(text))

    if (root / "pubspec.yaml").exists():
        text = read_file_safe(root / "pubspec.yaml", max_bytes=131_072)
        add("pub", _yaml_block_keys(text, ("dependencies", "dev_dependencies")))

    return {ecosystem: sorted(found) for ecosystem, found in names.items()}


# Keyword groups for class detection, keyed by group then by ecosystem ("any"
# matches every ecosystem). A keyword matches a whole dependency name, a whole
# path/coordinate segment (`gin-gonic` in `github.com/gin-gonic/gin`) or an npm
# scope (`@angular` in `@angular/core`). A trailing `*` is the only partial form
# and matches a segment PREFIX (`google-cloud*` -> `google-cloud-storage`).
#
# Rules for adding one: it must be an actual package/module name in the ecosystem
# it is listed under, and it must not be a name that non-members of the class
# routinely depend on. `cryptography` and `paramiko` were dropped from
# security_libs for the second reason — every repo that talks SSH or TLS pulls
# them, and one hit was worth 3.0 points, enough to name the primary class.
CLASS_DEP_KEYWORDS: dict[str, dict[str, list[str]]] = {
    "ml_libs": {
        "pypi": [
            "torch", "pytorch", "tensorflow", "tensorflow-gpu", "jax", "jaxlib",
            "flax", "scikit-learn", "sklearn", "keras", "xgboost", "lightgbm",
            "catboost", "transformers", "timm", "onnx", "onnxruntime", "diffusers",
            "sentence-transformers",
        ],
        "npm": ["@tensorflow/tfjs", "onnxruntime-node", "onnxruntime-web"],
    },
    "experiment_tracking": {
        "pypi": [
            "wandb", "mlflow", "tensorboard", "sacred", "neptune", "neptune-client",
            "clearml", "hydra-core", "optuna", "accelerate", "deepspeed",
            "pytorch-lightning", "lightning",
        ],
    },
    "data_eng": {
        "pypi": [
            "apache-airflow*", "dagster", "prefect", "dbt",
            "dbt-core", "pyspark", "apache-beam", "luigi", "kafka-python",
            "confluent-kafka", "great-expectations", "pandera", "dask",
            "apache-flink", "kedro", "feast", "delta-spark",
        ],
        "npm": ["kafkajs"],
        "go": ["sarama", "kafka-go"],
    },
    "security_libs": {
        "pypi": [
            "bandit", "semgrep", "scapy", "pwntools", "pycryptodome", "python-nmap",
            "yara-python", "volatility", "volatility3", "impacket", "mitmproxy",
            "angr", "capstone",
        ],
        "go": ["trufflehog", "nuclei", "gosec"],
    },
    "backend_frameworks": {
        "npm": ["express", "fastify", "koa", "hono", "@nestjs"],
        "pypi": ["fastapi", "flask", "django"],
        "go": ["gin-gonic", "gofiber", "labstack/echo"],
        "cargo": ["actix-web", "axum", "rocket"],
        "gem": ["sinatra"],
        "composer": ["laravel"],
        "maven": ["org.springframework.boot", "spring-boot*"],
    },
    # npm-only on purpose. PyPI has packages called `lit`, `astro`, `solid` and
    # `ember`; none of them make a repo a frontend.
    "frontend_frameworks": {
        "npm": [
            "react", "react-dom", "react-native", "vue", "@vue", "svelte",
            "@sveltejs", "@angular", "next", "nuxt", "@nuxt", "solid-js", "preact",
            "tailwindcss", "@mui", "styled-components", "@chakra-ui", "@remix-run",
            "gatsby", "astro", "@builder.io/qwik", "ember-source", "lit",
        ],
    },
    "orm_db": {
        "npm": [
            "prisma", "@prisma", "drizzle-orm", "typeorm", "sequelize", "mongoose",
            "pg", "ioredis", "knex",
        ],
        "pypi": ["sqlalchemy", "psycopg*", "alembic", "pymongo", "asyncpg", "peewee"],
        "go": ["gorm", "sqlx", "pgx"],
        "any": ["redis", "mysql*", "mongodb"],
    },
    "infra_libs": {
        "any": ["pulumi", "@pulumi", "aws-cdk*", "@aws-cdk", "kubernetes", "@kubernetes",
                "ansible", "ansible-core"],
        "pypi": ["boto3", "google-cloud*", "azure-mgmt*"],
        "npm": ["@google-cloud"],
    },
}


def dep_keyword_tokens() -> set[str]:
    """Every token that can appear in `class_signals.dep_keyword_hits` (a keyword
    minus its trailing `*`). Used by emit_allowlist.py to enumerate the schema."""
    return {
        kw.rstrip("*")
        for groups in CLASS_DEP_KEYWORDS.values()
        for kws in groups.values()
        for kw in kws
    }


def _compile_keyword(keyword: str) -> re.Pattern:
    # `/`, `:` and `.` are the separators inside a dependency name (npm scopes, go
    # module paths, maven coordinates). A keyword must align to one of them at both
    # ends: `mongodb` matches `go.mongodb.org/mongo-driver`, `next` does not match
    # `next-tick`.
    boundary = r"[/:.]"
    if keyword.endswith("*"):
        return re.compile(
            f"(?:^|{boundary})" + re.escape(keyword[:-1]) + f"[a-z0-9+-]*(?:$|{boundary})"
        )
    return re.compile(f"(?:^|{boundary})" + re.escape(keyword) + f"(?:$|{boundary})")


_KEYWORD_PATTERNS: dict[str, re.Pattern] = {
    kw: _compile_keyword(kw)
    for groups in CLASS_DEP_KEYWORDS.values()
    for kws in groups.values()
    for kw in kws
}


def match_dep_keywords(dep_names: dict[str, list[str]]) -> dict[str, list[str]]:
    """Keyword hits per class group, matched against dependency names only."""
    all_names = sorted({name for found in dep_names.values() for name in found})
    hits: dict[str, list[str]] = {}
    for group, by_ecosystem in CLASS_DEP_KEYWORDS.items():
        found: set[str] = set()
        for ecosystem, keywords in by_ecosystem.items():
            candidates = all_names if ecosystem == "any" else dep_names.get(ecosystem, [])
            if not candidates:
                continue
            for keyword in keywords:
                pattern = _KEYWORD_PATTERNS[keyword]
                if any(pattern.search(name) for name in candidates):
                    found.add(keyword.rstrip("*"))
        hits[group] = sorted(found)
    return hits


def analyze_class_signals(
    root: Path,
    all_files: list[tuple[Path, str]],
    loc_by_language: dict[str, int],
    frameworks: list[str],
) -> dict:
    """Collect raw signals that classify_repo.py turns into per-class confidences.

    Pure counting and keyword matching — no scoring happens here.
    """
    keyword_hits = match_dep_keywords(collect_dependency_names(root))

    notebook_count = 0
    tf_count = 0
    dockerfile_count = 0
    sql_file_count = 0
    data_file_count = 0
    ui_component_count = 0  # .tsx/.jsx/.vue/.svelte
    css_loc = 0  # counted here because CSS extensions are not in CODE_EXTENSIONS
    yaml_candidates: list[Path] = []

    for abs_path, rel_path in all_files:
        name = abs_path.name.lower()
        ext = abs_path.suffix.lower()
        if ext == ".ipynb":
            notebook_count += 1
        elif ext == ".tf" or ext == ".tfvars":
            tf_count += 1
        elif ext in (".tsx", ".jsx", ".vue", ".svelte"):
            ui_component_count += 1
        elif ext == ".sql":
            sql_file_count += 1
        elif ext in (".css", ".scss", ".sass", ".less"):
            css_loc += count_lines(abs_path)
        elif ext in (".parquet", ".avro", ".orc", ".feather"):
            data_file_count += 1
        if name == "dockerfile" or name.startswith("dockerfile."):
            dockerfile_count += 1
        if ext in (".yaml", ".yml") and len(yaml_candidates) < 400:
            yaml_candidates.append(abs_path)

    # Bounded scan of YAML files for Kubernetes manifests.
    k8s_manifest_count = 0
    for p in yaml_candidates:
        head = read_file_safe(p, max_bytes=2048)
        if "apiVersion:" in head and "kind:" in head:
            k8s_manifest_count += 1

    infra_hits = set(keyword_hits.get("infra_libs", []))
    pulumi_present = (root / "Pulumi.yaml").exists() or bool(infra_hits & {"pulumi", "@pulumi"})
    ansible_present = (
        (root / "ansible.cfg").exists()
        or (root / "playbook.yml").exists()
        or (root / "playbooks").is_dir()
        or bool(infra_hits & {"ansible", "ansible-core"})
    )
    helm_present = "Helm" in frameworks or (root / "Chart.yaml").exists()
    terraform_present = tf_count > 0 or "Terraform" in frameworks

    # css_loc is accumulated in the loop above (CSS is excluded from CODE_EXTENSIONS,
    # so it never appears in loc_by_language). sql_loc does come from loc_by_language
    # because .sql IS a counted code extension.
    sql_loc = loc_by_language.get("SQL", 0)
    total_loc = sum(loc_by_language.values()) or 1

    return {
        "dep_keyword_hits": keyword_hits,
        "notebook_count": notebook_count,
        "terraform_file_count": tf_count,
        "terraform_present": terraform_present,
        "k8s_manifest_count": k8s_manifest_count,
        "helm_present": helm_present,
        "pulumi_present": pulumi_present,
        "ansible_present": ansible_present,
        "dockerfile_count": dockerfile_count,
        "ui_component_file_count": ui_component_count,
        "sql_file_count": sql_file_count,
        "sql_loc": sql_loc,
        "css_loc": css_loc,
        # Fraction of all lines (code + CSS) that are CSS — bounded [0, 1].
        "css_loc_ratio": round(css_loc / (total_loc + css_loc), 4),
        "data_file_count": data_file_count,
    }


def infer_project_type(root: Path, frameworks: list[str]) -> str:
    has_pkg = (root / "package.json").exists()
    if has_pkg:
        pkg = {}
        try:
            pkg = json.loads(read_file_safe(root / "package.json"))
        except (json.JSONDecodeError, ValueError):
            pass
        is_lib = pkg.get("private") is not True and pkg.get("main") and not any(
            f in frameworks for f in ["Next.js", "Remix", "Gatsby", "Nuxt", "SvelteKit", "Angular"]
        )
        if is_lib:
            return "library"
    if any(f in frameworks for f in ["Next.js", "Remix", "Gatsby", "Nuxt", "SvelteKit", "Angular", "React", "Vue CLI"]):
        return "web app"
    if any(f in frameworks for f in ["Express", "NestJS", "FastAPI", "Flask", "Django", "Hono", "Fastify", "Elysia", "Koa", "tRPC"]):
        return "API service"
    if "Flutter/Dart" in frameworks:
        return "mobile"
    if "Terraform" in frameworks or "Helm" in frameworks or "Fly.io" in frameworks:
        return "infrastructure"
    if any(f in frameworks for f in ["Rust (Cargo)", "Go Modules"]):
        # Could be CLI or library — check for main
        if (root / "main.go").exists() or (root / "cmd").is_dir():
            return "CLI / service"
        if (root / "src" / "main.rs").exists() or (root / "src" / "lib.rs").exists():
            src_main = read_file_safe(root / "src" / "main.rs")
            return "CLI" if "fn main()" in src_main else "library"
    return "unknown"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Static metadata collector for repo-quality-score.")
    parser.add_argument("repo", help="Path to the repository")
    parser.add_argument("--top-files", type=int, default=10, help="Number of largest files to report")
    args = parser.parse_args()

    root = Path(args.repo).resolve()
    if not root.exists():
        print(json.dumps({"error": f"path not found: {root}"}))
        return 1

    sys.stderr.write(f"Scanning {root} ...\n")
    all_files = walk_source_files(root)
    sys.stderr.write(f"  Found {len(all_files)} files (pre-filter)\n")

    # Core analyses
    languages = analyze_languages(all_files)
    iac = analyze_iac(all_files, root)

    # Fold IaC LOC into the language breakdown and totals so infra repos (compose,
    # k8s, terraform, helm) report real LOC and a meaningful primary_language
    # instead of 0 / "Unknown". IaC extensions are outside CODE_EXTENSIONS, so this
    # never double-counts with analyze_languages.
    iac_sized_files = iac.pop("_sized_files", [])
    if iac.get("iac_loc"):
        merged_loc = dict(languages["loc_by_language"])
        merged_fc = dict(languages["file_count_by_language"])
        for iac_type, loc in iac["iac_loc_by_type"].items():
            merged_loc[iac_type] = merged_loc.get(iac_type, 0) + loc
            merged_fc[iac_type] = merged_fc.get(iac_type, 0) + \
                iac["iac_file_count_by_type"].get(iac_type, 0)
        sorted_langs = sorted(merged_loc.items(), key=lambda x: x[1], reverse=True)
        languages["loc_by_language"] = dict(sorted_langs)
        languages["file_count_by_language"] = merged_fc
        languages["total_loc"] = sum(merged_loc.values())
        languages["primary_language"] = sorted_langs[0][0] if sorted_langs else "Unknown"
        languages["secondary_languages"] = [l for l, _ in sorted_langs[1:4]]

    file_sizes = analyze_file_sizes(all_files, top_n=args.top_files, extra_files=iac_sized_files)
    tests = analyze_tests(all_files)
    frameworks = analyze_frameworks(root)
    dependencies = analyze_dependencies(root)
    ci = analyze_ci(root)
    security = analyze_hygiene(root, all_files)
    lint = analyze_lint_config(root)
    test_fw = analyze_test_framework(root)
    docs = analyze_documentation(root)
    # No repository name: `root.name` is the directory the operator cloned into, not the
    # repository's own name. See analyze_demo_signals.
    demo_signals = analyze_demo_signals(root, None, docs)
    repro = analyze_reproducibility(root, all_files)
    observability = analyze_observability(root, all_files)
    class_signals = analyze_class_signals(root, all_files, languages["loc_by_language"], frameworks)
    # Overlay the authoritative IaC signals (LOC-backed) onto the class signals so
    # classify_repo.py can weight infra by real IaC volume, not just file presence.
    _tf_was_present = bool(class_signals.get("terraform_present"))
    for k in ("terraform_file_count", "terraform_loc",
              "k8s_manifest_count", "k8s_loc", "docker_compose_file_count",
              "docker_compose_loc", "helm_file_count", "helm_loc",
              "ansible_file_count", "ansible_loc", "cloudformation_file_count",
              "cloudformation_loc", "dockerfile_loc", "iac_loc", "iac_file_count",
              "iac_loc_by_type"):
        class_signals[k] = iac[k]
    # Presence flags: keep True if EITHER the file-based IaC detector or the existing
    # framework/keyword detector fired.
    class_signals["terraform_present"] = _tf_was_present or iac["terraform_present"]
    class_signals["helm_present"] = bool(class_signals.get("helm_present")) or iac["helm_file_count"] > 0
    class_signals["ansible_present"] = bool(class_signals.get("ansible_present")) or iac["ansible_file_count"] > 0
    project_type = infer_project_type(root, frameworks)

    # Test:source ratio — denominator is NON-test source files so tests aren't
    # counted on the source side (which would understate test density).
    non_test_source = file_sizes.get("total_non_test_source_files", 0)
    spec_count = tests.get("spec_files", 0)
    # "0 tests" only when there are no specs; with specs present but no non-test
    # source (all-test repo) report "1:0" rather than contradicting spec_files.
    test_source_ratio = f"1:{round(non_test_source / spec_count)}" if spec_count > 0 else "0 tests"

    output = {
        "schema_version": "1.0",
        "repo_path": str(root),
        "repo_name": root.name,

        # Identity
        "primary_language": languages["primary_language"],
        "secondary_languages": languages["secondary_languages"],
        "loc_by_language": languages["loc_by_language"],
        "file_count_by_language": languages["file_count_by_language"],
        # The one language-family scalar a gate can read; divide loc_by_language to check it.
        "jvm_dotnet_loc_share": jvm_dotnet_loc_share(
            languages["loc_by_language"], languages["total_loc"]
        ),
        "detected_frameworks": frameworks,
        "project_type": project_type,

        # Size
        "total_loc": languages["total_loc"],
        "total_source_files": file_sizes.get("total_source_files", 0),
        "median_file_size_loc": file_sizes.get("median_loc", 0),
        "p90_file_size_loc": file_sizes.get("p90_loc", 0),
        "god_files_over_500_loc": file_sizes.get("god_files_over_500", 0),
        "god_files_over_1000_loc": file_sizes.get("god_files_over_1000", 0),
        "top_largest_files": file_sizes.get("largest_files", []),
        "generated_files_excluded": file_sizes.get("generated_excluded", 0),

        # Infrastructure-as-Code LOC (folded into total_loc above; broken out here
        # so infra repos are legible instead of showing 0 LOC / Unknown language).
        "iac_loc": iac.get("iac_loc", 0),
        "iac_file_count": iac.get("iac_file_count", 0),
        "iac_loc_by_type": iac.get("iac_loc_by_type", {}),

        # Tests
        "test_spec_files": tests["spec_files"],
        "test_fixture_files": tests["fixture_and_snapshot_files"],
        "test_source_ratio": test_source_ratio,
        "test_spec_sample": tests["spec_file_paths_sample"],
        "test_framework": test_fw["frameworks"],
        "test_config_files": test_fw["config_files"],
        "coverage_tooling": test_fw["coverage_tooling"],
        "coverage_threshold": test_fw["coverage_threshold"],

        # Dependencies
        "package_managers": dependencies["package_managers"],
        "manifests_found": dependencies["manifests_found"],
        "lockfiles_found": dependencies["lockfiles_found"],
        "lockfiles_expected": dependencies["lockfiles_expected"],
        "direct_runtime_deps": dependencies["direct_runtime_deps"],
        "direct_dev_deps": dependencies["direct_dev_deps"],
        "total_transitive_deps": dependencies["total_transitive_deps"],
        "dep_update_tooling": dependencies["dep_update_tooling"],

        # CI
        "ci_systems": ci["ci_systems"],
        "ci_config_files": ci["ci_configs"],
        "ci_runs_tests": ci["runs_tests"],
        "ci_runs_lint": ci["runs_lint"],
        "ci_runs_typecheck": ci["runs_typecheck"],
        "ci_has_deploy": ci["has_deploy_pipeline"],
        "ci_present": ci["ci_present"],
        # How the three ci_runs_* flags above were reached, so a null is legible as
        # "we could not read the config" rather than as "the pipeline does nothing".
        "ci_analysis_method": ci["ci_analysis_method"],

        # Security
        "hardcoded_secret_hits": security["hardcoded_secret_hits"],
        "secret_hit_details": security["secret_hit_details"],
        "env_files_committed": security["env_files_committed"],
        "dep_audit_in_ci": security["dep_audit_in_ci"],
        "input_validation_patterns": security["input_validation_patterns"],

        # Linting
        "linters_and_formatters": lint["linters_and_formatters"],
        "has_lint_config": lint["has_lint_config"],

        # Documentation
        "readme": docs["readme"],
        "readme_loc": docs["readme_loc"],
        "readme_sections": docs["readme_sections_detected"],
        "changelog": docs["changelog"],
        "contributing_guide": docs["contributing_guide"],
        "has_pr_template": docs["has_pr_template"],
        "has_issue_template": docs["has_issue_template"],

        # Demo / template / exercise-copy static signals (combined with git
        # burst-copy history in score.py -> is_likely_demo).
        "demo_signals": demo_signals,

        # Reproducibility
        "has_dockerfile": repro["has_dockerfile"],
        "has_docker_compose": repro["has_docker_compose"],
        "has_devcontainer": repro["has_devcontainer"],
        "has_nix": repro["has_nix"],
        "env_example_file": repro["env_example_file"],
        "env_vars_referenced_in_source": repro["env_vars_referenced_in_source"],
        "env_vars_in_example": repro["env_vars_in_example"],
        "env_vars_missing_from_example": repro["env_vars_missing_from_example"],

        # Observability
        "logging_framework": observability["logging_framework"],
        "error_tracking": observability["error_tracking"],
        "has_health_endpoint": observability["has_health_endpoint"],
        "has_metrics": observability["has_metrics"],

        # Class signals (consumed by classify_repo.py)
        "class_signals": class_signals,
    }

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

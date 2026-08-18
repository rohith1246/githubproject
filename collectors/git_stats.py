#!/usr/bin/env python3
"""
git_stats.py — extract commit / author / bot / conventional-commit / tag stats
from a git repository, for the repo-quality-score skill.

Usage:
    python git_stats.py <repo-path> [--limit N] [--top N]

Positional arguments:
    repo-path           Path to the git repository to analyze.

Options:
    --limit N           How many recent commits to scan for the per-commit
                        feature/test classification. 0 = full history (the
                        default), which is what the class tallies are computed
                        over. Pass a positive N for a fast preview. Repo-wide stats (commit
                        count, authors, tags, recency) always cover full history
                        regardless of this value.
    --top N             Cap each emitted per-class commit list (and the scanned-
                        commit dump) to N entries. 0 = emit all (the default).
                        Truncation is always logged to stderr, never silent.
                        Worth setting on a large repo: full history with full
                        numstat is a lot of JSON.

Environment variables: none. This script reads no secrets and no credentials.

Emits JSON to stdout. Uses only `git` (invoked with fixed argument lists, never a
shell) and the Python standard library. Read-only: it never modifies the repo and
never hits the network.

The output feeds the host-agnostic activity/maintenance signals the skill scores:
total commits, human vs bot contributors, recency/staleness in days, repo span,
conventional-commit rate, and tags/releases count. PR/review data is deliberately
NOT collected — it is GitHub-specific and would not work for GitLab/Bitbucket
repos; commit and tag counts are the host-agnostic activity proxy instead.

Anonymizer-commit awareness: anonymized code deliveries append a trailing "patch"
commit (author "Anonymizer Auto-Fix" / noreply@anonymizer.local, subject like
"Anonymize repository for code review delivery") that repairs post-anonymization
issues. That commit is NOT real project history — trusting it would make every repo
look freshly active and add a phantom author. When the HEAD commit matches the
anonymizer signature AND the repo has more than one commit, all history stats
(recency, span, total commits, authors, conventional rate, and the per-commit scan)
are computed against HEAD~1 instead. The raw count and the detected commit are still
reported for transparency. The only-commit case is left untouched (never drop the
sole commit).
"""

from __future__ import annotations

import argparse
import json
import hashlib
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Kept in step with `git_history._BOT`; a test pins the two against the same table.
#
# `\b\[bot\]\b` used to sit at the bottom of this list and matched nothing ever: `]` is a
# non-word character and so is the space or `@` that follows it, so the trailing `\b` had no
# boundary to sit on. Every GitHub App that is not separately named below -- `codecov[bot]`,
# `vercel[bot]`, `stale[bot]` -- was therefore counted as a human, which inflates
# `human_authors` and weakens the validated "single author AND no tests" no-buy screen.
#
# The vendor names that are also company names or surnames (codecov, vercel, netlify,
# sonarcloud, stale) are covered by the `[bot]` suffix rather than as bare words, so an
# engineer with a `@vercel.com` address is not filed as a robot.
BOT_NAME_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r"\[bot\]",
        r"\bdependabot\b",
        r"\brenovate\b",
        r"\bsnyk-bot\b",
        r"\bgithub-actions\b",
        r"\bactions-user\b",
        r"\bmergify\b",
        r"\bpre-commit-ci\b",
        r"\bwhitesource\b",
        r"\bgreenkeeper\b",
        r"\bimgbot\b",
        r"\ballcontributors\b",
        r"\brestyled\b",
        r"\bscala-steward\b",
        r"\bpyup-bot\b",
        r"\bdepfu\b",
        r"\btravis-ci\b",
        r"\bcircleci\b",
        r"\bbuildkite\b",
        r"\bsemantic-release\b",
        r"\banonymi[sz]er\b",
        r"\bjenkins[-_. ]?(ci|bot|build|builder|agent|server)\b",
        r"\bbot\b",
    ]
]

CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|chore|docs|refactor|test|perf|build|ci|style|revert)(\([^)]+\))?!?:\s",
    re.I,
)

# Anonymizer patch-commit signature (appended to anonymized code deliveries).
ANON_EMAIL_DOMAINS = ("anonymizer.local",)
ANON_NAME_RE = re.compile(r"anonymiz", re.I)
ANON_SUBJECT_RE = re.compile(r"\banonymiz\w*\s+(repo|repository|code)\b", re.I)

# Subjects that signal "this is not substantive engineering work" regardless of
# file shape. Catches chore/revert/wip/dependency-bump/cleanup commits that
# incidentally touch a few code files, which would otherwise inflate the class
# tallies: "Removed unnecessary ebextensions" is chore work that happens to look
# atomic.
NOISE_SUBJECT_RE = re.compile(
    r"^(chore|revert|wip|bump|deps?|dependabot|lint|style|format|prettier|typo|"
    r"merge|release|version|removed?|cleanup|cleanups?|delete|remove|rename|"
    r"upgrade|update\s+(deps|dep|dependenc|package|lock))[\s:(\[]",
    re.I,
)
# Subjects that signal a feature commit. We deliberately exclude bare "test"
# (it matches noise like "test push") — only the conventional-commit forms
# `test:` or `test(scope)` count, and they tend to be Class A or B anyway.
FEATURE_SUBJECT_RE = re.compile(
    r"^(feat|add|implement|introduce|support)\b|^test[:(]",
    re.I,
)

# Bug/repair commits, counted as the class D tally. A fix that ALSO adds a
# regression test in the same commit is recorded separately, because a repository
# that demonstrates its own fixes is showing a stronger engineering practice than
# one that does not. `revert` is intentionally covered here even though it's also
# a NOISE subject: as a bug signal a revert marks a real regression, but it stays
# out of the feature classes (NOISE excludes it there).
# Inflections included on purpose: `\b` after `fix` refused "Fixed crash in parser" and
# "Fixes #123", which are the two most common ways a repair is announced in English.
# This regex is a NECESSARY condition, never a sufficient one -- see `classify_bug_repair`,
# where the diff has to corroborate it.
BUGFIX_SUBJECT_RE = re.compile(
    r"^(fix(e[sd])?|hotfix|bugfix|bug|regression|revert(s|ed)?|patch(es|ed)?|"
    r"resolve[sd]?|correct(s|ed)?)\b|"
    r"^(fix|revert)[:(]",
    re.I,
)

TEST_FILE_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r"\.(test|spec)\.(ts|tsx|js|jsx|mjs)$",
        r"(^|/)(test_|tests/)",
        r"_test\.(go|py|rb)$",
        r"(^|/)__tests__/",
        r"(^|/)spec/",
        r"\.spec\.rb$",
        r"Test\.(java|kt|cs)$",
        r"Tests\.(java|kt|cs)$",
    ]
]

# Test-related infrastructure (configs, setup, fixtures, helpers). These are
# NEITHER test specs (they don't contribute to test_file_count for class B
# decomposition) NOR implementation (they don't justify Class A on their own).
# Keeping them out of impl is what matters most — so a commit that adds 9
# test specs + 2 vitest config files isn't mis-classified as Class A.
TEST_INFRA_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r"(^|/)vitest[^/]*\.config\.(ts|js|mjs)$",
        r"(^|/)jest\.config\.(ts|js|mjs|cjs|json)$",
        r"(^|/)playwright\.config\.(ts|js|mjs)$",
        r"(^|/)cypress\.config\.(ts|js)$",
        r"(^|/)karma\.conf\.(ts|js)$",
        r"(^|/)tests?/setup/",
        r"(^|/)tests?/fixtures/",
        r"(^|/)tests?/helpers/",
        r"(^|/)tests?/__mocks__/",
        r"(^|/)tests?/conftest\.py$",
        r"(^|/)conftest\.py$",
        r"(^|/)pytest\.ini$",
        r"(^|/)tox\.ini$",
        r"(^|/)\.mocharc\.(js|cjs|json|yml|yaml)$",
    ]
]

SCHEMA_FILE_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r"(^|/)schemas?/",
        r"(^|/)types?/",
        r"\.schema\.(ts|js|py)$",
        r"(^|/)migrations?/",
        r"(^|/)drizzle/",
        r"(^|/)prisma/",
        r"\.proto$",
        r"openapi\.(ya?ml|json)$",
    ]
]

DOCS_OR_CONFIG_PATTERNS = [
    re.compile(r, re.I)
    for r in [
        r"\.md$",
        r"^\.github/",
        r"^\.gitlab/",
        r"\.ya?ml$",
        r"^Dockerfile",
        r"package(-lock)?\.json$",
        r"pnpm-lock\.yaml$",
        r"yarn\.lock$",
        r"poetry\.lock$",
        r"Cargo\.lock$",
    ]
]


def run_git(repo: Path, *args: str) -> str:
    """Run a git command in the given repo and return stdout (text)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        # Surface git errors as empty output rather than crashing — caller decides.
        sys.stderr.write(f"git error: {e.stderr}\n")
        return ""


def is_git_repo(repo: Path) -> bool:
    return (repo / ".git").exists() or run_git(repo, "rev-parse", "--git-dir").strip() != ""


# --- author cardinality without author identity -------------------------------------------------
# A per-process random salt. It exists so `author_key` cannot be reversed by dictionary attack and
# cannot be correlated across runs or across repositories: the key is meaningful only inside one
# invocation, which is all "how many distinct authors" needs. Regenerated every process start.
_AUTHOR_SALT = secrets.token_bytes(32)


def author_key(name: str, email: str) -> str:
    """A salted, non-reversible, run-local handle for one author.

    This is the ONLY thing derived from a name or an email address anywhere in this module. It
    lets us count distinct authors and attribute commits to the same person within a run; it
    identifies nobody outside this process, survives no restart, and is never written in a form
    that could be joined against anything.
    """
    return hashlib.blake2b(f"{name}\x00{email}".encode("utf-8", "replace"),
                           key=_AUTHOR_SALT, digest_size=8).hexdigest()


def is_bot_author(name: str, email: str) -> bool:
    haystack = f"{name} {email}"
    return any(p.search(haystack) for p in BOT_NAME_PATTERNS)


def matches_any(path: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(path) for p in patterns)


def classify_files(files: list[str]) -> dict:
    """Classify a commit's files into test/test-infra/impl/schema/docs buckets.

    Order matters: test specs win over test-infra (some config files match
    both patterns); both win over impl. Schema files are tagged for the
    has_schema signal but still counted as impl for the size check.
    """
    test_files = [f for f in files if matches_any(f, TEST_FILE_PATTERNS)]
    test_infra = [
        f
        for f in files
        if f not in test_files and matches_any(f, TEST_INFRA_PATTERNS)
    ]
    schema_files = [f for f in files if matches_any(f, SCHEMA_FILE_PATTERNS)]
    docs_or_config = [f for f in files if matches_any(f, DOCS_OR_CONFIG_PATTERNS)]
    impl_files = [
        f
        for f in files
        if not matches_any(f, TEST_FILE_PATTERNS)
        and not matches_any(f, TEST_INFRA_PATTERNS)
        and not matches_any(f, DOCS_OR_CONFIG_PATTERNS)
    ]
    return {
        "test_files": test_files,
        "test_infra_files": test_infra,
        "schema_files": schema_files,
        "docs_or_config_files": docs_or_config,
        "impl_files": impl_files,
    }


def directory_diversity(files: list[str]) -> int:
    """Count distinct top-level directories touched."""
    return len({f.split("/", 1)[0] for f in files if "/" in f})


def parse_commits(repo: Path, limit: int, ref: str = "HEAD") -> list[dict]:
    """Get commits with full metadata + numstat, starting from `ref` (HEAD, or
    HEAD~1 when a trailing anonymizer commit is excluded).

    `limit <= 0` scans the FULL history, which is the default: substantive work is
    spread across a repository's whole life and a 200-commit window near HEAD
    misrepresents it. A positive `limit` is a fast-preview knob."""
    fmt = "%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s"
    log_args = ["log", f"--pretty=format:{fmt}", "--numstat", "--no-merges"]
    if limit and limit > 0:
        log_args.insert(1, f"-{limit}")
    log_args.append(ref)
    raw = run_git(repo, *log_args)
    if not raw:
        return []

    commits = []
    current = None
    for line in raw.splitlines():
        if not line:
            continue
        if "\x1f" in line:
            if current:
                commits.append(current)
            sha, abbrev, author_name, author_email, iso_date, subject = line.split("\x1f", 5)
            # Identity is consumed HERE and never stored. `author_key` is a per-run salted digest
            # -- enough to count distinct authors and to tell two commits apart, useless as an
            # identifier outside this process. The name and email go out of scope on the next line.
            current = {
                "sha": sha,
                "abbrev": abbrev,
                "author_key": author_key(author_name, author_email),
                "author_is_bot": is_bot_author(author_name, author_email),
                "date": iso_date,
                "subject": subject,
                "files": [],
                "additions": 0,
                "deletions": 0,
            }
        else:
            # numstat line: "<add>\t<del>\t<path>"
            parts = line.split("\t")
            if len(parts) == 3 and current is not None:
                add, dele, path = parts
                # binary files show "-\t-\t"
                try:
                    a = int(add) if add != "-" else 0
                    d = int(dele) if dele != "-" else 0
                except ValueError:
                    a, d = 0, 0
                current["additions"] += a
                current["deletions"] += d
                current["files"].append(path)
    if current:
        commits.append(current)
    return commits


def score_atomic_feature(commit: dict) -> dict:
    """Classify a commit as Class A (strict atomic feature+test), Class B (bulk
    test-add — decomposable), Class C-pre (impl-only feature) or noise. Returns
    the classification plus the `latent_tasks` count.

    Class C is "pre" here because this collector reads commits and never the tree
    at HEAD, so it cannot tell whether the feature ended up covered by a test.
    That is why the C-pre tally is reported separately from A and B rather than
    added to them."""
    files = commit["files"]
    classified = classify_files(files)
    diversity = directory_diversity(files)
    is_bot = commit["author_is_bot"]
    subject = commit["subject"]
    is_noise_subject = bool(NOISE_SUBJECT_RE.match(subject))
    is_feature_subject = bool(FEATURE_SUBJECT_RE.match(subject))

    test_count = len(classified["test_files"])
    impl_count = len(classified["impl_files"])
    has_schema = len(classified["schema_files"]) > 0
    small_enough = len(files) <= 30
    focused = diversity <= 3
    not_bot = not is_bot
    not_noise = not is_noise_subject  # subject-line negative filter

    # Class A: strict atomic feature+test
    is_class_a = (
        small_enough
        and focused
        and test_count >= 1
        and impl_count >= 1
        and not_bot
        and not_noise
    )

    # Class B: bulk test-add (≥5 tests, <3 impl). Class A and B are exclusive.
    is_class_b = (
        not is_class_a
        and test_count >= 5
        and impl_count < 3
        and not_bot
        and not_noise
    )

    # Class C-pre: impl-only feature commit (looks like a feature added without
    # tests — typical "ship now, test later" pattern). Kept separate from A and B
    # because nothing here checks whether the behaviour is covered at HEAD, so the
    # tally is weaker evidence than the other two.
    is_class_c_pre = (
        not is_class_a
        and not is_class_b
        and small_enough
        and focused
        and impl_count >= 1
        and test_count == 0
        and is_feature_subject
        and not_bot
    )

    if is_class_a:
        klass = "A"
        latent_tasks = 1
    elif is_class_b:
        klass = "B"
        latent_tasks = min(test_count, 15)
    elif is_class_c_pre:
        klass = "C-pre"
        latent_tasks = 1  # provisional: reported separately from A and B
    else:
        klass = None
        latent_tasks = 0

    qualifies = klass is not None

    confidence = sum([qualifies, is_feature_subject, has_schema])

    return {
        "qualifies": qualifies,
        "klass": klass,
        "latent_tasks": latent_tasks,
        "confidence": confidence,
        "small_enough": small_enough,
        "focused": focused,
        "has_tests": test_count > 0,
        "has_impl": impl_count > 0,
        "has_schema": has_schema,
        "is_bot": is_bot,
        "is_noise_subject": is_noise_subject,
        "is_feature_subject": is_feature_subject,
        "diversity": diversity,
        "test_file_count": test_count,
        "impl_file_count": impl_count,
    }


def _complexity_band(x: float) -> str:
    """Map a 0..1 repair-complexity proxy to the shared Small/Medium/Large band."""
    return "Small" if x < 0.33 else "Medium" if x < 0.66 else "Large"


def classify_bug_repair(commit: dict) -> dict:
    """Class D: a bug-fix / regression / revert commit.

    A fix that adds a regression test in the same commit is recorded as
    `has_regression_test`. Repair complexity is a bounded proxy from impl breadth,
    directory spread, and churn — a one-line hotfix scores near 0, a multi-file
    regression fix near 1. Nothing here executes any of the repository's code.

    The subject alone does not decide. Under the Bugzilla convention every subject opens
    "Bug <id> - ...", so `^bug\\b` matched the entire history of gecko, servo and every
    downstream fork and reported them as 100% bug repairs — "Bug 1783377 - Add WebGPU
    compositing support" scored a Large repair off 900 added and 0 deleted lines. So the
    diff has to corroborate the claim: a repair CHANGES code that already exists, which
    means lines were removed or replaced, and a commit that is overwhelmingly new code is a
    feature whatever its subject says. Landing a test with the change is the other
    corroboration, and the stronger one."""
    files = commit["files"]
    classified = classify_files(files)
    is_bot = commit["author_is_bot"]
    subject = commit["subject"]
    is_bug_subject = bool(BUGFIX_SUBJECT_RE.match(subject))

    impl_count = len(classified["impl_files"])
    test_count = len(classified["test_files"])
    diversity = directory_diversity(files)
    additions, deletions = commit["additions"], commit["deletions"]
    churn = additions + deletions

    diff_corroborates = deletions >= 1 and (test_count >= 1 or additions <= 10 * deletions)
    qualifies = is_bug_subject and impl_count >= 1 and not is_bot and diff_corroborates
    has_regression_test = qualifies and test_count >= 1
    repair_complexity = round(
        min(1.0, 0.15 * impl_count + 0.1 * diversity + churn / 2000.0), 3
    )

    return {
        "qualifies": qualifies,
        "has_regression_test": has_regression_test,
        "repair_complexity": repair_complexity,
        "complexity_band": _complexity_band(repair_complexity),
        "impl_file_count": impl_count,
        "test_file_count": test_count,
        "diversity": diversity,
        "churn": churn,
        "is_bot": is_bot,
    }


def _looks_like_anonymizer(name: str, email: str, subject: str) -> bool:
    email_l = (email or "").lower()
    if any(email_l.endswith("@" + d) or email_l.endswith("." + d) or email_l == d
           for d in ANON_EMAIL_DOMAINS):
        return True
    if name and ANON_NAME_RE.search(name):
        return True
    if subject and ANON_SUBJECT_RE.search(subject):
        return True
    return False


def detect_anonymizer_tip(repo: Path) -> dict:
    """Detect whether HEAD is an appended anonymizer patch commit and, if so and
    there is more than one commit, choose HEAD~1 as the ref for history stats."""
    total = run_git(repo, "rev-list", "--count", "HEAD").strip()
    total_commits = int(total) if total.isdigit() else 0

    info = run_git(repo, "log", "-1", "--pretty=format:%H%x1f%an%x1f%ae%x1f%s", "HEAD")
    detected = False
    commit = None
    if info and "\x1f" in info:
        sha, name, email, subject = info.split("\x1f", 3)
        if _looks_like_anonymizer(name, email, subject):
            detected = True
            # The signature is what matters, not who signed it: emit the sha only.
            commit = {"sha": sha}

    # Never drop the only commit; only peel the tip when real history remains.
    analysis_ref = "HEAD~1" if (detected and total_commits > 1) else "HEAD"
    return {
        "detected": detected,
        "applied": analysis_ref != "HEAD",
        "commit": commit,
        "total_commits_including_anonymizer": total_commits,
        "analysis_ref": analysis_ref,
    }


def aggregate_repo_stats(repo: Path, anon: dict | None = None) -> dict:
    """Repo-wide stats: total commits, authors, dates, bot ratio, conventional rate.

    History stats are computed against `anon["analysis_ref"]` (HEAD, or HEAD~1 when a
    trailing anonymizer patch commit is detected) so that commit never inflates
    recency/age/authorship. See detect_anonymizer_tip and the module docstring."""
    if anon is None:
        anon = detect_anonymizer_tip(repo)
    ref = anon["analysis_ref"]

    total = run_git(repo, "rev-list", "--count", ref).strip()
    total_commits = int(total) if total.isdigit() else 0

    # --max-count is applied BEFORE --reverse, so we can't combine them. Use the
    # rev-list root-finder instead, which is exact and cheap.
    root_sha = run_git(repo, "rev-list", "--max-parents=0", ref).strip().splitlines()
    first = ""
    if root_sha:
        first = run_git(repo, "log", "-1", "--pretty=format:%aI", root_sha[0]).strip()
    last = run_git(repo, "log", "-1", "--pretty=format:%aI", ref).strip()

    # Author CARDINALITY, not authorship. `git shortlog -sne` is parsed line by line; each name
    # and email is used to decide bot-vs-human and then discarded on the next iteration. What
    # survives is a salted digest, a commit count and a boolean -- the three things the rubric and
    # the depth signal actually consume. No name or address is ever placed in a structure.
    shortlog = run_git(repo, "shortlog", "-sne", ref)
    authors = []
    for line in shortlog.splitlines():
        line = line.strip()
        if not line:
            continue
        # format: "  <count>\t<name> <email>"
        m = re.match(r"^\s*(\d+)\s+(.*?)\s+<(.+)>\s*$", line)
        if not m:
            continue
        count, name, email = m.groups()
        authors.append({
            "author_key": author_key(name, email),
            "commits": int(count),
            "is_bot": is_bot_author(name, email),
        })
    del shortlog

    human_authors = [a for a in authors if not a["is_bot"]]
    bot_authors = [a for a in authors if a["is_bot"]]
    bot_commit_count = sum(a["commits"] for a in bot_authors)
    bot_ratio = bot_commit_count / total_commits if total_commits else 0.0

    # Conventional-commit rate over last 200 commits
    last_subjects = run_git(repo, "log", "-200", "--pretty=format:%s", ref).splitlines()
    conv = sum(1 for s in last_subjects if CONVENTIONAL_RE.match(s))
    conv_rate = conv / len(last_subjects) if last_subjects else 0.0

    # Tags / releases. A tag is the host-agnostic proxy for a "release" — it works the same on
    # GitHub, GitLab, and Bitbucket. Only the COUNTS are measured: how many tags, and how many of
    # them are semver. Tag names themselves are read to test the semver shape and then dropped;
    # they are never emitted, because release names routinely carry product, customer or internal
    # milestone identity. Count only tags REACHABLE from the analysis ref (HEAD~1 when an
    # anonymizer patch is peeled), so a tag placed on the delivery patch commit is not counted as
    # release cadence.
    tag_lines = [t for t in run_git(repo, "tag", "--merged", ref).splitlines() if t.strip()]
    semver_tags = sum(
        1 for t in tag_lines if re.match(r"^v?\d+\.\d+", t.strip())
    )
    n_tags = len(tag_lines)
    del tag_lines

    # Recency in days
    recency_days = None
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            now = datetime.now(timezone.utc)
            recency_days = (now - last_dt.astimezone(timezone.utc)).days
        except ValueError:
            pass

    # Span in days
    span_days = None
    if first and last:
        try:
            first_dt = datetime.fromisoformat(first)
            last_dt = datetime.fromisoformat(last)
            span_days = (last_dt - first_dt).days
        except ValueError:
            pass

    head_sha = run_git(repo, "rev-parse", "HEAD").strip() or None
    effective_tip_sha = run_git(repo, "rev-parse", ref).strip() or None

    # Merge commits (reachable from the analysis ref) + a "burst copy" fingerprint:
    # SEVERAL commits (>=2), made in a <=2-day window, with no merges and <=2 authors
    # is the shape of a scaffolded/demo repo dumped-and-abandoned rather than
    # developed. A single commit is NOT a burst (that would flag many normal
    # one-commit repos), so require at least two. Fed into demo detection (score.py)
    # alongside static name/README signals from repo_stats.py.
    mc = run_git(repo, "rev-list", ref, "--min-parents=2", "--count").strip()
    merge_commit_count = int(mc) if mc.isdigit() else 0
    looks_like_burst_copy = bool(
        2 <= total_commits <= 12
        and span_days is not None and span_days <= 2
        and merge_commit_count == 0
        and len(human_authors) <= 2
    )

    return {
        "head_sha": head_sha,
        # When a trailing anonymizer commit is peeled, history stats below are
        # measured from this commit (HEAD~1), not from head_sha.
        "effective_tip_sha": effective_tip_sha,
        "anonymizer_commit_detected": anon["detected"],
        "anonymizer_commit_excluded": anon["applied"],
        "anonymizer_commit": anon["commit"],
        "total_commits": total_commits,
        "total_commits_including_anonymizer": anon["total_commits_including_anonymizer"],
        "first_commit": first or None,
        "last_commit": last or None,
        "span_days": span_days,
        "recency_days": recency_days,
        "human_authors": len(human_authors),
        "bot_authors": len(bot_authors),
        "bot_commit_count": bot_commit_count,
        "bot_commit_ratio": round(bot_ratio, 4),
        "conventional_rate_last_200": round(conv_rate, 4),
        "tag_count": n_tags,
        "semver_tag_count": semver_tags,
        # Tag NAMES are not emitted: releases are routinely named after the product, the customer
        # or an internal milestone. The count and the semver count carry the signal. Key retained
        # (null) so the emitted schema is unchanged.
        "latest_tag": None,
        "merge_commit_count": merge_commit_count,
        "looks_like_burst_copy": looks_like_burst_copy,
        # Author identity is not emitted anywhere, in either variant. `human_authors` /
        # `bot_authors` above are the counts; that is the whole signal. Key retained (empty).
        "top_authors": [],
    }


def _cap(items: list, top: int, label: str) -> list:
    """Cap an emitted list to `top` entries, logging the drop to stderr so a
    truncation is never silent (per the skill's no-silent-caps rule). top<=0 = all."""
    if top and top > 0 and len(items) > top:
        sys.stderr.write(f"git_stats: capping {label} {len(items)} -> {top} (--top)\n")
        return items[:top]
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract git history stats for repo-quality-score.")
    parser.add_argument("repo", help="Path to the git repository")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Commits to scan for the per-commit classification; 0 = full history "
             "(default). "
             "Use a positive N only for a fast preview.",
    )
    parser.add_argument(
        "--top", type=int, default=0,
        help="Cap each emitted per-class commit list to N entries (logged, never "
             "silent); 0 = emit all (default).",
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.exists():
        print(json.dumps({"error": f"path not found: {repo}"}))
        return 1
    if not is_git_repo(repo):
        print(json.dumps({"error": "not a git repository", "path": str(repo)}))
        return 1

    anon = detect_anonymizer_tip(repo)
    stats = aggregate_repo_stats(repo, anon)
    commits = parse_commits(repo, args.limit, ref=anon["analysis_ref"])

    analyzed = []
    for c in commits:
        cls = score_atomic_feature(c)
        bug = classify_bug_repair(c)
        row = {
            "sha": c["abbrev"],
            "full_sha": c["sha"],
            "subject": c["subject"],
            "author_key": c["author_key"],
            "date": c["date"],
            "files_changed": len(c["files"]),
            "additions": c["additions"],
            "deletions": c["deletions"],
            **cls,
            "bug": bug,
        }
        analyzed.append(row)

    class_a = [c for c in analyzed if c.get("klass") == "A"]
    class_b = [c for c in analyzed if c.get("klass") == "B"]
    class_c_pre = [c for c in analyzed if c.get("klass") == "C-pre"]
    class_d = [c for c in analyzed if c.get("bug", {}).get("qualifies")]

    # The confident count uses A and B only, because those two classes are decided
    # from the commit alone. C-pre depends on whether the behaviour is covered at
    # HEAD, which nothing here reads, so it is reported separately as provisional.
    confirmed_candidates = sum(
        c.get("latent_tasks", 0) for c in analyzed if c.get("klass") in ("A", "B")
    )
    provisional_candidates = confirmed_candidates + len(class_c_pre)

    output = {
        "schema_version": "4.0",
        "repo_path": str(repo),
        "repo_stats": stats,
        "analyzed_commits": len(analyzed),
        "full_history_scanned": args.limit <= 0,
        "class_a_count": len(class_a),
        "class_b_count": len(class_b),
        "class_c_pre_count": len(class_c_pre),
        "class_d_bug_count": len(class_d),
        "confirmed_candidate_count": confirmed_candidates,
        "provisional_candidate_count": provisional_candidates,
        "class_a_commits": _cap(class_a, args.top, "class_a"),
        "class_b_commits": _cap(class_b, args.top, "class_b"),
        "class_c_pre_commits": _cap(class_c_pre, args.top, "class_c_pre"),
        "class_d_bug_commits": _cap(class_d, args.top, "class_d"),
        # Every commit scanned, with numstat. Capped like the class lists: with
        # --limit 0 (the default) this is full history, which on a large repo is
        # hundreds of MB of JSON if left unbounded. The old name said "recent",
        # which stopped being true when the default became full history.
        "scanned_commits": _cap(analyzed, args.top, "scanned_commits"),
    }

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

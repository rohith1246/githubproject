#!/usr/bin/env python3
"""history_brief.py -- the repository's development history, pre-computed as read-only files.

Usage:
    python history_brief.py <repo> --out <dir> [--max-commits N] [--max-diff-bytes N]
                            [--max-total-bytes N] [--ref REF]

Positional arguments:
    repo                 Path to the git repository to summarise. Read-only; nothing in the
                         checkout is written to, moved or modified.

Options:
    --out DIR            REQUIRED. Directory to write the brief into (created if absent). Use a
                         scratch directory; the caller is expected to delete it after the lane
                         that reads it has finished.
    --max-commits N      Ceiling on how many commits get a materialised patch (default 160).
                         When the history holds more substantive commits than this, the ones
                         written are sampled at even intervals across the whole history rather
                         than taken from the tip, so an old migration is as visible as a recent
                         fix.
    --max-diff-bytes N   Ceiling on one patch file, in bytes (default 200000). A patch longer
                         than this is truncated and says so on its last line.
    --max-total-bytes N  Ceiling on the whole brief, in bytes (default 48000000). Reached, the
                         remaining commits stay in the table with no patch of their own.
    --ref REF            Restrict the history to one ref. Default: every ref in the repository.

Environment variables:
    None are read. Every `git` call here runs with an environment built by ``childenv``.

WHY THIS EXISTS
---------------
The model lanes used to be handed the ``Bash`` tool so the agent could run ``git log`` and
``git show`` for itself. ``--add-dir`` bounds ``Read``/``Grep``/``Glob``; it does not bound
``Bash``, so that grant was shell access to the operator's machine, held in place by nothing
sturdier than the model deciding not to use it. It is withdrawn. The history the agent actually
needed is computed here, by us, with a fixed argv, and handed to it as files it can ``Read``.

Two hazards are closed by computing it here rather than there, and neither is closable by asking
a model nicely:

  * **A repository can execute code through ``git show``.** ``.git/config`` can name a
    ``textconv`` or an external diff driver for a path pattern, and ``git show`` on a matching
    file runs it. Every content-producing call below therefore carries ``--no-textconv`` and
    ``--no-ext-diff``, which is verified to neutralise it; the agent, having no shell, cannot
    reach the vulnerable form at all.
  * **A committed credential arrives through a diff.** The prompt's "do not open environment
    files" guard is written against filenames and a diff is not a filename. The patches written
    here exclude credential-shaped paths by git pathspec, so the material never reaches the
    model, the provider or a transcript in the first place. ``credential_paths_excluded`` in the
    returned summary says how many commits had something withheld.

What the brief contains is metadata, changed paths and patch text -- the same material the agent
read for itself before, minus the credential-shaped files and minus the ability to read anything
outside the checkout.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

import childenv

# Content-producing git, with the two repository-controlled execution hooks disabled. `git show`
# and `git log -p` run `.git/config`'s `diff.<driver>.textconv` and `diff.external` commands
# against matching paths; these two flags are what stop that, and a test pins it.
_NO_REPO_CODE = ("--no-textconv", "--no-ext-diff")

# Paths whose CONTENT never appears in a materialised patch. Written as git pathspecs, so the
# exclusion happens inside git rather than in a filter we have to get right afterwards. Broad on
# purpose: withholding the diff of a file called `secrets.yml` costs a little history detail,
# and including it costs a credential. Patterns that would routinely catch ordinary source --
# `*token*`, `*secret*` as bare substrings -- are deliberately absent.
_CREDENTIAL_GLOBS = (
    ".env", ".env.*", "*.env", ".envrc",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".htpasswd", ".dockercfg",
    "*.pem", "*.key", "*.pfx", "*.p12", "*.p8", "*.jks", "*.jceks", "*.keystore", "*.ppk",
    "*.asc", "*.gpg", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "credentials", "credentials.*", "*.credentials",
    "secrets.*", "*.secrets", "secrets.yaml", "secrets.yml", "secrets.json",
    "service-account*.json", "*serviceaccount*.json",
    "kubeconfig", "*.kubeconfig",
)
_EXCLUDE_PATHSPECS = tuple(f":(exclude,glob,icase)**/{glob}" for glob in _CREDENTIAL_GLOBS)

# Kept in step with git_history.MIN_CHURN / MAX_CHURN and its `mineable_commits` rule: a commit
# is substantive when it touches at least two first-party implementation files with a plausible
# amount of churn. The same definition in both places means the table the model reads and the
# count the rubric consumes describe the same commits.
MIN_CHURN, MAX_CHURN = 20, 10_000
MIN_FILES = 2

_NON_IMPL = re.compile(
    r"(^|/)(node_modules|vendor|third_party|dist|build|out|target|\.git|"
    r"__pycache__|\.venv|venv|site-packages)/|"
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Gemfile\.lock|"
    r"composer\.lock|Cargo\.lock|go\.sum|requirements\.txt\.lock)$|"
    r"\.(min\.js|min\.css|map|snap|lock|svg|png|jpe?g|gif|ico|woff2?|ttf|eot|pdf|mp4)$",
    re.I,
)

DEFAULT_MAX_COMMITS = 160
DEFAULT_MAX_DIFF_BYTES = 200_000
DEFAULT_MAX_TOTAL_BYTES = 48_000_000
_GIT_TIMEOUT = 900
_PATHS_SHOWN_PER_COMMIT = 8

DIFF_SUBDIR = "commit-diffs"
OVERVIEW_NAME = "HISTORY-OVERVIEW.md"


def _git(repo: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> str:
    """Read-only git with a fixed argv. Empty string on any failure.

    `core.quotepath=false` keeps non-ASCII paths literal so the pattern tables above still see
    the real extension. The environment is built rather than inherited, so no credential of the
    operator's is in scope for a subprocess pointed at somebody else's repository.
    """
    try:
        proc = subprocess.run(
            ("git", "-c", "core.quotepath=false", "-C", str(repo), "--no-pager", *args),
            capture_output=True, text=True, errors="replace", timeout=timeout,
            env=childenv.build_env(childenv.STATIC, passthrough=("HOME", "USERPROFILE")),
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _parse_log(raw: str) -> list[dict]:
    """Parse `git log --numstat` with our record separator into one dict per commit."""
    commits: list[dict] = []
    for record in raw.split("\x00"):
        lines = [ln for ln in record.splitlines() if ln.strip()]
        if not lines:
            continue
        header = lines[0].split("\x1f")
        if len(header) < 3:
            continue
        sha, when, subject = header[0], header[1], header[2]
        paths: list[str] = []
        impl = 0
        churn = 0
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, path = parts
            paths.append(path)
            if not _NON_IMPL.search(path):
                impl += 1
            for value in (added, removed):
                if value.isdigit():
                    churn += int(value)
        commits.append({"sha": sha, "when": when, "subject": subject,
                        "paths": paths, "impl": impl, "churn": churn})
    return commits


def substantive_commits(repo: Path, ref: str | None = None) -> list[dict]:
    """Every commit that looks like real multi-file development, newest first.

    The same rule `git_history.mineable_commits` counts: at least two first-party
    implementation files, churn inside the plausible band. Merges are excluded because a merge
    commit's diff is the work of the commits under it.
    """
    scope = (ref,) if ref else ("--all",)
    raw = _git(repo, "log", *scope, "--no-merges", "--numstat",
               "--date=short", "--format=%x00%H%x1f%cd%x1f%s")
    return [c for c in _parse_log(raw)
            if c["impl"] >= MIN_FILES and MIN_CHURN <= c["churn"] <= MAX_CHURN]


def _sample(commits: list[dict], limit: int) -> list[dict]:
    """At most `limit` commits, spread evenly across the whole list rather than taken off the top.

    Taking the newest N would hide every structural change a long history went through, which is
    one of the nine things the census exists to count.
    """
    if limit <= 0 or len(commits) <= limit:
        return list(commits)
    step = len(commits) / limit
    return [commits[int(i * step)] for i in range(limit)]


def is_credential_path(path: str) -> bool:
    """Whether git's exclusion pathspecs will withhold this path's content.

    The same globs, matched in Python against the path list `git log --numstat` already gave us,
    so `_patch` needs one git call rather than three. `_EXCLUDE_PATHSPECS` remains the mechanism
    -- this only reports what that mechanism did.
    """
    name = path.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatch(name, glob.lower()) for glob in _CREDENTIAL_GLOBS)


def _patch(repo: Path, commit: dict) -> tuple[str, bool]:
    """One commit's patch, with credential-shaped paths withheld by git pathspec.

    Returns (text, something_was_withheld).
    """
    text = _git(repo, "show", commit["sha"], *_NO_REPO_CODE, "--format=%H%n%cd%n%s%n",
                "--date=short", "--", *_EXCLUDE_PATHSPECS)
    return text, any(is_credential_path(p) for p in commit["paths"])


def write_brief(repo: Path, dest: Path, *, ref: str | None = None,
                max_commits: int = DEFAULT_MAX_COMMITS,
                max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
                max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES) -> dict:
    """Write the brief into `dest` and return a summary of what was written.

    The summary is diagnostic only -- nothing in it is emitted. `ok` is False when the
    repository yielded no history at all, which is the signal the caller needs to say "this
    lane ran without history" rather than "this repository has none".
    """
    dest = Path(dest)
    diffs = dest / DIFF_SUBDIR
    diffs.mkdir(parents=True, exist_ok=True)

    commits = substantive_commits(repo, ref)
    chosen = _sample(commits, max_commits)

    written: dict[str, str] = {}
    withheld_count = 0
    total = 0
    for index, commit in enumerate(chosen):
        if total >= max_total_bytes:
            break
        text, withheld = _patch(repo, commit)
        if not text.strip():
            continue
        if withheld:
            withheld_count += 1
            text += ("\n[one or more credential-shaped files changed in this commit; their "
                     "contents are deliberately not included]\n")
        if len(text) > max_diff_bytes:
            text = text[:max_diff_bytes] + "\n[patch truncated at the size limit]\n"
        name = f"{index:04d}-{commit['sha'][:12]}.diff"
        (diffs / name).write_text(text, encoding="utf-8")
        written[commit["sha"]] = name
        total += len(text)

    refs = [ln.strip() for ln in
            _git(repo, "for-each-ref", "--format=%(refname:short)").splitlines() if ln.strip()]
    (dest / OVERVIEW_NAME).write_text(
        _overview(repo, commits, written, refs, withheld_count), encoding="utf-8")
    return {"ok": bool(commits), "commits_substantive": len(commits),
            "patches_written": len(written), "credential_paths_excluded": withheld_count,
            "bytes": total, "overview": str(dest / OVERVIEW_NAME), "diffs": str(diffs)}


def _cell(text: str) -> str:
    """One markdown table cell: pipes escaped so a commit subject cannot break the table."""
    return text.replace("|", r"\|")


def _overview(repo: Path, commits: list[dict], written: dict[str, str], refs: list[str],
              withheld: int) -> str:
    reachable = _git(repo, "rev-list", "--all", "--count").strip() or "unknown"
    lines = [
        "# Development history of the repository under measurement",
        "",
        "This file and the patches beside it were produced by the measurement tool, not by the",
        "repository. They exist because the agent reading them has no shell: everything below was",
        "computed with a fixed, read-only `git` argv before the session started.",
        "",
        f"- refs in the repository: {len(refs)}",
        f"- commits reachable from all refs: {reachable}",
        f"- commits that look like real multi-file development: {len(commits)}",
        f"- of those, full patches written to `{DIFF_SUBDIR}/`: {len(written)}",
    ]
    if withheld:
        lines.append(f"- patches with credential-shaped files withheld: {withheld}")
    if len(commits) > len(written):
        lines.append("- the patches were sampled at even intervals across the whole history, so "
                     "the oldest work is represented as well as the newest")
    lines += [
        "",
        "## Substantive commits, newest first",
        "",
        f"`patch` names the file in `{DIFF_SUBDIR}/` holding that commit's full diff, and is "
        "empty for a commit that only has the summary line below.",
        "",
        "| commit | date | files | churn | patch | subject | changed paths |",
        "|---|---|---|---|---|---|---|",
    ]
    for commit in commits:
        name = written.get(commit["sha"], "")
        # Credential-shaped paths are withheld by NAME as well as by content. "there is a file
        # called .env and it changed in this commit" is exactly the fact the census promises
        # never to record, and a path list is a place it would otherwise be recorded.
        shown = [p for p in commit["paths"] if not is_credential_path(p)]
        paths = shown[:_PATHS_SHOWN_PER_COMMIT]
        more = "" if len(shown) <= _PATHS_SHOWN_PER_COMMIT else \
            f" +{len(shown) - _PATHS_SHOWN_PER_COMMIT} more"
        lines.append(
            f"| `{commit['sha'][:12]}` | {commit['when']} | {len(commit['paths'])} | "
            f"{commit['churn']} | {('`' + name + '`') if name else ''} | "
            f"{_cell(commit['subject'])[:160]} | "
            f"{', '.join(_cell(p) for p in paths)}{more} |")
    lines.append("")
    return "\n".join(lines)


def stat_sample(repo: Path, ref: str, n: int = 20) -> str:
    """A text sample of the history's SHAPE: `git show --stat` for n commits, evenly spread.

    Diff statistics only -- no file content, no author. This is what the development-substance
    judgement used to gather for itself with a shell; computing it here means that call can run
    with no tools at all.
    """
    shas = [ln.strip() for ln in
            _git(repo, "log", "--no-merges", "--format=%H", ref, "-n", "500").splitlines()
            if ln.strip()]
    if not shas:
        return ""
    picked = shas if len(shas) <= n else [shas[int(i * (len(shas) / n))] for i in range(n)]
    blocks = []
    for sha in picked:
        stat = _git(repo, "show", sha, "--stat", *_NO_REPO_CODE, "--format=%H%n%s",
                    "--", *_EXCLUDE_PATHSPECS)
        if stat.strip():
            blocks.append(stat.strip())
    return "\n\n".join(blocks)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write a repository's development history out as read-only files.")
    parser.add_argument("repo", help="Path to the git repository to summarise.")
    parser.add_argument("--out", required=True, help="Directory to write the brief into.")
    parser.add_argument("--max-commits", type=int, default=DEFAULT_MAX_COMMITS)
    parser.add_argument("--max-diff-bytes", type=int, default=DEFAULT_MAX_DIFF_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--ref", default=None, help="Restrict to one ref (default: every ref).")
    args = parser.parse_args()

    summary = write_brief(Path(args.repo).expanduser(), Path(args.out).expanduser(),
                          ref=args.ref, max_commits=args.max_commits,
                          max_diff_bytes=args.max_diff_bytes,
                          max_total_bytes=args.max_total_bytes)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""git_lane.py -- ref selection, development history, and repository controls.

Two things here are less obvious than they look and both were learned the hard way on a
417-repository corpus:

  * `HEAD` and `master` are not reliable. 206 of 417 repositories kept their real history on
    some other ref, and trusting HEAD scored dozens of them as nearly empty. We enumerate the
    candidate refs with cheap aggregate evidence, let a model pick the one that best represents
    the working codebase, and report which one we used.
  * A commit count is not a measure of development. Bots, lockfile-only churn and bulk
    anonymisation passes inflate it. `mineable_commits` counts only commits that touch at
    least two first-party implementation files with 20-10,000 lines of churn.

Both judgements above used to be pure heuristics and both were wrong in the same way: they
substituted a number that is easy to compute for the question actually being asked. "Most
reachable commits" is not "holds the codebase", and a regex list written by someone who has
worked in Python and JavaScript does not know what vendoring looks like in an unfamiliar
ecosystem. So the two JUDGEMENTS are now agentic while the COUNTING stays deterministic:

  * Ref selection: candidates and their evidence are enumerated by git; the CHOICE is the
    model's. It is shown ref names and aggregate integers only -- never a file's contents --
    so the sampling boundary of this lane does not move.
  * Development substance: `mineable_commits` is unchanged, still deterministic, and still the
    number the rubric consumes. Alongside it the model samples the history and reports, as
    `development_substance` 0-4, how much of that history is real multi-file development rather
    than bulk imports, vendoring, generated churn, reformatting or anonymisation passes. That
    figure is advisory and is deliberately NOT fed back into `mineable_commits`.

When `claude` is unavailable neither judgement is faked: ref selection falls back to the
deepest-history rule, `development_substance` is None (never 0 -- an unmeasured judgement must
never read as a bad one), and `history_probe_mode` records which path ran.

Nothing in this module reads file contents, and nothing it returns contains a path, an author
name or a commit message.
"""
from __future__ import annotations

import json
import os
import hashlib
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import childenv
import history_brief

from redact import redact

# Files that are not first-party implementation. Churn here is not development.
_NON_IMPL = re.compile(
    r"(^|/)(node_modules|vendor|third_party|dist|build|out|target|\.git|"
    r"__pycache__|\.venv|venv|site-packages)/|"
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Gemfile\.lock|"
    r"composer\.lock|Cargo\.lock|go\.sum|requirements\.txt\.lock)$|"
    r"\.(min\.js|min\.css|map|snap|lock|svg|png|jpe?g|gif|ico|woff2?|ttf|eot|pdf|mp4)$",
    re.I,
)
_TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__|e2e)/|(^|/)(test_|conftest)|"
                        r"[._-](test|spec)\.[a-z0-9]+$", re.I)
# Automation, identified by the things that actually mark a machine account. Kept in step with
# `git_stats.BOT_NAME_PATTERNS`; a test pins the two against the same table.
#
# `noreply` used to be in here and is deliberately gone. GitHub hands every account with email
# privacy enabled a `<id>+<user>@users.noreply.github.com` address and uses it for every web-UI
# commit and every squash merge, so matching it filed most of a normal repository's humans as
# robots: their commits were dropped from `real_commits` and `mineable_commits` -- which is
# `capacity` in the emitted row and the multiplier in `mining_rank` -- and `human_authors`
# collapsed. Observed on a 300-commit fastapi clone: 17 human commits discarded and
# `human_authors = 1`. A privacy address is not evidence of anything.
_BOT = re.compile(
    # GitHub Apps author as `<slug>[bot]`. No trailing `\b`: `]` is a non-word character and so
    # is the space or `@` that follows it, so `\b\[bot\]\b` could never match a real haystack.
    r"\[bot\]|"
    # Handles that are unambiguous automation on their own.
    r"\b(dependabot|renovate|greenkeeper|snyk-bot|github-actions|actions-user|mergify|"
    r"pre-commit-ci|whitesource|imgbot|allcontributors|restyled|scala-steward|pyup-bot|"
    r"depfu|travis-ci|circleci|buildkite|semantic-release|anonymi[sz]er)\b|"
    # `jenkins` is also a surname, so it needs a machine suffix before it counts.
    r"\bjenkins[-_. ]?(ci|bot|build|builder|agent|server)\b|"
    # A bare `bot` token: `renovate-bot`, `some_bot`, `bot@ci.internal`.
    r"\bbot\b",
    re.I,
)

MIN_CHURN, MAX_CHURN = 20, 10_000

# Agentic defaults. `timeout` below bounds the ADDED wall time -- the two model calls together,
# never each -- so a caller can cap this lane's extra cost with one number.
#
# The model is the shared default from models.py, not a cheaper one. A cheaper model is defensible
# here on the merits -- both calls reason over small tables of counts rather than over source, and
# that is what this lane originally did. It is still wrong: a run that reports one model while a
# second, unnamed one chose the ref, and therefore chose which TREE every other lane went on to
# measure, cannot be reproduced from its own output. Cost is the wrong thing to optimise at the
# one point where a judgement decides what gets measured. A caller who means it can still pass
# `model=` explicitly.
from models import DEFAULT_MODEL  # noqa: E402
DEFAULT_TIMEOUT = 420
_REF_CHOICE_SHARE = 0.30      # of the budget; the substance sample needs the larger half

MAX_REFS_ENUMERATED = 400     # matches the pre-existing bound on ref enumeration
MAX_CANDIDATES = 12           # refs we gather expensive tree evidence for, and show the model
SUBSTANCE_SAMPLE_CAP = 500    # upper bound we accept back for a sample size, for sanity only



# Per-process salt for author cardinality. See git_stats.author_key for the rationale: this is the
# only thing derived from an email address in this module, it counts distinct humans, and it
# identifies nobody once the process exits.
_AUTHOR_SALT = secrets.token_bytes(32)


def _author_key(email: str) -> str:
    return hashlib.blake2b(email.strip().lower().encode("utf-8", "replace"),
                           key=_AUTHOR_SALT, digest_size=8).hexdigest()


def _git(repo: Path, *args: str, timeout: int = 600) -> str:
    # -c core.quotepath=false: without it git octal-escapes non-ASCII bytes in paths and wraps
    # the whole path in quotes. `/` survives either way (git uses `/` internally on every
    # platform, including Windows, so _NON_IMPL and _TEST_PATH are safe), but literal UTF-8
    # keeps the extension anchors in those patterns matching on non-English trees.
    r = subprocess.run(("git", "-c", "core.quotepath=false", "-C", str(repo)) + args,
                       capture_output=True, text=True, errors="replace", timeout=timeout)
    return r.stdout if r.returncode == 0 else ""


# --- ref selection ------------------------------------------------------------------------

def deepest_ref(repo: Path) -> tuple[str | None, int]:
    """The ref with the most reachable commits. The fallback when the model is unavailable.

    Kept exactly as it was: it is the documented deterministic behaviour and the only thing
    standing between an unavailable model and a repository scored off a placeholder branch.
    """
    refs = [l for l in _git(repo, "for-each-ref", "--format=%(refname)").split() if l]
    if not refs:
        head = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        return (head or None), 0
    best, best_n = None, -1
    for ref in refs[:MAX_REFS_ENUMERATED]:
        out = _git(repo, "rev-list", "--count", ref, timeout=300).strip()
        if out.isdigit() and int(out) > best_n:
            best, best_n = ref, int(out)
    return best, max(best_n, 0)


def _ref_kind(refname: str) -> str:
    if refname.startswith("refs/tags/"):
        return "tag"
    if refname.startswith("refs/remotes/"):
        return "remote"
    if refname.startswith("refs/heads/"):
        return "local"
    return "other"


def ref_candidates(repo: Path, limit: int = MAX_CANDIDATES) -> list[dict]:
    """Candidate refs with cheap aggregate evidence. Fully deterministic.

    Evidence per candidate: reachable commits, files in the tree, distinct top-level entries,
    date of the tip commit, whether it is a local branch / remote branch / tag, and how many
    other refs point at the same commit. That is enough to tell a working codebase from a
    placeholder branch and it is all aggregate -- no filename and no file content is collected,
    so nothing the model sees can carry source detail.

    Three bounds keep the cost flat on a repository with hundreds of refs: enumeration stops at
    MAX_REFS_ENUMERATED, refs sharing a tip commit collapse to one candidate (a mirror with 40
    remote branches at the same sha is one candidate, not 40), and the expensive tree walk runs
    only for the `limit` deepest survivors.
    """
    raw = _git(repo, "for-each-ref", "--format=%(refname)%09%(objecttype)%09%(objectname)"
                                    "%09%(*objectname)")
    rows: list[tuple[str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        refname, objtype = parts[0], parts[1]
        # An annotated tag's own object is not a commit; %(*objectname) is what it points at.
        sha = (parts[3] if objtype == "tag" and len(parts) > 3 and parts[3] else parts[2])
        if sha:
            rows.append((refname, sha))
        if len(rows) >= MAX_REFS_ENUMERATED:
            break

    if not rows:
        return []

    # Collapse aliases. Representative is the first in git's own (bytewise refname) order, which
    # is what deepest_ref would have returned for the same tie -- so the fallback and the agentic
    # path never disagree merely about which alias to name.
    groups: dict[str, dict] = {}
    for refname, sha in rows:
        g = groups.setdefault(sha, {"ref": refname, "sha": sha, "aliases": 0})
        g["aliases"] += 1

    for g in groups.values():
        out = _git(repo, "rev-list", "--count", g["sha"], timeout=300).strip()
        g["commits"] = int(out) if out.isdigit() else 0

    ranked = sorted(groups.values(), key=lambda g: (-g["commits"], g["ref"]))[:limit]
    for g in ranked:
        # ls-tree -r lists every blob; the unrecursive -d form gives top-level directories.
        # Both are counted, never retained.
        g["files"] = sum(1 for _ in _git(repo, "ls-tree", "-r", "--name-only", g["sha"],
                                         timeout=180).splitlines() if _.strip())
        g["top_dirs"] = sum(1 for _ in _git(repo, "ls-tree", "-d", "--name-only", g["sha"],
                                            timeout=120).splitlines() if _.strip())
        g["last_commit"] = _git(repo, "log", "-1", "--format=%cs", g["sha"],
                                timeout=120).strip() or "unknown"
        g["kind"] = _ref_kind(g["ref"])
    return ranked


# The two prompts below are fixed strings. Both were written to keep the model's view aggregate:
# the ref-choice call is given no tools and no repository access at all, and the substance call is
# told to stay on diff statistics.
REF_PROMPT = """\
You are choosing which git ref of ONE repository best represents its working codebase. You are
given only ref names and aggregate numbers. There is no file content here and you cannot read
any -- decide from the table alone.

Context you need: this repository is a mirror from a corpus in which the default branch is
frequently NOT where the code lives. Half the corpus kept its real tree on a feature branch, an
integration branch or a fork's branch while the default branch held a placeholder, a readme or a
single configuration file.

Pick the ref whose TREE most plausibly holds the whole codebase. Reachable commit count is
corroborating evidence, not the answer:

  * A broad tree -- many files spread over several top-level directories -- is a working
    codebase. A tree of a handful of files is a placeholder however long its history.
  * A long history on a nearly-empty tree is a documentation, configuration or release branch,
    or a history that was later moved elsewhere. Do not pick it over a real tree.
  * Between candidates of comparable tree size, prefer more history, then more recent activity.
  * A tag is usually a snapshot of a branch; prefer the branch unless the tag plainly holds more.
  * Refs pointing at the same commit are already collapsed into one row; `aliases` says how many.

CANDIDATES
index | kind | aliases | reachable commits | files in tree | top-level dirs | last commit
{table}

Return ONLY a JSON object, no prose around it:
{{"choice": <index from the table>, "reason": "<one sentence>"}}

The reason must explain the choice IN TERMS OF THE NUMBERS and must not contain any ref, branch
or tag name, any file name or any path -- for example "much the largest tree, spread over the
most top-level directories, with the deepest history of any candidate". A reason that names a
ref is thrown away and replaced by a fixed code, which is a worse outcome than plain wording.
"""

SUBSTANCE_PROMPT = """\
You are auditing the commit history of ONE repository to judge how much of it is real software
development. The ref to audit is `{ref}`.

You have no tools of any kind. Everything you need is below: a sample of about twenty commits
spread evenly from oldest to newest -- NOT just the tip, because the tip of an anonymised mirror
is routinely unrepresentative of everything under it -- each shown as its subject line and its
diff STATISTICS. No file content and no author name appears, by construction rather than by
instruction: a bot or a scrub pass is identifiable from its subject and its file pattern, and
putting a person's name in front of a provider to save one heuristic is not a trade this tool
makes.

THE SAMPLE
{sample}
END OF SAMPLE

Classify each sampled commit into exactly ONE bucket:
  * development -- several first-party source files changed together with a plausible amount of
    churn: the shape of a person building, extending or repairing something.
  * bulk_import -- an initial dump, or a wholesale addition of an entire tree or of a vendored
    third-party dependency.
  * generated -- churn dominated by lockfiles, build output, minified or bundled assets,
    generated clients, compiled artefacts or data files.
  * reformat_or_rename -- mass mechanical change: reformatting, renames, moves, licence headers,
    import reordering.
  * scrub -- an anonymisation or identifier-rewrite pass touching most of the tree at once.
  * trivial -- single-file or near-zero-churn edits, version bumps, configuration tweaks, bot
    commits.

Judge by THIS ecosystem's own conventions. Where dependencies are vendored, how tests are named
and where generated code lands all differ by language and by decade; do not assume the habits of
any one stack. If the layout is unfamiliar, say so in the note rather than guessing.

Then score DEVELOPMENT SUBSTANCE 0-4 for the history as a whole:
  4 -- essentially every sampled commit is development; a history of sustained real work.
  3 -- most sampled commits are development, with some noise.
  2 -- mixed: real development exists but a large share is import, generated or mechanical churn.
  1 -- mostly import, generated, mechanical or scrub churn, with a little real work.
  0 -- no sampled commit represents development.

Return ONLY a JSON object, no prose around it:
{{"sampled": <int>, "development": <int>, "bulk_import": <int>, "generated": <int>,
  "reformat_or_rename": <int>, "scrub": <int>, "trivial": <int>,
  "substance": <int 0-4>,
  "note": "<at most two sentences>"}}

The note is plain English about the SHAPE of the history. It must contain no path, no file name,
no directory name, no author name, no commit message, no commit hash and no code. Anything
naming code is stripped before the note is kept, so a note that names a file loses the part that
mattered. Good: "Two thirds of the sample is one person adding features across several modules;
the remainder is an initial bulk import and a tree-wide identifier rewrite."
"""


# --- calling the model --------------------------------------------------------------------
#
# This lane deliberately carries its own copy of the claude invocation and JSON extraction
# rather than importing the census lane's. run.py treats a git_history failure as FATAL to the
# whole run, so history collection must not be able to break because a different lane's private
# helper was renamed. Independence is worth the duplication here.

def _claude_argv(cli: str) -> list[str]:
    """argv prefix for the CLI. On Windows `claude` resolves to a .cmd shim.

    CreateProcess will not execute a batch file directly, so a resolved .cmd/.bat has to go
    through the command interpreter. Still a list -- no shell string is ever built, so nothing
    here is interpolated into a shell.
    """
    if sys.platform == "win32" and cli.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", cli]
    return [cli]


def _extract_json(stdout: str):
    """claude -p --output-format json wraps the result; the payload may also be fenced."""
    try:
        outer = json.loads(stdout)
        inner = outer.get("result") if isinstance(outer, dict) else None
        if isinstance(inner, dict):
            return inner
        text = inner if isinstance(inner, str) else stdout
    except json.JSONDecodeError:
        text = stdout
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _ask(cli: str, prompt: str, model: str, timeout: int, *,
         cwd: Path | None = None, tools: str | None = None,
         add_dir: Path | None = None) -> tuple[dict | None, str]:
    """One `claude -p` call. Returns (payload, error). Never raises.

    `tools=None` means no tools at all: the ref-choice call must not be able to look at the
    repository, which is what makes its aggregate-only view a property of the code rather than a
    promise in a prompt.
    """
    if timeout <= 0:
        return None, "no time left in the agentic budget"
    try:
        isolation = childenv.isolation_flags("claude", cli)
    except childenv.ProviderNotIsolated:
        # Our own words: this string can reach an emitted field, and the exception text names
        # flags and configuration files that the leak audit would have to scrub.
        return None, "this CLI cannot be isolated from configuration supplied by the repository"
    cmd = _claude_argv(cli) + ["-p", prompt, "--output-format", "json", "--model", model,
                               *isolation]
    if add_dir is not None:
        cmd += ["--add-dir", str(add_dir)]
    cmd += ["--allowedTools", tools if tools else ""]
    try:
        # Authentication is the subprocess's own business: nothing here reads, logs or stores a key.
        p = subprocess.run(cmd, cwd=(str(cwd) if cwd else None), capture_output=True, text=True,
                           errors="replace", timeout=timeout,
                           env=childenv.build_env(
                               childenv.MODEL, provider="claude",
                               passthrough=("HOME", "USERPROFILE", "XDG_CONFIG_HOME")))
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except OSError as e:
        return None, f"could not start claude: {type(e).__name__}"
    if p.returncode != 0:
        return None, f"claude exited {p.returncode}"
    payload = _extract_json(p.stdout)
    if payload is None:
        return None, "no parseable JSON in the reply"
    return payload, ""


def _clamp(v, lo: int, hi: int, default: int | None = None) -> int | None:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


# A reason is only worth reporting if it survives redaction as readable English. Ref names are
# paths, so redact() turns one into "[path]" -- and a sentence that was mostly a ref name comes
# back as placeholder soup that reads like a measurement rather than a reason. When that happens
# we report a fixed code derived from the evidence we already hold, which is honest about being
# a code and cannot mislead. The alternative -- exempting this field from the audit so ref names
# pass through -- would put a repository-derived string into the output unchecked, and this lane's
# whole promise is that it does not do that.
_REASON_MIN_WORDS = 6
_REASON_CODES = {
    "largest_tree": "chose the candidate with the largest working tree",
    "largest_tree_and_deepest_history": ("chose the candidate with both the largest working tree "
                                         "and the deepest history"),
    "deepest_history": "chose the candidate with the deepest reachable history",
    "model_choice_recorded": ("model chose this candidate; its wording named a ref and was "
                              "discarded"),
}


def _reason_code(chosen: dict, candidates: list[dict]) -> str:
    """A fixed, non-leaking reason built from the numbers we measured ourselves."""
    max_files = max(c.get("files", 0) for c in candidates)
    max_commits = max(c.get("commits", 0) for c in candidates)
    biggest = chosen.get("files", 0) >= max_files
    deepest = chosen.get("commits", 0) >= max_commits
    if biggest and deepest:
        return _REASON_CODES["largest_tree_and_deepest_history"]
    if biggest:
        return _REASON_CODES["largest_tree"]
    if deepest:
        return _REASON_CODES["deepest_history"]
    return _REASON_CODES["model_choice_recorded"]


def _safe_reason(raw: str, chosen: dict, candidates: list[dict]) -> str:
    """Redact the model's sentence; substitute a fixed code if redaction gutted it."""
    cleaned, _ = redact(str(raw or ""))
    words = [w for w in cleaned.split() if not w.startswith("[")]
    if len(words) < _REASON_MIN_WORDS or cleaned.count("[") > 1:
        return _reason_code(chosen, candidates)
    return cleaned[:300]


def choose_ref(repo: Path, cli: str | None, model: str, timeout: int
               ) -> tuple[str | None, int, int, str, str, bool]:
    """Pick the ref to analyse.

    Returns (ref, reachable_commits, n_candidates, reason, mode, overrode_deterministic) where
    mode is "agentic" or "deterministic". Any failure at all -- no CLI, no candidates, a timeout,
    an out-of-range index -- lands on the deterministic rule with a reason saying so.
    """
    cands = ref_candidates(repo)
    if not cands:
        ref, n = deepest_ref(repo)
        return ref, n, 0, "no ref candidates could be enumerated", "deterministic", False

    # Reproduce deepest_ref's tie-break (first in bytewise refname order) without a second walk,
    # so "what the fallback would have said" is exact rather than approximately right.
    best_n = max(c["commits"] for c in cands)
    fallback = min((c for c in cands if c["commits"] == best_n), key=lambda c: c["ref"])

    if cli is None:
        return (fallback["ref"], fallback["commits"], len(cands),
                "the model was unavailable; used the deepest reachable history",
                "deterministic", False)

    table = "\n".join(
        f"{i} | {c['kind']} | {c['aliases']} | {c['commits']} | {c['files']} | "
        f"{c['top_dirs']} | {c['last_commit']}   ({c['ref']})"
        for i, c in enumerate(cands))
    payload, err = _ask(cli, REF_PROMPT.format(table=table), model, timeout)
    idx = _clamp((payload or {}).get("choice"), 0, len(cands) - 1) if payload else None
    if payload is None or idx is None:
        return (fallback["ref"], fallback["commits"], len(cands),
                f"ref choice unavailable ({err or 'no index returned'}); used the deepest "
                f"reachable history", "deterministic", False)

    chosen = cands[idx]
    return (chosen["ref"], chosen["commits"], len(cands),
            _safe_reason(payload.get("reason"), chosen, cands),
            "agentic", chosen["ref"] != fallback["ref"])


def development_substance(repo: Path, ref: str, cli: str | None, model: str, timeout: int
                          ) -> dict:
    """Sampled judgement of how much of the history is real development.

    Advisory only. It sits BESIDE `mineable_commits`, which the rubric consumes and which this
    never touches: a model-sampled proportion and a deterministic count answer different
    questions, and quietly blending them would make the rubric's input unauditable.
    """
    if cli is None:
        return {"development_substance": None,
                "development_substance_note": None,
                "development_substance_error": "the model was unavailable"}
    # No tools, and therefore no shell. This call used to be granted `Bash` so it could sample
    # the history itself; `--add-dir` does not bound `Bash`, so that was a shell on the
    # operator's machine to answer a question about diff statistics. The sample is computed here,
    # with a fixed read-only git argv, and passed in the prompt.
    sample = history_brief.stat_sample(repo, ref)
    if not sample.strip():
        return {"development_substance": None,
                "development_substance_note": None,
                "development_substance_error": "no commits could be sampled from this ref"}
    payload, err = _ask(cli, SUBSTANCE_PROMPT.format(ref=ref, sample=sample), model, timeout)
    if payload is None:
        return {"development_substance": None,
                "development_substance_note": None,
                "development_substance_error": err}
    note, _ = redact(str(payload.get("note") or ""))
    sampled = _clamp(payload.get("sampled"), 0, SUBSTANCE_SAMPLE_CAP)
    return {
        "development_substance": _clamp(payload.get("substance"), 0, 4),
        "development_substance_note": note[:400] or None,
        "development_sampled_commits": sampled,
        "development_real_in_sample": _clamp(payload.get("development"), 0,
                                             sampled if sampled is not None
                                             else SUBSTANCE_SAMPLE_CAP),
    }


def anonymizer_tip(repo: Path, ref: str) -> bool:
    """Some corpora carry a synthetic tip commit that rewrites identifiers tree-wide.

    It must not be counted as development, and history stats belong to its parent.
    """
    out = _git(repo, "log", "-1", "--format=%an|%ae|%s", ref)
    return bool(_BOT.search(out)) and "anonymi" in out.lower()


def collect(repo: Path, model: str = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT,
            agentic: bool = True, redact_refs: bool = False) -> dict:
    """History measurements plus the size controls. Never scored on directly.

    `timeout` bounds the ADDED wall time of both model calls together, not each. `agentic=False`
    forces the deterministic path for a caller that must not spend money -- the same path an
    absent `claude` produces, so it is also how the fallback is regression-tested.
    """
    out: dict = {"probe": "git_history", "ok": False}
    cli = shutil.which("claude") if agentic else None
    budget = max(0, int(timeout))
    ref_budget = int(budget * _REF_CHOICE_SHARE)

    t0 = time.monotonic()
    ref, n_ref_commits, n_cands, reason, mode, overrode = choose_ref(repo, cli, model, ref_budget)
    if not ref:
        out["error"] = "no refs found"
        return out
    # The ref NAME is resolved because git needs one, but it is a repository-derived string that
    # routinely carries product or customer identity. External callers pass redact_refs=True and
    # get null; the selection is still fully explained by ref_choice_reason below.
    out["ref_analysed"] = None if redact_refs else ref
    out["ref_commits"] = n_ref_commits
    out["ref_candidates_considered"] = n_cands
    out["ref_choice_reason"] = reason
    out["ref_choice_overrode_deepest"] = overrode
    out["anonymizer_tip_detected"] = anonymizer_tip(repo, ref)
    out["commit_sha"] = _git(repo, "rev-parse", ref).strip()[:40]

    # History belongs to the pre-anonymisation parent when a synthetic tip is present.
    hist_ref = f"{ref}~1" if out["anonymizer_tip_detected"] else ref

    authors: set[str] = set()
    mineable = real = 0
    cur_bot = False
    impl = churn = 0

    def close():
        nonlocal mineable, impl, churn, real
        if not cur_bot:
            real += 1
            if impl >= 2 and MIN_CHURN <= churn <= MAX_CHURN:
                mineable += 1
        impl = churn = 0

    log = _git(repo, "log", hist_ref, "--no-merges", "--numstat", "--no-renames",
               "--format=@@|%H|%an|%ae", "-n", "20000", timeout=1800)
    started = False
    for line in log.splitlines():
        if line.startswith("@@|"):
            if started:
                close()
            started = True
            _, _sha, an, ae = (line.split("|", 3) + ["", "", ""])[:4]
            cur_bot = bool(_BOT.search(f"{an} {ae}"))
            if not cur_bot:
                # Salted digest, not the address. The builtin hash() this replaced was only 32
                # bits wide, which collides inside a few tens of thousands of authors and would
                # UNDERCOUNT a large repo; blake2b keyed with a per-process salt is both
                # collision-safe at this scale and non-reversible off this machine.
                authors.add(_author_key(ae))
            continue
        if not line.strip() or not started:
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        add, dele, path = parts
        if _NON_IMPL.search(path):
            continue
        churn += (int(add) if add.isdigit() else 0) + (int(dele) if dele.isdigit() else 0)
        if not _TEST_PATH.search(path):
            impl += 1
    if started:
        close()

    # Whatever the ref call spent comes off the substance call's allowance, so the two together
    # stay inside `timeout` however slow the first one was.
    out.update(development_substance(repo, hist_ref, cli, model,
                                    budget - int(time.monotonic() - t0)))

    out.update({
        "real_commits": real,
        "mineable_commits": mineable,
        "human_authors": len(authors),
        "history_probe_mode": mode,
        "history_model": model if mode == "agentic" else None,
        "ok": True,
    })
    return out

#!/usr/bin/env python3
"""
agentic_miner.py — mine nuanced task candidates via a code-diving agent (strategy: agentic).

Usage:
    python agentic_miner.py <repo-path> [--n N] [--provider claude|codex|gemini]
                            [--model ID] [--timeout SECONDS] [--out PATH]

Arguments:
    repo-path           Path to the git repo to dive (code and full history).

Options:
    --n N               Ceiling on how many candidates are enumerated (default 40). This is
                        a census: the agent lists everything that clears the bar, up to this
                        number, and each type's count is the length of its own list.
    --provider NAME     Which agent CLI to spawn: claude (default), codex or gemini. A
                        provider that is not installed is an error, never a quiet zero.
    --model ID          Model id/alias passed to the provider (default: its own configured
                        model).
    --timeout SECONDS   Agent wall-clock timeout (default 1800). A census of a whole
                        repository is not a quick question.
    --out PATH          Also write the JSON result to PATH (stdout always gets it).

Environment:
    None read directly. The provider subprocess receives its own auth variables from
    childenv and nothing else on the machine. This script never accepts, logs, or
    persists a key.

Counting miners recover only the structure that is legible from diffs and test files.
They miss the *nuanced* tasks a human reviewer would flag: a subtle algorithm buried in
one module, a cross-cutting feature that spans several subsystems, an interesting
historical bug fix. This miner spawns a single headless agent pointed at the repo, lets
it read the code with Read/Grep/Glob and the development history from a pre-computed
brief, and asks it to propose the top-N ranked, long-horizon, hard-for-a-coding-model
task candidates, each grounded in real files / commits it actually inspected.

The agent has NO shell. `--add-dir` bounds the read tools and does not bound `Bash`, so
the shell this miner used to be given was access to the operator's whole machine, held
in place only by the model deciding not to use it. `collectors/history_brief.py` writes
the history out first -- every substantive commit with its patch, credential-shaped
files withheld by git pathspec -- and the agent reads that.

WHAT LEAVES: for the external variant, only a COUNT per task type plus ONE sentence per
candidate describing the engineering SHAPE of the work — "add cursor pagination to a
list endpoint backed by a relational store". The files, symbols and commits the agent
cited stay on this machine; they are what makes a candidate real, not what makes it
communicable. Every sentence is validated by `emit_allowlist.validate_sentence` here, at
the point it is created, and one that names a path, a symbol, a product or a company is
dropped whole — the candidate is still counted, it simply arrives without a description.

The agent must return STRICT JSON; we parse it robustly (it may wrap the array in
prose or a code fence) and map each item onto the shared `Candidate` schema. A
timeout, a non-zero exit or unparseable output yields an empty candidate list plus an
explanatory `notes` string. A provider CLI that is not installed is the one hard error:
it exits non-zero rather than reporting a repository as having nothing in it.

Output: JSON {strategy, repo, candidates:[…], notes} to stdout (and --out if given).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

# The emission boundary lives with the collectors; this module runs with cwd=miners/, so its
# directory goes on the path explicitly rather than relying on the caller's PYTHONPATH.
_COLLECTORS = Path(__file__).resolve().parents[1] / "collectors"
if str(_COLLECTORS) not in sys.path:
    sys.path.insert(0, str(_COLLECTORS))

import agent_io  # noqa: E402
import candidates as cand  # noqa: E402
import history_brief  # noqa: E402
from emit_allowlist import validate_sentence  # noqa: E402

# A census, not a shortlist: the ceiling is high enough that a rich repository is
# described by what it holds rather than by the number we were willing to hear.
_DEFAULT_N = 40
_DEFAULT_TIMEOUT = 1800
# No Bash. This miner is supposed to read git history, which is why it used to have a
# shell — but `--add-dir` bounds Read/Grep/Glob and does not bound Bash, so the grant
# was shell access to the operator's machine with nothing but the model's own judgement
# between it and their home directory. The history arrives pre-computed instead, from
# `collectors/history_brief.py`, as files this grant can read.
_TOOLS = ["Read", "Grep", "Glob"]


def _prompt(n: int, history: Path) -> str:
    return (
        "You are taking an EXHAUSTIVE CENSUS of the candidate tasks this git repository "
        "could support for training and evaluating a coding model. Its working tree is "
        "your current directory. You have no shell and no git: use your Read, Grep and "
        "Glob tools on the source code, and read the repository's development history "
        f"from the files prepared for you under {history} -- "
        f"{history}/HISTORY-OVERVIEW.md lists every substantive commit with its date, "
        f"changed files and churn, and {history}/commit-diffs/ holds the full patch for "
        "each of them.\n\n"
        "This is a census, not a top-N list. Enumerate every idea that clears the bar, "
        f"up to {n}. The count for each task type is simply how many ideas you listed "
        "for it, so never report a number you did not enumerate, and never pad: an "
        "ordinary idea listed to reach a round number makes every count less useful. "
        "Prefer long-horizon, multi-file, nuanced problems that pure metrics would "
        "miss: subtle algorithms, cross-module features, interesting real bug fixes. "
        "Every idea MUST come from code or commits you actually read — cite real file "
        "paths and commit SHAs and never invent one, because an idea resting on a file "
        "that is not there describes some other repository. Those citations stay on this "
        "machine. Budget 15-25 minutes and as many tool calls as the repository "
        "warrants.\n\n"
        "CLASSIFY EACH IDEA into exactly one `task_type`, using the type that describes "
        "how the task would be BUILT. Every type names an exercise that would be set up "
        "against a SANDBOXED COPY of a codebase like this one for a coding model to attempt. "
        "None of them is a change anyone makes to the owner's repository, and this tool "
        "writes nothing to it:\n"
        "  net_new        a substantial capability this codebase does not have yet but "
        "is shaped to support\n"
        "  bug_repair     reproduce and fix a subtle defect the history actually fixed\n"
        "  repo_evolution a structural change the codebase went through or needs: a "
        "migration, a split, an interface redesign\n"
        "  agentic        a nuanced long-horizon task that fits none of the above\n\n"
        "LEAVE THE OWNER'S CREDENTIALS ALONE. This is a census of engineering work, and a "
        "repository's own keys and passwords are no part of that: do not open environment "
        "files, credential files or key material, and do not build a task around them. The "
        "same holds if credential material reaches you another way -- inside a commit diff, "
        "a log, or a file you opened for a different reason -- because history contains "
        "whatever was once committed. Do not copy it, paraphrase it, or record where it "
        "was; none of that is a measurement and none of it belongs in your answer. This is "
        "about not collecting the owner's secrets, not about hiding anything from them: the "
        "run tells the owner how many files were withheld on this basis, and they read the "
        "whole artifact before deciding to send it.\n\n"
        "ONE FIELD LEAVES THIS MACHINE: `summary`. Everything else you return stays here "
        "and is counted, never quoted. `summary` is one sentence describing the ENGINEERING "
        "SHAPE of the work — what kind of problem it is, in words that would be just as true "
        "of the same problem in somebody else's codebase.\n"
        "  GOOD  add cursor pagination to a list endpoint backed by a relational store\n"
        "  GOOD  make a retry path idempotent so a replayed request cannot double-apply a "
        "balance change\n"
        "  GOOD  split a synchronous batch job into resumable chunks that survive a restart "
        "mid-run\n"
        "  BAD   fix the pagination bug in api/routes/orders.py     (names a path)\n"
        "  BAD   refactor OrderStateMachine to be reentrant          (names a symbol)\n"
        "  BAD   add Stripe webhook replay protection                (names a product)\n"
        "  BAD   improve the Acme billing reconciler                 (names a company)\n"
        "Rules for `summary`, all enforced — a summary that breaks one is DISCARDED and the "
        "idea is counted without a description:\n"
        "  * no file name, path, module name, function, class, variable or commit hash\n"
        "  * no product, company, service or project name; no domain proper noun\n"
        "  * at most 40 words; plain lower case except the first word\n"
        "  * no acronyms, no quotes, no parentheses, brackets, braces, slashes or dotted names\n"
        "  * no underscores and no capital letters inside a word\n"
        "\n"
        f"Return your answer as STRICT JSON and NOTHING else: a JSON array of up to {n} "
        "objects, as many as genuinely clear the bar, each with these keys:\n"
        '  "title": short task title (string)\n'
        '  "task_type": one of the four types above (string)\n'
        '  "summary": the one sentence described above (string)\n'
        '  "files": relevant file paths (array of strings)\n'
        '  "symbols": relevant functions/classes/symbols (array of strings)\n'
        '  "subsystems": subsystems/modules the task spans (array of strings)\n'
        '  "source_commit_shas": grounding commit SHAs, may be empty (array of strings)\n'
        '  "complexity": one of "Small", "Medium", "Large" (string)\n'
        '  "est_loc": estimated lines of code to write/rewrite (integer)\n'
        '  "has_strong_tests": whether the repo has strong tests pinning this behavior (bool)\n'
        '  "test_files": relevant test file paths — only tests that ALREADY EXIST '
        "(array of strings)\n"
        "Output only the JSON array."
    )


def _task_type(item: dict) -> str:
    """Which of the four task types this idea is, or `agentic` when it says something else.

    `agentic` is the honest bucket for an unrecognised answer: it is this lane's own name, so an
    idea that lands there is counted as "mined by the diving agent, type not established" rather
    than being quietly filed under a type nobody chose.
    """
    claimed = str(item.get("task_type") or item.get("strategy") or "").strip().lower()
    return claimed if claimed in cand.STRATEGIES else "agentic"


def _summary(item: dict) -> str:
    """The candidate's one emittable sentence, or "" when it may not leave.

    `summary` is what the current instruction asks for; `why_interesting` is what older
    instructions asked for and is still accepted so a re-run against an old transcript behaves.
    """
    text = str(item.get("summary") or item.get("why_interesting") or "").strip()
    return "" if not text or validate_sentence(text) else text


def _to_candidate(item: dict, repo_id: str, head_sha: str) -> dict:
    complexity = item.get("complexity", "Medium")
    if complexity not in cand.COMPLEXITY_BANDS:
        complexity = "Medium"
    files = [str(f) for f in item.get("files", []) or []]
    symbols = [str(s) for s in item.get("symbols", []) or []]
    subsystems = [str(s) for s in item.get("subsystems", []) or []]
    shas = [str(s) for s in item.get("source_commit_shas", []) or []]
    test_files = [str(t) for t in item.get("test_files", []) or []]
    try:
        est_loc = int(item.get("est_loc", 0) or 0)
    except (TypeError, ValueError):
        est_loc = 0
    strong_tests = bool(item.get("has_strong_tests", False))

    c = cand.Candidate(
        strategy=_task_type(item),
        source_commit_shas=shas,
        locus=cand.Locus(files=files, symbols=symbols, subsystems=subsystems),
        difficulty=cand.DifficultySignals(
            complexity=complexity,
            n_subsystems=len(subsystems),
            true_loc=est_loc,
            rewrite_scope_loc=est_loc,
        ),
        test_evidence=cand.TestEvidence(
            test_files=test_files,
            breadth=0.5 if strong_tests else 0.0,
        ),
        # The one field that can leave the machine, so it is gated here rather than scrubbed
        # later: a summary naming a path, a symbol or a product is dropped and the candidate is
        # emitted as a count with no description. There is no fallback to `title` -- a title is
        # a name for the thing, which is exactly what may not travel.
        why_interesting=_summary(item),
        provenance={"repo": repo_id, "head_sha": head_sha, "mined_from": "agentic"},
        # Without the title in the fingerprint, two different proposals over the same
        # file set collide on candidate_id and the inventory merge drops one of them.
        candidate_id=cand.make_candidate_id(
            _task_type(item), shas, [str(item.get("title") or ""), *files]),
    )
    return c.to_dict()


def mine(repo: Path, n: int, model: str | None, timeout: int, provider: str = "claude") -> dict:
    """Mine the repository, with its history pre-computed rather than shelled for.

    The scratch directory holding the history exists only for the length of the agent turn and
    is deleted on the way out, so the patches this lane needed are not left anywhere the
    operator was not told about.
    """
    repo_id = repo.name
    head_sha = agent_io.head_sha(repo)
    with tempfile.TemporaryDirectory(prefix="repo-quality-history-") as scratch:
        history = Path(scratch)
        history_brief.write_brief(repo, history)
        text, note = agent_io.run_agent(
            _prompt(n, history), cwd=repo, tools=_TOOLS, timeout=timeout, model=model,
            add_dir=repo, provider=provider, read_dirs=[history],
        )
    if text is None:
        return {"strategy": "agentic", "repo": repo_id, "candidates": [], "notes": note}

    parsed = agent_io.extract_json(text)
    if parsed is None:
        return {"strategy": "agentic", "repo": repo_id, "candidates": [],
                "notes": f"{note}, but could not parse JSON from the agent's output."}
    items = parsed if isinstance(parsed, list) else [parsed]
    cands = [_to_candidate(it, repo_id, head_sha) for it in items if isinstance(it, dict)]
    return {"strategy": "agentic", "repo": repo_id, "candidates": cands,
            "notes": f"{note}; agent proposed {len(cands)} candidate(s)."}


def main() -> int:
    p = argparse.ArgumentParser(
        description="Mine nuanced/long-horizon task candidates via a headless Claude Code agent.")
    p.add_argument("repo", help="Path to the git repo to dive.")
    p.add_argument("--n", type=int, default=_DEFAULT_N,
                   help=f"Ceiling on candidates enumerated (default {_DEFAULT_N}).")
    p.add_argument("--provider", choices=("claude", "codex", "gemini"), default="claude",
                   help="Agent CLI provider (default claude).")
    p.add_argument("--model", default=None,
                   help="Provider model id/alias (default: provider configuration).")
    p.add_argument("--timeout", type=int, default=_DEFAULT_TIMEOUT,
                   help=f"Agent wall-clock timeout in seconds (default {_DEFAULT_TIMEOUT}).")
    p.add_argument("--out", help="Also write the JSON result here.")
    args = p.parse_args()

    repo = Path(args.repo).expanduser()
    if not repo.is_dir():
        result = {"strategy": "agentic", "repo": repo.name, "candidates": [],
                  "notes": f"repo path {args.repo!r} is not a directory."}
    else:
        try:
            result = mine(repo, args.n, args.model, args.timeout, provider=args.provider)
        except (agent_io.ProviderUnavailable, agent_io.ProviderNotIsolated) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    payload = json.dumps(result, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload)
    print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

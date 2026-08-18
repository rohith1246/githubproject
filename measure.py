#!/usr/bin/env python3
"""measure.py -- privacy-preserving repo measurement (MEASURE stage, EXTERNAL).

Runs the repo-quality MEASURE stage. Every stat except the model-assisted lanes is deterministic:
the same commit always produces the same numbers. TWO lanes call a model:

  1. LOGIC DEPTH -- the material census (``collectors/material_census.py``), which reads the tree
     and history and returns a 0-6 logic-depth band, a self-containment score, and per-category
     counts of the substantial, minable material the repository holds.
  2. AGENTIC MINING -- ``miners/agentic_miner.py`` spawns one headless agent that dives the code
     and full history and proposes nuanced, long-horizon task candidates (the task-type census).
     Mining is imperative even for ext, but ext stays privacy-preserving: it emits per-task-type
     COUNTS and ONE SENTENCE per candidate describing the engineering SHAPE of the work. The files,
     symbols and commits behind an idea never leave the machine.

Both lanes run by default and ``--no-llm`` skips both; ``--no-mine`` skips only mining. Three
providers are supported -- claude (default), codex and gemini -- and a provider that is not
installed is an ERROR, checked before any measurement starts. It is not degraded to an unscored
lane: "codex is not on this machine" and "this repository held nothing" are different facts and
must not arrive looking the same. Every other model failure (throttle, timeout, refusal) is still
recorded unscored and the run completes. ``provider`` and ``model`` record what was REQUESTED;
because nothing ever falls back to another provider or model, that is also what was USED whenever
the lane's ``scored`` flag is true, and nothing at all was used when it is false.

It emits the exact schema in ``skills/repo-quality/CONTRACT.md`` -- the flat ``codebase_repos``
row (38 promoted fields + the MEASURE additions), the full ``measurement.json`` (``tree`` /
``git`` / ``classification`` blocks, an additive ``ext_signals`` block from ext's deterministic
structural/history/build collectors, and a ``material`` block carrying the census / logic depth plus
a redacted ``task_type_counts`` summary), and ``codebase_repo_mining.json`` (the per-task-type
mining counts + ``mined_task_summaries``, one "<task_type>: <sentence>" line per mined idea, so
each per-type count can be read against the sentences behind it; counts are zeroed when mining is
skipped).

EXTERNAL variant = the one we hand to a vendor to run on their own machine, against their own
code, with their own provider login. The governing rule is therefore NOT "redact carefully" but
"do not acquire":

  * No credential scanning. Nothing here searches a repository for secret-shaped strings, and no
    such string is ever matched, counted, recorded or emitted. Two disclosed exceptions sit beside
    that and are written out in ``references/client-runbook.md`` section 4: ``repo_digest`` below
    hashes the bytes of every file in the tree, a committed ``.env`` included, and under ``--build``
    only, ``build_probe`` checks the checkout's own registry configuration to attribute an
    authentication failure. Neither retains anything it read.
  * No identity. Author names, author emails and committer identity are consumed at the git parse
    site to derive a COUNT and a bot flag, then discarded; nothing downstream can see them.
  * No repository-derived strings we do not need. Ref names, tag names and environment-variable
    names are not emitted.
  * No credential of the OPERATOR'S is read: this script loads no ``.env`` of its own, opens no
    network connection of its own, and hands every model and build subprocess an explicit minimal
    environment. Three ``git`` call sites inherit the ambient environment instead, named in
    ``collectors/childenv.py``.

WHAT THE RUN DISCLOSES is a separate question from what the artifact contains, and it is the larger
of the two. The model lanes show your source and your development history to your own provider,
under your own account. That is the disclosure, and it is real; everything else about how those
lanes are launched is now bounded by controls the provider CLI enforces rather than by the model's
own judgement:

  * **The agent has no shell.** The grant is ``Read Grep Glob``. ``--add-dir`` bounds those three.
    It never bounded ``Bash``, which is why ``Bash`` is gone: the history the lanes needed is
    computed here by ``collectors/history_brief.py`` with a fixed read-only ``git`` argv and handed
    over as files in a scratch directory that is deleted when the lane ends.
  * **The repository cannot configure the agent that measures it.** Every provider launch carries
    ``childenv.isolation_flags``, so ``.claude/settings.json`` hooks, ``CLAUDE.md``, ``AGENTS.md``,
    project MCP servers, project agents and project commands are all ignored. A CLI too old to
    offer that control stops the run instead of measuring under it.
  * **No transcript is left behind.** The provider CLI is told not to persist the session, so
    nothing it read is written outside the output directory you named.
  * **Committed credentials are withheld at the mechanism.** The patches the lanes read exclude
    credential-shaped paths by git pathspec -- content and filename both -- so a committed
    ``.env`` does not reach the model through a diff. The prompts say the same thing, as a second
    layer rather than the first.

What leaves is exactly four kinds of thing: numbers, booleans/closed-vocabulary enums, the content
digest and its derived display handle, and ONE sentence per mined task idea describing the shape of
the work. ``collectors/emit_allowlist.py`` enforces that at the write boundary and fails the run
rather than emitting anything it was not told to expect; the redactor and the leak audit stay
behind it as a backstop.

The artifact belongs to whoever ran this. Every run ends by printing a REVIEW of what is in the
files -- the counts per kind, every one-line description verbatim, and the list of things this
skill does not collect and why. ``--review`` prints every field and its value. Read it, then
decide whether to send anything. Nothing is uploaded, and nothing this skill produced is deleted
by it.

HOW LONG IT TAKES, and what a budget means here
-----------------------------------------------
The independent lanes run CONCURRENTLY (see ``--jobs``), so the wall clock is the slowest lane
rather than the sum of all of them. The two model lanes are the long poles and they overlap the
deterministic collectors entirely. The build probe is the one exception: it executes the project's
own install and build commands inside the checkout, so it runs alone, after every reader has
finished. IT RUNS ON EVERY INVOCATION unless ``--build none`` is passed -- see ``--build`` below.

ONE REPOSITORY CANNOT EXCEED ``--budget-seconds``, default 9000 -- two and a half hours. There is a
single monotonic deadline for the whole invocation and every lane draws from it: the two model
lanes are capped at what is left minus the build probe's reserve, and the probe is capped at that
reserve. The run prints its own estimate before it starts ("this looks like a 20 minute run") and
the per-lane wall-clock table when it finishes, so a long run is visibly alive rather than
apparently hung, and a truncated one is never a surprise.

NOTHING IS SILENTLY TRUNCATED. ``--census-timeout`` and ``--mine-timeout`` are runaway backstops
four hours out rather than budgets -- the budget above is what bounds the run -- and a lane that
hits either records every measurement it owns as NULL with a reason: never a partial count, never
a zero, never a low band. The build probe reports the projects the budget did not reach as
``skipped_budget`` with null measurements, and ``observed_runnability`` goes null rather than
summing terms nobody executed. Downstream, grade-ext marks that dimension UNSCORED and flags the
whole grade ``grade_status: UNGRADED`` / ``score_comparable: false``. An unmeasured repository is
reported as unmeasured; it is never reported as a poor one.

Usage:
    python measure.py <repo> --out <dir> [--sink json|csv|db] [--review]
                       [--build [none|discover|full]] [--budget-seconds N]
                       [--build-budget-seconds N] [--full-attempt-seconds N]
                       [--timeout-build N] [--max-build-projects N]
                       [--top-files N] [--git-top N] [--threshold FLOAT] [--jobs N]
                       [--provider claude|codex|gemini] [--model NAME] [--no-llm]
                       [--census-timeout SECONDS]
                       [--no-mine] [--mine-n N] [--mine-timeout SECONDS]

Positional arguments:
    repo                 Path to the git repository (or sub-tree) to measure. BY DEFAULT this
                         skill runs the project's own dependency-resolution, install and build
                         commands inside this checkout, and asks its test runners to enumerate
                         their tests, so USE A CLEAN, DISPOSABLE CLONE OR WORKTREE. Pass
                         `--build none` to measure without executing anything. Model-assisted
                         lanes inspect source and history through the configured provider CLI and
                         may use its network.

Options:
    --out DIR            REQUIRED. Directory to write outputs into (created if absent).
                         Writes: codebase_repos.json, codebase_repos.csv, measurement.json,
                         codebase_repo_mining.json.
    --sink {json,csv,db} Output sink. `json` (default) writes both the JSON docs and the CSV
                         row. `csv` writes only the flat codebase_repos.csv (plus the row's
                         JSON). `db` is a documented stub that raises NotImplementedError -- the
                         platform ingests the emitted JSON/CSV; there is no direct DB writer here.
    --build [LEVEL]      The non-model build probe (build_probe.collect, agentic=False). IT RUNS BY
                         DEFAULT, at level `full`: this skill resolves dependencies, installs and
                         builds the project, asks each test runner to list its own tests, then RUNS
                         the suite and reads coverage. Those commands are the project's own and they
                         execute inside the target checkout, which is why the docs say so up front
                         rather than in a footnote. `--build none` is the escape hatch and turns
                         every executed measurement into an explicit NULL. LEVEL is one of:
                           none      runs nothing. `build_ok`, `testable_at_head`,
                                     `tests_discovered`, `coverage_pct`, `discover_runnability`
                                     and `observed_runnability` are all NULL with a reason, and
                                     grade-ext reports the environment dimensions UNSCORED.
                           discover  Resolves dependencies, installs, builds, and asks the RUNNER
                                     to list its tests (`pytest --collect-only`,
                                     `cargo test -- --list`, `go test -list`, `jest --listTests`).
                                     Fills `install_ok`, `build_ok`, `tests_discovered`,
                                     `discover_runnability` and the real test framework and
                                     package manager as EXECUTED facts. Measured at 13-23s against
                                     21-62s for `full` on two real repositories.
                           full      THE DEFAULT. Also executes the suite and reads coverage, which
                                     is what `observed_runnability` needs -- band 4 of that index
                                     is the largest measured step in the corpus and is unreachable
                                     at any other level. A full run that does not finish inside
                                     `--full-attempt-seconds` COMPLETES AT `discover` with what is
                                     left of the build reserve, records
                                     `build_level_fallback_reason`, and reports
                                     `observed_runnability` NULL with a reason. It is never scored
                                     low for our clock: `discover_runnability` still carries the
                                     executed facts and the grade stays comparable.
                         A build that genuinely FAILS needs no fallback and does not get one -- a
                         completed full record already holds every discover-level term, and the
                         repository scores low on the full-level dimension because that is the
                         measurement. The two are told apart by `attribution`, which every phase
                         carries: `repository` scores, anything else is UNSCORED with a reason.
                         Caches and most dependencies use scratch storage where supported; in-tree
                         changes are restored best-effort. Run only against a clean, disposable
                         clone or worktree. This lane NEVER overlaps another: it mutates the tree
                         the other lanes are reading, so it runs alone once they have finished.
    --repair             OFF BY DEFAULT, and requires `--build full`. After a project's install or
                         build FAILS, give the provider CLI ONE bounded turn inside the sandbox to
                         make the ENVIRONMENT work -- install a system library the README assumes,
                         write a config file the project's own docs say to write -- and then RE-RUN
                         the same command and measure what happens. The re-run is the evidence. The
                         model is never asked for the outcome and never supplies it.
                         THE AS-SHIPPED VERDICT IS NEVER OVERWRITTEN. `install_ok`, `build_ok` and
                         `observed_runnability` go on describing the repository as it arrived; the
                         repair result rides alongside in `repair_offered`, `repair_candidates_n`,
                         `repair_attempted_n`, `repair_succeeded_n`, `repair_refused_n`,
                         `repair_rejected_source_edit_n` and `repair_seconds`. Those are counts.
                         Nothing about WHAT had to be installed leaves the machine.
                         A turn that modifies any git-TRACKED file is recorded as a FAILED repair
                         whatever the re-run then said: an agent that makes a build pass by
                         deleting the failing target has produced the one measurement we must never
                         take, so the diff is counted afterwards rather than trusted.
                         No turn is offered for a failure no agent can move -- a credential we
                         cannot supply, a registry that is down, or our own time limit running out.
                         WHAT THIS COSTS YOU. The turn needs provider authentication, so unlike
                         every other command this skill runs it does NOT execute in the credential-
                         free build environment. Commands the agent chooses to run inherit its
                         environment and can therefore see your provider credential for the length
                         of the turn. Everything the skill itself runs -- the failing command, the
                         re-run that decides the outcome -- stays credential-free as before. That
                         trade is why this is opt-in.
    --budget-seconds N   GLOBAL wall-clock budget for the whole run, shared by every lane. Default
                         9000 (two and a half hours). This is THE bound on one repository: the two
                         model lanes are capped at what is left minus the build reserve, the probe
                         is capped at the reserve, and anything the budget does not reach is
                         reported NULL with a reason rather than as a low measurement.
    --build-budget-seconds N
                         The build probe's reserved share of that budget, cumulative across every
                         project and every phase. Default 1800. Reserved rather than
                         first-come-first-served because the probe runs last and is the only lane
                         that executes anything, so without a reserve it is the lane that never
                         runs.
    --full-attempt-seconds N
                         How long a `--build full` run may take before the measurement is completed
                         at `discover` with whatever is left of the build reserve. Default 900. This
                         is a SLICE of --build-budget-seconds, not a second budget: the full attempt
                         and the discover fallback together never exceed the reserve, so the global
                         --budget-seconds is unaffected.
    --timeout-build N    Ceiling for ONE command inside the probe (an install, a build, a suite).
                         Default 900, always subordinate to the two budgets above.
    --max-build-projects N
                         How many discovered project roots the probe may run commands in, largest
                         first. Default 8. The rest are reported `skipped_budget` with reason
                         `project_cap_reached` -- a silent cap reads as "we looked and found
                         nothing".
    --jobs N             Maximum lanes to run concurrently (default: one worker per lane, i.e. the
                         whole fan-out at once). Same flag and same meaning as measure-int's.
                         `--jobs 1` serialises the run, which is the way to reproduce the old
                         sequential timings or to measure on a machine with no spare cores.
    --top-files N        Passed to repo_stats.py: number of largest files to profile (default 10).
    --git-top N          Cap on each per-class commit list emitted by git_stats.py (default 1).
                         The measure stage consumes only the COUNTS, so this is kept small to
                         bound subprocess output; the full-history tallies are unaffected by it.
    --threshold FLOAT    Passed to classify_repo.classify: minimum normalized class confidence
                         for a class to be "detected" (default 0.18).
    --provider NAME      Model CLI provider: claude (default), codex, or gemini. A provider whose
                         CLI is not installed is an error before any work starts -- never a
                         silent fallback and never a quietly unscored lane.
    --model NAME         Provider model id/alias. Claude defaults from collectors/models.py (env
                         RQE_MODEL); Codex and Gemini require an explicit model unless --no-llm.
                         Recorded as `model`, which is the model REQUESTED; nothing substitutes
                         another one, so it is also the model used whenever `scored` is true.
    --no-llm             Skip BOTH model-assisted lanes (the material census AND agentic mining).
                         logic_depth / self_contained are reported unscored (not zero) and the
                         mining counts are zeroed with the flag named as the reason. The row
                         reports `status: partial` with `skip_reason: llm_disabled` -- the largest
                         measurement is genuinely absent, and a row that said `measured` would be
                         claiming otherwise. Fully deterministic: use this for a reproducible,
                         offline, no-provider run.
    --review             Print every emitted field and its value, grouped by kind, after writing.
                         Without it a shorter review is still printed: the per-kind counts, every
                         one-line description in full, and what is not collected and why.
    --census-timeout SECONDS
                         Ceiling for the census LANE -- retries and backoff are spent out of it,
                         never on top of it. Default 14400 (four hours), which is a RUNAWAY
                         BACKSTOP rather than a budget: a large repository is allowed to take one
                         to two hours. Pass a smaller number to cap the lane deliberately. Hitting
                         the ceiling does not produce a small measurement -- logic depth,
                         self-containment and every category count go NULL with a reason.
                         Ignored with --no-llm.
    --no-mine            Skip ONLY the agentic mining lane (the census still runs unless --no-llm
                         is given). codebase_repo_mining.json is still written with all task-type
                         counts zeroed and an empty mined_task_summaries list.
    --mine-n N           Ceiling on how many task ideas the miner enumerates (default 150). The
                         lane is a census, not a shortlist: the agent lists everything that clears
                         the bar up to this number, each classified into one of the four task
                         types, and each type's count is the length of its own list of sentences.
                         The old default of 40 was sized against a 30-minute budget; the mined
                         ideas are the most valuable thing this skill produces, so the budget is
                         better spent enumerating them. Raising it cannot lengthen the run past
                         --budget-seconds; it only decides what the budget is spent on.
    --mine-timeout SECONDS
                         Ceiling for the agentic mining LANE. Default 14400 (four hours), a
                         runaway backstop on the same terms as --census-timeout. Enumerating a
                         repository's whole task census takes longer than picking a top few, and a
                         lane that runs out of clock has measured nothing -- so its per-task-type
                         counts are emitted NULL, not zero. Ignored with --no-mine / --no-llm.

Environment variables:
    MEASURER_VERSION     Optional. Stamped into every record as `measurer_version`. Default
                         "repo-quality-evaluation-measure-ext@1". No secrets are read anywhere.
    RQE_MODEL            Optional. Default model for the census when --model is not passed
                         (see collectors/models.py). Not a secret.

This script reads no credentials, tokens, or account IDs. Every subprocess is invoked with a
fixed argument list and shell=False -- including the provider CLI the census and the agentic miner
call, which authenticates from the ambient environment (never read, logged, or stored here). No
user- or machine-specific paths are baked in: the repo and the output directory are CLI arguments.
Nothing this script produced is ever deleted by it, and this script sends nothing anywhere. The
provider CLI it launches does talk to the operator's own provider account; see the disclosure
paragraph above and `references/client-runbook.md` section 3.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# The deterministic collectors live alongside this file; put their directory first on the path
# so their sibling imports (git_stats -> candidates, code_structure -> models) resolve.
_COLLECTORS = Path(__file__).resolve().parent / "collectors"
if str(_COLLECTORS) not in sys.path:
    sys.path.insert(0, str(_COLLECTORS))

# The agentic miner lives in its own directory. It is NOT imported in-process (its bare-name
# imports -- `import agent_io`, `import candidates` -- would collide with the collectors' own
# `candidates.py` on sys.path); it is run as a fixed-argv subprocess with cwd=_MINERS, so its
# sibling imports resolve against its own directory and nothing here.
_MINERS = Path(__file__).resolve().parent / "miners"

# Task type -> the codebase_repo_mining count column it feeds. This external variant declares FIVE
# types; a candidate that names anything else lands in `agentic`, which is the mining lane's own
# name and therefore the honest bucket for "found by the diving agent, type not established".
# The vocabulary is duplicated in emit_allowlist.TASK_TYPES on purpose: that is the write boundary
# and it refuses anything this table has not declared, so the two disagreeing is a loud failure.
STRATEGY_COUNT_KEYS = {
    "net_new": "n_net_new",
    "agentic": "n_agentic",
    "bug_repair": "n_bug_repair",
    "repo_evolution": "n_repo_evolution",
}
_MINING_COUNT_KEYS = (
    "n_net_new", "n_agentic", "n_bug_repair", "n_repo_evolution",
)

import build_probe  # noqa: E402
import childenv  # noqa: E402
import classify_repo  # noqa: E402
import code_structure  # noqa: E402
import emit_allowlist  # noqa: E402
import git_history  # noqa: E402
import material_census  # noqa: E402
import models  # noqa: E402
import redact as _redact  # noqa: E402

MEASURER_VERSION = os.environ.get(
    "MEASURER_VERSION", "repo-quality-evaluation-measure-ext@1"
)

# How often the heartbeat reprints the lanes that are still going. A silent forty-minute lane is
# what makes an operator conclude the tool has hung and kill it, and a killed run is the most
# expensive possible outcome: everything already spent is lost and the repository is reported as
# unmeasured. One line a minute is cheap enough to always be on.
HEARTBEAT_SECONDS = 60

# The mining lane's ceiling, on the same terms as material_census.DEFAULT_TIMEOUT: four hours is a
# runaway backstop for a wedged CLI, not a statement about how long a census should take. The old
# 1800 was a budget, and a budget on this lane buys nothing -- the run does not finish sooner, it
# finishes without the measurement. What bounds the run is DEFAULT_BUDGET_SECONDS below; these two
# only stop a CLI that has wedged in a way the deadline would otherwise wait out.
DEFAULT_MINE_TIMEOUT = 14400

# THE bound on a single repository: two and a half hours, and nothing may exceed it. Every lane
# draws from one Deadline built from this, so the arithmetic is total = the slowest concurrent lane
# (capped at the budget minus the build reserve) + the build probe (capped at the reserve), plus a
# small grace for subprocess cleanup. Work the budget does not reach is reported NULL with a
# reason; it is never reported as a measurement that came out low.
DEFAULT_BUDGET_SECONDS = 9000

# The build probe's guaranteed share of that budget. It is the only lane that EXECUTES anything, so
# it must not be the lane that gets whatever the model lanes leave behind -- which, since both are
# blocked on a provider that finishes when it finishes, would be nothing.
DEFAULT_BUILD_BUDGET_SECONDS = 1800

# Ceiling for one command inside the probe (an install, a build, a suite). Subordinate to the two
# budgets above: a phase gets the smallest of this, its project's share, and what is left overall.
DEFAULT_TIMEOUT_BUILD = 900

# The level the probe runs at unless the operator says otherwise. `full` -- resolve, install,
# build, enumerate AND execute the suite, then read coverage -- because it is the only level that
# can reach `observed_runnability` band 4, which ext-diligence-v5 prices at 14 of 100, and the only
# level that produces `coverage_pct` at all. A repository that cannot sustain it is not punished for
# that: it falls back to `discover`, keeps the executed facts it did earn, and has the full-level
# dimension reported UNSCORED with a reason rather than scored low.
DEFAULT_BUILD_LEVEL = "full"

# How long a `full` attempt may run before the measurement is completed at `discover` instead. Half
# the build reserve, so a fallback and the attempt that provoked it both fit inside the reserve and
# the run's own 9000s deadline never notices. This is not a second timer: it is a slice of the
# probe's existing cumulative allowance, handed to the probe as its `budget_seconds`.
DEFAULT_FULL_ATTEMPT_SECONDS = 900

# How many task ideas the mining lane enumerates. This was 40, which was sized against a 1800s
# budget that no longer exists, and it is a CENSUS ceiling -- the whole point of the lane is that
# the count IS the list. The mined ideas are the most valuable thing this skill produces, so the
# budget is better spent enumerating them than sitting unused. The lane reports its own ceiling
# in `mine_unavailable_reason` when it binds, so "we found 150" and "we stopped at 150" stay
# different facts.
DEFAULT_MINE_N = 150

# Lane display names for the emitted timing note. The note goes through the emission allowlist's
# prose rule, which rejects snake_case as a repository identifier, so the lane keys are spelled out
# in words. Keys not listed here fall back to their own name with underscores turned into spaces.
_LANE_WORDS = {
    "repo_stats": "tree scan",
    "git_stats": "history scan",
    "code_structure": "structure",
    "git_history": "history depth",
    "material_census": "census",
    "agentic_mining": "mining",
    "build_probe": "build probe",
}


# ---------------------------------------------------------------------------
# The run budget -- one deadline, shared by every lane
# ---------------------------------------------------------------------------

class Deadline:
    """One monotonic budget for the whole invocation. Same shape as measure-int's.

    Every lane draws from this, and no lane may ask for more than `remaining()`. That is what
    bounds the run: the per-lane ceilings are runaway backstops hours out, and a backstop cannot
    bound anything. When this is spent, remaining work is reported as unmeasured with a reason --
    NULL, never a partial count -- so an exhausted budget produces a slow answer or no answer, and
    never a low one.
    """

    def __init__(self, budget_seconds: int) -> None:
        self.total = max(0, int(budget_seconds))
        self._start = time.monotonic()
        self._end = self._start + self.total

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def slice(self, cap: int, reserve: float = 0.0) -> int:
        """Seconds a lane may take: its own ceiling, or what is left minus what is reserved.

        `reserve` is how the build probe keeps its share when the two model lanes would otherwise
        take the whole budget. The lanes are long poles that finish when they finish, so without a
        reserve the executed lane -- the one carrying the only measurements we did not infer --
        would be the one that never runs.
        """
        return max(0, int(min(cap, self.remaining() - max(0.0, reserve))))


# ---------------------------------------------------------------------------
# Lane clock -- where the time went, and proof of what overlapped what
# ---------------------------------------------------------------------------

class LaneClock:
    """Start/finish offsets for every lane, plus the heartbeat that shows a long run is alive.

    Two jobs, and the second is not cosmetic. The spans ARE the evidence for the exclusivity
    rule: `--build` must not overlap a lane that is reading the tree, and two half-open intervals
    either overlap or they do not. `overlaps()` answers that from the recorded numbers rather
    than from anybody's reading of the control flow.
    """

    def __init__(self, interval: int = HEARTBEAT_SECONDS) -> None:
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._spans: dict[str, tuple[float, float | None]] = {}
        self._stop = threading.Event()
        self._interval = max(1, interval)
        self._beat: threading.Thread | None = None

    @contextlib.contextmanager
    def lane(self, name: str):
        """Time one lane. Records the span even when the lane raises."""
        with self._lock:
            self._spans[name] = (time.monotonic() - self._t0, None)
        print(f"[lane] {name} started", file=sys.stderr, flush=True)
        try:
            yield
        finally:
            with self._lock:
                start, _ = self._spans[name]
                end = time.monotonic() - self._t0
                self._spans[name] = (start, end)
            print(f"[lane] {name} finished after {end - start:.1f}s", file=sys.stderr, flush=True)

    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def spans(self) -> dict[str, tuple[float, float]]:
        """Every recorded span, unfinished lanes closed at now."""
        with self._lock:
            now = time.monotonic() - self._t0
            return {name: (start, now if end is None else end)
                    for name, (start, end) in self._spans.items()}

    def seconds(self, name: str) -> float | None:
        span = self.spans().get(name)
        return None if span is None else span[1] - span[0]

    def overlaps(self, name: str) -> list[str]:
        """Lanes whose span intersects `name`'s. Empty means it ran alone."""
        spans = self.spans()
        if name not in spans:
            return []
        start, end = spans[name]
        return sorted(other for other, (s, e) in spans.items()
                      if other != name and s < end and start < e)

    def start_heartbeat(self) -> None:
        if self._beat is None:
            self._beat = threading.Thread(target=self._pulse, daemon=True)
            self._beat.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        if self._beat is not None:
            self._beat.join(timeout=2)
            self._beat = None

    def _pulse(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                live = [(name, start) for name, (start, end) in self._spans.items() if end is None]
            if not live:
                continue
            now = time.monotonic() - self._t0
            detail = ", ".join(f"{name} {now - start:.0f}s" for name, start in sorted(live))
            print(f"[alive] {now:.0f}s elapsed; still running: {detail}",
                  file=sys.stderr, flush=True)

    def table(self) -> str:
        """The per-lane wall-clock table, printed at the end of every run."""
        spans = self.spans()
        rows = ["lane wall clock (seconds, offsets from the start of the run):",
                f"  {'lane':<18}{'start':>9}{'end':>9}{'elapsed':>9}"]
        for name, (start, end) in sorted(spans.items(), key=lambda kv: kv[1][0]):
            rows.append(f"  {name:<18}{start:>9.1f}{end:>9.1f}{end - start:>9.1f}")
        total = self.elapsed()
        rows.append(f"  {'total':<18}{0.0:>9.1f}{total:>9.1f}{total:>9.1f}")
        return "\n".join(rows)

    def note(self) -> str:
        """The same numbers as one sentence, for the emitted record.

        The measurement schema is frozen (see ../CONTRACT.md), so this cannot become a new key.
        `census_platform_note` is the one always-writable prose field the material block declares
        and it already exists to carry facts about how the lane ran on this machine, so the timing
        rides there. Written in plain words with no identifiers, because it is held to the
        allowlist's prose rule like any other string.
        """
        spans = self.spans()
        parts = [f"{_LANE_WORDS.get(name, name.replace('_', ' '))} {end - start:.0f}"
                 for name, (start, end) in sorted(spans.items(), key=lambda kv: kv[1][0])]
        return ("lane wall clock in seconds: " + ", ".join(parts)
                + f", total {self.elapsed():.0f}; independent lanes ran at the same time, "
                  "so the total is below their sum")

# ---------------------------------------------------------------------------
# Redaction -- the privacy boundary for the EXTERNAL variant.
# ---------------------------------------------------------------------------

# Keys whose VALUES are pure identity and are dropped outright rather than redacted: redacting
# them would leave a placeholder where the honest answer is "not emitted".
#
# This set is now a BACKSTOP, not the boundary. As of the data-minimisation pass the collectors no
# longer produce author names, author emails, secret snippets, environment-variable names or tag
# names at all -- there is nothing left for most of these entries to drop. They are retained so
# that a future collector change cannot reintroduce a leak silently, and because a defence that
# only works when the layer above it is correct is not a defence. The real boundary is
# emit_allowlist.py, which fails the run on anything it has not been told to expect.
_DROP_KEYS = {
    "repo_path", "repo_name",            # tree: absolute/relative identity of the checkout
    "head_sha", "effective_tip_sha",     # git: object hashes
    "anonymizer_commit", "top_authors",  # git: never populated any more; dropped if ever revived
    "secret_hit_details",                # tree: never populated any more (no scanner exists)
    "commit_sha",                        # ext history: object hash
    "latest_tag",                        # git: never populated any more
}

# Extra scrubs layered on top of redact.redact() for email addresses and uppercase config
# identifiers. TWO capitals is the threshold, not five: the old one let `DB`, `SDK` and every
# three-letter service abbreviation through, and a repository's own initialism is exactly the
# kind of word that identifies it. Closed-vocabulary values are exempt (see _PASS_THROUGH), so
# this only ever runs over free text.
_EMAIL = re.compile(r"\b[\w.\-+]+@[\w.\-]+\.[A-Za-z]{2,}\b")
_UPPER_IDENTIFIER = re.compile(r"\b(?:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z][A-Z0-9]+)\b")
# Environment-variable NAMES are no longer collected (repo_stats.analyze_reproducibility emits the
# three keys empty). The mask stays as a backstop for the same reason as _DROP_KEYS.
_ENV_VAR_KEYS = {
    "env_vars_referenced_in_source", "env_vars_in_example", "env_vars_missing_from_example",
}

# Keys whose string values are public technology facts, not repo-derived symbols. Naming the
# stack is explicitly legitimate in a diligence record, so these pass through unscrubbed and are
# exempt from the leak audit -- the same stance the ext redactor takes with its TECH allowlist.
_STACK_KEYS = {
    "primary_language", "secondary_languages", "detected_frameworks", "test_framework",
    "package_managers", "ci_systems", "project_type", "primary_class",
}
# Keys whose values are provenance the record exists to carry -- a content digest, the digest-
# derived display handle, the version string, the census model id. Auditing them would reject the
# thing they carry.
_PROVENANCE_KEYS = {
    "repo_digest", "fake_repo_name", "measurer_version", "provider", "model", "repo_id",
    # The repository's own name, emitted so the platform can map an external measurement to the
    # repository it describes. Exempt because the redactor would otherwise read `owner/repo` as
    # a filesystem path and blank it to `[path]`, and the leak audit would then refuse the whole
    # document over the one field the platform needs. This is a name the submitter already types
    # into the upload form by hand, so emitting it makes an identity we already collect accurate
    # rather than adding a disclosure. See "What leaves your machine" in SKILL.md.
    "real_repo_name",
}
# Keys whose values are this tool's OWN closed vocabulary (fixed signal-name literals), not text
# drawn from the repo. Scrubbing them would blank a meaningful enum to no privacy benefit. The
# census emits several such literals (`probe`, the `skip_reason` / `census_failure_kind` enums);
# `census_error_detail`, by contrast, carries CLI text and is deliberately NOT here -- it is
# scrubbed and audited like any other free string.
_OWN_VOCAB_KEYS = {"demo_reasoning", "probe", "skip_reason", "census_failure_kind"}
# Fields emit_allowlist has already matched against a closed vocabulary. Running the scrub over
# them cannot make them safer and does corrupt them -- it reads "Next.js" as a filename and
# "GitHub Actions" as a class name -- so the boundary's verdict stands and the backstop steps
# aside. Everything NOT in this set is still scrubbed and still audited.
_PASS_THROUGH = _STACK_KEYS | _OWN_VOCAB_KEYS | emit_allowlist.CLOSED_VOCABULARY_KEYS
_AUDIT_EXEMPT = _PASS_THROUGH | _PROVENANCE_KEYS


class LeakDetected(emit_allowlist.EmissionRefused):
    """Raised by the BACKSTOP if a real path/symbol/name/hash/email survives to the write.

    A subclass of EmissionRefused because it is the same event seen one layer down: the
    allowlist refuses what it was not told to expect, and this refuses what the allowlist let
    through and the scrub then failed to clean. Either way nothing is written.
    """


def scrub(text: str) -> str:
    """Redact source-derived detail from a single string. Redaction, not rejection."""
    cleaned, _removed = _redact.redact(text)
    cleaned = _EMAIL.sub("[email]", cleaned)
    cleaned = _UPPER_IDENTIFIER.sub("[identifier]", cleaned)
    return cleaned


def leaks(text: str) -> str | None:
    """Return a human label if `text` still carries a leak, else None. The write-boundary audit."""
    what = _redact.contains_leak(text)
    if what:
        return what
    if _EMAIL.search(text):
        return "an email address"
    if _UPPER_IDENTIFIER.search(text):
        return "an uppercase identifier"
    return None


def redact_tree(node, key: str | None = None):
    """Recursively drop identity keys and scrub every remaining string leaf AND dict key.

    Values under a `_STACK_KEYS` key are public technology facts and pass through unscrubbed.

    Dict keys are scrubbed too, and that is not decoration. A dynamic map -- `loc_by_language`,
    `iac_loc_by_type`, the linter map -- carries repository-derived text in its KEYS, and an
    earlier version of this function copied every key through untouched while carefully
    scrubbing the values underneath them. A key that emit_allowlist declares (a structural field
    name, or a member of a map's closed vocabulary) is left alone; anything else is a key
    nobody declared, and it is scrubbed like the string it is.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in _DROP_KEYS:
                continue
            safe_key = k if k in emit_allowlist.DECLARED_KEYS else scrub(str(k))
            out[safe_key] = redact_tree(v, k)
        return out
    if isinstance(node, list):
        return [redact_tree(v, key) for v in node]
    if isinstance(node, str):
        if key in _ENV_VAR_KEYS:
            return "[identifier]"
        return node if key in _PASS_THROUGH else scrub(node)
    return node


def audit_no_leak(node, path: str = "", key: str | None = None) -> None:
    """Walk the final payload and raise if any string still leaks. Backstop for redact_tree().

    Keys are audited as well as values, for the reason given in redact_tree(): a map key is a
    string from the repository just as much as a map value is.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else str(k)
            if k not in emit_allowlist.DECLARED_KEYS:
                what = leaks(str(k))
                if what:
                    raise LeakDetected(
                        f"refusing to write: the KEY '{here}' contains {what}. Undeclared map "
                        f"keys are scrubbed by redact_tree(); this one survived, which is a bug "
                        f"there, not a content problem."
                    )
            audit_no_leak(v, here, k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            audit_no_leak(v, f"{path}[{i}]", key)
    elif isinstance(node, str):
        if key in _AUDIT_EXEMPT:
            return
        what = leaks(node)
        if what:
            raise LeakDetected(
                f"refusing to write: field '{path}' contains {what} -- value was "
                f"{node[:90]!r}. redact_tree() should have removed it; this is a bug there, "
                f"not a content problem."
            )


# ---------------------------------------------------------------------------
# Running the deterministic collectors
# ---------------------------------------------------------------------------

def _run_collector(script: str, args: list[str], stdin: str | None = None) -> dict:
    """Invoke a collector script with a fixed argument list (shell=False) and parse its JSON."""
    argv = [sys.executable, str(_COLLECTORS / script), *args]
    # STATIC domain: a deterministic collector needs PATH and a locale, not credentials.
    proc = subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        shell=False,
        cwd=str(_COLLECTORS),
        env=childenv.build_env(childenv.STATIC, passthrough=("HOME", "USERPROFILE")),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{script} exited {proc.returncode}: {proc.stderr.strip()[:400]}"
        )
    return json.loads(proc.stdout)


def repo_digest(repo: Path) -> str:
    """Content hash of the tree: sha256 over sorted (relpath, sha256(bytes)) for every file,
    excluding the .git directory. Same tree contents -> same digest, independent of location.

    EVERY file, a committed ``.env`` included, and that is deliberate. It was reconsidered
    against the alternative -- skipping ignored or credential-shaped files -- and the alternative
    is worse in both directions:

      * It buys nothing. Nothing read here is retained, compared, matched or emitted. What leaves
        is one 64-character hex digest over a tree; no byte, length, name or existence of any
        file is recoverable from it, and the run holds no other copy.
      * It costs the property the digest exists for. This is the repository's identity: two
        checkouts of the same tree must agree and two different trees must not. Skipping whatever
        ``.gitignore`` currently says would make that identity depend on a file the repository
        can rewrite, and would make one operator's digest disagree with another's over an
        untracked local file that has nothing to do with the code.

    So it reads the bytes, and the honest claim -- the one in the runbook and in this module's
    docstring -- is that it reads them and keeps nothing, not that it avoids them.
    """
    h = hashlib.sha256()
    for p in sorted(repo.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(repo).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        try:
            fh = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            fh = "unreadable"
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(fh.encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def _git(repo: Path, *args: str) -> str:
    """Read-only git with a fixed argv (shell=False). Empty string on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, shell=False,
            env=childenv.build_env(childenv.STATIC, passthrough=("HOME", "USERPROFILE")),
        )
    except OSError:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def repo_full_name(repo: Path) -> str | None:
    """The repository's ``owner/name`` as its remote knows it, or ``None``.

    Read from the REMOTE rather than the checkout directory, deliberately: the platform keys a
    repository on the provider's full name, and a local clone is frequently just ``name`` with
    the owner lost, which would not match. Both remote spellings are handled::

        git@github.com:owner/name.git       ->  owner/name
        https://host/group/sub/name.git     ->  group/sub/name

    ``None`` whenever there is no remote to read. That is a real case rather than an error -- a
    local-only clone, or a vendor measuring an export -- and it behaves exactly as this tool did
    before the field existed, leaving the platform to fall back to the name the submitter typed.

    **A remote that is a local filesystem path yields None, not a name.** ``git clone /srv/repo``
    and ``file:///home/someone/work/repo`` are both legal remotes, and their paths would otherwise
    satisfy any "looks like owner/name" test while disclosing the operator's account name and
    directory layout -- the exact class of thing this variant exists to withhold. So a remote is
    only read when it names a HOST: either an explicit non-``file`` scheme, or the scp-like
    ``host:path`` form. Anything else is reported as no name at all.
    """
    url = _git(repo, "config", "--get", "remote.origin.url").strip()
    if not url:
        return None
    trimmed = url[:-4] if url.endswith(".git") else url

    if "://" in trimmed:
        scheme, rest = trimmed.split("://", 1)
        if scheme.lower() == "file" or "/" not in rest:
            return None                       # no host to speak of, so a local path
        host, path = rest.split("/", 1)
        if not host or host.startswith(":"):
            return None                       # file:///... leaves an empty host
    elif ":" in trimmed and not trimmed.startswith(("/", ".", "~")):
        host, path = trimmed.split(":", 1)
        # An scp-like remote's host cannot contain a slash. `C:/src/repo` is a Windows path,
        # not a host, so a single-letter host is refused too.
        if "/" in host or len(host.split("@")[-1]) < 2:
            return None
    else:
        return None                           # /abs/path, ./rel, or a bare directory name

    parts = [segment for segment in path.strip("/").split("/") if segment]
    if not 1 <= len(parts) <= 4:
        return None
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", segment) for segment in parts):
        return None
    return "/".join(parts)


def commits_by_month(repo: Path, tip: str) -> list[int]:
    """Length-12 array: commit counts for the 12 calendar months ending at the most recent
    commit (index 11 = latest month, index 0 = eleven months earlier). Zeros where none."""
    out = _git(repo, "log", tip, "--no-merges", "--format=%cd", "--date=format:%Y-%m")
    months = [line.strip() for line in out.splitlines() if line.strip()]
    counts: list[int] = [0] * 12
    if not months:
        return counts
    latest = max(months)
    ly, lm = int(latest[:4]), int(latest[5:7])
    index: dict[str, int] = {}
    for slot in range(12):
        total = (ly * 12 + (lm - 1)) - (11 - slot)
        index[f"{total // 12:04d}-{total % 12 + 1:02d}"] = slot
    for m in months:
        if m in index:
            counts[index[m]] += 1
    return counts


def active_days(repo: Path, tip: str) -> int | None:
    """Distinct calendar days on which a commit landed. None if git yielded nothing."""
    out = _git(repo, "log", tip, "--no-merges", "--format=%cd", "--date=format:%Y-%m-%d")
    days = {line.strip() for line in out.splitlines() if line.strip()}
    return len(days) if days else None


# ---------------------------------------------------------------------------
# Field mapping -- CONTRACT codebase_repos (38) + measurement.json (3 blocks)
# ---------------------------------------------------------------------------

# Content signals only. `strong_name_hit` was in here and is gone: a repository NAME cannot on
# its own settle whether a repository is a demo -- "poc", "sample", "playground" and "starter"
# are ordinary words in product repository names -- and the name this pipeline had was the
# directory the operator cloned into, not the repository's. See repo_stats.analyze_demo_signals.
_STRONG_DEMO_KEYS = (
    "known_demo_app", "authoritative_demo", "scaffold_fingerprint", "template_readme",
)


def _demo(tree: dict) -> tuple[bool, list[str]]:
    signals = tree.get("demo_signals", {}) or {}
    fired = [k for k, v in signals.items() if v]
    is_demo = any(signals.get(k) for k in _STRONG_DEMO_KEYS)
    return is_demo, fired


def build_git_block(gs: dict) -> dict:
    """The `git` block = git_stats' repo_stats sub-block + its full-history class tallies."""
    block = dict(gs.get("repo_stats", {}) or {})
    for k in (
        "class_a_count", "class_b_count", "class_c_pre_count", "class_d_bug_count",
        "confirmed_candidate_count", "provisional_candidate_count",
        "analyzed_commits", "full_history_scanned",
    ):
        if k in gs:
            block[k] = gs[k]
    return block


def build_material_block(repo: Path, provider: str, model: str | None, census_timeout: int,
                         do_llm: bool) -> dict:
    """The `material` block -- the ONE model-assisted measurement (logic depth via the census).

    Degrades, never crashes, mirroring the original ext behaviour: ``--no-llm`` skips it, and a
    census that could not run (no `claude`, throttle, timeout, ...) is recorded unscored with its
    reason. In every case `logic_depth` / `self_contained` are reported unscored, not as 0 -- a
    zero here would falsely claim the repository was measured and found empty.

    The returned dict is redacted and leak-audited with the rest of ``measurement.json``; the
    census also self-redacts every sentence before it gets here.
    """
    if not do_llm:
        return {
            "probe": "material_census",
            "scored": False,
            "provider": provider,
            "model": model,
            "skip_reason": "llm_disabled",
            "logic_depth_unavailable_reason": (
                "material census skipped (--no-llm); logic depth and self-containment are "
                "reported unscored, not zero"
            ),
            **material_census.unmeasured(),
        }

    mc = material_census.collect(repo, model=model, timeout=census_timeout, provider=provider)
    block = {k: v for k, v in mc.items() if k != "probe"}
    block["probe"] = "material_census"
    block["scored"] = bool(mc.get("ok"))
    if not mc.get("ok"):
        # collect() has already nulled every measurement; this is the sentence that says so, and
        # it is what grade-ext quotes when it marks the dimension unscored. Naming the timeout
        # explicitly matters: "the census exceeded its time budget" and "this repository holds
        # little material" are opposite findings and used to arrive as the same low number.
        reason = material_census.unavailable_reason(mc)
        if mc.get("timed_out"):
            reason += (f"; the lane was capped at {census_timeout} seconds and every material "
                       "measurement is reported unmeasured rather than scored low")
        block["logic_depth_unavailable_reason"] = reason
    return block


# The miner's own completion marker. `agentic_miner.mine()` ends a run that actually enumerated
# with this sentence, and no failure path produces it -- a timeout, a dead CLI or unparseable agent
# output all degrade to an empty candidate list plus a different note. Matching it is how a lane
# that LOOKED and found nothing (a real, reportable zero) is told apart from a lane that never
# finished looking. Without the distinction the two arrive identical, and a repository we ran out
# of clock on is indistinguishable from a repository with nothing in it.
_MINER_COMPLETED = re.compile(r"agent completed; agent proposed \d+ candidate\(s\)\.\s*$")


def _run_agentic_miner(repo: Path, provider: str, model: str, mine_n: int,
                       mine_timeout: int) -> tuple[dict, str | None]:
    """Run miners/agentic_miner.py as a fixed-argv subprocess (shell=False) and parse its JSON.

    Returns (payload, failure_reason). `failure_reason` is None only when the lane ran to
    completion, so the caller can emit measured counts; otherwise it is the sentence explaining
    why there is no measurement, and the counts go NULL.

    The miner is invoked with cwd=_MINERS so its bare-name sibling imports (`import agent_io`,
    `import candidates`) resolve against the miners directory and never collide with the
    collectors' own `candidates.py`. The miner itself never crashes -- every failure mode (no
    `claude`, timeout, unparseable agent output) yields an empty candidate list plus a `notes`
    string, which is why the completion marker rather than the exit status decides the verdict.
    Never raises.
    """
    argv = [
        sys.executable, str(_MINERS / "agentic_miner.py"), str(repo),
        "--n", str(mine_n), "--provider", provider, "--model", model,
        "--timeout", str(mine_timeout),
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, shell=False,
            cwd=str(_MINERS), timeout=mine_timeout + 120,
            # MODEL domain: the miner spawns the vendor's own provider CLI. It receives that
            # provider's auth variables and nothing else on the machine.
            env=childenv.build_env(childenv.MODEL, provider=provider,
                                   passthrough=("HOME", "USERPROFILE", "XDG_CONFIG_HOME")),
        )
    except subprocess.TimeoutExpired:
        return {}, f"agentic mining subprocess timed out after {mine_timeout}s"
    except OSError as exc:
        return {}, f"agentic mining subprocess could not run: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:300]
        return {}, f"agentic mining exited {proc.returncode}: {detail}"
    try:
        parsed = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return {}, "agentic mining produced no parseable JSON"
    if not isinstance(parsed, dict):
        return {}, "agentic mining produced unexpected output"
    notes = str(parsed.get("notes") or "")
    if not _MINER_COMPLETED.search(notes):
        return parsed, notes or "agentic mining did not report a completed census"
    return parsed, None


def build_mining_block(
    repo: Path, digest: str, provider: str, model: str | None, mine_n: int, mine_timeout: int,
    do_mine: bool, no_llm: bool, mined_at: str,
) -> dict:
    """The agentic mining lane, ext-privacy: per-task-type COUNTS + one tagged sentence each.

    This is the ext task-type census and it is the part of the artifact a reader actually uses.
    The miner enumerates every idea that clears the bar rather than a top-N shortlist, classifies
    each one into a task type, and returns one sentence per idea describing the SHAPE of the work.
    Each surviving sentence is emitted as "<task_type>: <sentence>", so a type's count is the
    number of sentences carrying its tag and can be audited by reading them.

    The files, symbols and commits behind each idea stay on the machine; only the counts and the
    validated sentences are written. Degrades, never crashes, and the block is always emitted so
    the schema is stable -- but WHAT the counts say depends on which of three things happened:

      * the lane ran and enumerated       -> numeric counts, 0 meaning measured-none
      * the operator did not ask for it   -> zeros plus skip_reason, the flag named
      * the lane could not finish         -> NULL counts plus mine_unavailable_reason
      * the lane filled `--mine-n`        -> numeric counts plus a note that they are a FLOOR

    The third case is the one that used to be wrong. A timed-out or crashed miner returned zeros,
    which reads downstream as "this repository supports no tasks" -- a verdict about the
    repository produced by a failure of ours. A count is a measurement or it is null.
    """
    block: dict = {
        "repo_id": f"repo-{digest[:12]}",
        "measurer_version": MEASURER_VERSION,
        "provider": provider,
        "model": model,
        "mined_at": mined_at,
        "total_candidates": 0,
        "n_net_new": 0,
        "n_agentic": 0,
        "n_bug_repair": 0,
        "n_repo_evolution": 0,
        "scored": False,
        "mined_task_summaries": [],
    }
    if not do_mine:
        block["skip_reason"] = "llm_disabled" if no_llm else "mine_disabled"
        block["mine_unavailable_reason"] = scrub(
            "agentic mining skipped ({}); task-type counts are reported as zero-because-not-run, "
            "not as measured-none".format("--no-llm" if no_llm else "--no-mine")
        )
        return block

    result, lane_failure = _run_agentic_miner(repo, provider, model, mine_n, mine_timeout)
    if lane_failure is not None:
        for key in ("total_candidates", *_MINING_COUNT_KEYS):
            block[key] = None
        block["mine_unavailable_reason"] = scrub(
            f"{lane_failure}; the lane did not finish, so every task-type count is reported "
            "unmeasured rather than as zero"
        )
        return block

    candidates = [c for c in (result.get("candidates") or []) if isinstance(c, dict)]
    for c in candidates:
        col = STRATEGY_COUNT_KEYS.get(str(c.get("strategy") or ""))
        if col:
            block[col] += 1
    block["total_candidates"] = len(candidates)
    block["scored"] = bool(candidates)
    # One sentence per candidate, describing the SHAPE of the work. The miner already held each
    # one to validate_sentence; it is checked again here because this is the process that writes
    # the file, and a boundary that trusts its input is not a boundary. A sentence that fails is
    # DROPPED and the candidate keeps its place in the count -- the alternative, emitting it with
    # a placeholder where the interesting noun was, tells the reader nothing and hides the fact
    # that something was removed.
    summaries = []
    for c in candidates:
        text = str(c.get("why_interesting") or "").strip()
        if text and not emit_allowlist.validate_sentence(text):
            task_type = str(c.get("strategy") or "agentic")
            summaries.append(f"{task_type}: {scrub(text)}")
    block["mined_task_summaries"] = summaries
    if not candidates:
        # The lane completed and enumerated nothing. That IS a measurement, so the zeros stand and
        # the note says which of the two zeros this is.
        block["mine_unavailable_reason"] = scrub(
            "the agentic census completed and enumerated no candidate that cleared the bar; "
            "these zeros are measured, not a failed lane"
        )
    elif len(candidates) >= mine_n:
        # The census filled its ceiling, so the count is a floor rather than a total. "we looked
        # exhaustively and found this many" and "we stopped counting here" are different facts
        # and a census that cannot tell them apart is not a census.
        block["mine_unavailable_reason"] = scrub(
            f"the agentic census reached its ceiling of {mine_n} ideas, so this count is a floor "
            f"rather than a total; raise the ceiling to enumerate the rest"
        )
        print(f"[cap] the mining census stopped at its ceiling of {mine_n} ideas",
              file=sys.stderr, flush=True)
    return block


def row_status(material: dict, mining: dict, build: dict | None) -> tuple[str, str | None]:
    """The flat row's `status` / `skip_reason`. `measured` ONLY when everything ran.

    measure-int answers this from a lane envelope per collector; ext has no envelope block, so the
    same question is answered from the two blocks that can degrade -- the census and the mining
    lane -- plus the build probe when it was asked for. The answer is int's answer: a run with a
    hole in it is `partial`.

    It used to be the literal string "measured", unconditionally, which meant the top-level status
    of a run whose census died was indistinguishable from one that measured everything. The nulls
    were honest and the summary column was not, and the summary column is the one people query.

    ONE deliberate difference from int: ext calls a run partial even when the OPERATOR skipped a
    model lane. int's row is read by our own orchestrator, which knows what it asked for; ext's row
    is read by a buyer, and `measured` on a row whose largest measurement is absent is a claim we
    have not earned. `skip_reason` still names the flag, so "you turned it off" and "it broke" stay
    different facts.

    A timeout outranks everything else in the reason, because it is the one gap whose remedy is a
    flag the operator controls (`--census-timeout`) rather than a machine they have to fix.
    """
    timed_out = bool(material.get("timed_out"))
    unavailable: list[str] = []
    skipped: list[str] = []

    if material.get("skip_reason"):
        skipped.append(str(material["skip_reason"]))
    elif not material.get("scored"):
        unavailable.append("census")

    if mining.get("skip_reason"):
        skipped.append(str(mining["skip_reason"]))
    elif mining.get("total_candidates") is None:
        # build_mining_block nulls the counts exactly when the lane did not finish.
        unavailable.append("mining")

    if build is not None:
        if build.get("timed_out"):
            timed_out = True
        elif build.get("build_skipped"):
            # The lane was asked for and the run's clock did not reach it. That is a hole in the
            # row, and a row that said `measured` would be claiming the probe answered.
            timed_out = True
        elif not build.get("ok"):
            unavailable.append("build")

    if timed_out:
        return "partial", "lanes_timed_out"
    if unavailable:
        return "partial", "lanes_unavailable"
    if skipped:
        return "partial", "llm_disabled" if "llm_disabled" in skipped else skipped[0]
    return "measured", None


def build_codebase_repos_row(
    tree: dict, git_block: dict, classification: dict,
    digest: str, structure: dict, capacity: int | None,
    build_ok, testable_at_head, measured_at: str,
    status: str = "measured", skip_reason: str | None = None,
) -> dict:
    """Map the collected signals onto the 38 promoted fields + MEASURE additions.

    Numbers/enums/dates only -- no path/symbol strings appear in this row, so it is safe by
    construction. NULL where a field is genuinely unmeasured; 0 only where measured-none.
    """
    total_source_files = tree.get("total_source_files") or 0
    test_spec_files = tree.get("test_spec_files")
    test_ratio = (
        round(test_spec_files / total_source_files, 4)
        if test_spec_files is not None and total_source_files else None
    )
    return {
        # identity / provenance (platform assigns the ids; ext never emits a real name)
        "id": None,
        "codebase_id": None,
        "service_id": None,
        "repo_digest": digest,
        "real_repo_name": None,                       # internal-only; withheld in the ext variant
        "fake_repo_name": f"repo-{digest[:12]}",      # stable, non-identifying display handle
        "status": status,
        "skip_reason": skip_reason,
        "measured_at": measured_at,
        # size / language
        "loc": tree.get("total_loc"),
        "zip_bytes": None,                            # not measured (no bundle is produced)
        "excluded_loc": None,                         # generated files are counted, not summed as LOC
        "languages": tree.get("loc_by_language"),
        "primary_language": tree.get("primary_language"),
        "frontend_pct": None,                         # no clean deterministic LOC split
        "backend_pct": None,
        # tests
        "test_loc": None,                             # test LOC not separately summed
        "test_code_files": structure.get("test_files") if structure.get("ok") else None,
        "test_spec_files": test_spec_files,
        "test_ratio": test_ratio,
        "test_source_ratio": tree.get("test_source_ratio"),
        "test_framework": tree.get("test_framework"),
        # ci
        "has_ci": tree.get("ci_present"),
        "ci_present": tree.get("ci_present"),
        "ci_runs_tests": tree.get("ci_runs_tests"),
        "detected_frameworks": tree.get("detected_frameworks"),
        # history
        "commit_count": git_block.get("total_commits"),
        "author_count": git_block.get("human_authors"),
        "first_commit_at": git_block.get("first_commit"),
        "last_commit_at": git_block.get("last_commit"),
        "active_days": git_block.get("active_days"),
        "span_days": git_block.get("span_days"),
        "commits_by_month": git_block.get("commits_by_month"),
        # provider tables (absent -> null, per CONTRACT)
        "pr_count": None,
        "issue_count": None,
        # classification
        "repo_class": classification.get("primary_class"),
        "is_likely_demo": classification.get("is_likely_demo"),
        # legacy, deprecated
        "quality_score": None,
        # MEASURE additions (new nullable columns)
        "build_ok": build_ok,
        "testable_at_head": testable_at_head,
        "capacity": capacity,
    }


_CSV_COLUMNS = [
    "id", "codebase_id", "service_id", "repo_digest", "real_repo_name", "fake_repo_name",
    "status", "skip_reason", "measured_at", "loc", "zip_bytes", "excluded_loc", "languages",
    "primary_language", "frontend_pct", "backend_pct", "test_loc", "test_code_files",
    "test_spec_files", "test_ratio", "test_source_ratio", "test_framework", "has_ci",
    "ci_present", "ci_runs_tests", "detected_frameworks", "commit_count", "author_count",
    "first_commit_at", "last_commit_at", "active_days", "span_days", "commits_by_month",
    "pr_count", "issue_count", "repo_class", "is_likely_demo", "quality_score",
    "build_ok", "testable_at_head", "capacity",
]


def _csv_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        return json.dumps(v, sort_keys=True, separators=(",", ":"))
    return str(v)


def write_outputs(
    out_dir: Path, row: dict, measurement: dict, mining: dict, sink: str
) -> list[Path]:
    """Emit codebase_repos.{json,csv}, measurement.json and codebase_repo_mining.json per sink."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    row_json = out_dir / "codebase_repos.json"
    row_json.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
    written.append(row_json)

    row_csv = out_dir / "codebase_repos.csv"
    with row_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerow({c: _csv_cell(row.get(c)) for c in _CSV_COLUMNS})
    written.append(row_csv)

    # The mining counts are always emitted (zeroed when the lane is skipped) so the mining schema
    # is present for every measured repo, regardless of sink -- they are the ext task-type census.
    mine_path = out_dir / "codebase_repo_mining.json"
    mine_path.write_text(json.dumps(mining, indent=2) + "\n", encoding="utf-8")
    written.append(mine_path)

    if sink != "csv":
        mpath = out_dir / "measurement.json"
        mpath.write_text(json.dumps(measurement, indent=2) + "\n", encoding="utf-8")
        written.append(mpath)
    return written


def _git_lane(repo: Path, git_top: int) -> tuple[dict, dict]:
    """git_stats plus the month/day calendar it needs a tip commit for.

    One lane rather than two, because the calendar depends on `effective_tip_sha` and both halves
    are git-log walks over the same history -- splitting them would only add a barrier.
    """
    git_raw = _run_collector("git_stats.py", [str(repo), "--limit", "0", "--top", str(git_top)])
    git_block = build_git_block(git_raw)
    tip = git_block.get("effective_tip_sha") or git_block.get("head_sha") or "HEAD"
    git_block["commits_by_month"] = commits_by_month(repo, tip)
    git_block["active_days"] = active_days(repo, tip)
    return git_raw, git_block


REPAIR_HELP = (
    "After a project's install or build FAILS, give a provider CLI one bounded turn inside the sandbox to make the environment work -- install a missing system library, add config the project's own docs call for -- and then RE-RUN the same command and measure the outcome. The as-shipped verdict is never overwritten: install_ok, build_ok and observed_runnability keep describing the repository as it arrived, and the repair result rides alongside in the `repair` keys. The model never reports success; the re-run does. A turn that modifies any git-TRACKED file is recorded as a FAILED repair whatever the re-run said, because an agent that deletes a failing target has produced the one measurement we must never take. Requires --build full; ignored at discover, where there is no executed failure to repair."
)


def build_lane(repo: Path, level: str, probe_budget: int, timeout_build: int, max_projects: int,
               full_attempt_seconds: int, deadline: Deadline, repair: bool = False,
        repair_provider: str = "claude",
        repair_model: str | None = None) -> dict:
    """Run the build probe at `level`, and complete the measurement at `discover` if `full` runs
    out of clock.

    WHY THE FALLBACK EXISTS. `full` is the default because it is the only level that can reach
    `observed_runnability` band 4 -- the largest measured step in the corpus, 4.26 mean delivered
    tasks against a flat 2.20 below it -- and the only level that produces a coverage number at
    all. But a repository whose suite takes an hour would otherwise return NOTHING, and nothing is
    the worst of the available answers: an unmeasured repository reads as a bad one.

    WHEN IT FIRES, AND WHEN IT MUST NOT. Only when the probe's own clock cut the run short
    (`run_budget_exhausted`) AND the full-level index therefore has no value. Every other outcome
    of a `full` run is already COMPLETE:

      * a build that genuinely fails, or a project the runner found no tests in, is a measured
        property of the repository. Its record already carries install_ok, build_ok and
        tests_discovered, so a `discover` re-run would spend the rest of the reserve reproducing
        facts we hold. It gets fewer points, which is the correct outcome and not a fallback.
      * a missing language runtime or an unreachable registry is OURS, and the probe already
        reports the affected indices NULL with the attribution. A `discover` re-run would hit the
        same missing runtime.

    THE BUDGET IS ONE ALLOWANCE, NOT TWO TIMERS. `build_probe.Budget` is the probe's own cumulative
    clock and it is what bounds this lane: the `full` attempt draws `full_attempt_seconds` of it,
    and the fallback gets what that attempt did not spend, floored by the run-wide `Deadline`. The
    two calls together can never exceed the build reserve, so the 9000-second run budget is
    untouched. Each `collect()` also takes its own snapshot of the tree and restores it in its own
    `finally`, so a second call does not weaken the checkout guarantee.
    """
    common = {"agentic": False, "timeout": timeout_build, "max_projects": max_projects,
              "repair": repair, "repair_provider": repair_provider, "repair_model": repair_model}
    if level != "full":
        result = build_probe.collect(repo, level=level, budget_seconds=probe_budget, **common)
        result["build_level_requested"] = level
        return result

    reserve = build_probe.Budget(probe_budget, phase_cap=timeout_build)
    attempt = min(full_attempt_seconds, probe_budget)
    result = build_probe.collect(repo, level="full", budget_seconds=attempt, **common)
    result["build_level_requested"] = "full"
    if not (result.get("run_budget_exhausted")
            and result.get("observed_runnability") is None):
        return result

    left = int(min(reserve.remaining(), deadline.remaining()))
    if left < build_probe.MIN_PHASE_SECONDS:
        result["build_level_fallback_reason"] = (
            f"the full run did not finish inside its {int(attempt)} second share of the build "
            f"budget, and too little of the budget remained to complete the measurement at the "
            f"discover level; the executed indices are null with a reason, never a low band")
        print(f"[build] {result['build_level_fallback_reason']}", file=sys.stderr, flush=True)
        return result

    fallback = build_probe.collect(repo, level="discover", budget_seconds=left, **common)
    fallback["build_level_requested"] = "full"
    fallback["build_level_fallback_reason"] = (
        f"the full run did not finish inside its {int(attempt)} second share of the build budget, "
        f"so the measurement was completed at the discover level with the {left} seconds that "
        f"remained; dependencies, the build and test enumeration are executed facts, the suite was "
        f"not run, and the full-level index is unscored with a reason rather than scored low")
    print(f"[build] {fallback['build_level_fallback_reason']}", file=sys.stderr, flush=True)
    return fallback


def measure(
    repo: Path, top_files: int, git_top: int, threshold: float, build_level: str,
    model: str | None, do_llm: bool, census_timeout: int,
    do_mine: bool, mine_n: int, mine_timeout: int, provider: str = "claude",
    jobs: int = 0, clock: LaneClock | None = None, deadline: Deadline | None = None,
    build_budget: int = DEFAULT_BUILD_BUDGET_SECONDS,
    timeout_build: int = DEFAULT_TIMEOUT_BUILD,
    max_build_projects: int = build_probe.MAX_PROBED_PROJECTS,
    full_attempt_seconds: int = DEFAULT_FULL_ATTEMPT_SECONDS,
    repair: bool = False,
) -> tuple[dict, dict, dict]:
    """Collect the stats and return (codebase_repos_row, measurement_doc, mining_doc).

    Everything is deterministic except two model-assisted lanes -- the material census (logic depth)
    and the agentic miner (task-type census). Both degrade to an unscored block when disabled or
    unavailable. All string leaves in the returned structures are already redacted; a leak audit has
    passed over every one of them (including the mined proposal summaries) before this returns.

    THE LANE GRAPH. Everything except classification and the build probe is independent, so the
    independent lanes run CONCURRENTLY and the wall clock is the slowest one rather than the sum:

        phase 1, all at once   digest, repo_stats, git_stats (+ calendar), code_structure,
                               git_history, material_census, agentic_mining
        phase 2, after phase 1 classify_repo -- it consumes repo_stats' output, and it is a pure
                               in-process function over a dict, so it costs nothing to serialise
        phase 3, ALONE         build_probe, only with --build

    Phase 3 is exclusive by construction and that is not a performance choice. The build probe runs
    the project's own install and test commands INSIDE the checkout: it rewrites lockfiles and drops
    artefacts into the tree that every other lane is reading. Overlapping it with a tree reader
    would not be slow, it would be wrong -- the readers would be measuring a tree the probe was in
    the middle of changing. It therefore starts only once every reader, the two model lanes
    included, has finished. `LaneClock.overlaps("build_probe")` is the recorded proof of that.

    Both model lanes sit in phase 1 deliberately. They are the long poles -- tens of minutes on a
    large repository -- and each is blocked on a provider subprocess rather than on this
    interpreter, so overlapping them with the deterministic collectors costs nothing and removes
    the collectors from the wall clock entirely.

    THE BUDGET. One `Deadline` covers the whole invocation and every lane draws from it. The two
    model lanes are capped at what is left MINUS the build reserve, so the one lane that executes
    anything cannot be starved by the two that are waiting on a provider; the build probe is then
    capped at the smaller of that reserve and what is actually left. A lane the budget does not
    reach reports its measurements NULL with a reason. Nothing anywhere reports a truncated count.
    """
    measured_at = datetime.now(timezone.utc).isoformat()
    clock = clock if clock is not None else LaneClock()
    deadline = deadline if deadline is not None else Deadline(DEFAULT_BUDGET_SECONDS)
    do_build = build_level != "none"

    # The reserve is only carved out of the model lanes when there is a build probe to protect, and
    # it never leaves a model lane less than half the budget: starving the census to guarantee a
    # build probe would trade the largest measurement for one of the cheapest.
    reserve = min(build_budget, deadline.total // 2) if do_build else 0
    census_ceiling = deadline.slice(census_timeout, reserve) if do_llm else census_timeout
    mine_ceiling = deadline.slice(mine_timeout, reserve) if do_mine else mine_timeout

    # A lane is a name and a thunk. Anything a lane raises propagates out of fut.result() on this
    # thread exactly as it did when the calls were sequential -- a broken collector still stops the
    # run rather than yielding a quietly empty measurement.
    #
    # The mining lane is handed an empty digest and has its repo_id stamped on after the barrier.
    # The alternative -- making the longest lane in the run wait on a full-tree sha256 walk for one
    # provenance field -- puts a several-second crawl in front of an hour of work for no reason.
    lanes = {
        "digest": lambda: repo_digest(repo),
        "repo_stats": lambda: _run_collector(
            "repo_stats.py", [str(repo), "--top-files", str(top_files)]),
        "git_stats": lambda: _git_lane(repo, git_top),
        "code_structure": lambda: code_structure.collect(repo, allow_agentic=False),
        "git_history": lambda: git_history.collect(repo, agentic=False, redact_refs=True),
        "material_census": lambda: build_material_block(
            repo, provider, model, census_ceiling, do_llm),
        # do_mine already folds in --no-llm (the caller ANDs them), so this lane is off whenever
        # either switch is set; no_llm is passed through only so the skip note names the right flag.
        "agentic_mining": lambda: build_mining_block(
            repo, "", provider, model, mine_n, mine_ceiling, do_mine, not do_llm, measured_at),
    }

    def run(name: str, thunk):
        with clock.lane(name):
            return thunk()

    workers = jobs if jobs and jobs > 0 else len(lanes)
    clock.start_heartbeat()
    try:
        with cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(lanes)))) as pool:
            futures = {name: pool.submit(run, name, thunk) for name, thunk in lanes.items()}
            results = {name: fut.result() for name, fut in futures.items()}

        digest = results["digest"]
        tree_raw = results["repo_stats"]
        git_raw, git_block = results["git_stats"]
        structure_raw = results["code_structure"]
        history_raw = results["git_history"]
        material = results["material_census"]
        mining = results["agentic_mining"]
        mining["repo_id"] = f"repo-{digest[:12]}"

        with clock.lane("classify"):
            classify_raw = classify_repo.classify(tree_raw, threshold)

        # --- phase 3: the build probe, alone, after every reader has finished ---
        build_ok = None
        testable_at_head = None
        build_raw = None
        if do_build:
            with clock.lane("build_probe"):
                # Whatever is left, up to the reserve. The probe divides that between its projects
                # and phases itself, and reports what it did not reach as skipped rather than
                # failed -- so a spent budget costs measurements, never accuracy.
                probe_budget = min(build_budget, int(deadline.remaining()))
                if probe_budget < build_probe.MIN_PHASE_SECONDS:
                    build_raw = build_probe.skipped_budget(deadline.remaining())
                    print(f"[budget] the build probe was not started: {probe_budget}s of the "
                          f"{deadline.total}s budget remained", file=sys.stderr, flush=True)
                else:
                    build_raw = build_lane(
                        repo, build_level, probe_budget, timeout_build, max_build_projects,
                        full_attempt_seconds, deadline,
                        repair=repair, repair_provider=provider,
                        repair_model=model)
            build_ok = build_raw.get("build_ok")
            testable_at_head = build_raw.get("build_and_tests_ran")
            if build_raw.get("note"):
                print(f"[budget] build probe: {build_raw['note']}", file=sys.stderr, flush=True)
            # The level that actually happened, printed for every run. An operator reading a grade
            # with the full-level dimension unscored needs to see this line, not infer it.
            if build_raw.get("build_level"):
                ran_at, asked = build_raw["build_level"], build_raw.get("build_level_requested")
                fell_back = "" if ran_at == asked else f" (requested {asked})"
                print(f"[build] ran at level {ran_at}{fell_back}; "
                      f"observed_runnability={build_raw.get('observed_runnability')}, "
                      f"discover_runnability={build_raw.get('discover_runnability')}",
                      file=sys.stderr, flush=True)
            if build_raw.get("observed_runnability_reason"):
                print(f"[build] the full-level index is unscored: "
                      f"{build_raw['observed_runnability_reason']}", file=sys.stderr, flush=True)
    finally:
        clock.stop_heartbeat()

    # A redacted, numbers-only summary of the task-type census, mirrored into the material block so
    # a reader of measurement.json alone sees the mining shape without opening the mining file.
    material["task_type_counts"] = {
        "total_candidates": mining["total_candidates"],
        **{k: mining[k] for k in _MINING_COUNT_KEYS},
    }
    # Where the time went, in the record as well as on the terminal. The measurement schema is
    # frozen, so this rides in the one always-writable prose field the material block declares
    # rather than becoming a new key -- see LaneClock.note().
    material["census_platform_note"] = "; ".join(
        part for part in (material.get("census_platform_note"), clock.note()) if part
    )

    # CONTRACT.md defines capacity as "the deterministic count of mineable commits across
    # history", which is git_history's `mineable_commits` and NOT git_stats'
    # `confirmed_candidate_count` -- a different quantity that ext used to promote under the same
    # name. The two disagree (150 against 71 on this repository), and grade multiplies capacity
    # into `mining_rank`, so the divergence made ext and int rankings incomparable while looking
    # identical. Unavailable -> null: a wrong number under the right name is worse than none.
    capacity = history_raw.get("mineable_commits") if history_raw.get("ok") else None

    is_demo, demo_reasoning = _demo(tree_raw)
    classification = {
        "primary_class": classify_raw.get("primary_class"),
        "class_confidence": classify_raw.get("class_confidence"),
        "is_monorepo": classify_raw.get("is_monorepo"),
        "is_likely_demo": is_demo,
        "demo_reasoning": demo_reasoning,
    }

    measurement = {
        "measurer_version": MEASURER_VERSION,
        "measured_at": measured_at,
        "repo_digest": digest,
        # Emitted so an external measurement can be mapped to the repository it describes.
        # `repo_digest` cannot do that job: it identifies the TREE, while the platform keys
        # repositories on their name, so without this the mapping is a human guess.
        "real_repo_name": repo_full_name(repo),
        "variant": "ext",
        "tree": tree_raw,
        "git": git_block,
        "classification": classification,
        "capacity": capacity,           # in measurement.json so grade reads it
        "ext_signals": {
            "structure": structure_raw,
            "history": history_raw,
            "build": build_raw,
        },
        "material": material,
    }

    # --- ALLOWLIST, then REDACT, then AUDIT. Nothing reaches disk until all three pass. ---
    # The allowlist is the boundary: every leaf and every key is matched against what
    # emit_allowlist declares this document may contain, and anything undeclared stops the run.
    # The scrub and the audit run behind it as a backstop over what it permitted.
    measurement = emit_allowlist.enforce("measurement", measurement)
    measurement = redact_tree(measurement)
    audit_no_leak(measurement)

    # The flat row holds numbers, dates, enums and public stack-name lists only -- no repo-derived
    # symbols -- so it is built from the RAW collector output (real language/framework names) and
    # verified by the same leak audit, which exempts the provenance and stack-fact fields.
    status, skip_reason = row_status(material, mining, build_raw if do_build else None)
    row = build_codebase_repos_row(
        tree=tree_raw,
        git_block=git_block,
        classification=classification,
        digest=digest,
        structure=structure_raw,
        capacity=capacity,
        build_ok=build_ok,
        testable_at_head=testable_at_head,
        measured_at=measured_at,
        status=status,
        skip_reason=skip_reason,
    )
    row = emit_allowlist.enforce("codebase_repos", row)
    audit_no_leak(row)

    # The mining block carries only integer counts, provenance and PRE-VALIDATED free text (each
    # summary was held to validate_sentence where the miner built it, then scrubbed). It is NOT
    # passed through redact_tree -- that would mangle the digest-derived repo_id -- but it IS
    # held to the allowlist and to the same write-boundary leak audit.
    mining = emit_allowlist.enforce("codebase_repo_mining", mining)
    audit_no_leak(mining)
    return row, measurement, mining


# Rough per-lane costs for the up-front estimate, in seconds. The deterministic figures are fitted
# to observed runs (2.7s to 8.8s for the whole concurrent fan-out on four real repositories); the
# two model-lane figures are the mid-point of the documented "minutes to two hours" range, scaled
# by tree size. They are an ESTIMATE, printed as one, and the run is bounded by the budget rather
# than by them.
_EST_DETERMINISTIC = (5.0, 3.0)      # constant, plus this many seconds per thousand files
_EST_MODEL_LANE = (900.0, 45.0)      # a census or a mining pass, same shape


def _est(base: tuple[float, float], kfiles: float, cap: float) -> float:
    return min(cap, base[0] + base[1] * kfiles)


def _duration(seconds: float) -> str:
    """Minutes once there are minutes to speak of, seconds before that. "a 0 minute run" is not a
    useful thing to tell somebody who asked how long this will take. Reads as an adjective: the
    caller writes "a {duration} run"."""
    return f"{seconds:.0f} second" if seconds < 120 else f"{seconds / 60:.0f} minute"


def estimate_run(repo: Path, build_level: str, do_llm: bool, do_mine: bool,
                 census_timeout: int, mine_timeout: int,
                 max_build_projects: int = build_probe.MAX_PROBED_PROJECTS
                 ) -> tuple[float, dict, dict]:
    """(estimated seconds, per-lane estimates, the read-only survey the estimate came from).

    The lane graph is two phases, so the estimate is the slowest concurrent lane plus the build
    probe, not the sum of everything.
    """
    survey = build_probe.survey(repo, max_build_projects)
    kfiles = (survey["files_scanned"] or 0) / 1000.0
    lanes = {"deterministic collectors": _est(_EST_DETERMINISTIC, kfiles, 3600.0)}
    if do_llm:
        lanes["census"] = _est(_EST_MODEL_LANE, kfiles, census_timeout)
    if do_mine:
        lanes["mining"] = _est(_EST_MODEL_LANE, kfiles, mine_timeout)
    lanes["build probe"] = build_probe.estimate_seconds(survey, build_level)
    concurrent = max(v for k, v in lanes.items() if k != "build probe")
    return concurrent + lanes["build probe"], lanes, survey


def plan_lines(repo: Path, build_level: str, do_llm: bool, do_mine: bool, budget: int,
               census_timeout: int, mine_timeout: int, max_build_projects: int) -> list[str]:
    """The up-front prediction, and -- when the estimate exceeds the budget -- what will be cut.

    An operator should never be surprised by a truncated run. If the estimate does not fit, this
    says so and names the lane that will report NULL, before a minute has been spent.
    """
    total, lanes, survey = estimate_run(repo, build_level, do_llm, do_mine,
                                        census_timeout, mine_timeout, max_build_projects)
    ecosystems = ", ".join(survey["ecosystems"]) or "no build manifest found"
    split = ", ".join(f"{name} {_duration(value)}s" for name, value in lanes.items() if value)
    lines = [
        f"[plan] {survey['n_projects']} project roots ({ecosystems}), "
        f"{survey['files_scanned']} files in {survey['dirs_scanned']} directories scanned",
        f"[plan] this looks like a {_duration(total)} run against a {_duration(budget)} budget "
        f"(rough, from repository size and project count): {split}",
    ]
    if survey["n_projects_over_cap"]:
        lines.append(
            f"[plan] {survey['n_projects_over_cap']} of {survey['n_projects']} project roots are "
            f"past the probe cap and will be reported skipped, not measured; raise "
            f"--max-build-projects to probe them")
    if total > budget:
        cut = "the build probe" if lanes["build probe"] else "the model lanes"
        lines.append(
            f"[plan] the estimate EXCEEDS the budget, so expect {cut} to be cut short: whatever "
            f"the budget does not reach is reported null with a reason, never as a low number. "
            f"Raise --budget-seconds to measure it all.")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic, privacy-preserving repo measurement (MEASURE / external)."
    )
    parser.add_argument("repo", help="Path to the git repository to measure.")
    parser.add_argument("--out", required=True, help="Output directory (created if absent).")
    parser.add_argument(
        "--sink", choices=["json", "csv", "db"], default="json",
        help="Output sink: json (default), csv (row only), or db (stub -> NotImplementedError).",
    )
    parser.add_argument(
        "--build", nargs="?", const=DEFAULT_BUILD_LEVEL, default=DEFAULT_BUILD_LEVEL,
        choices=build_probe.BUILD_LEVELS, dest="build",
        help=(f"The deterministic build probe, which RUNS BY DEFAULT at level "
              f"{DEFAULT_BUILD_LEVEL}: resolve dependencies, install, build, ask each runner to "
              f"LIST its own tests, then EXECUTE the suite and read coverage. A full run that does "
              f"not finish inside --full-attempt-seconds completes at `discover` instead, and says "
              f"so. --build discover pins the cheaper level. --build none is the escape hatch for "
              f"an operator who cannot run project code, and leaves every executed measurement "
              f"NULL."),
    )
    parser.add_argument(
        "--full-attempt-seconds", type=int, default=DEFAULT_FULL_ATTEMPT_SECONDS,
        dest="full_attempt_seconds",
        help=(f"How long a `--build full` run may take before the measurement is completed at "
              f"`discover` with what is left of the build reserve. Default "
              f"{DEFAULT_FULL_ATTEMPT_SECONDS}. A slice of --build-budget-seconds, not an extra "
              f"budget: the attempt and the fallback together never exceed the reserve."),
    )
    parser.add_argument(
        "--budget-seconds", type=int, default=DEFAULT_BUDGET_SECONDS, dest="budget",
        help=(f"GLOBAL wall-clock budget for the whole run, shared by every lane. Default "
              f"{DEFAULT_BUDGET_SECONDS} (two and a half hours). Work the budget does not reach is "
              f"reported NULL with a reason, never as a low measurement."),
    )
    parser.add_argument(
        "--build-budget-seconds", type=int, default=DEFAULT_BUILD_BUDGET_SECONDS,
        dest="build_budget",
        help=(f"The build probe's reserved share of the global budget, cumulative across every "
              f"project and phase. Default {DEFAULT_BUILD_BUDGET_SECONDS}."),
    )
    parser.add_argument(
        "--timeout-build", type=int, default=DEFAULT_TIMEOUT_BUILD, dest="timeout_build",
        help=(f"Ceiling for ONE command inside the build probe (an install, a build, a suite). "
              f"Default {DEFAULT_TIMEOUT_BUILD}, and always subordinate to the two budgets."),
    )
    parser.add_argument(
        "--max-build-projects", type=int, default=build_probe.MAX_PROBED_PROJECTS,
        dest="max_build_projects",
        help=(f"How many discovered project roots the probe may run commands in, largest first. "
              f"Default {build_probe.MAX_PROBED_PROJECTS}; the rest are reported skipped."),
    )
    parser.add_argument("--top-files", type=int, default=10)
    parser.add_argument("--git-top", type=int, default=1)
    parser.add_argument("--threshold", type=float, default=0.18)
    parser.add_argument(
        "--jobs", type=int, default=0,
        help="Max lanes to run concurrently (default: one worker per lane). --jobs 1 serialises.",
    )
    parser.add_argument(
        "--repair", action="store_true", help=REPAIR_HELP)
    parser.add_argument(
        "--provider", choices=("claude", "codex", "gemini"), default="claude",
        help="Model CLI provider (default claude).",
    )
    parser.add_argument(
        "--model", default=None,
        help="Provider model id/alias (Claude default from models.py / RQE_MODEL; required otherwise).",
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help=("Skip BOTH model lanes (material census and agentic mining). logic_depth / "
              "self_contained are unscored, mining counts zeroed, status partial."),
    )
    parser.add_argument(
        "--census-timeout", type=int, default=material_census.DEFAULT_TIMEOUT,
        help=(f"Ceiling (seconds) for the census lane, retries included. Default "
              f"{material_census.DEFAULT_TIMEOUT} -- a runaway backstop, not a budget. Hitting it "
              f"nulls every material measurement rather than lowering it."),
    )
    parser.add_argument(
        "--no-mine", action="store_true",
        help="Skip only the agentic mining lane; codebase_repo_mining.json is still written zeroed.",
    )
    parser.add_argument(
        "--mine-n", type=int, default=DEFAULT_MINE_N,
        help=(f"Ceiling on task ideas the miner enumerates in one turn (default {DEFAULT_MINE_N}). "
              f"The lane is a census, not a top-N: raising this does not make the run longer than "
              f"the budget, it spends the budget on the most valuable thing the skill produces."),
    )
    parser.add_argument(
        "--mine-timeout", type=int, default=DEFAULT_MINE_TIMEOUT,
        help=(f"Ceiling (seconds) for the agentic mining lane. Default {DEFAULT_MINE_TIMEOUT} -- a "
              f"runaway backstop, not a budget. Hitting it nulls every task-type count."),
    )
    parser.add_argument(
        "--review", action="store_true",
        help="Print every emitted field and its value, grouped by kind, before you send.",
    )
    args = parser.parse_args()

    if args.sink == "db":
        raise NotImplementedError(
            "The 'db' sink is a documented stub: this skill does not write to a database. "
            "The platform ingests the emitted codebase_repos.{json,csv} and measurement.json. "
            "Run with --sink json (default) or --sink csv and load those artifacts."
        )

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"error: not a directory: {repo}", file=sys.stderr)
        return 2

    model = args.model or (models.DEFAULT_MODEL if args.provider == "claude" else None)
    if not args.no_llm and not model:
        print(f"error: --model is required for provider {args.provider}", file=sys.stderr)
        return 2

    # Resolve the requested provider BEFORE any measurement runs. A CLI that is missing is a
    # fact about this machine, not about the repository, and finding it out twenty minutes in --
    # as an unscored lane in a file somebody else reads next month -- is how "not installed"
    # becomes "this codebase had nothing in it".
    # The same reasoning covers the second question, which is not "is the CLI here" but "can it be
    # stopped from honouring configuration committed to the repository we are about to point it
    # at". A CLI that cannot is not a degraded run, it is a run that would execute the measured
    # tree's hooks on this machine, so it stops here too rather than in a traceback thirty seconds
    # into the census.
    if not args.no_llm:
        try:
            exe, _stdin, _note = material_census.resolve_cli(args.provider)
            childenv.isolation_flags(args.provider, exe)
        except (material_census.ProviderUnavailable, childenv.ProviderNotIsolated) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # What this is about to cost, before it starts costing it. An operator who knows a run is a
    # twenty-minute run does not kill it at minute fifteen, and a killed run is the most expensive
    # outcome there is: everything spent is lost and the repository is reported as unmeasured.
    for line in plan_lines(repo, args.build, not args.no_llm,
                           not (args.no_mine or args.no_llm), args.budget,
                           args.census_timeout, args.mine_timeout, args.max_build_projects):
        print(line, file=sys.stderr, flush=True)

    clock = LaneClock()
    deadline = Deadline(args.budget)
    row, measurement, mining = measure(
        repo, args.top_files, args.git_top, args.threshold, args.build,
        model=model, do_llm=not args.no_llm, census_timeout=args.census_timeout,
        do_mine=not (args.no_mine or args.no_llm), mine_n=args.mine_n,
        mine_timeout=args.mine_timeout, provider=args.provider, jobs=args.jobs, clock=clock,
        deadline=deadline, build_budget=args.build_budget, timeout_build=args.timeout_build,
        max_build_projects=args.max_build_projects,
        full_attempt_seconds=args.full_attempt_seconds,
        repair=args.repair,
    )
    written = write_outputs(
        Path(args.out).expanduser().resolve(), row, measurement, mining, args.sink
    )

    # Where the time actually went. Printed for every run, however short: an operator deciding
    # whether an hour was reasonable needs the split, not the total.
    print(clock.table(), file=sys.stderr)
    print(f"  budget: {deadline.elapsed():.0f}s of {deadline.total}s used", file=sys.stderr)
    if args.build != "none":
        # Stated rather than assumed: the probe mutates the tree the readers walk, so "it ran
        # alone" is a correctness claim and it is checked against the recorded spans.
        overlapping = clock.overlaps("build_probe")
        verdict = "yes" if not overlapping else "NO -- overlapped " + ", ".join(overlapping)
        print(f"  build probe ran alone: {verdict}", file=sys.stderr)

    print(f"{row['status']} {row['fake_repo_name']} (digest {row['repo_digest'][:12]})")
    for p in written:
        print(f"  wrote {p}")
    if row["status"] != "measured":
        print(f"WARNING: this run is PARTIAL ({row['skip_reason']}); the lanes that did not run "
              f"report NULL, not zero. Read logic_depth_unavailable_reason and "
              f"mine_unavailable_reason before treating any number here as a verdict.",
              file=sys.stderr)

    # The files are yours and they are still on your machine. This is what is in them.
    documents = {"codebase_repos.json": row, "codebase_repo_mining.json": mining}
    if args.sink != "csv":
        documents["measurement.json"] = measurement
    print(emit_allowlist.review(documents, full=args.review))
    if not args.review:
        print("Run again with --review to see every field and its value before you send.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

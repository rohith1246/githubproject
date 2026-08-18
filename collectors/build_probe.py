#!/usr/bin/env python3
"""build_probe.py -- work out how this repository is actually built, then build it and test it.

Usage (library module, no CLI):

    import build_probe
    result = build_probe.collect(repo, timeout=900, skip=False, model=DEFAULT_MODEL,
                                 agentic=True, level="discover", budget_seconds=1800,
                                 max_projects=8)

    repo            Path to the checkout to probe. The project's own install, build and test
                    commands run inside it, so pass a clean, disposable clone or worktree.
    timeout         Seconds. The PER-PHASE ceiling, subordinate to `budget_seconds`: a phase gets
                    the smallest of this, what is left of its project's share, and what is left of
                    the whole probe. Default 900.
    budget_seconds  Seconds. The CUMULATIVE ceiling for the whole probe -- every project and every
                    phase draws from one monotonic allowance. Default 1800. This is the number
                    that bounds the run; `timeout` alone never did, because it applied per phase
                    per project and 24 projects x 5 phases x 900s is 30 hours.
    level           "none" | "discover" | "full". `none` runs nothing and returns the skip stub.
                    `discover` resolves dependencies, builds, and asks the runner to LIST its
                    tests. `full` also executes the suite and reads coverage. Default "discover".
    max_projects    How many discovered project roots may be PROBED, largest first. Default 8.
                    Discovery still enumerates the rest and the aggregate says how many were
                    skipped and why -- a silent cap reads as "we looked and found nothing".
    skip            True returns the `build_skipped` stub and runs nothing (same as level="none").
    model           Provider model id. Agentic path only; ignored when `agentic=False`.
    agentic         False forces the deterministic multi-project path and never looks for
                    `claude`. This is the contract for the measure skills, which must not spend
                    money. `level` other than "full" also forces it: the levels are a property of
                    the deterministic plan, and an agent decides its own phases.
    allow_home_toolchains
                    Whether runtime resolution may select an interpreter installed under the
                    OPERATOR'S HOME -- nvm, pyenv, asdf, mise, uv and sdkman all live there and
                    between them hold most of the version coverage a real corpus needs. Default
                    False, and it must stay False on a machine we do not own: `childenv` strips
                    home entries from the BUILD PATH so a repository's own postinstall hook cannot
                    see inside somebody's home directory, and selecting a runtime from there hands
                    it one. measure-int passes True (disposable container of ours); measure-ext
                    never does. Nothing is installed either way -- see `runtime.py`.

Environment variables: this module READS none. Every child process runs in the BUILD trust
domain built by `childenv.build_env(childenv.BUILD, home=<scratch>)`, which starts from an empty
mapping -- no cloud, VCS, database, registry, SSH or provider credential ever reaches a
repository-controlled command -- and gets a scratch HOME so the operator's dotfile credentials
are unreachable too. `childenv` copies PATH, LANG, LC_ALL, LC_CTYPE, TZ, TERM and the Windows
process variables from the ambient environment, and nothing else.

Carries 40 of the 100 rubric points, and it is the only lane that executes anything, so four
properties matter here as much as the numbers do.

  * **Every project in the tree is probed, not the first one a precedence table matched.** The
    previous version picked ONE ecosystem by manifest order, so a repository holding a Node
    frontend and a Rust core was measured as Node and the Rust half was invisible. Discovery now
    enumerates all project roots -- npm/pnpm/Yarn workspaces, Cargo workspaces, Python package
    and tox roots, go.work and go.mod, Maven parents, Gradle settings and composites, .NET
    solutions, Ruby, PHP -- and emits one plan per root unless one authoritative workspace
    command genuinely covers the rest. Roots that discovery dropped are recorded, because
    `no_manifest` was the single largest failure class in the 417-repo run and part of it was
    OUR discovery failing while being written down as the repository's.

  * **The commands are derived, not guessed from a filename.** A fixed toolchain table cannot
    cover every language or every host, and the one that used to live in this file proved it: it
    ran `npm ci` against trees that had only a `package.json`, and `npm ci` requires a lockfile,
    so the install failed on the first line; it installed Python projects with
    `pip install -e .` into the host interpreter, which a Debian or Homebrew Python refuses
    outright (`externally-managed-environment`). Neither error matched the ENVIRONMENT
    signatures, so both landed UNCLASSIFIED and silently zeroed forty points while reading as
    the repository's fault. The default path hands the job to an agent that reads whatever
    declares how the project is built and adapts; a corrected table survives as the
    deterministic path, and `build_probe_mode` says which one ran.

  * **Five phases, kept apart.** Locked dependency resolution, build, runner-native test
    DISCOVERY, test EXECUTION and coverage are separate records with their own status. Fusing
    them is how "there are no tests" and "the tests could not run" became the same number. A
    locked install that fails IS a failure; the relaxed retry that follows is diagnostic and
    never upgrades the verdict. Discovery asks the runner (`pytest --collect-only`,
    `go test -list`, `cargo test -- --list`, `jest --listTests`), never a filename regex.

  * **One cumulative budget, and unfinished work is NULL rather than a low number.** `timeout`
    used to be the only limit and it was applied per phase per project, so a tree with 24 project
    roots had a worst case around 30 hours -- which is how a 417-repo backtest becomes unrunnable
    and how a customer waits all afternoon for one answer. There is now ONE monotonic allowance
    (`budget_seconds`) that every project and every phase draws from. Projects are probed largest
    first, each gets a share of what is left in proportion to its size, and a project that
    finishes or fails early releases the rest of its share to the ones behind it. When the
    allowance runs out the remaining work is recorded `skipped_budget` with its measurements NULL
    and a reason -- never zero, never a low band, because a repository we ran out of clock on is
    not a repository that failed.

  * **Three levels, and a fallback between two of them.** `discover` resolves, installs, builds,
    and asks the runner to enumerate its own tests. That converts the test framework, the package
    manager and `tests_discovered` from inferred to EXECUTED for a fraction of the cost of running
    the suite (13s against 60s on `psf/requests`). `full` additionally executes the suite and reads
    coverage, which is what `observed_runnability` needs; under `discover` that index is NULL with
    a reason, because its test-execution terms were honestly not attempted and a partial sum of
    them would read as a repository that does not work. `full` is what the measure skills ask for.
    A `full` run that the run's own clock cuts short reports `run_budget_exhausted`, and the CALLER
    -- see measure-ext's `build_lane` -- then completes the measurement at `discover` out of what
    is left of the same reserve. A repository whose build simply FAILS needs no such fallback: a
    completed `full` record already contains every discover-level term, so re-running it would buy
    nothing but time.

  * **It runs the runtime the repository asked for, and says which one it got.** `wrong_runtime`
    was 27 of the 417-repo corpus: repositories that failed because we ran the wrong interpreter.
    `_plan_python` built its virtualenv from `sys.executable` -- the interpreter running the
    collector -- whatever `requires-python` said, and every other lane took whatever PATH resolved
    to first, so a project pinned to Python 3.8 was installed under a 3.14 host and then charged
    for the result. `runtime.py` now reads what the tree declares (`.nvmrc`, `.node-version`,
    package.json engines, `.python-version`, requires-python, python_requires, go.mod's go
    directive, rust-toolchain, `.ruby-version`, `.tool-versions`, pom.xml compiler levels, a Gradle
    toolchain, global.json), selects the installation nearest the declared floor, and records the
    ask beside the answer in `runtime_requested` / `runtime_used`. It only MOVES when the host
    default is disallowed: switching runtimes is itself a way to break a working build, and the
    boilerplate `engines: >=4.5` a 2015 generator wrote does not want Node 4 in 2026. When the ask
    cannot be met at all, the lane is named in `runtime_lanes_unsatisfied` and every failure under
    it is attributed to the `runner` -- BEFORE the log signatures get a vote, because we knew the
    runtime was wrong before we ran the command and what the command said afterwards cannot be used
    to blame the codebase for it. Nothing is installed; selecting among what is present is a read.

  * **It separates the repository's failures from the runner's.** A missing language runtime, a
    host version conflict, a missing system header, an absent network, pip's
    externally-managed-environment, a root restriction or a timeout is the operator's problem
    and must not be scored against the codebase. Where the evidence is ambiguous both paths err
    toward blaming the runner: a wrong REPO_INTRINSIC costs a repository ten points for
    something it did not do, while a wrong ENVIRONMENT costs only a little discrimination. The
    one signature that genuinely belongs to either side is a registry rejecting credentials, and
    it is decided by whether the TREE declares the private index or only the operator's home
    directory does.

  * **Project commands run inside the target checkout.** A virtualenv, GOPATH, bundler path,
    composer vendor directory and package caches use scratch storage wherever the ecosystem permits.
    Some tools require in-tree state such as `node_modules`, and project scripts may write arbitrary
    files. The probe snapshots known mutable manifests, modes and common build artifacts and restores
    them best-effort, but it cannot promise a byte-identical checkout after arbitrary project code
    runs. Use `--build` only on a clean, disposable clone or worktree and verify it remains clean.

The aggregate is deliberately tri-state. `build_ok` is true only when every required project
built, false only on an explicit repository-intrinsic failure, and the `build_projects.aggregate`
block reports NULL where coverage of the tree was incomplete or the runner was the limit -- a
repository we could not measure must not be recorded as a repository that failed.

`observed_runnability` is the pre-specified 0..4 index from the 417-repo study,
`build_ok + tests_ran + (n_passed > 0) + (coverage_pct > 0)`, emitted raw. It is a top-band
threshold and not a ramp: mean delivered tasks by band ran 2.20 / 2.19 / 2.23 / 2.21 / 4.26, so
smoothing it destroys the only signal it carries. Only a `full` run can reach band 4.

`discover_runnability` is its DISCOVER-LEVEL companion, `install_ok + build_ok + tests_discovered`
in 0..3, and it exists because the index above is unreachable without a suite. Every one of its
three terms is executed at `--build discover`, and each was measured on the 278 TRAIN repositories:
install_ok rho +0.141 (2.80 against 2.15 mean delivered tasks), build_ok +0.131 (2.77 against
2.18), tests_discovered +0.156 (2.69 against 1.88). Individually modest, and together they are what
lets a run that could not finish a suite still be GRADED rather than filed as unmeasured.

WHO OWNS AN ABSENT TERM is the load-bearing question for both indices, and the answer is read off
the `attribution` every phase already carries. A suite that did not run because the build genuinely
fails, or because the runner looked and the project declares no tests, is THEIR measured property:
the index is a real 0 or 1 and it is supposed to cost points. A suite that did not run because a
language runtime is absent from this host, a registry was unreachable, the tree's build verdict
could not be established, or our own clock expired is OURS: the index is NULL with a reason in
`observed_runnability_reason` and the attribution in `observed_runnability_blocked_by`. Summing
absences as zeros is exactly how a repository that installed, built, discovered 441 tests and
passed all 441 came to be graded 13.7 out of 100.

When a build fails the module also reports how much work a fix would take, because "needs a
version pin" and "needs an unobtainable internal package" are very different findings.

Nothing returned here contains a dependency name or a line of output. The commands that were run
and the project roots are reported as evidence, and because in agentic mode the commands are
model-derived -- and in either mode they can embed a repository path -- every one of them goes
through `redact.redact()` before it is emitted, as does the remediation prose.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import childenv
import runtime

from redact import redact

# DEFAULT_MODEL comes from the single model registry (models.py), the same source every lane
# uses, so it cannot drift. `_extract_json` parses what `claude -p --output-format json` returns
# and is only reached on the agentic path; the measure-ext skill never runs that path (it calls
# collect(agentic=False)), so the extractor is inlined here rather than dragging in the census
# module, which would couple this deterministic probe to the LLM census it must stay free of.
from models import DEFAULT_MODEL


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

_IS_WIN = sys.platform.startswith("win")

FAILURE_CLASSES = ("NONE", "REPO_INTRINSIC", "ENVIRONMENT", "TIMEOUT", "UNCLASSIFIED")
EFFORTS = ("none", "trivial", "moderate", "substantial", "infeasible", "unknown")
MAX_COMMANDS = 20          # reported commands are evidence, not a transcript

# What the probe is allowed to do. `discover` is the default because it converts four inferred
# measurements into executed ones for a quarter of the cost of `full`; see the module docstring.
BUILD_LEVELS = ("none", "discover", "full")
DEFAULT_LEVEL = "discover"

# The two ceilings, and the difference between them is the whole point. `DEFAULT_PHASE_TIMEOUT`
# bounds ONE command; `DEFAULT_BUDGET_SECONDS` bounds the PROBE. Only the second one is a
# statement about how long a repository can take.
DEFAULT_PHASE_TIMEOUT = 900
DEFAULT_BUDGET_SECONDS = 1800

# Below this there is no point starting a command: it would be killed mid-resolve and the only
# thing produced would be a timeout record that reads like a repository defect.
MIN_PHASE_SECONDS = 15
MIN_PROJECT_SECONDS = 30

# The five phases, in the order they run. Kept apart because collapsing them is what made
# "there are no tests" indistinguishable from "the tests could not run".
# Whether `collect()` returns the per-project evidence alongside the frozen flags. This is the
# ONE default that differs between the external and internal copies of this module, for the same
# reason `redact.py` differs: measure-ext emits a frozen, data-minimised record whose every field
# is declared in `emit_allowlist.SPEC`, and per-project roots are repository detail that record
# does not carry. measure-int is full-detail by contract and its rubric reads the evidence
# directly. The FROZEN flags -- including the tri-state `build_ok` and `install_ok` -- are emitted
# either way, so nothing a downstream loader reads depends on this switch.
_DETAIL_DEFAULT = False
# `observed_runnability` is NOT detail. It is the executed verdict -- build_ok + tests_ran +
# (n_passed > 0) + (coverage_pct > 0) -- and ext-diligence-v5 weights it 14 of 100, with the
# discover-level companion below carrying 8 more of the same 22. Stripping it
# meant ripgrep, which installed, built, discovered 441 tests and passed all 441, graded F with
# 6% of the rubric's positive weight assessed. It is a small integer in 0..4 with no repository
# detail in it, so data minimisation was never the reason it was here.
_DETAIL_KEYS = ("build_projects", "build_error_class")

# Which runtime lane an ecosystem's commands actually run on, so resolution only pays for the
# lanes a project needs. PHP is absent on purpose: nothing in the corpus declares a PHP version in
# a file this module reads, and inventing a source to read is how a taxonomy stops matching reality.
_ECOSYSTEM_LANE = {"node": "node", "python": "python", "rust": "rust", "go": "go",
                   "maven": "java", "gradle": "java", "dotnet": "dotnet", "ruby": "ruby"}

PHASES = ("resolve", "build", "discover", "test", "coverage")
# `no_tests` is a MEASUREMENT, not a failure: discovery ran and the project has no suite.
# `unavailable` means the runner could not offer the tool; `blocked` means an earlier phase
# in the same project made this one unreachable. The two `skipped_*` statuses mean WE decided not
# to run it -- because the run's time budget was spent, or because the level did not ask for it --
# and they are kept apart from every other status precisely so that nothing we chose to skip can
# be read as something the repository failed.
STATUSES = ("passed", "failed", "no_tests", "unavailable", "timed_out", "blocked",
            "skipped_budget", "skipped_level")
_SKIPPED_STATUSES = ("skipped_budget", "skipped_level")
ATTRIBUTIONS = ("repository", "runner", "external_service", "credentials", "unknown")
CONFIDENCES = ("high", "medium", "low")

# The instruction below is fixed prose, for the same reason the census's is: its wording decides
# what comes back. The hard rules are the ones a wrong answer would cost points for -- work
# outside the tree, never blame the repository for the host, and return JSON only.
PROMPT = """\
You are the build probe of a code due-diligence tool. Your job is to find out whether ONE
repository's dependencies install and whether its tests run, on THIS machine, right now, by
actually doing it. You are not reviewing the code and not proposing changes.

The repository is at:
  REPO: {repo}
A scratch directory you own, outside the repository, for virtualenvs, caches and any file you
need to create:
  SCRATCH: {scratch}

WORK IN THIS ORDER.

1. FIND OUT HOW THIS PROJECT IS BUILT. Read what declares it, not just what the filenames
   suggest: package manifests and lockfiles, README, CONTRIBUTING, Makefile or Justfile, the CI
   workflow files, a Dockerfile, scripts/ or bin/ helpers, and any contributing docs. The CI
   workflow is usually the most reliable statement of the real install and test commands,
   because it is the one that had to work. Any language is in scope; do not assume this is a
   JavaScript or Python project because you saw one familiar file. A tree can hold SEVERAL
   projects -- a workspace, a monorepo, a service beside a library, two languages side by side.
   Probe every one of them unless a single workspace command genuinely covers them all.

2. INSTALL THE DEPENDENCIES. Then RUN THE TEST SUITE. Adapt when an attempt fails for an obvious
   mechanical reason instead of recording the failure:
     - a frozen or clean install (`npm ci`, `--frozen-lockfile`, `--locked`) fails because there
       is no lockfile or the lockfile is stale -> retry the ordinary non-frozen install, and say
       that the LOCKED install was the one that failed;
     - `pip install -e .` or `pip install .` fails because there is no installable package ->
       install the requirements file that actually exists;
     - a checked-in wrapper script (gradlew, mvnw, a shell helper) is not executable -> make it
       executable and retry;
     - a test runner is not present in the environment you created -> install it there;
     - a test command needs a service you cannot start -> say so, do not keep retrying.
   Two or three adaptations per phase is the right amount of persistence. Ten is not.

3. HARD RULES, in order of importance.
     - Create and use a virtual environment or a local dependency directory INSIDE SCRATCH
       wherever the ecosystem allows it. Never install anything globally or system-wide, never
       use sudo, never pass --break-system-packages, never write outside SCRATCH and the
       repository.
     - Do not modify any tracked file in the repository. Setting the executable bit on a wrapper
       script is the one permitted exception. Never commit, never stash, never reset, never
       check out another ref. Prefer install flags that leave the lockfile alone; a lockfile you
       caused to be written or rewritten is a modification like any other.
     - Keep any single command well under {per_cmd}s and finish everything within {budget}s.
       If you are running out of budget, stop and report what you know.

4. WHOSE FAILURE WAS IT. This is the judgement that matters most.
     REPO_INTRINSIC -- the repository's own fault: a declared dependency that cannot be resolved
       or no longer exists at the pinned version, a broken or inconsistent lockfile, code that
       does not compile as checked in, an uninitialised submodule, a dependency that lives in a
       private registry the project assumes you have.
     ENVIRONMENT -- the runner's fault: the language runtime or build tool is not installed, the
       installed version conflicts with what the project requires, a system library or C header
       or compiler is missing, there is no network or the registry is unreachable, the host OS or
       architecture is not one the project supports, pip refuses with
       externally-managed-environment, a tool refuses to run as root or lacks permission.
   WHERE IT IS AMBIGUOUS, CHOOSE ENVIRONMENT. Naming a repository's failure wrongly costs it ten
   points for something it did not do; naming the runner's wrongly costs almost nothing. A
   package that exists but fails to compile from source on this host is ENVIRONMENT.
   One ambiguity worth resolving properly, because it is common: a registry that rejects your
   credentials (401, 403, E401, "unable to authenticate"). That is REPO_INTRINSIC only when the
   REPOSITORY ITSELF asks for the private index -- a checked-in .npmrc, .yarnrc, pip.conf,
   poetry source, settings.xml or nuget.config naming a non-public host. Look for one. If the
   only such configuration lives in the operator's home directory rather than in the tree, the
   rejected credentials were the operator's and this is ENVIRONMENT.

5. COVERAGE. If the ecosystem has an obvious way to run the suite with line-coverage
   instrumentation, try it once and report the total percentage. Write coverage output into
   SCRATCH, not into the repository. If there is no obvious way, or it produced no total, say so
   in one clause and move on -- a missing coverage tool is not a defect and is not scored.

REPORTING. `commands_tried` is the literal list of the install, build and test commands you
actually ran, in order, shortest useful form, at most {max_cmds}. `remediation_notes` is two or
three sentences on what it would take to make this build work, written for someone who will
never see the source: describe the situation, do NOT name a file, a path, an identifier or a
dependency. `toolchain` is a short lowercase slug for the ecosystem you used, such as
node-pnpm, python-uv, go-modules, gradle-wrapper, cargo, dotnet, mix, swiftpm.
`tests_discovered` is false when the project HAS no suite; do not confuse that with a suite you
could not run.

Return ONLY a JSON object, no prose around it:
{{"build_definition_found": <bool: did anything in the tree declare how to build it>,
  "toolchain": "<short slug, or null>",
  "commands_tried": ["...", "..."],
  "install_ok": <bool: dependencies resolved and installed>,
  "build_ok": <bool: install succeeded AND anything that needed compiling compiled>,
  "tests_discovered": <bool: a real test suite was found, whether or not it passes>,
  "tests_ran": <bool: the suite executed to completion; PASSING IS NOT REQUIRED>,
  "n_passed": <int: tests that passed, or null if you could not count them>,
  "failure_class": "NONE" | "REPO_INTRINSIC" | "ENVIRONMENT" | "TIMEOUT" | "UNCLASSIFIED",
  "coverage_pct": <number 0-100, or null>,
  "coverage_method": "<how the total was produced, or null>",
  "coverage_unsupported_reason": "<one clause, or null when coverage_pct is set>",
  "remediation_effort": "none" | "trivial" | "moderate" | "substantial" | "infeasible" | "unknown",
  "remediation_notes": "..."}}
"""

# --- deterministic classification -----------------------------------------------------------
#
# Checked ENVIRONMENT first, deliberately. Several signatures below could in principle be either
# side's fault; sending those to the runner is the whole policy of this module.
_ENV = re.compile(
    # tool or runtime simply absent
    r"command not found|executable file not found|is not recognized as an internal or external"
    r"|no such file or directory:\s*(npm|node|python3?|go|mvn|cargo|bundle|composer|pnpm|yarn|dotnet)"
    r"|No module named"
    # host runtime version does not match what the project asks for
    r"|unsupported engine|EBADENGINE|engine \"node\""
    r"|requires (node|python|ruby|\.NET|dotnet)|wrong ruby version"
    r"|requires a different Python|requires Python\s*[<>=]"
    r"|could not find (java|javac|jdk)|JAVA_HOME is not set"
    r"|invalid source release|class file version|Unsupported class file"
    # the interpreter this host ships refuses to be written to, or we are the wrong user.
    # `externally.managed` unanchored on purpose: pip emits the phrase three ways in the same
    # failure (`error: externally-managed-environment`, then prose reading "This environment is
    # externally managed"), and a wrapper may forward only one of them.
    r"|externally.managed"
    r"|break-system-packages|ensurepip is not available|python3?-venv"
    r"|permission denied.*gem|EACCES|EPERM|EROFS|operation not permitted"
    r"|(should not|must not|cannot|refus\w+ to) be run as root|running pip as the .root. user"
    # a system library, header or compiler the host is missing
    r"|fatal error: .*\.h: No such file|cannot find -l|ld: library not found"
    r"|library not found for|linker command failed|Microsoft Visual C\+\+ \d|cl\.exe"
    r"|command '(gcc|cc|clang|g\+\+|cmake)' failed|unable to execute '(gcc|cc|clang)'"
    r"|Failed building wheel for|metadata-generation-failed|subprocess-exited-with-error"
    r"|pkg-config.*not found|No package '[^']+' found"
    # no usable network, or the registry cannot be reached from here
    r"|Temporary failure in name resolution|Could not resolve host|getaddrinfo"
    r"|ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EAI_AGAIN|network is unreachable"
    r"|connection refused|Read timed out|Connection timed out|proxy|CERTIFICATE_VERIFY_FAILED"
    r"|SSLError|TLS handshake|offline mode|no internet"
    # the host is simply not a platform this project targets
    r"|EBADPLATFORM|unsupported platform|not supported on this platform|Unsupported architecture"
    r"|incompatible architecture|wrong architecture|only supported on|requires (macOS|Windows|Linux)"
    # wrapper machinery the checkout does not carry
    r"|gradle-wrapper\.jar|gradle wrapper.*not found"
    r"|corepack.*(prompt|enable)|Cannot find matching keyid", re.I)

# Failures that belong to the repository. Credential failures are deliberately NOT here; they
# are decided separately, below, because they belong to whichever side asked for the registry.
_REPO = re.compile(
    r"E404|404 Not Found|ERESOLVE|unable to resolve dependency tree|peer dep"
    r"|version solving failed|no matching distribution|could not find a version"
    r"|could not resolve dependencies|artifact.*(not found|resolution)"
    # A lockfile that no longer matches its manifest is the repository's own inconsistency. The
    # frozen install is tried first precisely so this shows up instead of being papered over.
    r"|lock(file)? (is )?(out of date|outdated|mismatch)|integrity check failed"
    r"|(package-lock\.json|lock ?file).{0,40}(out of sync|in sync)|Missing: .+ from lock file"
    r"|compilation (error|failed)|cannot find symbol|syntax error|parse error"
    r"|submodule.*(not initialized|failed|missing)", re.I)

# A registry refused our credentials. This is the one signature that genuinely belongs to either
# side, and the difference is worth ten points: a project that declares a dependency in a private
# registry cannot be built by an outsider (REPO_INTRINSIC, and infeasible), while an operator
# whose own package manager is logged into a stale internal registry has a host problem that has
# nothing to do with the repository. Observed for real: a `~/.npmrc` with an expired corporate
# token turns every `npm ci` on the machine into E401.
_AUTH = re.compile(
    r"E401|E403|401 Unauthorized|403 Forbidden|authentication required"
    r"|Unable to authenticate|authentication token|npm login|not authori[sz]ed"
    r"|Not authorized to|Access denied|401 \(Unauthorized\)|403 \(Forbidden\)", re.I)

# The build_error_class vocabulary, validated over 417 repositories: no_manifest 60,
# dependency_resolution 54, unclassified 32, build_backend_failed 29, wrong_runtime 27,
# private_registry 22, compile_error 18, build_failed 13, toolchain_missing 7, and a long tail.
# Ordered, first match wins, ENVIRONMENT-flavoured specifics ahead of generic repository ones.
# Entries are added here only with new evidence; nothing is renamed, because the histogram above
# is what every downstream comparison is calibrated against.
#
# `unclassified` at 32 was the entry that mattered, because a failure with no class is a failure we
# cannot attribute, and attribution is what decides whether a repository loses points for something
# it did not do. The entries marked "added from the executed corpus" below were each written against
# error text taken from a real run over these repositories, one signature per observed message. No
# class was invented for a message nobody has seen: a taxonomy that outruns its evidence is how
# `unclassified` gets replaced by a label that is confidently wrong.
_ERROR_SIGNATURES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"No space left on device|ENOSPC", re.I), "out_of_disk"),
    (re.compile(r"Out of memory|OutOfMemoryError|JavaScript heap out of memory"), "out_of_memory"),
    (re.compile(r"ERR_OSSL_EVP_UNSUPPORTED|error:0308010C"), "wrong_runtime"),
    (re.compile(r"Unsupported class file major version|invalid (source|target) release"
                r"|class file has wrong version|Source option \d+ is no longer supported"), "wrong_runtime"),
    (re.compile(r"Requires-Python|requires a different Python|python_requires"
                r"|This package requires Python|is not supported on Python"), "wrong_runtime"),
    (re.compile(r"engine \"?node\"? is incompatible|Unsupported engine|EBADENGINE"), "wrong_runtime"),
    (re.compile(r"go\.mod requires go >= |requires go1\.\d+ or later"), "wrong_runtime"),
    (re.compile(r"Your Ruby version is [\d.]+, but your Gemfile specified"
                r"|Your PHP version \([\d.]+\) does not satisfy"), "wrong_runtime"),
    (re.compile(r"MSB3644|NETSDK1045|The reference assemblies for .* were not found"), "wrong_runtime"),
    (re.compile(r"externally.managed|break-system-packages|ensurepip is not available"), "wrong_runtime"),
    # --- added from the executed corpus, and only from text that was actually observed ---------
    #
    # The host's package manager is NEWER than a pinned dependency's metadata allows. Observed on a
    # 2019-era requirements.txt under a current pip: the wheel's own metadata is rejected, and the
    # resolution failure that follows reads exactly like a repository pinning something that no
    # longer exists. It landed `dependency_resolution` / REPO_INTRINSIC, which blamed the codebase
    # for the age of our pip.
    (re.compile(r"has invalid metadata: Expected matching|Please use pip<[\d.]+ if you need"
                r"|since it has invalid metadata"), "wrong_runtime"),
    # A stdlib member the interpreter we chose has removed, surfacing from a pinned tool rather
    # than from the tree: `ast.Str` and `configparser.SafeConfigParser` both went in 3.12 and both
    # were observed, on two different repositories, under a 3.14 host.
    (re.compile(r"module '(?:ast|configparser|collections|imp|inspect|asyncio|cgi|locale|"
                r"distutils)' has no attribute"), "wrong_runtime"),
    # setuptools or Cython newer than a pinned source distribution can build against. Observed
    # building PyYAML 5.x, where it landed `build_backend_failed` and read as the repository's.
    (re.compile(r"'build_ext' object has no attribute 'cython_sources'"
                r"|use_2to3 is invalid|No module named 'distutils'"), "wrong_runtime"),
    # This host's bundler is not the one the lockfile was built with. A statement about our Ruby
    # installation, not about the repository; it was landing UNCLASSIFIED.
    (re.compile(r"Could not find 'bundler' \([\d.]+\) required by your"
                # `Gem::GemNotFoundException` on its own is NOT a runtime mismatch -- it is what an
      # ordinary missing gem raises under `bundle exec`, so matching it bare classified plain
      # dependency failures as `wrong_runtime` before the dependency rules could see them.
      r"|Gem::GemNotFoundException.{0,80}bundler|bundle update --bundler"), "wrong_runtime"),
    (re.compile(r"world-writable|Don't run Bundler as root"), "container_misconfig"),
    (re.compile(r"Corepack is about to download|COREPACK_ENABLE_DOWNLOAD_PROMPT|YN0050"), "container_misconfig"),
    (re.compile(r"fatal error: .*\.h: No such file|cannot find -l[a-zA-Z0-9_]+"
                r"|Could NOT find [A-Za-z0-9_]+ \(missing|pg_config executable is not found"
                r"|mysql_config: not found|libpq-fe\.h"), "missing_system_lib"),
    (re.compile(r"Exit handler never called|npm error code ENOTEMPTY"
                r"|Maximum call stack size exceeded"), "package_manager_bug"),
    (re.compile(r"(Cannot find|No usable|Failed to launch|Could not find) (Chrome|Chromium|browser)"
                r"|CHROME_BIN|no DISPLAY|cannot open display"), "browser_missing"),
    (re.compile(r"Is the docker daemon running|Cannot connect to the Docker daemon"
                r"|docker: not found|docker-compose: not found"), "docker_missing"),
    (re.compile(r"(Connection refused|ECONNREFUSED|could not connect to server)"
                r".{0,80}(5432|3306|6379|27017|9200|localhost|127\.0\.0\.1)", re.S), "external_service_missing"),
    (re.compile(r"Temporary failure in name resolution|Could not resolve host|getaddrinfo"
                r"|EAI_AGAIN|Network is unreachable|Could not transfer artifact"), "no_network"),
    # AHEAD of `toolchain_missing` on purpose. Every node-gyp transcript contains a bare
    # `: not found` from one of the compiler probes it runs, so the generic signature matched first
    # and a native rebuild that failed under a too-new Node was filed as a missing toolchain.
    # Observed on node-sass under Node 26: `gyp ERR! not ok`, with `: not found` earlier in the log.
    (re.compile(r"gyp ERR!|node-gyp|prebuild-install"), "native_build_failed"),
    (re.compile(r"command not found|executable file not found|: not found\b"
                r"|could not determine executable to run|npm ERR! could not determine"
                # `npx --no-install` refusing to fetch a runner the project never installed is
                # our environment lacking the tool, not the project lacking a test suite.
                r"|npx canceled due to missing packages"),
     "toolchain_missing"),
    # A runner that STARTED and then could not import something the install did not provide. Three
    # facts that used to be one: `no_tests` is the runner looking and finding no suite, a failing
    # suite is a suite that ran, and this is neither -- the suite exists and we could not load it.
    # Observed as a pytest collection ImportError and as a vitest config MODULE_NOT_FOUND.
    (re.compile(r"ImportError while importing test module"
                r"|Interrupted: \d+ errors? during collection"
                r"|code: 'MODULE_NOT_FOUND'|Cannot find module '"), "test_dependency_missing"),
    # The build backend a pyproject declares is not importable here, which is what
    # `--no-build-isolation` means on a host that does not already carry it. Ours, not theirs.
    # Observed as poetry-core absent during the relaxed install retry.
    (re.compile(r"BackendUnavailable|Cannot import '[A-Za-z0-9_.]+\.(?:api|build_meta)'"),
     "build_backend_missing"),
    (re.compile(r"E401|E403|401 Unauthorized|403 Forbidden|ENEEDAUTH|authentication required"
                r"|Incorrect or missing password|Permission denied \(publickey\)"
                r"|could not read Username|Authentication failed for"), "private_registry"),
    (re.compile(r"go: unrecognized import path|is not in GOROOT"), "private_registry"),
    (re.compile(r"MSB4025|The project file could not be loaded"
                r"|Could not find file .*\.(csproj|sln|fsproj)"), "broken_project_file"),
    (re.compile(r"probe: no installable manifest"), "no_installable_manifest"),
    (re.compile(r"GradleWrapperMain|gradle-wrapper\.jar.*(No such file|not found)"), "missing_wrapper_jar"),
    (re.compile(r"No url found for submodule path|failed to clone .*submodule"
                r"|Submodule .* could not"), "missing_submodule"),
    (re.compile(r"npm ci can only install packages when your package\.json and package-lock\.json"
                r"|Missing: .* from lock file|Invalid: lock file|EUSAGE"), "broken_lockfile"),
    (re.compile(r"(lockfile|Gemfile\.lock|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock)"
                r".{0,80}(out of date|needs to be updated|is not up to date|does not match|frozen)",
                re.I | re.S), "broken_lockfile"),
    (re.compile(r"Your lock file does not satisfy|The lockfile is not up to date"
                r"|the lock file .* needs to be updated"), "broken_lockfile"),
    (re.compile(r"No matching distribution found|Could not find a version that satisfies"
                r"|ResolutionImpossible|version solving failed|SolverProblemError"), "dependency_resolution"),
    (re.compile(r"ERESOLVE|Conflicting peer dependency|Couldn't find any versions for"
                r"|error Couldn't find package"), "dependency_resolution"),
    (re.compile(r"404 Not Found.{0,120}(registry\.npmjs\.org|registry\.yarnpkg\.com)",
                re.S), "dependency_resolution"),
    (re.compile(r"Could not resolve dependencies for project|Could not find artifact"
                r"|Failed to collect dependencies|Non-resolvable (parent POM|import POM)"
                r"|Could not resolve all (files|dependencies|artifacts) for configuration"),
     "dependency_resolution"),
    (re.compile(r"Bundler could not find compatible versions|Could not find gem "
                r"|Your requirements could not be resolved|Root composer\.json requires"
                r"|failed to select a version|no matching package named"), "dependency_resolution"),
    (re.compile(r"go: .*: (unknown revision|invalid version|no required module provides)"
                r"|NU1101|NU1102|NU1103|Unable to find package"), "dependency_resolution"),
    (re.compile(r"Could not open requirements file|is not a valid editable requirement"),
     "broken_manifest"),
    (re.compile(r"COMPILATION ERROR|cannot find symbol|package .* does not exist"
                r"|incompatible types:"), "compile_error"),
    (re.compile(r"\bSyntaxError\b|\bIndentationError\b|\bTabError\b"), "compile_error"),
    (re.compile(r"\.go:\d+:\d+: (undefined|cannot use|syntax error|declared and not used)"),
     "compile_error"),
    (re.compile(r"error\[E\d+\]:|could not compile `"), "compile_error"),
    (re.compile(r"error TS\d+:|error CS\d+:|error BC\d+:|error FS\d+:"), "compile_error"),
    (re.compile(r"PHP Parse error|PHP Fatal error|unresolved reference"), "compile_error"),
    (re.compile(r"error in .* setup command|Failed building wheel for"
                r"|metadata-generation-failed|error: subprocess-exited-with-error"),
     "build_backend_failed"),
    (re.compile(r"npm ERR! code ELIFECYCLE|Command failed with exit code"
                r"|handling the (post-autoload-dump|post-install-cmd|pre-install-cmd) event"),
     "script_failed"),
    (re.compile(r"FAILURE: Build failed with an exception|BUILD FAILURE|BUILD FAILED"),
     "build_failed"),
]

# Which side of the fence an error class sits on, for `attribution`. Everything not named here
# takes its attribution from the validated failure_class, so the two can never disagree.
_CREDENTIAL_CLASSES = {"private_registry"}
_EXTERNAL_CLASSES = {"external_service_missing", "docker_missing", "browser_missing", "no_network"}

# Registry configuration CHECKED INTO the tree, i.e. the repository asking for a private index
# itself. Presence of one of these plus a credential failure is what makes the failure the
# repository's; a credential failure without one is the host's.
_REGISTRY_CONFIG = {
    ".npmrc": re.compile(r"registry\s*=|_authToken|_auth\s*=", re.I),
    ".yarnrc": re.compile(r"registry\s+|registry\s*=", re.I),
    ".yarnrc.yml": re.compile(r"npmRegistryServer|npmScopes", re.I),
    "pip.conf": re.compile(r"index-url|extra-index-url", re.I),
    "pip/pip.conf": re.compile(r"index-url|extra-index-url", re.I),
    "poetry.toml": re.compile(r"\[\[?tool\.poetry\.source|url\s*=", re.I),
    "settings.xml": re.compile(r"<repository|<server", re.I),
    "nuget.config": re.compile(r"packageSources|<add\s", re.I),
    "gradle.properties": re.compile(r"(repo|registry|artifactory|nexus).*(url|user|password)", re.I),
    ".netrc": re.compile(r"machine\s+\S+", re.I),
}
# Public indexes. A checked-in config that only names one of these is not a private registry.
_PUBLIC_INDEX = re.compile(
    r"registry\.npmjs\.org|registry\.yarnpkg\.com|pypi\.org|files\.pythonhosted\.org"
    r"|repo\.maven\.apache\.org|repo1\.maven\.org|jcenter\.bintray|api\.nuget\.org"
    r"|rubygems\.org|proxy\.golang\.org|crates\.io", re.I)


def _declares_private_registry(repo: Path) -> bool:
    """Does the TREE itself point at a non-public package index?

    Read from the checkout only. The host's own configuration is irrelevant here -- that is
    precisely the thing this function exists to rule out.
    """
    for name, pattern in _REGISTRY_CONFIG.items():
        p = repo / name
        if not p.is_file():
            continue
        try:
            body = p.read_text(errors="replace")[:20000]
        except OSError:
            continue
        if pattern.search(body):
            # A config naming only public indexes is configuration, not a private dependency.
            hosts = re.findall(r"https?://([^/\s\"']+)", body)
            if not hosts or any(not _PUBLIC_INDEX.search(h) for h in hosts):
                return True
    return False

# Remediation signatures, checked in order. First match wins.
_REMEDIATION = [
    # Only reached once the classifier has already decided the credential failure was the
    # repository's, so the strong wording is earned rather than assumed.
    ("infeasible", re.compile(
        r"submodule.*(not initialized|failed|missing)"
        r"|E401|E403|401 Unauthorized|403 Forbidden|authentication required"
        r"|Unable to authenticate|authentication token|npm login"
        r"|private (registry|repository)|\.npmrc.*token|credentials", re.I),
     "the build needs a private or internal artefact that cannot be obtained from outside the "
     "originating organisation; nothing in the repository alone will fix it"),
    ("substantial", re.compile(
        r"E404|404 Not Found|no matching distribution|could not find a version"
        r"|artifact.*not found|end.?of.?life|no longer supported|deprecated runtime", re.I),
     "one or more declared dependencies are no longer obtainable at the pinned version, so "
     "resolving this means finding replacements and adapting the code that uses them"),
    ("moderate", re.compile(
        r"ERESOLVE|unable to resolve dependency tree|peer dep|version solving failed"
        r"|lock(file)? (is )?(out of date|outdated|mismatch)|integrity check failed"
        r"|(package-lock\.json|lock ?file).{0,40}(out of sync|in sync)|Missing: .+ from lock file"
        r"|could not resolve dependencies", re.I),
     "the dependency graph does not resolve as pinned; regenerating the lockfile or relaxing a "
     "small number of constraints is the likely fix"),
    ("moderate", re.compile(r"compilation (error|failed)|cannot find symbol|parse error", re.I),
     "the project does not compile as checked in, which points at either missing generated code "
     "or a toolchain version different from the one it was written against"),
    ("trivial", re.compile(
        r"unsupported engine|EBADENGINE|engine \"node\"|requires (node|python|ruby)"
        r"|wrong ruby version|JAVA_HOME|missing environment variable|\.env", re.I),
     "the declared runtime or an environment variable does not match what is present; pinning "
     "the expected version or supplying the variable should be sufficient"),
]

# In-tree artefacts to clear on the way out, and dependency directories an ecosystem refuses to
# put anywhere else. Every one of these is removed ONLY if it was absent before the run: a
# repository that ships a directory or a file by one of these names must get it back untouched.
_ARTEFACTS = (
    # coverage and test-report output
    "coverage.json", ".coverage", ".coverage.tmp", "coverage.xml", "lcov.info", "junit.xml",
    "test-results.xml", "htmlcov", "coverage", ".nyc_output",
    # dependency and build directories. `build` and `.eggs` are here because a setuptools
    # install writes them into the source tree; both are removed only when the repository did not
    # already ship a directory by that name, which is what the pre_existing set is for.
    "node_modules", "vendor", ".venv", "venv", ".tox", "target", "obj", "build", ".eggs",
    # caches
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "__pycache__", ".gradle", ".next", ".turbo",
    ".pnpm-store",
)
# Artefacts whose name depends on the project. Same rule: removed only when ours.
_ARTEFACT_GLOBS = ("*.egg-info",)

# Files an installer REWRITES rather than merely creates, most of them tracked. `npm install`
# writes a package-lock.json where there was none, `go mod download` can rewrite go.sum, and
# bundler updates Gemfile.lock -- all of which leave the working tree dirty, which this probe is
# not allowed to do. Their bytes are captured before the run and put back afterwards, so the
# guarantee holds whether the file was created, modified, or left alone.
_MUTABLE = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
            "Gemfile.lock", "composer.lock", "Cargo.lock", "poetry.lock", "Pipfile.lock",
            "go.sum", "go.mod", "packages.lock.json", "gradle.lockfile", "composer.json",
            "package.json")
_MUTABLE_MAX = 32 * 1024 * 1024     # do not hold a pathological file in memory to protect it


def _artefact_names(root: Path) -> set[str]:
    """Artefact entries present in one project root right now, fixed names plus the globbed ones."""
    names = {name for name in _ARTEFACTS if (root / name).exists()}
    for pattern in _ARTEFACT_GLOBS:
        names.update(p.name for p in root.glob(pattern))
    return names


def _snapshot(roots: list[Path]) -> dict[Path, bytes | None]:
    """Bytes of every installer-writable file before the run. None means "did not exist".

    Taken over EVERY project root, not just the repository root: a workspace member's lockfile is
    as rewritable as the top-level one and is just as much a tracked file.
    """
    snap: dict[Path, bytes | None] = {}
    for root in roots:
        for name in _MUTABLE:
            p = root / name
            if p in snap:
                continue
            try:
                if not p.exists():
                    snap[p] = None
                elif p.is_file() and p.stat().st_size <= _MUTABLE_MAX:
                    snap[p] = p.read_bytes()
            except OSError:
                continue
    return snap


def _restore_snapshot(snap: dict[Path, bytes | None]) -> None:
    """Put the working tree back exactly as it was found."""
    for p, before in snap.items():
        try:
            if before is None:
                if p.is_file():
                    p.unlink(missing_ok=True)      # we generated it; it is not theirs to keep
            elif not p.is_file() or p.read_bytes() != before:
                p.write_bytes(before)
        except OSError:
            pass


# --- process plumbing -----------------------------------------------------------------------

def _which(cmd: str, env: dict | None = None) -> str | None:
    """Resolve a command against the CHILD's PATH, not the parent's.

    `shutil.which(cmd)` searches the probe's own PATH, which on a developer machine includes
    `~/.cargo/bin`, `~/.local/bin` and every tool shim. Handing the absolute path it finds to a
    child would execute a binary out of the operator's home even though `childenv` stripped those
    entries from the child's PATH precisely so that repository-controlled code cannot. Resolving
    against the child's own PATH keeps the two answers to "is this tool available" identical.
    """
    return shutil.which(cmd, path=None if env is None else env.get("PATH"))


def _run(cmd: list[str], cwd: Path, env: dict, timeout: int):
    """(returncode, combined output, timed_out). cmd[0] is resolved through PATH first.

    Resolving matters on Windows, where `npm` is `npm.cmd` and CreateProcess will not find it
    from the bare name the way a POSIX shell would.

    Routed through `childenv.run` rather than `subprocess.run`: build and test commands
    daemonise (a dev server, a watcher, a database a test suite started) and `subprocess.run`'s
    timeout reaps only the direct child, so the daemon outlives the probe holding ports and the
    checkout. `childenv.run` starts a new session and signals the whole process group.
    """
    argv = list(cmd)
    resolved = _which(argv[0], env)
    if resolved:
        argv[0] = resolved
    try:
        p = childenv.run(argv, domain=childenv.BUILD, cwd=cwd, env=env, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or ""), False
    except subprocess.TimeoutExpired as e:
        return 124, (e.output or "") + (e.stderr or ""), True
    except OSError as e:
        # FileNotFoundError, PermissionError and NotADirectoryError all land here, and all three
        # mean the same thing to the classifier: this host could not start the tool.
        return 127, f"command not found: {cmd[0]} ({type(e).__name__})", False


def _display(cmd: list[str]) -> str:
    """What we report as having been run. Absolute host paths are collapsed to their last
    component so the evidence stays readable; redact() then removes whatever is left."""
    parts = []
    for a in cmd:
        parts.append(Path(a).name if (os.sep in a and Path(a).is_absolute()) else a)
    return " ".join(parts)


def _ensure_executable(path: Path, restore: list[tuple[Path, int]]) -> None:
    """Give a checked-in wrapper script its executable bit, remembering the old mode.

    Windows has no such bit, so this is a no-op there. Elsewhere the mode is a tracked property
    of the file, so the original is recorded and put back during cleanup -- the probe is not
    allowed to leave the working tree dirty, not even by one permission bit.
    """
    if _IS_WIN or not path.is_file() or os.access(path, os.X_OK):
        return
    try:
        mode = path.stat().st_mode
        restore.append((path, mode))
        path.chmod(mode | 0o111)
    except OSError:
        pass


def _venv_python(venv: Path) -> Path:
    """Interpreter inside a venv. Layout differs by platform and there is no portable form."""
    return venv / ("Scripts" if _IS_WIN else "bin") / ("python.exe" if _IS_WIN else "python")


def _read_text(path: Path, cap: int = 200_000) -> str:
    try:
        return path.read_text(errors="replace")[:cap]
    except OSError:
        return ""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None


# --- the cumulative budget ---------------------------------------------------------------------

class Budget:
    """One monotonic allowance for the whole probe, drawn on by every project and every phase.

    `phase_cap` is the ceiling for a single command. `remaining()` is the ceiling for everything
    still to come. A phase gets the smaller of the two, and of whatever is left of its project's
    share, so no single hanging install can spend the run.
    """

    def __init__(self, seconds: float, phase_cap: int = DEFAULT_PHASE_TIMEOUT) -> None:
        self.total = max(0.0, float(seconds))
        self.phase_cap = max(1, int(phase_cap))
        self._start = time.monotonic()
        self._end = self._start + self.total

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def exhausted(self) -> bool:
        return self.remaining() < MIN_PHASE_SECONDS

    def slice(self, project_end: float | None = None) -> int:
        """Seconds one command may take. Zero means there is not enough left to be worth starting."""
        left = self.remaining()
        if project_end is not None:
            left = min(left, project_end - time.monotonic())
        seconds = int(min(self.phase_cap, left))
        return seconds if seconds >= MIN_PHASE_SECONDS else 0


# --- project discovery ----------------------------------------------------------------------
#
# `no_manifest` was 60 of 417 in the executed run, the largest single failure class, and an
# unknown part of it was this scan giving up rather than the repository lacking a manifest.
# Everything the scan declines to probe is therefore RECORDED, with a reason, so the two can be
# told apart from the outside.

_IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor", "dist", "build",
    "target", "out", "bin", "obj", ".venv", "venv", "env", "__pycache__", ".tox", ".nox",
    ".gradle", ".idea", ".vscode", ".terraform", "third_party", "Pods", "coverage", ".next",
    ".turbo", ".pnpm-store", ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "site-packages", "Carthage", "DerivedData", ".dart_tool", "elm-stuff",
})

# Bounds. A deterministic scan that is allowed to run forever is not deterministic in any sense
# a caller cares about, and a tree with 400 manifests is a vendored dump rather than 400
# projects. Anything past a bound is recorded as omitted, never silently dropped.
_MAX_DEPTH = 4
_MAX_DIRS = 4000
_MAX_PROJECTS = 24
# How many of the discovered projects are actually PROBED, largest first. Discovery still
# enumerates up to _MAX_PROJECTS and the aggregate reports the ones it did not probe: enumerating
# is a directory walk, probing runs somebody's install script.
MAX_PROBED_PROJECTS = 8

_PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
               "tox.ini", "noxfile.py", "environment.yml")
_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("node", ("package.json",)),
    ("python", _PY_MARKERS),
    ("go", ("go.mod",)),
    ("maven", ("pom.xml",)),
    ("gradle", ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")),
    ("rust", ("Cargo.toml",)),
    ("ruby", ("Gemfile",)),
    ("php", ("composer.json",)),
)
_DOTNET_SUFFIXES = (".sln", ".csproj", ".fsproj", ".vbproj")


@dataclass
class Project:
    """One thing in the tree that declares how it is built.

    `weight` is how many files the scan saw under this root. It is not a measurement and is never
    emitted; it exists so the budget can be spent on the big project rather than on whichever
    one happens to sort first.
    """
    project_id: str
    root: Path
    rel: str
    ecosystem: str
    members: list[str] = field(default_factory=list)
    workspace: bool = False
    required: bool = True
    weight: int = 1


def _scan(repo: Path) -> tuple[dict[Path, set[str]], dict]:
    """Every directory holding a build marker, and what the scan refused to look at.

    Symlinks are never followed and every candidate is re-checked against the resolved
    repository root, so a link pointing outside the tree cannot make the probe run commands in
    somebody else's directory.
    """
    repo_real = repo.resolve()
    found: dict[Path, set[str]] = {}
    omitted: list[dict] = []
    files_per_dir: dict[Path, int] = {}
    seen_dirs = 0
    stack: list[tuple[Path, int]] = [(repo, 0)]
    while stack:
        current, depth = stack.pop()
        seen_dirs += 1
        if seen_dirs > _MAX_DIRS:
            omitted.append({"root": "<scan>", "reason_code": "directory_budget_exhausted",
                            "ecosystems": []})
            break
        try:
            entries = list(os.scandir(current))
        except OSError:
            omitted.append({"root": _rel(repo, current), "reason_code": "unreadable_directory",
                            "ecosystems": []})
            continue
        names: set[str] = set()
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    child = Path(entry.path)
                    if entry.name in _IGNORE_DIRS or entry.name.startswith("."):
                        continue
                    if depth + 1 > _MAX_DEPTH:
                        omitted.append({"root": _rel(repo, child), "reason_code": "depth_limit",
                                        "ecosystems": _peek_ecosystems(child)})
                        continue
                    if not child.resolve().is_relative_to(repo_real):
                        omitted.append({"root": _rel(repo, child),
                                        "reason_code": "symlink_escapes_repository",
                                        "ecosystems": []})
                        continue
                    stack.append((child, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    names.add(entry.name)
            except OSError:
                continue
        files_per_dir[current] = len(names)
        ecosystems = {eco for eco, markers in _MARKERS if names & set(markers)}
        if any(n.endswith(_DOTNET_SUFFIXES) for n in names):
            ecosystems.add("dotnet")
        if ecosystems:
            found[current] = ecosystems
    return found, {"dirs_scanned": min(seen_dirs, _MAX_DIRS), "omitted": omitted,
                   "files_scanned": sum(files_per_dir.values()),
                   "files_per_dir": files_per_dir}


def _peek_ecosystems(path: Path) -> list[str]:
    """Which ecosystems a directory we are NOT going to probe declares.

    One non-recursive scandir. It exists so that a skipped root is reported as "a Rust project
    we did not reach" rather than folded into `no_manifest`, which is what made a real customer
    read "unknown environment" off a repository whose Rust suite was sitting right there.
    """
    try:
        names = {e.name for e in os.scandir(path) if e.is_file(follow_symlinks=False)}
    except OSError:
        return []
    found = sorted(eco for eco, markers in _MARKERS if names & set(markers))
    if any(n.endswith(_DOTNET_SUFFIXES) for n in names):
        found.append("dotnet")
    return found


def _rel(repo: Path, path: Path) -> str:
    try:
        rel = path.relative_to(repo).as_posix()
    except ValueError:
        return path.name
    return rel or "."


def _node_workspace_globs(root: Path) -> list[str]:
    """The workspace globs a Node root declares, across the three ways of declaring them."""
    pkg = _read_json(root / "package.json")
    globs: list[str] = []
    if isinstance(pkg, dict):
        ws = pkg.get("workspaces")
        if isinstance(ws, list):
            globs += [g for g in ws if isinstance(g, str)]
        elif isinstance(ws, dict) and isinstance(ws.get("packages"), list):
            globs += [g for g in ws["packages"] if isinstance(g, str)]
    pnpm = root / "pnpm-workspace.yaml"
    if pnpm.is_file():
        # A three-line `packages:` list, read without a YAML dependency. Anything more exotic
        # falls back to "the whole subtree is covered", which is the conservative answer.
        body = _read_text(pnpm, 20000)
        entries = re.findall(r"^\s*-\s*['\"]?([^'\"\n]+)['\"]?\s*$", body, re.M)
        globs += [e.strip() for e in entries if e.strip()]
        if not entries:
            globs.append("**")
    return globs


def _cargo_workspace(root: Path) -> tuple[list[str], list[str]] | None:
    """(members, exclude) when Cargo.toml declares a workspace, else None. No TOML parser here:
    the file may be the only reason we look at this directory and a parse dependency would make
    discovery fail on a syntax error that cargo itself would tolerate."""
    body = _read_text(root / "Cargo.toml", 200_000)
    if not re.search(r"^\s*\[workspace\]", body, re.M):
        return None

    def _array(key: str) -> list[str]:
        m = re.search(rf"^\s*{key}\s*=\s*\[(.*?)\]", body, re.M | re.S)
        return re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)) if m else []

    return _array("members"), _array("exclude")


def _matches_any(rel: str, globs: list[str]) -> bool:
    for g in globs:
        g = g.strip().rstrip("/")
        if not g:
            continue
        if g in ("**", "*"):
            return True
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g + "/*") or rel == g:
            return True
        if g.endswith("/*") and rel.startswith(g[:-2] + "/"):
            return True
    return False


def _authoritative_roots(repo: Path, found: dict[Path, set[str]]) -> dict[str, dict[Path, list[str]]]:
    """Per ecosystem, the roots whose ONE command covers their descendants, and what it covers.

    This is the only suppression the discovery does, and it is the only one that is honest: a
    pnpm workspace really is installed once at the top. Two ecosystems in one directory are
    never collapsed into each other, which is the defect this rewrite exists to fix.
    """
    covers: dict[str, dict[Path, list[str]]] = {eco: {} for eco, _ in _MARKERS}
    covers["dotnet"] = {}
    for root, ecosystems in found.items():
        if "node" in ecosystems:
            globs = _node_workspace_globs(root)
            if globs:
                covers["node"][root] = globs
        if "rust" in ecosystems:
            ws = _cargo_workspace(root)
            if ws is not None:
                members, exclude = ws
                covers["rust"][root] = members or ["**"]
                covers["rust"][root] = [m for m in covers["rust"][root] if m not in exclude]
        if "go" in ecosystems or (root / "go.work").is_file():
            if (root / "go.work").is_file():
                covers["go"][root] = ["**"]
        if "maven" in ecosystems and re.search(r"<modules>", _read_text(root / "pom.xml", 200_000)):
            covers["maven"][root] = ["**"]
        if "gradle" in ecosystems and any(
                (root / n).is_file() for n in ("settings.gradle", "settings.gradle.kts")):
            covers["gradle"][root] = ["**"]
        if "dotnet" in ecosystems and next(root.glob("*.sln"), None) is not None:
            covers["dotnet"][root] = ["**"]
    return covers


def _subtree_files(root: Path, files_per_dir: dict[Path, int]) -> int:
    """How many files the scan saw at or below one project root. Ordering input, never emitted."""
    prefix = root.as_posix().rstrip("/") + "/"
    total = 0
    for directory, count in files_per_dir.items():
        path = directory.as_posix()
        if directory == root or path.startswith(prefix):
            total += count
    return max(1, total)


def discover_projects(repo: Path) -> tuple[list[Project], dict]:
    """Every project root in the tree, plus a report of what discovery could not reach.

    No precedence: a directory holding both a package.json and a Cargo.toml yields TWO projects.
    The old table returned one, chosen by marker order, and the other half of the repository was
    never measured.
    """
    found, scan = _scan(repo)
    covers = _authoritative_roots(repo, found)
    projects: list[Project] = []
    omitted: list[dict] = list(scan["omitted"])
    ambiguous: list[dict] = []

    ordered = sorted(found.items(), key=lambda kv: (len(kv[0].parts), kv[0].as_posix()))
    for root, ecosystems in ordered:
        rel = _rel(repo, root)
        for eco in sorted(ecosystems):
            owner = None
            for wroot, globs in covers.get(eco, {}).items():
                if wroot == root:
                    continue
                try:
                    inner = root.relative_to(wroot).as_posix()
                except ValueError:
                    continue
                if _matches_any(inner, globs):
                    owner = wroot
                    break
            if owner is not None:
                for p in projects:
                    if p.root == owner and p.ecosystem == eco:
                        p.members.append(rel)
                        break
                else:
                    ambiguous.append({"root": rel, "ecosystems": [eco],
                                      "reason_code": "workspace_root_not_probed"})
                continue
            if len(projects) >= _MAX_PROJECTS:
                omitted.append({"root": rel, "ecosystems": [eco],
                                "reason_code": "project_budget_exhausted"})
                continue
            projects.append(Project(
                project_id=f"p{len(projects) + 1}",
                root=root,
                rel=rel,
                ecosystem=eco,
                workspace=root in covers.get(eco, {}),
                weight=_subtree_files(root, scan["files_per_dir"]),
            ))

    # Which ecosystems exist in the tree but were NOT probed. This is the field that stops a
    # discovery gap being written down as a repository defect: "we found a Rust workspace and
    # did not reach it" and "there is nothing here" are different sentences.
    unreachable = sorted({eco for gap in omitted + ambiguous
                          for eco in gap.get("ecosystems") or []})
    report = {
        "dirs_scanned": scan["dirs_scanned"],
        "files_scanned": scan["files_scanned"],
        "n_projects": len(projects),
        "ecosystems": sorted({p.ecosystem for p in projects}),
        "omitted_roots": omitted[:50],
        "ambiguous_roots": ambiguous[:50],
        "unreachable_ecosystems": unreachable,
        "truncated": len(projects) >= _MAX_PROJECTS or bool(
            [o for o in omitted if o["reason_code"] == "directory_budget_exhausted"]),
        # Only claimable when the scan reached everything. `no_manifest` was 60 of 417 in the
        # executed run and part of that was this scan giving up, recorded as the repo's failing.
        "no_manifest": not projects and not omitted and not ambiguous,
    }
    return projects, report


def survey(repo: Path, max_projects: int = MAX_PROBED_PROJECTS) -> dict:
    """A read-only look at what a probe would face: project roots, ecosystems, tree size.

    One bounded directory walk and nothing else -- no subprocess, no manifest parse. It exists so
    a run can tell an operator what it is about to cost BEFORE it starts spending, which is the
    difference between a long run and an apparently hung one.
    """
    projects, report = discover_projects(repo)
    probe_list, over_cap = _allocate(projects, max_projects)
    return {
        "n_projects": report["n_projects"],
        "n_projects_probed": len(probe_list),
        "n_projects_over_cap": len(over_cap),
        "ecosystems": report["ecosystems"],
        "files_scanned": report["files_scanned"],
        "dirs_scanned": report["dirs_scanned"],
    }


# Rough per-project costs, seconds, fitted to the audit's executed runs: ripgrep resolved+built+
# discovered in 10.1s warm and ran its suite in 1.5s more; psf/requests took 13s to discover and
# 60s to run. They are an ESTIMATE and are labelled as one wherever they are printed -- a cold
# cache, a large native build or a slow suite will beat them.
_ESTIMATE_PER_PROJECT = {"discover": 30.0, "full": 90.0}
_ESTIMATE_PER_KFILE = {"discover": 4.0, "full": 20.0}


def estimate_seconds(survey_report: dict, level: str) -> float:
    """Roughly how long a probe at this level will take. Never a promise; always printed as one."""
    if level not in ("discover", "full"):
        return 0.0
    projects = max(0, int(survey_report.get("n_projects_probed") or 0))
    kfiles = max(0.0, float(survey_report.get("files_scanned") or 0) / 1000.0)
    return (_ESTIMATE_PER_PROJECT[level] * projects) + (_ESTIMATE_PER_KFILE[level] * kfiles)


# --- runner-native parsers ------------------------------------------------------------------
#
# Each of these reads what a TEST RUNNER said. None of them looks at a filename: "there is a
# file called test_foo.py" and "pytest can collect a test" are different claims, and the study
# conflated them.

def _parse_pytest(text: str) -> dict:
    out: dict = {}
    m = re.search(r"(\d+) tests? collected", text) or re.search(r"collected (\d+) items?", text)
    if m:
        out["collected"] = int(m.group(1))
    if re.search(r"no tests ran|collected 0 items", text):
        out.setdefault("collected", 0)
    for key, pat in (("passed", r"(\d+) passed"), ("failed", r"(\d+) failed"),
                     ("errored", r"(\d+) errors?"), ("skipped", r"(\d+) skipped")):
        m = re.search(pat, text[-6000:])
        if m:
            out[key] = int(m.group(1))
    return out


def _parse_jest(text: str) -> dict:
    out: dict = {}
    m = re.search(r"^Tests:\s+(.+)$", text, re.M)
    if m:
        for key, pat in (("failed", r"(\d+) failed"), ("passed", r"(\d+) passed"),
                         ("skipped", r"(\d+) skipped"), ("collected", r"(\d+) total")):
            mm = re.search(pat, m.group(1))
            if mm:
                out[key] = int(mm.group(1))
    return out


def _parse_vitest(text: str) -> dict:
    out: dict = {}
    m = re.search(r"^\s*Tests\s+(.+?)\s*$", text, re.M)
    if m:
        for key, pat in (("failed", r"(\d+) failed"), ("passed", r"(\d+) passed"),
                         ("skipped", r"(\d+) skipped")):
            mm = re.search(pat, m.group(1))
            if mm:
                out[key] = int(mm.group(1))
        if out:
            out["collected"] = sum(out.values())
    return out


def _parse_mocha(text: str) -> dict:
    out: dict = {}
    for key, pat in (("passed", r"(\d+) passing"), ("failed", r"(\d+) failing"),
                     ("skipped", r"(\d+) pending")):
        m = re.search(pat, text)
        if m:
            out[key] = int(m.group(1))
    if out:
        out["collected"] = sum(out.values())
    return out


def _parse_cargo(text: str) -> dict:
    total = {"passed": 0, "failed": 0, "skipped": 0}
    hit = False
    for m in re.finditer(r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", text):
        hit = True
        total["passed"] += int(m.group(1))
        total["failed"] += int(m.group(2))
        total["skipped"] += int(m.group(3))
    if not hit:
        return {}
    total["collected"] = sum(total.values())
    return total


def _parse_go_json(text: str) -> dict:
    """Terminal per-test actions from `go test -json`. Package-level events are not tests."""
    passed = failed = skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not event.get("Test"):
            continue
        action = event.get("Action")
        if action == "pass":
            passed += 1
        elif action == "fail":
            failed += 1
        elif action == "skip":
            skipped += 1
    if not (passed or failed or skipped):
        return {}
    return {"collected": passed + failed + skipped, "passed": passed, "failed": failed,
            "skipped": skipped}


def _parse_dotnet(text: str) -> dict:
    out: dict = {}
    for key, pat in (("failed", r"Failed!?:\s+(\d+)"), ("passed", r"Passed!?:\s+(\d+)"),
                     ("skipped", r"Skipped!?:\s+(\d+)"), ("collected", r"Total:\s+(\d+)")):
        m = re.search(pat, text)
        if m:
            out[key] = int(m.group(1))
    return out


def _parse_phpunit(text: str) -> dict:
    m = re.search(r"OK \((\d+) tests?", text)
    if m:
        return {"collected": int(m.group(1)), "passed": int(m.group(1)), "failed": 0}
    m = re.search(r"Tests: (\d+)", text)
    if not m:
        return {}
    out = {"collected": int(m.group(1))}
    for key, pat in (("failed", r"Failures: (\d+)"), ("errored", r"Errors: (\d+)"),
                     ("skipped", r"Skipped: (\d+)")):
        mm = re.search(pat, text)
        if mm:
            out[key] = int(mm.group(1))
    out["passed"] = max(0, out["collected"] - out.get("failed", 0) - out.get("errored", 0)
                        - out.get("skipped", 0))
    return out


def _parse_rspec(text: str) -> dict:
    m = re.search(r"(\d+) examples?, (\d+) failures?(?:, (\d+) pending)?", text)
    if not m:
        return {}
    total, failed, pending = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return {"collected": total, "failed": failed, "skipped": pending,
            "passed": max(0, total - failed - pending)}


def _junit_counts(root: Path, patterns: tuple[str, ...]) -> dict:
    total = {"collected": 0, "failed": 0, "errored": 0, "skipped": 0}
    seen = 0
    for pattern in patterns:
        for xf in root.glob(pattern):
            try:
                tree = ET.parse(xf).getroot()
            except (ET.ParseError, OSError):
                continue
            suites = [tree] if tree.tag == "testsuite" else list(tree.iter("testsuite"))
            for suite in suites:
                seen += 1
                total["collected"] += int(suite.get("tests") or 0)
                total["failed"] += int(suite.get("failures") or 0)
                total["errored"] += int(suite.get("errors") or 0)
                total["skipped"] += int(suite.get("skipped") or 0)
    if not seen:
        return {}
    total["passed"] = max(0, total["collected"] - total["failed"] - total["errored"]
                          - total["skipped"])
    return total


_PARSERS = {
    "pytest": _parse_pytest, "jest": _parse_jest, "vitest": _parse_vitest, "mocha": _parse_mocha,
    "cargo": _parse_cargo, "go": _parse_go_json, "dotnet": _parse_dotnet,
    "phpunit": _parse_phpunit, "rspec": _parse_rspec,
}


# What a runner's --list output actually enumerates. jest and vitest list FILES, gradle lists
# TASKS, and recording either as a test count would overstate the suite -- the executed run's own
# counts fill `n_tests_collected` for those.
_LISTING_UNIT = {
    "jest_files": "test_files", "gradle_dry": "test_tasks", "cargo_list": "tests",
    "go_list": "tests", "dotnet_list": "tests", "phpunit_list": "tests", "rspec_dry": "tests",
}
_JEST_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:\\).*\.[cm]?[jt]sx?$")


def _count_listing(text: str, kind: str) -> int:
    """How many units a runner's own --list output names. See `_LISTING_UNIT` for the unit."""
    if kind == "jest_files":
        return sum(1 for line in text.splitlines() if _JEST_PATH_RE.match(line.strip()))
    if kind == "cargo_list":
        return len(re.findall(r": test\s*$", text, re.M))
    if kind == "go_list":
        return sum(1 for line in text.splitlines()
                   if re.match(r"^(Test|Example|Fuzz|Benchmark)\w*$", line.strip()))
    if kind == "dotnet_list":
        return sum(1 for line in text.splitlines() if line.startswith("    "))
    if kind == "phpunit_list":
        return len(re.findall(r"^\s*-\s+\S", text, re.M))
    if kind == "rspec_dry":
        counts = _parse_rspec(text)
        return int(counts.get("collected") or 0)
    if kind == "gradle_dry":
        return len(re.findall(r"^> Task .*:test\b", text, re.M))
    return 0


# --- per-ecosystem plans --------------------------------------------------------------------
#
# A plan is data, not control flow, so every ecosystem answers the same five questions and the
# phase runner below has one shape. `resolve.locked` is the command whose failure is a real
# failure; `resolve.relaxed` runs afterwards for diagnosis only and never changes the verdict.


def _pkg_scripts(root: Path) -> dict:
    pkg = _read_json(root / "package.json")
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _pkg_deps(root: Path) -> dict:
    pkg = _read_json(root / "package.json")
    deps: dict = {}
    if isinstance(pkg, dict):
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            value = pkg.get(key)
            if isinstance(value, dict):
                deps.update(value)
    return deps


def _node_framework(root: Path) -> str:
    deps = _pkg_deps(root)
    script = str(_pkg_scripts(root).get("test") or "").lower()
    if "vitest" in deps or "vitest" in script:
        return "vitest"
    if "jest" in deps or "@jest/core" in deps or "react-scripts" in deps or "jest" in script:
        return "jest"
    if "mocha" in deps or "mocha" in script:
        return "mocha"
    return "unknown"


def _real_test_script(root: Path) -> bool:
    """npm's initialiser writes a placeholder that exits 1 with "no test specified". Running it
    produces a failure that is neither the repository's nor the runner's, so treat it as absent."""
    script = str(_pkg_scripts(root).get("test") or "")
    return bool(script) and "no test specified" not in script.lower()


def _plan_node(project: Project, scratch: Path) -> dict:
    root = project.root
    cov_dir = scratch / "cov"
    if (root / "pnpm-lock.yaml").is_file():
        tool, toolchain = "pnpm", "node-pnpm"
        locked = ["pnpm", "install", "--frozen-lockfile"]
        relaxed = ["pnpm", "install", "--no-frozen-lockfile"]
    elif (root / "yarn.lock").is_file():
        tool, toolchain = "yarn", "node-yarn"
        berry = (root / ".yarnrc.yml").is_file() or "__metadata:" in _read_text(
            root / "yarn.lock", 4000)
        locked = ["yarn", "install", "--immutable"] if berry else [
            "yarn", "install", "--frozen-lockfile", "--non-interactive"]
        relaxed = ["yarn", "install"] if berry else ["yarn", "install", "--non-interactive"]
    elif (root / "package-lock.json").is_file() or (root / "npm-shrinkwrap.json").is_file():
        tool, toolchain = "npm", "node-npm"
        locked = ["npm", "ci", "--no-audit", "--no-fund"]
        relaxed = ["npm", "install", "--no-audit", "--no-fund", "--legacy-peer-deps"]
    else:
        # No lockfile at all. `npm install` IS the declared install here, so it is the locked
        # command; there is nothing to be strict about and calling it a lockfile failure would
        # be inventing one.
        tool, toolchain = "npm", "node-npm"
        locked = ["npm", "install", "--no-audit", "--no-fund"]
        relaxed = None

    scripts = _pkg_scripts(root)
    build = ["npm", "run", "build"] if "build" in scripts else None
    framework = _node_framework(root)
    if framework == "jest":
        discover = (["npx", "--no-install", "jest", "--listTests"], "jest_files")
        test = (["npx", "--no-install", "jest", "--ci", "--runInBand", "--passWithNoTests",
                 "--coverage", "--coverageReporters=json-summary",
                 f"--coverageDirectory={cov_dir}"], "jest")
        coverage = ("istanbul", cov_dir / "coverage-summary.json")
    elif framework == "vitest":
        discover = (["npx", "--no-install", "vitest", "list"], "jest_files")
        test = (["npx", "--no-install", "vitest", "run", "--coverage",
                 "--coverage.reporter=json-summary",
                 f"--coverage.reportsDirectory={cov_dir}"], "vitest")
        coverage = ("istanbul", cov_dir / "coverage-summary.json")
    elif framework == "mocha":
        # `--dry-run --reporter min` prints mocha's own "N passing" summary, so the mocha
        # parser reads it. It used to be passed `None`, which meant nothing parsed the output,
        # the count fell through to 0 and a working three-test suite was recorded as a
        # repository with no tests.
        discover = (["npx", "--no-install", "mocha", "--dry-run", "--reporter", "min"], "mocha")
        test = (["npm", "test", "--silent"], "mocha")
        coverage = None
    elif _real_test_script(root):
        discover = None
        test = (["npm", "test", "--silent"], None)
        coverage = None
    else:
        discover = None
        test = None
        coverage = None
    return {"toolchain": toolchain, "tool": tool, "locked": locked, "relaxed": relaxed,
            "build": build, "discover": discover, "test": test, "coverage": coverage}


def _plan_python(project: Project, scratch: Path, env: dict, timeout: int,
                 base_python: str | None = None) -> dict:
    root = project.root
    venv = scratch / "venv"
    # `base_python` is the interpreter runtime resolution chose for this tree. Falling back to
    # `sys.executable` -- the interpreter running the collector -- is what this module did for
    # every repository before that existed, and it is why a project pinned to Python 3.8 was
    # installed under whatever the host happened to run us with and then blamed for the result.
    interpreter = base_python or sys.executable
    rc, log, _ = _run([interpreter, "-m", "venv", str(venv)], scratch, env, min(timeout, 300))
    if rc != 0 or not _venv_python(venv).exists():
        # A host that cannot create a virtualenv is a host problem; _ENV matches the message.
        return {"toolchain": "python", "tool": None, "preflight": log or
                "ensurepip is not available"}
    py = str(_venv_python(venv))
    cov_json = scratch / "coverage.json"
    if (root / "requirements.txt").is_file():
        locked = [py, "-m", "pip", "install", "-r", "requirements.txt"]
        relaxed = None
    elif any((root / n).is_file() for n in ("pyproject.toml", "setup.py", "setup.cfg")):
        locked = [py, "-m", "pip", "install", "."]
        relaxed = [py, "-m", "pip", "install", "--no-build-isolation", "."]
    else:
        for extra in ("requirements-dev.txt", "dev-requirements.txt", "requirements/base.txt",
                      "requirements/dev.txt"):
            if (root / extra).is_file():
                locked, relaxed = [py, "-m", "pip", "install", "-r", extra], None
                break
        else:
            # A Python marker matched (tox.ini, noxfile.py) but nothing here is installable.
            # Running an invented command would score the repository for our guess.
            return {"toolchain": "python-venv", "tool": None,
                    "preflight": "probe: no installable manifest present"}
    return {
        "toolchain": "python-venv", "tool": py, "locked": locked, "relaxed": relaxed,
        "build": None,
        "harness": [py, "-m", "pip", "install", "-q", "pytest", "pytest-cov"],
        "discover": ([py, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                     "pytest"),
        "test": ([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--cov=.",
                  f"--cov-report=json:{cov_json}", "--cov-report="], "pytest"),
        "coverage": ("coveragepy", cov_json),
        "test_fallback": ([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "pytest"),
    }


def _plan_rust(project: Project, scratch: Path) -> dict:
    # `--locked` errors out when there is no Cargo.lock to honour, which would score a crate
    # that simply has not committed its lockfile as a resolution failure -- the same defect
    # `npm ci` without a package-lock.json used to produce here.
    has_lock = (project.root / "Cargo.lock").is_file()
    return {
        "toolchain": "cargo", "tool": "cargo",
        "locked": ["cargo", "fetch", "--locked"] if has_lock else ["cargo", "fetch"],
        "relaxed": ["cargo", "fetch"] if has_lock else None,
        "build": ["cargo", "build", "--locked"] if has_lock else ["cargo", "build"],
        "build_relaxed": ["cargo", "build"] if has_lock else None,
        "discover": (["cargo", "test", "--", "--list"], "cargo_list"),
        "test": (["cargo", "test", "--no-fail-fast"], "cargo"),
        "coverage": None,
        "coverage_reason": "cargo ships no line-coverage reporter and cargo-llvm-cov is not "
                           "assumed to be installed",
    }


def _plan_go(project: Project, scratch: Path) -> dict:
    profile = scratch / "cover.out"
    return {
        "toolchain": "go-modules", "tool": "go",
        "locked": ["go", "mod", "download"], "relaxed": None,
        "build": ["go", "build", "./..."],
        "discover": (["go", "test", "-list", ".*", "./..."], "go_list"),
        "test": (["go", "test", "./...", "-count=1", "-json", "-covermode=set",
                  f"-coverprofile={profile}"], "go"),
        "coverage": ("go", profile),
    }


def _plan_maven(project: Project, scratch: Path, restore: list) -> dict:
    wrapper = project.root / ("mvnw.cmd" if _IS_WIN else "mvnw")
    if wrapper.is_file():
        _ensure_executable(wrapper, restore)
        exe = [str(wrapper)]
    else:
        exe = ["mvn"]
    base = exe + ["-B", "-ntp", f"-Dmaven.repo.local={scratch / 'm2'}"]
    return {
        "toolchain": "maven", "tool": exe[0],
        # NOT `-o`. `-Dmaven.repo.local` points at a scratch directory that this run created
        # empty, so offline resolution against it cannot succeed for any project with a
        # dependency or a plugin -- it failed, matched "offline mode" in _ENV, and Maven was
        # systematically unmeasurable. Downloading into a scratch repository is the same thing
        # `npm ci`, `pip install -r` and `cargo fetch` do in the other ecosystems.
        "locked": base + ["dependency:go-offline"],
        "relaxed": base + ["-U", "dependency:go-offline"],
        "build": base + ["-DskipTests", "test-compile"],
        # Maven has no native test enumeration. The selector that used to sit here,
        # `-Dtest=ProbeDiscovery_NoSuchTest`, was designed to match nothing and did: surefire
        # wrote no reports, the count came back 0, and the probe filed the repository as having
        # no tests without ever running one. `None` records the honest
        # `unavailable / no_native_discovery / runner`, and the `test` phase below runs the
        # suite for real and reads the JUnit XML it leaves behind.
        "discover": None,
        "test": (base + ["-Dmaven.test.failure.ignore=true", "test"], None),
        "test_junit": ("**/target/surefire-reports/*.xml", "**/target/failsafe-reports/*.xml"),
        "coverage": None,
        "coverage_reason": "jacoco is only reported when the project already applies the plugin",
    }


def _plan_gradle(project: Project, scratch: Path, restore: list) -> dict:
    wrapper = project.root / ("gradlew.bat" if _IS_WIN else "gradlew")
    if wrapper.is_file():
        _ensure_executable(wrapper, restore)
        exe = [str(wrapper)]
    else:
        exe = ["gradle"]
    base = exe + ["--no-daemon", "--console=plain", "-g", str(scratch / "gradle")]
    return {
        "toolchain": "gradle", "tool": exe[0],
        # `dependencies` resolves the graph without compiling, which is the closest Gradle has to
        # a locked-install phase; `--write-locks` is deliberately absent because it would edit
        # tracked lockfiles.
        "locked": base + ["dependencies"],
        "relaxed": None,
        "build": base + ["testClasses"],
        "discover": (base + ["test", "--dry-run"], "gradle_dry"),
        "test": (base + ["--continue", "test"], None),
        "test_junit": ("**/build/test-results/**/*.xml",),
        "coverage": None,
        "coverage_reason": "jacoco is only reported when the project already applies the plugin",
    }


def _plan_dotnet(project: Project, scratch: Path) -> dict:
    return {
        "toolchain": "dotnet", "tool": "dotnet",
        "locked": ["dotnet", "restore", "--locked-mode"], "relaxed": ["dotnet", "restore"],
        "build": ["dotnet", "build", "--no-restore"],
        "discover": (["dotnet", "test", "--no-build", "--list-tests"], "dotnet_list"),
        "test": (["dotnet", "test", "--no-build", "--nologo"], "dotnet"),
        "coverage": None,
        "coverage_reason": "coverlet is only reported when the project already references it",
    }


def _plan_ruby(project: Project, scratch: Path) -> dict:
    return {
        "toolchain": "bundler", "tool": "bundle",
        "locked": ["bundle", "install", "--deployment"], "relaxed": ["bundle", "install"],
        "build": None,
        "discover": (["bundle", "exec", "rspec", "--dry-run", "--no-color"], "rspec_dry"),
        "test": (["bundle", "exec", "rspec", "--no-color"], "rspec"),
        "coverage": None,
        "coverage_reason": "simplecov only reports when the project already configures it",
    }


def _plan_php(project: Project, scratch: Path) -> dict:
    phpunit = scratch / "vendor" / "bin" / ("phpunit.bat" if _IS_WIN else "phpunit")
    return {
        "toolchain": "composer", "tool": "composer",
        "locked": ["composer", "install", "--no-interaction", "--no-progress"],
        "relaxed": ["composer", "install", "--no-interaction", "--no-progress",
                    "--ignore-platform-reqs"],
        "build": None,
        "discover": ([str(phpunit), "--list-tests"], "phpunit_list"),
        "test": ([str(phpunit), "--do-not-cache-result"], "phpunit"),
        "coverage": None,
        "coverage_reason": "no PHP coverage driver is assumed to be installed",
    }


def _resolve_runtime(project: Project, env: dict, allow_home: bool) -> runtime.Plan:
    """What runtime this project's tree asks for, matched against this host.

    Only the ONE lane this project's commands will run on, which is what keeps resolution cheap
    enough to do per root: a Node package does not pay to enumerate the host's JDKs.
    """
    lane = _ECOSYSTEM_LANE.get(project.ecosystem)
    if lane is None:
        return runtime.Plan()
    return runtime.resolve(project.root, env, allow_home=allow_home, lanes=(lane,))


def _plan_for(project: Project, scratch: Path, env: dict, timeout: int,
              restore: list[tuple[Path, int]], allow_home: bool = False) -> dict:
    """The five commands for one project. `tool` None means the plan could not be formed.

    Runtime resolution happens FIRST and its environment overlay is applied before any command is
    formed, because a Python plan bakes an interpreter path into every phase and a Maven plan needs
    JAVA_HOME set before `mvn` is even looked for.
    """
    rt = _resolve_runtime(project, env, allow_home)
    env = runtime.apply_overlay(env, rt.overlay)
    if project.ecosystem == "node":
        plan = _plan_node(project, scratch)
    elif project.ecosystem == "python":
        plan = _plan_python(project, scratch, env, timeout, rt.interpreter.get("python"))
    elif project.ecosystem == "rust":
        plan = _plan_rust(project, scratch)
    elif project.ecosystem == "go":
        plan = _plan_go(project, scratch)
    elif project.ecosystem == "maven":
        plan = _plan_maven(project, scratch, restore)
    elif project.ecosystem == "gradle":
        plan = _plan_gradle(project, scratch, restore)
    elif project.ecosystem == "dotnet":
        plan = _plan_dotnet(project, scratch)
    elif project.ecosystem == "ruby":
        plan = _plan_ruby(project, scratch)
    elif project.ecosystem == "php":
        plan = _plan_php(project, scratch)
    else:
        plan = {"toolchain": project.ecosystem, "tool": None,
                "preflight": f"command not found: {project.ecosystem}"}
    plan["runtime"] = rt
    plan["env"] = env
    return plan


# --- classification -------------------------------------------------------------------------

def _classify(log: str, timed_out: bool, repo: Path | None = None) -> str:
    if timed_out:
        return "TIMEOUT"
    if _AUTH.search(log):
        # Whoever asked for the private registry owns the failure. Without a registry config in
        # the tree there is nothing to suggest the project needs one, so the credentials that were
        # rejected were the host's -- err toward the runner, as everywhere else here.
        return ("REPO_INTRINSIC" if repo is not None and _declares_private_registry(repo)
                else "ENVIRONMENT")
    if _ENV.search(log):
        return "ENVIRONMENT"
    if _REPO.search(log):
        return "REPO_INTRINSIC"
    return "UNCLASSIFIED"


def _error_class(log: str, timed_out: bool) -> str:
    """The validated build_error_class sub-vocabulary. `_classify` remains the sole authority on
    failure_class, so the two can describe the same failure but can never contradict."""
    if timed_out:
        return "timeout"
    if not log.strip():
        return "unknown_no_output"
    for pattern, code in _ERROR_SIGNATURES:
        if pattern.search(log):
            return code
    return "unclassified"


# Error classes whose OWNER is settled by the class itself. Every one of them is a statement about
# the machine, so a failure that matched one is not ambiguous even when the coarse `_classify`
# vocabulary happened not to recognise the message.
_RUNNER_CLASSES = frozenset({
    "wrong_runtime", "toolchain_missing", "native_build_failed", "missing_system_lib",
    "container_misconfig", "out_of_disk", "out_of_memory", "package_manager_bug",
    "test_dependency_missing", "build_backend_missing", "missing_wrapper_jar",
})


# Error classes that are POSITIVE evidence about this machine and cannot be caused by anything in
# a repository: the interpreter is the wrong version, the disk is full, the box is out of memory,
# the container is misconfigured. These are the only classes allowed to overturn a REPO_INTRINSIC
# verdict, and they are allowed to because the alternative is charging a repository ten points for
# the state of our host. Everything else that merely *smells* like the host is left alone -- a
# compile error whose log happens to mention node-gyp is still a compile error.
_HOST_STATE_CLASSES = frozenset({"wrong_runtime", "out_of_disk", "out_of_memory",
                                 "container_misconfig"})


def _reconcile(cls: str, error_class: str, repo: Path | None) -> str:
    """Settle `failure_class` when the error class already knows whose failure it was.

    The two vocabularies are separate regex sets and they can disagree: a Go module behind a
    private host emitted `could not read Username`, which `_ERROR_SIGNATURES` names
    `private_registry` and `_AUTH` does not match, so the repository came back
    `UNCLASSIFIED / private_registry` -- a named class with an unnamed owner, which is exactly the
    hole `unclassified` at 32 of 417 was measuring. Reconciling here rather than merging the two
    pattern sets keeps `_classify` as the authority on the coarse enum and keeps the calibrated
    `build_error_class` histogram untouched.

    It only ever moves a verdict TOWARD the runner, never toward the repository. A signature that
    looks repository-flavoured but that `_REPO` did not match is left UNCLASSIFIED, whose
    attribution is already `runner`: promoting it would cost a repository ten points on the
    strength of a pattern we did not think was good enough to put in `_REPO`.
    """
    if error_class in _HOST_STATE_CLASSES:
        # Observed: a 2019 requirements.txt under a current pip. Our pip rejects the pinned wheel's
        # own metadata, the resolution failure that follows matches `_REPO`, and the repository was
        # charged REPO_INTRINSIC for the age of our toolchain. The class already says the host was
        # the problem, so the coarse verdict must say so too.
        return "ENVIRONMENT"
    if cls != "UNCLASSIFIED":
        return cls
    if error_class in _CREDENTIAL_CLASSES:
        # The same rule `_classify` applies, on the same question: whoever asked for the private
        # index owns it, and only the TREE can be the one asking.
        return ("REPO_INTRINSIC" if repo is not None and _declares_private_registry(repo)
                else "ENVIRONMENT")
    if error_class in _RUNNER_CLASSES or error_class in _EXTERNAL_CLASSES:
        return "ENVIRONMENT"
    return cls


def _attribution(failure_class: str, error_class: str) -> str:
    """Who owns this outcome. `unknown` is a last resort, never a landing spot.

    A customer read an "unknown environment" verdict off this probe and could do nothing with
    it, so every outcome now names a side. UNCLASSIFIED resolves to `runner` because that is
    already the module's standing policy for ambiguity -- a wrong REPO_INTRINSIC costs a
    repository ten points for something it did not do -- and a clean outcome is attributed to
    the repository, since a build that works is a property of the repository.
    """
    if error_class in _CREDENTIAL_CLASSES:
        return "credentials"
    if error_class in _EXTERNAL_CLASSES:
        return "external_service"
    if failure_class == "REPO_INTRINSIC":
        return "repository"
    if failure_class in ("ENVIRONMENT", "TIMEOUT", "UNCLASSIFIED"):
        return "runner"
    if failure_class == "NONE":
        return "repository"
    return "unknown"


def _confidence(failure_class: str, error_class: str) -> str:
    if failure_class == "UNCLASSIFIED" or error_class in ("unclassified", "unknown_no_output"):
        return "low" if failure_class == "UNCLASSIFIED" else "medium"
    return "high"


def _remediation(cls: str, log: str, toolchain: str | None) -> tuple[str, str]:
    """(effort, notes). Reported, never scored -- remediation cost depends on the operator."""
    if cls == "NONE":
        return "none", "The project builds and its dependencies resolve as checked in."
    if cls == "ENVIRONMENT":
        return "trivial", (
            f"The failure was in the runner's environment rather than the repository "
            f"({toolchain or 'unknown'} toolchain unavailable or mismatched). Running on a host "
            f"with the expected runtime is likely sufficient; the repository is not penalised.")
    if cls == "TIMEOUT":
        return "unknown", (
            "The build did not finish inside the time limit, so nothing can be concluded about "
            "whether it would have succeeded. Re-run with a longer --timeout-build to classify it.")
    for effort, pattern, note in _REMEDIATION:
        if pattern.search(log):
            return effort, note[0].upper() + note[1:] + "."
    return "unknown", (
        "The build failed but the cause did not match a known signature, so the work required "
        "cannot be estimated from the output alone. Manual inspection is needed.")


def _coverage(scratch: Path, log: str) -> tuple[float | None, str | None]:
    """Coverage read from the agentic path's scratch output, or scraped from its transcript."""
    cj = scratch / "coverage.json"
    if cj.is_file():
        try:
            pct = json.loads(cj.read_text(errors="replace")).get("totals", {}).get("percent_covered")
            if pct is not None:
                return float(pct), "pytest-cov"
        except (OSError, ValueError, AttributeError):
            pass
    for pat, name in ((r"coverage:\s*([\d.]+)%\s*of statements", "go-cover"),
                      (r"^TOTAL\s+.*?\s([\d.]+)%\s*$", "coverage-total"),
                      (r"All files\s*\|\s*([\d.]+)", "istanbul"),
                      (r"Total\s*\|\s*([\d.]+)%", "dotnet-coverlet")):
        m = re.search(pat, log, re.M)
        if m:
            return float(m.group(1)), name
    return None, "no coverage reporter produced a parseable total"


def _read_coverage(kind: str, target: Path, env: dict, cwd: Path,
                   timeout: int) -> tuple[float | None, int | None, int | None, str | None]:
    """(pct, covered, total, method) from a reporter's own artefact."""
    if kind == "coveragepy":
        data = _read_json(target)
        totals = data.get("totals") if isinstance(data, dict) else None
        if isinstance(totals, dict) and totals.get("num_statements"):
            return (round(float(totals.get("percent_covered") or 0.0), 2),
                    int(totals.get("covered_lines") or 0),
                    int(totals.get("num_statements") or 0), "coverage.py via pytest-cov")
        return None, None, None, None
    if kind == "istanbul":
        data = _read_json(target)
        lines = (data or {}).get("total", {}).get("lines") if isinstance(data, dict) else None
        if isinstance(lines, dict) and lines.get("total"):
            return (round(float(lines.get("pct") or 0.0), 2), int(lines.get("covered") or 0),
                    int(lines.get("total") or 0), "istanbul json-summary")
        return None, None, None, None
    if kind == "go":
        if not target.is_file():
            return None, None, None, None
        rc, log, _ = _run(["go", "tool", "cover", f"-func={target}"], cwd, env, min(timeout, 180))
        m = re.search(r"^total:\s+\(statements\)\s+([\d.]+)%", log, re.M)
        if rc == 0 and m:
            return float(m.group(1)), None, None, "go test -coverprofile"
        return None, None, None, None
    return None, None, None, None


# --- the phase runner -----------------------------------------------------------------------

def _phase(name: str, status: str, reason_code: str, attribution: str = "repository",
           confidence: str = "high", seconds: float = 0.0, command: str = "",
           **detail) -> dict:
    record = {"phase": name, "status": status, "reason_code": reason_code,
              "attribution": attribution, "confidence": confidence,
              "seconds": round(seconds, 1), "command": command}
    record.update(detail)
    return record


def _fail_phase(name: str, log: str, timed_out: bool, repo: Path, command: str,
                seconds: float) -> tuple[dict, str, str]:
    ecode = _error_class(log, timed_out)
    cls = _reconcile(_classify(log, timed_out, repo), ecode, repo)
    attribution = _attribution(cls, ecode)
    return (_phase(name, "timed_out" if timed_out else "failed", ecode, attribution,
                   _confidence(cls, ecode), seconds, command, failure_class=cls),
            cls, ecode)


def _blame_runtime(record: dict, phase: dict) -> None:
    """Re-attribute a failure to US, because the tree asked for a runtime this host cannot supply.

    This is the rule the whole runtime lane exists to enforce, and it runs BEFORE the log
    signatures get a vote. A repository pinned to Node 16, built under Node 26, fails with
    `ERR_OSSL_EVP_UNSUPPORTED`, an ERESOLVE peer conflict or a native rebuild blowing up -- three
    different signatures, two of which read as the repository's own broken dependency graph. None
    of them are. We knew the runtime was wrong before we ran the command, so what the command said
    afterwards cannot be used to blame the codebase for it.
    """
    phase["failure_class"] = "ENVIRONMENT"
    phase["attribution"] = "runner"
    phase["reason_code"] = "wrong_runtime"
    phase["confidence"] = "high"
    record["failure_class"] = "ENVIRONMENT"
    record["build_error_class"] = "wrong_runtime"
    record["attribution"] = "runner"


def _blank_record(project: Project) -> dict:
    """The record shape every project reports, with every measurement null until one is made."""
    return {
        "project_id": project.project_id,
        "relative_root": redact(project.rel)[0] or ".",
        "ecosystem": project.ecosystem,
        "toolchain": None,
        "workspace": project.workspace,
        "members": [redact(m)[0] for m in project.members[:20]],
        "required": project.required,
        "phases": [],
        "n_tests_collected": None, "n_passed": None, "n_failed": None,
        "coverage_pct": None, "coverage_method": None,
        # A clean project is attributed to the repository, not left null: "who owns this
        # outcome" must have an answer on the success path too, or a reader learns nothing.
        "failure_class": "NONE", "build_error_class": None, "attribution": "repository",
        "relaxed_retry": None, "skipped_reason": None,
        # Set only when a VERIFIED repair let the phase sequence continue; the index is where the
        # post-repair phases begin, so the two runs can be rolled up apart.
        "repaired_from_phase": None, "repaired_from_index": None,
        # What runtime this project asked for and what it got. Empty when the tree declares none,
        # which is a different fact from asking for one we could not supply.
        "runtime": [],
    }


def _skipped_record(project: Project, reason_code: str) -> dict:
    """A project we never probed. Every phase `skipped_budget`, every measurement null.

    Attribution is the RUNNER, always: running out of clock, or capping how many roots we probe,
    is our decision about our machine. Recording it against the repository is the exact failure
    this module exists to avoid -- and a null here is what makes the aggregate go null rather
    than report a number that only covers the projects we happened to reach.
    """
    record = _blank_record(project)
    record["skipped_reason"] = reason_code
    record["failure_class"] = "NONE"
    record["attribution"] = "runner"
    for phase in PHASES:
        record["phases"].append(_phase(phase, "skipped_budget", reason_code, "runner", "high"))
    return record


def _probe_project(project: Project, repo: Path, scratch: Path, env: dict, budget: Budget,
                   project_end: float, level: str,
                   restore: list[tuple[Path, int]], tried: list[list[str]],
                   allow_home: bool = False, repair: dict | None = None) -> dict:
    """The five phases for one project, all of them drawn from the shared budget.

    `project_end` is this project's slice of the remaining allowance as a monotonic instant. Every
    command gets the smallest of the phase cap, what is left of that slice, and what is left of
    the whole probe; when that is too small to be worth starting, the rest of the project is
    recorded `skipped_budget` and the run moves on with the time it did not spend.
    """
    record = _blank_record(project)

    def allowance() -> int:
        return budget.slice(project_end)

    def skipped_from(phase_name: str, reason: str) -> dict:
        """This phase and everything after it: OUR limit, so null measurements and a reason."""
        record["skipped_reason"] = reason
        status = "skipped_level" if reason == "level_discover_only" else "skipped_budget"
        if status == "skipped_budget" and record["failure_class"] == "NONE":
            # Running out of clock is the runner's doing. A project that simply stopped at the
            # level it was asked for did not fail at all, so its attribution is left alone.
            record["attribution"] = "runner"
        for later in PHASES[PHASES.index(phase_name):]:
            record["phases"].append(_phase(later, status, reason, "runner", "high"))
        return record

    def blocked_from(phase_name: str, reason: str) -> dict:
        """Everything downstream of a failure is `blocked`, and inherits the failure's owner.

        A blocked phase with no attribution is how "we never got that far" used to read as
        "unknown", which told the reader nothing about who had to fix it.
        """
        cause = record["attribution"] or "runner"
        for later in PHASES[PHASES.index(phase_name):]:
            record["phases"].append(_phase(later, "blocked", reason, cause, "high"))
        return record

    if not allowance():
        return skipped_from("resolve", "run_budget_exhausted")

    pscratch = scratch / project.project_id
    pscratch.mkdir(parents=True, exist_ok=True)
    # Planning a Python project creates a virtualenv, which is itself a subprocess, so it is paid
    # for out of the same allowance as everything else.
    plan = _plan_for(project, pscratch, env, allowance(), restore, allow_home)
    record["toolchain"] = plan.get("toolchain")
    # Every command below runs in the environment resolution produced, not the one handed in: the
    # overlay is how a selected runtime is actually reached.
    env = plan["env"]
    rt = plan["runtime"]
    record["runtime"] = rt.records
    wrong_runtime = bool(rt.unsatisfied)

    if not plan.get("tool"):
        preflight = plan.get("preflight") or f"command not found: {project.ecosystem}"
        cls = _classify(preflight, False, project.root)
        ecode = _error_class(preflight, False)
        record["failure_class"] = "ENVIRONMENT" if cls == "UNCLASSIFIED" else cls
        record["build_error_class"] = ecode
        record["attribution"] = _attribution(record["failure_class"], ecode)
        phase = _phase("resolve", "unavailable", ecode, record["attribution"], "high")
        record["phases"].append(phase)
        if wrong_runtime:
            _blame_runtime(record, phase)
            phase["status"] = "unavailable"
        return blocked_from("build", "resolve_unavailable")
    if not _which(plan["locked"][0], env) and not Path(plan["locked"][0]).exists():
        record["failure_class"] = "ENVIRONMENT"
        record["build_error_class"] = "toolchain_missing"
        record["attribution"] = "runner"
        record["phases"].append(_phase("resolve", "unavailable", "toolchain_missing",
                                       "runner", "high"))
        return blocked_from("build", "resolve_unavailable")

    # --- phase 1: locked dependency resolution ------------------------------------------
    seconds = allowance()
    if not seconds:
        return skipped_from("resolve", "run_budget_exhausted")
    started = time.monotonic()
    tried.append(plan["locked"])
    rc, log, to = _run(plan["locked"], project.root, env, seconds)
    elapsed = time.monotonic() - started
    if rc == 0:
        record["phases"].append(_phase("resolve", "passed", "locked_install_succeeded",
                                       "repository", "high", elapsed, _display(plan["locked"])))
    else:
        phase, cls, ecode = _fail_phase("resolve", log, to, project.root,
                                        _display(plan["locked"]), elapsed)
        record["phases"].append(phase)
        record["failure_class"], record["build_error_class"] = cls, ecode
        record["attribution"] = _attribution(cls, ecode)
        if wrong_runtime:
            _blame_runtime(record, phase)
        # The relaxed retry is DIAGNOSIS. A repository whose pinned graph does not resolve has a
        # broken pinned graph, whatever a non-frozen install would have done, so its outcome is
        # recorded beside the failure and never replaces it.
        relaxed = plan.get("relaxed")
        if relaxed is not None and not to and allowance():
            tried.append(relaxed)
            rc2, log2, to2 = _run(relaxed, project.root, env, allowance())
            record["relaxed_retry"] = {
                "phase": "resolve", "ok": rc2 == 0,
                "reason_code": "relaxed_install_succeeded" if rc2 == 0
                else _error_class(log2, to2),
                "note": "diagnostic only; the locked install is the measurement",
            }
        if repair:
            _repair_phase(record, "resolve", project, pscratch, env, plan["locked"], log,
                          allowance, repair["provider"], repair["model"])
        if not (record.get("repair") or {}).get("ok"):
            return blocked_from("build", "resolve_failed")
        # The repair was verified by re-running THIS command, so the tree resolves now. Carrying
        # on is the entire point of the pass: a run that repairs the environment and then still
        # reports every downstream phase `blocked: resolve_failed` has spent a provider turn to
        # change nothing, which is what the first live run did -- 185 seconds for a footnote.
        # The as-shipped phases above are left exactly as they are; everything from here is
        # flagged `after_repair` and rolled up separately.
        record["repaired_from_phase"] = "resolve"
        record["repaired_from_index"] = len(record["phases"])
        # The post-repair run of the phase, recorded as its own phase rather than by mutating the
        # as-shipped one. Without it the effective view reads the FAILED as-shipped resolve and
        # reports install_ok False for a tree whose install we just watched succeed.
        record["phases"].append(_phase("resolve", "passed", "repair_verified", "repository",
                                       "high"))

    if plan.get("harness") and allowance():
        # Our own test runner, not the project's dependency. Best effort, and deliberately
        # outside the resolve verdict: a project that never declared pytest has not failed
        # because our virtualenv lacked it.
        _run(plan["harness"], project.root, env, min(allowance(), 300))

    # --- phase 2: build -----------------------------------------------------------------
    build_cmd = plan.get("build")
    if build_cmd is None:
        record["phases"].append(_phase("build", "passed", "interpreted_no_build_step",
                                       "repository", "high"))
    else:
        seconds = allowance()
        if not seconds:
            return skipped_from("build", "run_budget_exhausted")
        started = time.monotonic()
        tried.append(build_cmd)
        rc, log, to = _run(build_cmd, project.root, env, seconds)
        elapsed = time.monotonic() - started
        if rc == 0:
            record["phases"].append(_phase("build", "passed", "build_succeeded", "repository",
                                           "high", elapsed, _display(build_cmd)))
        else:
            phase, cls, ecode = _fail_phase("build", log, to, project.root,
                                            _display(build_cmd), elapsed)
            record["phases"].append(phase)
            record["failure_class"], record["build_error_class"] = cls, ecode
            record["attribution"] = _attribution(cls, ecode)
            if wrong_runtime:
                _blame_runtime(record, phase)
            if plan.get("build_relaxed") is not None and not to and allowance():
                rc2, log2, to2 = _run(plan["build_relaxed"], project.root, env, allowance())
                record["relaxed_retry"] = {
                    "phase": "build", "ok": rc2 == 0,
                    "reason_code": "relaxed_build_succeeded" if rc2 == 0
                    else _error_class(log2, to2),
                    "note": "diagnostic only; the locked build is the measurement",
                }
            if repair:
                _repair_phase(record, "build", project, pscratch, env, build_cmd, log,
                              allowance, repair["provider"], repair["model"])
            if not (record.get("repair") or {}).get("ok"):
                return blocked_from("discover", "build_failed")
            record["repaired_from_phase"] = "build"
            record["repaired_from_index"] = len(record["phases"])
            record["phases"].append(_phase("build", "passed", "repair_verified", "repository",
                                           "high"))

    # --- phase 3: runner-native test discovery ------------------------------------------
    discover = plan.get("discover")
    n_collected: int | None = None
    if discover is None:
        record["phases"].append(_phase("discover", "unavailable", "no_native_discovery",
                                       "runner", "medium"))
    else:
        argv, kind = discover
        unit = "tests"
        seconds = allowance()
        if not seconds:
            return skipped_from("discover", "run_budget_exhausted")
        started = time.monotonic()
        tried.append(argv)
        rc, log, to = _run(argv, project.root, env, seconds)
        elapsed = time.monotonic() - started
        if kind in _PARSERS:
            n_collected = _PARSERS[kind](log).get("collected")
        elif kind:
            n_collected = _count_listing(log, kind)
            unit = _LISTING_UNIT.get(kind, "tests")
        if to:
            record["phases"].append(_phase("discover", "timed_out", "timeout", "runner",
                                           "high", elapsed, _display(argv)))
            return blocked_from("test", "discovery_timed_out")
        # `no_tests` is claimable ONLY when the runner ran and said so -- exit 0, or pytest's
        # documented exit 5. Anything else means the runner could not be started or blew up, and
        # calling that "no tests" is precisely the reported defect: a repository whose JS runner
        # was not installed was recorded as having no suite while its real suite sat unprobed.
        if rc not in (0, 5) and not n_collected:
            phase, cls, ecode = _fail_phase("discover", log, to, project.root,
                                            _display(argv), elapsed)
            phase["status"] = "unavailable"
            phase["attribution"] = "runner" if ecode == "toolchain_missing" else phase["attribution"]
            record["phases"].append(phase)
            record["n_tests_collected"] = None
            if record["failure_class"] == "NONE":
                record["attribution"] = phase["attribution"]
            if wrong_runtime:
                _blame_runtime(record, phase)
                phase["status"] = "unavailable"
            return blocked_from("test", "discovery_unavailable")
        if n_collected is None:
            # The runner exited cleanly but said nothing we can count -- no parser for this
            # invocation, or a selector that enumerated nothing. That is OUR blind spot, not a
            # measured absence of tests: recording it as `no_tests / repository / high` is how
            # a working mocha suite came to be reported as a repository with no tests at all.
            # Say so, and go on to run the suite, which counts for itself.
            record["phases"].append(_phase("discover", "unavailable",
                                           "discovery_produced_no_count", "runner", "medium",
                                           elapsed, _display(argv)))
        else:
            if unit == "tests":
                record["n_tests_collected"] = n_collected
            if n_collected == 0:
                # The runner looked and found nothing. That is a measured property of the
                # repository, not a failure of ours, and it must never read as `tests_failed`.
                record["phases"].append(_phase(
                    "discover", "no_tests", "runner_collected_zero_tests", "repository", "high",
                    elapsed, _display(argv), discovered=0, discovery_unit=unit))
                record["phases"].append(_phase("test", "no_tests", "nothing_to_execute",
                                               "repository", "high"))
                record["phases"].append(_phase("coverage", "no_tests", "nothing_to_measure",
                                               "repository", "high"))
                return record
            record["phases"].append(_phase("discover", "passed", "runner_collected_tests",
                                           "repository", "high", elapsed, _display(argv),
                                           discovered=n_collected, discovery_unit=unit))

    # --- phase 4: test execution --------------------------------------------------------
    # At `discover` we stop here on purpose. The suite is the expensive half of the probe (38.9s
    # of the 60s a full run took on one 635-test Python suite) and everything above this line is
    # already an executed fact. What we did NOT do is recorded as `skipped_level`, never as a
    # suite that ran and found nothing.
    if level != "full":
        return skipped_from("test", "level_discover_only")
    test = plan.get("test")
    if test is None:
        record["phases"].append(_phase("test", "no_tests", "no_declared_test_command",
                                       "repository", "high"))
        record["phases"].append(_phase("coverage", "no_tests", "nothing_to_measure",
                                       "repository", "high"))
        return record
    argv, kind = test
    seconds = allowance()
    if not seconds:
        return skipped_from("test", "run_budget_exhausted")
    started = time.monotonic()
    tried.append(argv)
    rc, log, to = _run(argv, project.root, env, seconds)
    elapsed = time.monotonic() - started
    counts = _PARSERS[kind](log) if kind in _PARSERS else {}
    if not counts and plan.get("test_junit"):
        counts = _junit_counts(project.root, plan["test_junit"])
    if not counts and plan.get("test_fallback") and not to and allowance():
        # The instrumented invocation needed a plugin the project does not have. Re-run plain:
        # coverage is optional, the suite executing is not.
        argv, kind = plan["test_fallback"]
        tried.append(argv)
        rc, log, to = _run(argv, project.root, env, allowance())
        counts = _PARSERS[kind](log) if kind in _PARSERS else {}
    # A runner that reported counts and did not mention failures reported zero failures. Leaving
    # this null would make "no failures" indistinguishable from "we could not count".
    record["n_passed"] = counts.get("passed", 0) if counts else None
    record["n_failed"] = (counts.get("failed", 0) + counts.get("errored", 0)) if counts else None
    if record["n_tests_collected"] is None and counts.get("collected") is not None:
        record["n_tests_collected"] = counts["collected"]
    if to:
        record["phases"].append(_phase("test", "timed_out", "timeout", "runner", "high",
                                       elapsed, _display(argv)))
        return blocked_from("coverage", "test_timed_out")
    if not counts and rc == 5:
        record["phases"].append(_phase("test", "no_tests", "runner_collected_zero_tests",
                                       "repository", "high", elapsed, _display(argv)))
        record["phases"].append(_phase("coverage", "no_tests", "nothing_to_measure",
                                       "repository", "high"))
        return record
    if not counts and rc not in (0, 1):
        phase, cls, ecode = _fail_phase("test", log, to, project.root, _display(argv), elapsed)
        record["phases"].append(phase)
        if record["failure_class"] == "NONE":
            record["failure_class"], record["build_error_class"] = cls, ecode
            record["attribution"] = _attribution(cls, ecode)
        if wrong_runtime:
            _blame_runtime(record, phase)
        return blocked_from("coverage", "tests_did_not_run")
    # Passing is not required: a repository with failing tests still has a working harness, and a
    # harness is what makes work verifiable. `tests_failed` is a distinct reason code from
    # `no_tests` and both are distinct from a suite that could not run at all.
    failed = int(counts.get("failed") or 0) + int(counts.get("errored") or 0)
    record["phases"].append(_phase(
        "test", "passed", "tests_failed" if failed else "tests_passed", "repository", "high",
        elapsed, _display(argv), n_passed=counts.get("passed"), n_failed=failed))

    # --- phase 5: coverage ---------------------------------------------------------------
    coverage = plan.get("coverage")
    if coverage is None:
        record["phases"].append(_phase(
            "coverage", "unavailable",
            plan.get("coverage_reason") or "no coverage reporter for this ecosystem",
            "runner", "high"))
        return record
    kind, target = coverage
    pct, covered, total, method = _read_coverage(kind, target, env, project.root,
                                                 max(allowance(), MIN_PHASE_SECONDS))
    if pct is None:
        record["phases"].append(_phase("coverage", "unavailable",
                                       "reporter_produced_no_total", "runner", "medium"))
        return record
    record["coverage_pct"] = pct
    record["coverage_method"] = method
    record["phases"].append(_phase("coverage", "passed", "coverage_measured", "repository", "high",
                                   0.0, "", coverage_pct=pct, covered_lines=covered,
                                   total_lines=total))
    return record


# --- aggregation ----------------------------------------------------------------------------

def _phase_status(record: dict, name: str, after_repair: bool = False) -> str | None:
    """The status of `name`, AS SHIPPED unless `after_repair`.

    Once a verified repair lets the phase sequence continue, a project has two runs of the same
    phase names: the ones that describe the repository as it arrived, and the ones that describe
    it after we fixed its environment. Returning the wrong one silently overwrites the
    as-shipped verdict with a repaired one, which would make `--repair` improve every score by
    changing what is being measured rather than by measuring better.
    """
    for phase in record["phases"]:
        if phase["phase"] == name and bool(phase.get("after_repair")) == after_repair:
            return phase["status"]
    return None


def _mark_post_repair(record: dict) -> dict:
    """Flag every phase recorded after a verified repair took over. Applied once, centrally."""
    start = record.get("repaired_from_index")
    if start is not None:
        for phase in record["phases"][start:]:
            phase["after_repair"] = True
    return record


def _budget_skipped(record: dict) -> bool:
    """Did the run's clock, or the probe cap, stop this project rather than the repository?"""
    return any(p["status"] == "skipped_budget" for p in record["phases"])


# How a non-repository attribution reads in the one sentence that explains a null index. Plain
# English on purpose: the enum literals carry underscores, and an underscored token in an emitted
# prose field is indistinguishable from a leaked identifier to the audit that guards the boundary.
_ATTRIBUTION_PROSE = {
    "runner": "this runner",
    "external_service": "an external service",
    "credentials": "a credential the tree does not declare",
    "unknown": "a cause that could not be attributed",
}


def _suite_blocked_by_us(record: dict) -> str | None:
    """Who stopped this project's suite from executing, when the answer is not the repository.

    This is the distinction the whole index turns on. A suite that did not run because the build
    genuinely fails, or because the runner looked and the project declares no tests, is a MEASURED
    property of the repository and it is supposed to cost points. A suite that did not run because
    a language runtime is absent from this host, a registry was unreachable, or our own clock
    expired is a property of OUR machine, and scoring it against the codebase is the arithmetic
    that graded a repository which installed, built and passed 441 tests at 13.7 out of 100.

    Every phase already carries that answer in `attribution`, so this reads it rather than
    re-deriving it. Returns None when the suite ran or the repository owns the reason it did not,
    and the attribution otherwise.
    """
    for phase in record["phases"]:
        if phase["phase"] != "test":
            continue
        if phase["status"] == "passed":
            return None
        return None if phase["attribution"] == "repository" else phase["attribution"]
    return "unknown"


def _index_reason(level: str, skipped: bool, blocked_by: list[str],
                  build_ok: bool | None) -> str:
    """Why `observed_runnability` is null. Always one of OUR limits -- a repository's own failure
    produces a low index, not an absent one, and never reaches this function."""
    if level != "full":
        return "the suite was not executed at this build level, so the index has no executed terms"
    if skipped:
        return "part of the probe was skipped when the run's time budget was spent"
    if build_ok is None:
        return ("the build verdict could not be established across the whole tree, so the index "
                "would sum terms that were never observed")
    if blocked_by:
        return (f"no suite executed, for a reason attributed to "
                f"{_ATTRIBUTION_PROSE.get(blocked_by[0], 'a cause outside the repository')} "
                f"rather than to the repository, so its executed terms are absent, not zero")
    return "the index has no executed terms"


def _phase_status_effective(record: dict, name: str) -> str | None:
    """Post-repair where a repair took over, as-shipped everywhere else.

    The "best available" view of the tree: what it does once we have fixed the environment we
    were able to fix. A project nobody repaired contributes its as-shipped result unchanged, so
    this never invents an improvement that did not happen.
    """
    after = _phase_status(record, name, after_repair=True)
    return after if after is not None else _phase_status(record, name)


def _effective_records(records: list[dict]) -> list[dict]:
    """Copies in which a VERIFIED repair clears the failure it repaired.

    `_aggregate` reads `failure_class` and `attribution` as well as phase statuses, so the
    post-repair view needs both moved or a repaired project would keep counting as a
    repository-intrinsic failure and hold the whole verdict at False.
    """
    out = []
    for record in records:
        if record.get("repaired_from_index") is None:
            out.append(record)
            continue
        copy = dict(record)
        copy["failure_class"] = "NONE"
        copy["build_error_class"] = None
        copy["attribution"] = "repository"
        copy["skipped_reason"] = None
        out.append(copy)
    return out


def _aggregate(records: list[dict], discovery: dict, level: str = "full",
               status=_phase_status) -> dict:
    """The conservative tri-state roll-up.

    build_ok is TRUE only when every required project built, FALSE only on an explicit
    repository-intrinsic failure, and NULL where the runner was the limit or coverage of the
    tree was incomplete. A repository we could not measure must not be filed as one that failed:
    that is the survivorship bias the executed study found in the pipeline's own 93.4% build rate
    against a true 33.3%.

    Budget skips are treated exactly like an incomplete scan, and for the same reason: they are
    OUR limit. Anything the budget cut short makes the affected verdict NULL, and
    `observed_runnability` -- whose terms would otherwise silently become zeros -- is null
    whenever any phase anywhere was skipped by us rather than answered by the project.
    """
    required = [r for r in records if r["required"]]
    built = [r for r in required if status(r, "build") == "passed"]
    resolved = [r for r in required if status(r, "resolve") == "passed"]
    intrinsic = [r for r in required if r["failure_class"] == "REPO_INTRINSIC"]
    skipped = [r for r in required if _budget_skipped(r)]
    limited = [r for r in required if r not in built and (
        status(r, "resolve") in ("unavailable", "timed_out")
        or status(r, "build") in ("unavailable", "timed_out")
        or r in skipped
        or r["attribution"] in ("runner", "external_service"))]

    unreachable = discovery["unreachable_ecosystems"]
    unresolved_repo = [r for r in required if r not in resolved
                       and r["attribution"] in ("repository", "credentials")]
    if not required:
        install_ok = None
    elif len(resolved) == len(required):
        install_ok = True
    elif unresolved_repo:
        install_ok = False
    else:
        install_ok = None

    if not required:
        build_ok, reason = None, ("no project root was discovered; nothing was attempted"
                                  if discovery["no_manifest"] else
                                  "every discovered root was omitted by the scan: "
                                  + (", ".join(unreachable) or "reason unrecorded"))
    elif len(built) == len(required) and not unreachable and not skipped:
        build_ok, reason = True, f"all {len(required)} required projects built"
    elif len(built) == len(required) and not skipped:
        build_ok, reason = None, (
            f"all {len(required)} probed projects built, but the scan did not reach every "
            f"declared project ({', '.join(unreachable)}), so the tree is not fully covered")
    elif skipped and not intrinsic:
        build_ok, reason = None, (
            f"{len(built)} of {len(required)} required projects built and {len(skipped)} were "
            f"not reached before the run's time budget was spent, so no verdict is available")
    elif intrinsic:
        build_ok, reason = False, (
            f"{len(intrinsic)} of {len(required)} required projects failed with a "
            f"repository-intrinsic cause")
    elif limited or discovery["truncated"] or unreachable:
        build_ok, reason = None, (
            f"{len(built)} of {len(required)} required projects built and the rest were limited "
            f"by the runner or by an incomplete scan, so no verdict is available")
    else:
        build_ok, reason = None, (
            f"{len(built)} of {len(required)} required projects built for reasons that could "
            f"not be attributed")

    test_statuses = {status(r, "test") for r in required}
    n_passed = sum(int(r["n_passed"] or 0) for r in required)
    n_failed = sum(int(r["n_failed"] or 0) for r in required)
    ran = [r for r in required if status(r, "test") == "passed"]
    if ran:
        tests_status = "tests_failed" if n_failed else "tests_passed"
    elif required and test_statuses <= {"no_tests"}:
        tests_status = "no_tests"
    elif not required:
        tests_status = "unknown"
    elif required and test_statuses <= {"skipped_level", "skipped_budget", "no_tests"}:
        # Nobody ran a suite and nobody was going to: the level did not ask for one, or the clock
        # ran out. `tests_did_not_run` reads as an attempt that failed, and this was not one.
        tests_status = "not_attempted"
    else:
        tests_status = "tests_did_not_run"

    cov = [r for r in required if r["coverage_pct"] is not None]
    # A project with no suite has no coverage to expect, and one that never got there is a
    # coverage gap rather than a coverage expectation.
    expected = [r for r in required
                if status(r, "coverage") in ("passed", "unavailable")]
    coverage_pct = round(sum(r["coverage_pct"] for r in cov) / len(cov), 2) if cov else None

    tests_ran = bool(ran)
    # The index needs all four terms EXECUTED, and the whole question is who owns an absent term.
    #
    #   OURS -> NULL. A phase we chose not to run, a tree whose build verdict we could not
    #   establish, or a suite stopped by a missing runtime, an unreachable registry or our own
    #   clock contributes an ABSENCE. Summing absences as zeros is what graded a repository that
    #   installed, built and passed 441 tests at 13.7 out of 100.
    #
    #   THEIRS -> A LOW NUMBER. A build that genuinely fails, or a project the runner looked at
    #   and found no tests in, is a measured property of the codebase. It scores 0 or 1 and it is
    #   MEANT to: that is the signal, not a gap in the measurement.
    #
    # `attribution` already carries that answer on every phase, so this reads it rather than
    # guessing from a status code.
    #
    # `blocked_by` only decides the verdict when NO suite ran anywhere. Once one has, the terms
    # were observed and the index reports what was observed: on a polyglot tree whose Cargo suite
    # passes while a JS runner is absent from the host, "a suite ran" is a fact and nulling it
    # would throw away the measurement the probe exists to make.
    skipped_phases = any(p["status"] in _SKIPPED_STATUSES for r in required for p in r["phases"])
    blocked_by = sorted({a for a in (_suite_blocked_by_us(r) for r in required) if a})
    attempted_execution = (level == "full" and not skipped_phases and build_ok is not None
                           and (tests_ran or not blocked_by))
    index = ((int(build_ok is True) + int(tests_ran) + int(n_passed > 0)
              + int((coverage_pct or 0) > 0)) if attempted_execution else None)
    return {
        "build_ok": build_ok,
        "install_ok": install_ok,
        "build_ok_reason": reason,
        "build_level": level,
        "n_required": len(required),
        "n_resolved": len(resolved),
        "n_built": len(built),
        "n_intrinsic_failures": len(intrinsic),
        "n_runner_limited": len(limited),
        "n_budget_skipped": len(skipped),
        "tests_status": tests_status,
        "n_tests_collected": sum(int(r["n_tests_collected"] or 0) for r in required) or None,
        "n_passed": n_passed if level == "full" else None,
        "n_failed": n_failed if level == "full" else None,
        "coverage_pct": coverage_pct,
        "coverage_projects_measured": len(cov),
        "coverage_projects_expected": len(expected),
        "coverage_complete": bool(expected) and len(cov) == len(expected),
        "observed_runnability": index,
        # A 0 that means "we could not look" is not a 0 that means "nothing works", and the
        # index cannot carry that distinction on its own.
        "observed_runnability_complete": (index is not None and build_ok is not None
                                          and not discovery["truncated"] and not unreachable),
        "observed_runnability_reason": (
            None if index is not None
            else _index_reason(level, skipped_phases, blocked_by, build_ok)),
        # Who owned the absence, kept as data beside the prose so a consumer does not have to
        # parse a sentence to find out whether a null was theirs or ours.
        "observed_runnability_blocked_by": blocked_by,
        "unreachable_ecosystems": unreachable,
    }


# --- normalisation shared by both paths -----------------------------------------------------

def _slug(value, limit: int = 40) -> str | None:
    """A short ecosystem name. `toolchain` is exempt from the leak audit as an identifier field,
    so in agentic mode -- where the value is model-derived -- it is constrained by construction
    here instead. redact() cannot be used: it would turn `node-pnpm` into `[identifier]`."""
    if not isinstance(value, str):
        return None
    s = re.sub(r"[^A-Za-z0-9 ._+-]", "", value).strip()
    s = re.sub(r"\s+", "-", s)[:limit]
    return s or None


def _clean_commands(values) -> list[str]:
    """Commands are evidence, and they are also the one field that can carry a path -- a scratch
    location in either mode, plus whatever a model chose to echo in agentic mode. `emit.py` does
    not exempt `build_commands_tried`, so every entry goes through redact()."""
    out: list[str] = []
    for v in (values or []):
        if not isinstance(v, str) or not v.strip():
            continue
        cleaned, _ = redact(" ".join(v.split())[:200])
        if cleaned:
            out.append(cleaned)
        if len(out) >= MAX_COMMANDS:
            break
    return out


def _prose(value, fallback: str) -> str:
    """Model prose, redacted. `build_remediation_notes` and `coverage_unsupported_reason` used to
    be ours alone -- the first is still listed in emit's _EXEMPT_OWN_PROSE on that basis -- but in
    agentic mode a model writes them, so both are redacted here regardless of exemption. An
    exemption that outlives the reason for it is how a leak gets written to disk."""
    if not isinstance(value, str) or not value.strip():
        return fallback
    cleaned, _ = redact(" ".join(value.split())[:1200])
    return cleaned or fallback


def _skipped(note: str = "skipped by --no-build; runnability criteria unscored") -> dict:
    """The stub for a probe that never ran. Every runnability flag is absent, not false."""
    return {"probe": "build", "build_skipped": True, "ok": True, "note": note}


def skipped_budget(seconds_left: float = 0.0) -> dict:
    """The stub for a probe the run's clock never let start.

    Distinct prose from `--no-build`, because the two are different facts and the operator's
    remedy differs: one is a flag they chose, the other is a budget they can raise.
    """
    return _skipped(
        f"the run's time budget was spent before the build probe could start, with "
        f"{int(max(0.0, seconds_left))} seconds left; every runnability measurement is null")


def _finalise(out: dict, cls: str, install_ok: bool | None, build_ok: bool | None,
              discovered: bool, ran: bool | None, timed_out: bool, n_passed: int = 0,
              discover_measured: bool = True) -> dict:
    """Force the reported flags into a self-consistent set.

    Both paths funnel through here so that a model's answer and the table's answer cannot mean
    different things, and so that no combination the rubric would misread can escape: a build
    cannot be ok when the install was not, a suite cannot have run without being discovered, and
    a successful build cannot carry a failure class.

    `install_ok` and `build_ok` are TRI-STATE: None means the runner or an incomplete scan was
    the limit, not that the repository failed. That is CONTRACT.md extension point 2 -- "NULL
    where unmeasured", never a numeric zero -- and it is the load-bearing one here, because a
    lane that reports a failure it did not observe is indistinguishable from a repository that
    genuinely does not build. `emit_allowlist` already accepts null for a declared BOOL, so no
    field changes shape.

    `observed_runnability` is computed here, from the flags as finalised, so that the index and
    the flags a reader sees beside it can never disagree. Pass `ran=None` -- which the caller does
    whenever the suite was not attempted, at `--build discover` or when the clock ran out -- and
    both it and `observed_runnability` go NULL: the four terms were not measured, and a sum over
    unmeasured terms is a low band that reads as a repository that does not work.

    `discover_runnability` is the DISCOVER-LEVEL companion index and is computed here for the same
    reason: it must never disagree with the flags a reader sees beside it. It counts how many of
    {install_ok, build_ok, tests_discovered} are true -- 0..3 -- and every one of those three is
    EXECUTED at `--build discover`, which is what makes it the index a run that never ran a suite
    can still honestly report. It is NULL, never a lower count, when either tri-state term is null
    or when `discover_measured` is false: unmeasured is not zero here either.
    """
    if install_ok is False:
        build_ok = False
    elif build_ok is not None and install_ok is not None:
        build_ok = bool(build_ok)
    discovered = bool(discovered)
    # `is not False`, not `is True`: with several projects in one tree the build verdict can be
    # NULL because one project's toolchain is absent while another's suite demonstrably executed.
    # Requiring a positive build verdict here would report that suite as never having run.
    ran = None if ran is None else bool(ran and discovered and build_ok is not False)
    if build_ok is True and cls in ("REPO_INTRINSIC", "UNCLASSIFIED"):
        cls = "NONE"
    # A tree with nothing to build is NONE and stays NONE: there was no failure to classify.
    # Anything that was attempted and did not build has a cause, even an unrecognised one.
    if build_ok is False and cls == "NONE" and out.get("build_attempted"):
        cls = "UNCLASSIFIED"
    coverage = out.get("coverage_pct")
    out.update({
        "install_ok": install_ok,
        "build_ok": build_ok,
        "tests_discovered": discovered,
        "build_and_tests_ran": ran,
        "failure_class": cls,
        "repo_intrinsic_failure": cls == "REPO_INTRINSIC",
        "timed_out": bool(timed_out),
        # The pre-specified 0..4 index from the 417-repo study, emitted raw. Bands 0-3 are flat
        # (2.20 / 2.19 / 2.23 / 2.21 mean delivered tasks) and band 4 steps to 4.26 with the dud
        # rate falling from a 0.285 base to 0.053, so this is a threshold and smoothing it would
        # destroy the only thing it measures. NULL when its terms were not executed.
        "observed_runnability": (None if ran is None else
                                 int(build_ok is True) + int(ran) + int(n_passed > 0)
                                 + int((coverage or 0) > 0)),
        # The discover-level index, 0..3. MEASURED on the 278 TRAIN repositories: install_ok
        # rho +0.141 (2.80 against 2.15 mean delivered tasks), build_ok +0.131 (2.77 against
        # 2.18), tests_discovered +0.156 (2.69 against 1.88). Three individually modest signals,
        # each of them executed, and all three available without running a single test -- which is
        # why a run that fell back to `discover` still produces a comparable grade instead of an
        # unscored hole where 22 of the rubric's 100 points should be.
        "discover_runnability": (
            None if not discover_measured or install_ok is None or build_ok is None
            else int(bool(install_ok)) + int(bool(build_ok)) + int(discovered)),
        "ok": True,
    })
    return out


# A version-manager toolchain (rustup, pyenv, sdkman, nvm, volta) lives under the operator's HOME
# and `childenv` deliberately strips home-directory entries from the BUILD PATH, so such a
# toolchain is NOT reachable from a repository-controlled command and this module does not try to
# reach one. The consequence is honest and is the point: on a host whose only Rust came from
# rustup, a Rust project is recorded `unavailable / toolchain_missing / runner` -- named, and
# attributed to us -- rather than scored against the repository. Put the toolchain somewhere
# outside the operator's home if you want it measured.


def _scratch_env(scratch: Path) -> dict:
    """Every writable path an ecosystem might reach for, pointed outside the repository.

    GEM_HOME/BUNDLE_PATH stop Ruby writing system gem directories and COMPOSER_VENDOR_DIR keeps
    PHP's vendor tree out of the checkout -- failures that would be ours while reading as theirs.
    """
    # BUILD trust domain: this environment is handed to the REPOSITORY'S OWN install, build and
    # test commands -- `npm ci` postinstall hooks, `setup.py`, gradle plugins. It therefore starts
    # from nothing and receives no cloud, VCS, database, registry, SSH or provider credential, and
    # a scratch HOME so none of the operator's dotfile credentials are reachable either.
    env = childenv.build_env(childenv.BUILD, home=scratch)
    env.update({
        "GEM_HOME": str(scratch / "gems"), "BUNDLE_PATH": str(scratch / "gems"),
        "PIP_CACHE_DIR": str(scratch / "pip"), "npm_config_cache": str(scratch / "npm"),
        "YARN_CACHE_FOLDER": str(scratch / "yarn"), "PNPM_HOME": str(scratch / "pnpm"),
        "npm_config_store_dir": str(scratch / "pnpm-store"),
        "GOPATH": str(scratch / "go"), "GOMODCACHE": str(scratch / "go" / "pkg" / "mod"),
        "GOFLAGS": "-mod=mod", "CARGO_HOME": str(scratch / "cargo"),
        "COMPOSER_HOME": str(scratch / "composer"),
        "COMPOSER_VENDOR_DIR": str(scratch / "vendor"),
        "COVERAGE_FILE": str(scratch / ".coverage"),
        "GRADLE_USER_HOME": str(scratch / "gradle"),
        "NUGET_PACKAGES": str(scratch / "nuget"),
        "CI": "1", "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1", "npm_config_fund": "false",
        "npm_config_audit": "false", "npm_config_update_notifier": "false",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_ROOT_USER_ACTION": "ignore",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
    })
    return env


# The keys the post-repair view contributes. Deliberately short: these exist so an ACCEPTANCE
# decision can ask "does it build once we have set the environment up" without the as-shipped
# measurement having to lie about what arrived. Everything else stays as-shipped only.
# Only keys `_aggregate` actually returns. `build_and_tests_ran` and `tests_discovered` are
# built later by `_finalise`, so asking for them here yielded a null that read as "we repaired it
# and the tests still did not run" -- a fabricated negative, which is the one thing this file is
# least allowed to produce.
_REPAIRED_KEYS = ("install_ok", "build_ok", "observed_runnability", "observed_runnability_reason",
                  "tests_status", "n_passed", "n_failed", "coverage_pct")


def _repaired_aggregate(records: list[dict], discovery: dict, level: str) -> dict:
    """The same roll-up, over the tree AFTER verified environment repair.

    Null throughout when no project was repaired -- which is the honest answer, and stops a
    consumer reading "after repair it builds" off a run where nothing was repaired at all.
    """
    repaired = [r for r in records if r.get("repaired_from_index") is not None]
    if not repaired:
        return {f"{key}_after_repair": None for key in _REPAIRED_KEYS} | {
            "n_projects_repaired": 0,
            "after_repair_reason": "no project was repaired, so this view is the as-shipped one",
        }
    full = _aggregate(_effective_records(records), discovery, level,
                      status=_phase_status_effective)
    out = {f"{key}_after_repair": full.get(key) for key in _REPAIRED_KEYS}
    out["n_projects_repaired"] = len(repaired)
    out["after_repair_reason"] = (
        f"{len(repaired)} project(s) resolved or built only after a repair whose success was "
        f"verified by re-running the failing command; the unsuffixed keys describe the "
        f"repository as it arrived")
    return out


# --- the repair pass ---------------------------------------------------------------------------
#
# The deterministic probe answers "does this build HERE, AS SHIPPED". That is the right
# measurement and it stays the measurement. But a large share of the 417-repo corpus failed on
# something an engineer fixes in one step and never writes down -- a system library the README
# assumes, a lockfile that needs `--legacy-peer-deps`, a native extension wanting a header
# package. Scoring those as unbuildable measures how close the repository is to OUR default image,
# which is not a property of the repository at all.
#
# So: when a phase fails and the failure is attributed to the REPOSITORY, an agent gets one
# bounded turn to make the environment work, and then WE RE-RUN THE SAME COMMAND. The re-run is
# the evidence. The model never reports the outcome and is never asked to; every claim a model
# could make here is one we can check in a subprocess, so we check it.
#
# THREE RULES:
#
#   * The unrepaired verdict is never overwritten. `install_ok` / `build_ok` /
#     `observed_runnability` continue to describe the repository as shipped. The repair result
#     rides alongside in `repair`, so a consumer chooses which question it is asking, and a
#     rubric can price "builds as shipped" and "builds after one step" differently.
#   * We only ask when the failure is THEIRS to fix. A `runner` failure is our clock and an
#     `external_service` failure is a registry outage; handing either to an agent buys a
#     confident-sounding turn against a problem it cannot touch.
#   * The agent may not edit the repository's source or its tests. It changes the ENVIRONMENT.
#     An agent that fixes a failing build by deleting the failing target has produced exactly the
#     measurement we must never take, so the diff is counted by `git status` afterwards and a
#     turn that touched tracked source is recorded as a FAILED repair whatever the re-run said.

REPAIR_PROMPT = """A build failed in this checkout and your job is to make the environment able to \
run it. Work only inside {root}.

The command that failed:
    {command}

Its last output:
{log}

Install what is missing and adjust environment configuration so that command can succeed. You may \
install system and language packages, add configuration the project expects, and create files the \
project's own documentation says to create.

You MUST NOT edit any file already tracked by git, and you MUST NOT change, skip, delete or \
weaken any source file, test or build target. Making the command pass by removing what it \
checks is a failure, not a fix. If the failure cannot be resolved by environment changes alone, \
stop and change nothing.

Do not explain the result. We re-run the command ourselves and measure the outcome."""

REPAIR_MAX_LOG = 4000
REPAIR_MIN_SECONDS = 120


def _tracked_source_touched(root: Path, env: dict, timeout: int) -> int | None:
    """How many git-TRACKED files the turn modified. None when the tree is not a git checkout.

    This is the guard that makes "it may not edit the source" checkable rather than merely
    instructed. Untracked additions are what a legitimate repair looks like -- a virtualenv, a
    config file the README says to write -- so only modifications to tracked paths count.
    """
    rc, out, _ = _run(["git", "status", "--porcelain", "--untracked-files=no"], root, env,
                      max(15, min(timeout, 60)))
    if rc != 0:
        return None
    return sum(1 for line in out.splitlines() if line.strip())


def _repair_turn(root: Path, scratch: Path, env: dict, command: list[str], log: str,
                 provider: str, model: str, seconds: int) -> dict:
    """One bounded provider turn against a failing command. Returns what it did, never a verdict."""
    executable = _which(provider, env) or _which(provider)
    if not executable:
        return {"ran": False, "reason_code": "provider_cli_not_found"}
    try:
        isolation = childenv.isolation_flags(provider, executable)
    except childenv.ProviderNotIsolated:
        # A CLI we cannot isolate would honour the measured tree's own agent configuration, which
        # is the remote-code-execution path this codebase closed. No repair is worth reopening it.
        return {"ran": False, "reason_code": "provider_cli_not_isolatable"}
    prompt = REPAIR_PROMPT.format(root=root, command=_display(command),
                                  log=log[-REPAIR_MAX_LOG:])
    cmd = [executable, "-p", prompt, "--output-format", "json", "--add-dir", str(root),
           "--model", model, "--allowedTools", "Read Grep Glob Bash Write Edit", *isolation]
    # THE ONE PLACE THE TWO TRUST DOMAINS MEET, and the reason --repair is opt-in and off by
    # default. The agent needs the MODEL domain because a CLI with no provider credential exits in
    # under a second having done nothing -- which is exactly how this pass failed its first live
    # test, silently, while reporting a completed turn. So the turn runs under MODEL: the provider's
    # own auth variables, a scratch HOME, and nothing else -- no cloud, VCS, database, registry or
    # SSH credential. The RESIDUAL EXPOSURE IS REAL AND IS NOT CLOSED HERE: commands the agent
    # chooses to run inherit its environment, so repository-adjacent code can see the provider
    # credential for the length of the turn. Everything WE run -- the failing command, the re-run
    # that produces the verdict -- keeps using the BUILD environment and never sees it.
    turn_env = childenv.build_env(childenv.MODEL, home=scratch, provider=provider)
    started = time.monotonic()
    try:
        # Authentication is the subprocess's own business: nothing here reads, logs or stores a key.
        p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, errors="replace",
                           timeout=seconds, env=turn_env, start_new_session=True)
    except subprocess.TimeoutExpired:
        return {"ran": True, "reason_code": "repair_turn_timed_out",
                "seconds": round(time.monotonic() - started, 1)}
    except OSError:
        return {"ran": False, "reason_code": "provider_cli_failed_to_start"}
    elapsed = round(time.monotonic() - started, 1)
    if p.returncode != 0:
        # A CLI that refused is OUR problem -- unauthenticated, rate limited, model unavailable --
        # and must not be recorded as an agent that tried and could not fix the repository.
        return {"ran": False, "reason_code": "provider_cli_refused", "seconds": elapsed}
    return {"ran": True, "reason_code": "repair_turn_completed", "seconds": elapsed}


def _repair_phase(record: dict, phase_name: str, project: Project, scratch: Path, env: dict,
                  command: list[str], log: str, allowance, provider: str, model: str) -> None:
    """Try once to make `command` work, then RE-RUN it and record what actually happened.

    Writes `record["repair"]`. Never touches `record["phases"]`, `failure_class`, `attribution`
    or any runnability term: the as-shipped verdict is the measurement and this is an annotation
    on it.
    """
    # NOT gated on `attribution`. That field answers "who should be BLAMED", and its standing
    # policy sends every UNCLASSIFIED failure to `runner` so an ambiguous log never costs a
    # repository ten points. Correct for scoring, wrong here: UNCLASSIFIED and ENVIRONMENT are
    # precisely the cases worth an agent turn -- a missing system library IS an ENVIRONMENT
    # failure, and it is the single thing this pass exists to fix. Gating on blame refused every
    # candidate on the first fixture that had one.
    #
    # What is refused is what no agent can move: a credential we did not supply, a registry that
    # is down, and our own clock running out.
    error_class = record.get("build_error_class") or ""
    failure_class = record.get("failure_class") or ""
    if error_class in _CREDENTIAL_CLASSES:
        refusal = "no_credential_to_supply"
    elif error_class in _EXTERNAL_CLASSES:
        refusal = "external_service_unreachable"
    elif failure_class == "TIMEOUT":
        refusal = "failure_was_our_own_time_limit"
    else:
        refusal = None
    if refusal:
        record["repair"] = {"attempted": False, "phase": phase_name, "ok": None,
                            "reason_code": refusal}
        return
    seconds = allowance()
    if seconds < REPAIR_MIN_SECONDS:
        record["repair"] = {"attempted": False, "phase": phase_name, "ok": None,
                            "reason_code": "run_budget_exhausted"}
        return

    before = _tracked_source_touched(project.root, env, seconds)
    turn = _repair_turn(project.root, scratch, env, command, log, provider, model,
                        max(REPAIR_MIN_SECONDS, int(seconds * 0.6)))
    out = {"attempted": bool(turn["ran"]), "phase": phase_name, "ok": False,
           "reason_code": turn["reason_code"], "turn_seconds": turn.get("seconds"),
           "provider": provider, "model": model,
           "tracked_files_modified": None, "verified_by_rerun": False}
    if not turn["ran"]:
        out["ok"] = None
        record["repair"] = out
        return

    after = _tracked_source_touched(project.root, env, allowance() or 60)
    if before is not None and after is not None:
        out["tracked_files_modified"] = max(0, after - before)
        if out["tracked_files_modified"] > 0:
            # It changed files the repository ships. Whatever the re-run says, this is not a
            # measurement of the repository -- it is a measurement of the agent's edit.
            out["reason_code"] = "repair_modified_tracked_source"
            record["repair"] = out
            return

    seconds = allowance()
    if not seconds:
        out["reason_code"] = "run_budget_exhausted_before_rerun"
        record["repair"] = out
        return
    rc, rlog, timed_out = _run(command, project.root, env, seconds)
    out["verified_by_rerun"] = True
    out["ok"] = rc == 0
    out["reason_code"] = ("repair_verified" if rc == 0
                          else _error_class(rlog, timed_out))
    record["repair"] = out


def _repair_summary(records: list[dict], repair: dict | None) -> dict:
    """The tree-level roll-up of the repair pass, as counts and one enum.

    `repair_offered` distinguishes the three states that would otherwise all read as zero: the
    pass was not asked for, it was asked for and no project needed it, or it was asked for and
    every candidate was refused for a stated reason.
    """
    if not repair:
        return {"repair_offered": False, "repair_attempted_n": None, "repair_succeeded_n": None,
                "repair_refused_n": None, "repair_rejected_source_edit_n": None,
                "repair_seconds": None}
    blocks = [r["repair"] for r in records if isinstance(r.get("repair"), dict)]
    attempted = [b for b in blocks if b.get("attempted")]
    return {
        "repair_offered": True,
        "repair_candidates_n": len(blocks),
        "repair_attempted_n": len(attempted),
        "repair_succeeded_n": sum(1 for b in attempted if b.get("ok") is True),
        "repair_refused_n": sum(1 for b in blocks if not b.get("attempted")),
        "repair_rejected_source_edit_n": sum(
            1 for b in attempted if b.get("reason_code") == "repair_modified_tracked_source"),
        "repair_seconds": round(sum(float(b.get("turn_seconds") or 0) for b in attempted), 1),
    }


# --- the agentic path -----------------------------------------------------------------------

def _agentic_projects(payload: dict, cls: str, install_ok: bool, build_ok: bool,
                      discovered: bool, ran: bool) -> dict:
    """The agent reports one verdict for the whole tree, so its evidence is one project.

    Written into the same structure as the deterministic path's so a consumer never has to know
    which path produced a record -- but `attribution` is `unknown` unless the model's own
    failure_class settles it, because a judged opinion is not an observation and the study found
    judged buildability correlates only rho=0.50 with an executed build.
    """
    ecode = "unclassified" if cls in ("REPO_INTRINSIC", "UNCLASSIFIED") else (
        "timeout" if cls == "TIMEOUT" else None)
    project = {
        "project_id": "p1", "relative_root": ".",
        "ecosystem": _slug(payload.get("toolchain")) or "unknown",
        "toolchain": _slug(payload.get("toolchain")),
        "workspace": False, "members": [], "required": True,
        "phases": [
            _phase("resolve", "passed" if install_ok else "failed",
                   "reported_by_agent", _attribution(cls, ecode or ""), "low"),
            _phase("build", "passed" if build_ok else "blocked",
                   "reported_by_agent", _attribution(cls, ecode or ""), "low"),
            _phase("discover", "passed" if discovered else "no_tests",
                   "reported_by_agent", _attribution(cls, ecode or ""), "low"),
            _phase("test", "passed" if ran else ("no_tests" if not discovered else "failed"),
                   "reported_by_agent", _attribution(cls, ecode or ""), "low"),
        ],
        "n_tests_collected": None,
        "n_passed": payload.get("n_passed") if isinstance(payload.get("n_passed"), int) else None,
        "n_failed": None,
        "coverage_pct": None, "coverage_method": None,
        "failure_class": cls, "build_error_class": ecode,
        "attribution": _attribution(cls, ecode or ""), "relaxed_retry": None,
    }
    return {
        "projects": [project],
        "discovery": {"dirs_scanned": 0, "n_projects": 1, "omitted_roots": [],
                      "ambiguous_roots": [], "truncated": False, "no_manifest": False,
                      "note": "the agentic path does not enumerate project roots; it reports "
                              "one verdict for the tree"},
        "aggregate": {
            "build_ok": bool(build_ok), "build_ok_reason": "reported by the agentic probe",
            "n_required": 1, "n_resolved": int(install_ok), "n_built": int(build_ok),
            "n_intrinsic_failures": int(cls == "REPO_INTRINSIC"),
            "n_runner_limited": int(cls in ("ENVIRONMENT", "TIMEOUT")),
            "tests_status": ("tests_passed" if ran else
                             ("no_tests" if not discovered else "tests_did_not_run")),
            "n_tests_collected": None, "n_passed": project["n_passed"] or 0, "n_failed": 0,
            "coverage_pct": None, "coverage_projects_measured": 0,
            "coverage_projects_expected": 1, "coverage_complete": False,
            "observed_runnability": 0, "observed_runnability_complete": False,
            "build_level": "full", "n_budget_skipped": 0,
        },
        "counts": {"passed": int(build_ok), "failed": int(not build_ok), "no_tests": 0,
                   "unavailable": 0, "timed_out": int(cls == "TIMEOUT"), "blocked": 0},
    }


def _agentic(repo: Path, scratch: Path, timeout: int, model: str) -> dict | None:
    """Drive `claude -p` through the install and the test run. None means fall back.

    A returned dict is always a finished result, including the timeout case: once the budget is
    spent there is nothing left to run a fallback with, and TIMEOUT is a legitimate answer that
    the rubric already handles without penalising the repository.
    """
    prompt = PROMPT.format(repo=repo, scratch=scratch, budget=int(timeout * 0.85),
                           per_cmd=max(60, timeout // 3), max_cmds=MAX_COMMANDS)
    # `Bash` stays here and only here: this lane exists to run the project's own install and test
    # commands, which is the whole point of `--build` and is disclosed as such. The isolation
    # flags are still required -- running the repository's build is a decision the operator made,
    # running whatever its `.claude/settings.json` says is not, and a hook fires before any build
    # command does. A CLI that cannot be isolated falls back to the deterministic probe.
    try:
        isolation = childenv.isolation_flags("claude", "claude")
    except childenv.ProviderNotIsolated:
        return None
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--add-dir", str(repo), "--add-dir", str(scratch), "--model", model,
           "--allowedTools", "Read Grep Glob Bash", *isolation]
    try:
        # Authentication is the subprocess's own business: nothing here reads, logs or stores a key.
        p = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True,
                           errors="replace", timeout=timeout, env=_scratch_env(scratch))
    except subprocess.TimeoutExpired:
        out = {"probe": "build", "build_probe_mode": "agentic", "build_probe_model": model,
               "build_attempted": True, "toolchain": None, "build_commands_tried": [],
               "coverage_pct": None,
               "coverage_unsupported_reason": "the build did not finish inside the time limit",
               "build_remediation_effort": "unknown", "build_error_class": "timeout"}
        _, notes = _remediation("TIMEOUT", "", None)
        out["build_remediation_notes"] = notes
        out["build_projects"] = _agentic_projects({}, "TIMEOUT", False, False, False, False)
        return _finalise(out, "TIMEOUT", False, False, False, False, True)
    except OSError:
        return None
    if p.returncode != 0:
        return None

    payload = _extract_json(p.stdout)
    if not isinstance(payload, dict):
        return None

    out: dict = {"probe": "build", "build_probe_mode": "agentic", "build_probe_model": model,
                 "toolchain": _slug(payload.get("toolchain"))}
    cmds = _clean_commands(payload.get("commands_tried"))
    out["build_commands_tried"] = cmds
    # "Attempted" means something was actually executed. A tree with nothing declaring a build is
    # the same finding the old table reported when it recognised no marker.
    attempted = bool(cmds) and payload.get("build_definition_found") is not False
    out["build_attempted"] = attempted

    cls = str(payload.get("failure_class") or "").upper()
    if cls not in FAILURE_CLASSES:
        cls = "UNCLASSIFIED"
    install_ok = bool(payload.get("install_ok"))
    if not attempted:
        cls, install_ok = "NONE", False

    pct = payload.get("coverage_pct")
    try:
        pct = None if pct is None else max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        pct = None
    out["coverage_pct"] = pct
    if pct is None:
        out["coverage_unsupported_reason"] = _prose(
            payload.get("coverage_unsupported_reason"),
            "no coverage reporter produced a parseable total")
    else:
        out["coverage_method"] = _slug(payload.get("coverage_method"), 60) or "reported by probe"

    eff = str(payload.get("remediation_effort") or "").lower()
    if eff not in EFFORTS:
        eff = "unknown"
    default_eff, default_notes = _remediation(cls, "", out["toolchain"])
    if not attempted:
        eff = "unknown"
        default_notes = ("No recognised build definition was found, so there is nothing to "
                         "install or run. Adding one is a prerequisite to any further assessment.")
    elif cls in ("NONE", "ENVIRONMENT", "TIMEOUT"):
        # These three are determined by the classification, not by taste, so the module's own
        # wording is used and the model's effort estimate is overridden.
        eff = default_eff
    out["build_remediation_effort"] = eff
    out["build_remediation_notes"] = _prose(payload.get("remediation_notes"), default_notes)
    out["build_error_class"] = ("no_manifest" if not attempted else
                                (None if cls == "NONE" else _error_class("", cls == "TIMEOUT")))

    build_ok = bool(payload.get("build_ok"))
    discovered = bool(payload.get("tests_discovered"))
    ran = bool(payload.get("tests_ran"))
    n_passed = payload.get("n_passed")
    n_passed = n_passed if isinstance(n_passed, int) and n_passed >= 0 else 0
    projects = _agentic_projects(payload, cls, install_ok, build_ok, discovered, ran)
    out["build_projects"] = projects
    # The agent picks its own runtimes, so there is nothing for resolution to report. The keys are
    # still present and honest about that, rather than absent and read as "we did not check".
    out.update(runtime.summarise([]))
    out["runtime_resolution_note"] = ("the agentic probe selects its own runtimes, so no version "
                                      "was resolved for it")
    result = _finalise(out, cls, install_ok, build_ok, discovered, ran, cls == "TIMEOUT", n_passed)
    projects["aggregate"]["observed_runnability"] = result["observed_runnability"]
    projects["aggregate"]["coverage_pct"] = pct
    return result


# --- the deterministic path -----------------------------------------------------------------

# Ecosystems whose plan can produce a coverage number at all -- the rest set `coverage = None` by
# construction because the toolchain ships no reporter we can assume. `coverage_pct` is the
# strongest single executed signal in the 417-repo corpus (rho +0.315) and only 50 of 278 training
# repositories ever reached one, so among projects of similar size the one that CAN produce it is
# worth the budget. The factor is small on purpose: it breaks near-ties, it does not put a
# three-file Python helper ahead of the Rust workspace that is the repository.
_COVERAGE_CAPABLE = frozenset({"python", "node", "go"})
_COVERAGE_WEIGHT_BONUS = 1.25


def _allocate(projects: list[Project], max_projects: int
              ) -> tuple[list[Project], list[Project]]:
    """Which projects get probed, in which order. Largest first, the rest reported as skipped.

    Ordering by size is most of the allocation policy in one line: the budget buys more
    measurement in the project that holds most of the repository than in a two-file tooling
    directory that happens to sort first. Workspace roots come first within a size tier because
    one command there covers their members, and a project that can yield coverage gets a small
    thumb on the scale for the reason above.
    """
    def rank(project: Project) -> tuple:
        weight = project.weight * (_COVERAGE_WEIGHT_BONUS
                                   if project.ecosystem in _COVERAGE_CAPABLE else 1.0)
        return (-weight, not project.workspace, project.rel)

    ordered = sorted(projects, key=rank)
    cap = max(1, max_projects)
    return ordered[:cap], ordered[cap:]


def _deterministic(repo: Path, scratch: Path, budget: Budget, reason: str | None,
                   restore: list[tuple[Path, int]], level: str, max_projects: int,
                   allow_home: bool = False, repair: dict | None = None) -> dict:
    """The corrected table, over every project in the tree.

    Less capable than the agent by design -- it knows nine ecosystems and it does not read a
    README -- but it never fabricates a command, it always terminates, and unlike the agent it
    OBSERVES rather than judges.

    THE ALLOCATION. Projects are probed largest first. Before each one, its share of what is LEFT
    is set in proportion to its size against the sizes of the projects still to come, so a
    project that fails at resolve, or finishes early, hands the rest of its share to the ones
    behind it rather than burning it. When the allowance is gone the remaining projects are
    recorded `skipped_budget` with null measurements -- which is also why the roll-up above nulls
    `build_ok` and `observed_runnability` in that case rather than reporting what half a tree did.
    """
    out: dict = {"probe": "build", "build_probe_mode": "deterministic"}
    if reason:
        out["agentic_fallback_reason"] = reason
    env = _scratch_env(scratch)

    projects, discovery = discover_projects(repo)
    probe_list, over_cap = _allocate(projects, max_projects)
    tried: list[list[str]] = []
    records: list[dict] = []
    pending = list(probe_list)
    while pending:
        project = pending.pop(0)
        if budget.exhausted():
            records.append(_skipped_record(project, "run_budget_exhausted"))
            continue
        weight_left = project.weight + sum(p.weight for p in pending)
        share = budget.remaining() * (project.weight / max(1, weight_left))
        records.append(_mark_post_repair(_probe_project(
            project, repo, scratch, env, budget,
            time.monotonic() + max(share, min(MIN_PROJECT_SECONDS, budget.remaining())),
            level, restore, tried, allow_home, repair)))
    records.extend(_skipped_record(p, "project_cap_reached") for p in over_cap)
    discovery["n_projects_probed"] = len(probe_list)
    discovery["n_projects_over_cap"] = len(over_cap)
    aggregate = _aggregate(records, discovery, level)
    aggregate.update(_repaired_aggregate(records, discovery, level))
    aggregate["budget_seconds"] = int(budget.total)
    aggregate["budget_spent_seconds"] = round(budget.elapsed(), 1)

    counts = {status: 0 for status in STATUSES}
    for record in records:
        status = _phase_status(record, "build") or _phase_status(record, "resolve") or "blocked"
        counts[status] = counts.get(status, 0) + 1
    out["build_projects"] = {"projects": records, "discovery": discovery,
                             "aggregate": aggregate, "counts": counts}
    # Promoted out of the per-project evidence, because "we ran the wrong interpreter" is the one
    # thing a reader needs in order to know whether a failure below is the repository's at all.
    out.update(runtime.summarise([r["runtime"] for r in records]))
    out.update(_repair_summary(records, repair))

    if not projects:
        # The distinction the executed run could not draw: a tree with no manifest versus a scan
        # that gave up. `no_manifest` is now only claimed when discovery reached everything.
        error_class = "no_manifest" if discovery["no_manifest"] else "discovery_incomplete"
        out.update({
            "toolchain": None, "build_attempted": False, "build_commands_tried": [],
            "coverage_pct": None, "build_error_class": error_class,
            "coverage_unsupported_reason": "no build definition was found to instrument",
            "build_remediation_effort": "unknown",
            "build_remediation_notes": (
                "No recognised build definition was found, so there is nothing to install or "
                "run. Adding one is a prerequisite to any further assessment."),
            "run_budget_exhausted": False,
            "observed_runnability_reason": (
                None if discovery["no_manifest"] else
                "the project scan did not reach every declared root, so nothing was executed"),
        })
        # `no_manifest` is a measured property of the tree, so both its indices are a real 0. A
        # scan that gave up measured nothing, so both are null.
        return _finalise(out, "NONE", False, False, False,
                         False if discovery["no_manifest"] else None, False,
                         discover_measured=discovery["no_manifest"])

    primary = next((r for r in records if _phase_status(r, "build") == "passed"), records[0])
    out["toolchain"] = primary["toolchain"]
    out["build_commands_tried"] = _clean_commands([_display(c) for c in tried])
    out["build_attempted"] = bool(tried)

    failing = next((r for r in records if r["failure_class"] == "REPO_INTRINSIC"), None) \
        or next((r for r in records if r["failure_class"] not in ("NONE",)), None)
    cls = failing["failure_class"] if failing else "NONE"
    out["build_error_class"] = failing["build_error_class"] if failing else None

    out["coverage_pct"] = aggregate["coverage_pct"]
    if aggregate["coverage_pct"] is None:
        out["coverage_unsupported_reason"] = _coverage_reason(records, aggregate)
    else:
        out["coverage_method"] = (primary["coverage_method"]
                                  or next((r["coverage_method"] for r in records
                                           if r["coverage_method"]), "reported by probe"))
        if not aggregate["coverage_complete"]:
            out["coverage_method"] += (
                f" ({aggregate['coverage_projects_measured']} of "
                f"{aggregate['coverage_projects_expected']} projects)")

    eff, notes = _remediation(cls, out["build_error_class"] or "", out["toolchain"])
    out["build_remediation_effort"] = eff
    out["build_remediation_notes"] = notes

    install_ok = aggregate["install_ok"]
    build_ok = aggregate["build_ok"]
    # Discovery, not execution: a suite that was found and then failed to run is still a suite
    # that exists, and A3 pays for the suite existing. The second clause is the converse and is
    # equally required: where the ecosystem has no native enumeration (Maven, a plain `npm test`
    # script) the runner still tells us how many tests it executed, and a suite we watched run
    # is not one we failed to discover.
    discovered = any(
        _phase_status(r, "discover") == "passed"
        or (_phase_status(r, "test") == "passed" and (r["n_tests_collected"] or 0) > 0)
        for r in records)
    # NULL, not False, when the suite was never attempted: `--build discover` did not ask for one
    # and a spent budget did not allow one. `_finalise` propagates that null into the index.
    ran = (None if aggregate["observed_runnability"] is None
           else aggregate["tests_status"] in ("tests_passed", "tests_failed"))
    timed_out = any(p["status"] == "timed_out" for r in records for p in r["phases"])
    # Say which of the two limits stopped us, and how many roots it stopped. A cap nobody can see
    # in the output reads as "we looked and found nothing".
    capped = sum(1 for r in records if r["skipped_reason"] == "project_cap_reached")
    out_of_clock = aggregate["n_budget_skipped"] - capped
    # Promoted out of the (externally stripped) per-project evidence and into the block every
    # consumer reads. `run_budget_exhausted` is the one fact a caller needs to decide whether a
    # cheaper level would measure MORE of this tree, and a cap on how many roots we probe would
    # not, so the two causes are kept apart here as well as in the note below.
    out["run_budget_exhausted"] = bool(out_of_clock)
    out["observed_runnability_reason"] = aggregate["observed_runnability_reason"]
    if aggregate["n_budget_skipped"]:
        causes = []
        if out_of_clock:
            causes.append(f"{out_of_clock} when the run's time budget was spent")
        if capped:
            causes.append(f"{capped} past the limit on how many roots may be probed")
        out["note"] = (
            f"{aggregate['n_budget_skipped']} of {aggregate['n_required']} project roots were not "
            f"measured, " + " and ".join(causes) + "; their measurements are null, not zero")
    elif level != "full":
        out["note"] = ("dependencies were installed and the test runner was asked to list its "
                       "tests; the suite itself was not executed at this build level")
    result = _finalise(out, cls, install_ok, build_ok, discovered, ran, timed_out,
                       aggregate["n_passed"] or 0)
    aggregate["observed_runnability"] = result["observed_runnability"]
    return result


def _coverage_reason(records: list[dict], aggregate: dict) -> str:
    """Why there is no coverage number. Specific, because "not measurable" is not a finding."""
    if aggregate["tests_status"] == "no_tests":
        return "the projects declare no tests to instrument"
    if aggregate["tests_status"] == "not_attempted":
        return ("the suite was not executed, so there was nothing to instrument; run the probe "
                "at the full build level for coverage")
    if aggregate["tests_status"] == "tests_did_not_run":
        return "no test suite executed, so there was nothing to instrument"
    for record in records:
        for phase in record["phases"]:
            if phase["phase"] == "coverage" and phase["status"] == "unavailable":
                return phase["reason_code"]
    return "no coverage reporter produced a parseable total"


# --- entry point ----------------------------------------------------------------------------

def _strip_detail(result: dict, detail: bool) -> dict:
    """Drop the evidence keys unless the caller asked for them.

    Stripped at the very end rather than never computed, so both copies of this module run the
    same code and the frozen record is a projection of the detailed one rather than a different
    measurement. The tri-state flags survive the strip -- they are the finding, not the detail.
    """
    if detail:
        return result
    for key in _DETAIL_KEYS:
        result.pop(key, None)
    return result


def collect(repo: Path, timeout: int = DEFAULT_PHASE_TIMEOUT, skip: bool = False,
            model: str = DEFAULT_MODEL, agentic: bool = True,
            detail: bool = _DETAIL_DEFAULT, level: str = DEFAULT_LEVEL,
            budget_seconds: float = DEFAULT_BUDGET_SECONDS,
            max_projects: int = MAX_PROBED_PROJECTS,
            allow_home_toolchains: bool = False,
            repair: bool = False, repair_provider: str = "claude",
            repair_model: str | None = None) -> dict:
    """Install this repository's dependencies, run its tests, and report what happened.

    `budget_seconds` bounds the WHOLE probe and `timeout` bounds one command inside it. Both are
    real, and the first is the one that makes the run predictable: with `timeout` alone, 24
    project roots times five phases times 900 seconds is a worst case measured in days.

    `level` is "none" (run nothing), "discover" (resolve, install, build, list the tests) or
    "full" (also execute the suite and read coverage). Anything other than "full" also forces the
    deterministic path: the levels are properties of the deterministic plan and an agent decides
    its own phases.

    `max_projects` caps how many discovered roots are probed, largest first. The rest are reported
    `skipped_budget` with reason `project_cap_reached`, never dropped in silence.

    `detail` adds `build_projects` (per-project phase evidence) and `build_error_class` to the
    returned dict. Off by default in the external copy so the emitted record stays exactly the
    frozen shape; see `_DETAIL_DEFAULT`.

    `agentic=False` forces the deterministic table and never looks for `claude` on PATH -- the
    contract for a no-LLM caller (the measure skills) that must not spend money or vary by host.
    It is the same code path an absent `claude` produces, so the fallback stays regression-tested.

    `allow_home_toolchains` lets runtime resolution select an interpreter installed under the
    operator's HOME -- nvm, pyenv, asdf, mise, uv, sdkman all live there and between them hold most
    of the version coverage a real corpus needs. FALSE by default and it must stay false on a
    machine we do not own: `childenv` strips home entries from the BUILD PATH precisely so that a
    repository's own postinstall hook cannot see inside somebody's home directory, and selecting a
    runtime from there hands it one. measure-int runs in a disposable container of ours and passes
    True; measure-ext runs on the vendor's laptop and never does. Nothing is installed either way.
    """
    if level not in BUILD_LEVELS:
        raise ValueError(f"unknown build level {level!r}; expected one of {BUILD_LEVELS}")
    if skip or level == "none":
        return _skipped()
    budget = Budget(budget_seconds, phase_cap=timeout)
    if budget.exhausted():
        return skipped_budget(budget.remaining())
    agentic = agentic and level == "full"
    # Repair costs a provider turn and a re-run per failing project, so it is only offered at
    # the level that actually executes a build. Under `discover` there is no failure to repair.
    repair_on = repair and level == "full"

    scratch = Path(tempfile.mkdtemp(prefix="rqe-build-"))
    # What the tree looked like before we touched it, captured two ways: which artefact paths
    # already existed (so only ours are deleted) and the bytes of every file an installer might
    # rewrite (so a generated or modified lockfile is undone). Both are taken BEFORE anything
    # runs, because after the fact there is no way to tell our mess from theirs. Discovery runs
    # first, and read-only, so the snapshot covers every project root and not only the top one.
    project_roots = [repo]
    if not (agentic and _which("claude")):
        project_roots += [p.root for p in discover_projects(repo)[0]]
    artefact_roots = list(dict.fromkeys(project_roots))
    pre_existing = {(root, name) for root in artefact_roots for name in _artefact_names(root)}
    snap = _snapshot(artefact_roots)
    restore: list[tuple[Path, int]] = []
    try:
        if agentic and _which("claude"):
            # The agent gets the whole remaining allowance as its one ceiling: it runs every phase
            # itself, so there is nothing left for anyone else to spend afterwards.
            result = _agentic(repo, scratch, int(budget.remaining()), model)
            if result is not None:
                result["build_level"] = level
                return _strip_detail(result, detail)
            fallback_reason = ("the agentic probe could not be run or returned no parseable "
                               "result, so the deterministic table was used")
        else:
            fallback_reason = None      # no agent (or agentic=False) is the deterministic case
        result = _deterministic(repo, scratch, budget, fallback_reason, restore, level,
                                max_projects, allow_home_toolchains,
                                {"provider": repair_provider,
                                 "model": repair_model or model} if repair_on else None)
        # The level the probe ACTUALLY ran at, on the block itself. A reader who cannot see this
        # cannot tell a run that never attempted a suite from one that attempted and could not
        # finish, and those are different facts about the repository.
        result["build_level"] = level
        return _strip_detail(result, detail)
    finally:
        for path, mode in restore:
            try:
                path.chmod(mode)
            except OSError:
                pass
        shutil.rmtree(scratch, ignore_errors=True)
        for root in artefact_roots:
            for name in _artefact_names(root):
                if (root, name) in pre_existing:
                    continue
                p = root / name
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    p.unlink(missing_ok=True)
        # Last, because deleting node_modules can itself provoke a package manager into rewriting
        # a lockfile, and this is the step that has to have the final word.
        _restore_snapshot(snap)

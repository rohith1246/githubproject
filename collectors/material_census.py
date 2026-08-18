#!/usr/bin/env python3
"""material_census.py -- an exhaustive inventory of the material a codebase contains.

This is the model-assisted probe, and it carries the largest single block of the rubric. It reads
the working tree and the full history across every branch and enumerates, in nine categories,
each distinct piece of substantial material the repository holds: complex logic, features built
over many commits, subtle defect repairs, self-contained units of work, capabilities the codebase
is shaped to support but does not have yet, structural evolutions, performance work, correctness
and safety hardening, and integration work.

A parser can tell you a function has forty branches. It cannot tell you whether those branches
encode real domain rules or four hundred lines of configuration handling. That distinction is the
whole reason this probe exists, and every purely static substitute is inconclusive.

Three properties of what it emits, all deliberate:

  * **The counts are unbounded.** Each category is as long as the repository makes it. A bounded
    count cannot express a large difference between two repositories even when one exists: bound
    it low enough and most of a corpus reports the bound, at which point the number describes the
    limit rather than the codebase. Only a far-off sanity ceiling remains, and it is reported when
    reached rather than applied silently.
  * **One sentence per instance, and the count IS the list.** A category's count is the length of
    its sentence list, so a reader can audit the number by reading the evidence for it. A count
    without its instances cannot be checked.
  * **A lane that ran out of clock reports NOTHING, never a small number.** The timeout is a
    runaway backstop set four hours out, not a budget; when it is reached anyway, every
    measurement below goes NULL with `timed_out` set and `census_failure_kind` naming the reason.
    A partial census rendered as a low count is the same bug as a crashed lane reporting zero, and
    it is the more expensive one, because this lane carries the largest weight in the rubric.

  * **Prose, never source.** A sentence reads like "a fare engine applying tiered surge multipliers
    with rounding rules per currency" -- enough to judge whether the material is substantial, with
    no code, no diffs and no verbatim identifiers. Every sentence passes the redactor AND the
    emission boundary's sentence rule before it is kept; one that still names a file, a symbol or
    a product is dropped whole rather than patched. The operator reads all of them, in `--review`,
    before the output goes anywhere.
"""
from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import childenv
import history_brief

from emit_allowlist import validate_sentence
from models import DEFAULT_MODEL  # noqa: F401  -- re-exported; build_probe imports it from here
from redact import HARD_CHAR_LIMIT, contains_leak, redact


class ProviderUnavailable(RuntimeError):
    """The requested provider CLI is not installed. Never degraded, always raised.

    A missing CLI recorded as an unscored lane is indistinguishable, three weeks later and one
    spreadsheet away, from a repository that genuinely held nothing. The operator asked for a
    provider; if it is not here they need to be told so, at the start, in one sentence.
    """

# The category counts are UNBOUNDED. A ceiling low enough to be hit is a ceiling that replaces the
# measurement: a corpus scored under one mostly reports the bound, and a near-constant column
# cannot distinguish anything. What remains is a sanity ceiling, set far above where any real
# repository lands, whose only job is to stop a degenerate response from blowing the output size.
# It is reported when reached, never applied silently.
SANITY_CAP = 400

# Every instance carries its own sentence, and a category's count is the length of its list, so
# the number and the evidence for it are the same object.
MAX_SENTENCES_PER_CATEGORY = 400

# The nine categories, in the order they are reported. Module level because the unmeasured map
# below is derived from it -- the set of things this lane measures and the set it nulls out on
# failure have to be the same set, or a failed lane keeps a stale number in one of them.
CATEGORIES = ("complex_logic", "substantial_features", "defect_repairs", "work_units",
              "net_new_capabilities", "repo_evolutions", "performance_work",
              "hardening_work", "integration_work")

# The tool grant, and the reason `Bash` is not in it. `--add-dir` bounds Read, Grep and Glob; it
# does not bound Bash, and a headless agent with a shell reached the operator's home directory on
# the first try. What kept it out of anything worse was the model declining, which is not a
# control. The history this lane used to gather with `git log` and `git show` is pre-computed by
# `history_brief` and handed over as files.
TOOLS = "Read Grep Glob"

# A RUNAWAY BACKSTOP, not a budget. The old default was 900 seconds, and on a repository with ten
# thousand commits the census does not fit in fifteen minutes: it reads the tree, walks the whole
# history with numstat and opens the substantive commits. Capping it there did not make the lane
# fast, it made the lane WRONG -- the census came back unscored on exactly the largest and most
# valuable repositories, and a downstream score threshold then rejected them. Four hours is set
# far above where any real repository lands and exists only so a wedged CLI cannot run forever.
# An operator who genuinely wants a cap passes --census-timeout deliberately.
DEFAULT_TIMEOUT = 14400

# --- failure handling, sized for a fleet rather than for one laptop -------------------------
#
# This is the only lane that leaves the machine, so it is the only one whose failures are
# somebody else's. Run it in 278 containers at once and a share of them will be told to slow
# down -- and if all that survives is the exit status, "slow down" is indistinguishable from a
# misspelled model name, an expired credential and a segfault. Every constant below exists so
# that one row of the output sheet answers "why did this repository come back unscored?" without
# anyone re-running anything.

MAX_ATTEMPTS = 3                # the first attempt plus at most two retries
RETRY_BASE_SECONDS = 5.0        # first backoff, doubling per retry
RETRY_MAX_SLEEP = 30.0          # ceiling on any single backoff
RETRY_TOTAL_SLEEP_CAP = 45.0    # ceiling on the SUM of backoffs. A lane whose runtime nobody
                                # can predict is worse than a lane that gives up, so the added
                                # wall time is a number we can quote, not one we hope about.
MIN_USEFUL_ATTEMPT = 120.0      # a census spends minutes on tool calls, so a retry started with
                                # less than this left on the clock buys a guaranteed timeout and a
                                # bill. It is a ceiling, not a floor -- see _min_useful_attempt().
MIN_USEFUL_FRACTION = 0.25      # ...because the CALLER decides what "enough time" means. A lane
                                # given 60s cannot be told that no attempt under 120s is worth
                                # making: that reasoning makes a retry structurally impossible for
                                # every budget below the constant, which is exactly how the first
                                # version of this loop managed to never retry anything at all.
DETAIL_TAIL = 400               # chars kept per stream in census_error_detail
DETAIL_LIMIT = min(1000, HARD_CHAR_LIMIT)   # ceiling on the combined detail

# What the CLI's own words mean, ordered so that the expensive mistake cannot happen: an
# expired credential reported alongside the word "limit" must classify as auth and NOT be
# retried, because retrying it 278 times is how one bad secret becomes a fleet-wide outage.
#
# Each entry is (pattern, kind, retryable). `retryable` is deliberately a separate fact rather
# than a property of the kind: the kind vocabulary is fixed by the output contract and has no
# member for "the transport died", so a 503 or a reset socket is recorded as `crashed` while
# still being retried. census_retryable says which, so nothing has to be inferred from the name.
_KIND_RULES: list[tuple[re.Pattern, str, bool]] = [
    (re.compile(r"invalid[ _-]?api[ _-]?key|authentication_error|permission_error|unauthorized|"
                r"\b40[13]\b|not logged ?in|please run /?login|oauth token|"
                r"credit balance is too low", re.I), "auth", False),
    (re.compile(r"rate[ _-]?limit|\b429\b|too many requests|overloaded|\b529\b|"
                r"quota|usage limit", re.I), "rate_limited", True),
    (re.compile(r"model[ _-]?not[ _-]?found|not_found_error|unknown model|invalid model|"
                r"model .{0,40}?(?:not (?:found|available|supported)|does not exist)|"
                r"\b404\b", re.I), "model_unavailable", False),
    # Transport faults: the request never got an answer. Retried, because the next attempt very
    # often does, and recorded as `crashed` for the reason given above.
    (re.compile(r"econnreset|etimedout|eai_again|enotfound|socket hang ?up|fetch failed|"
                r"network error|connection (?:reset|closed|refused|error)|"
                r"\b50[0234]\b|bad gateway|service unavailable|gateway time-?out|"
                r"internal server error|api_error|apiconnection", re.I), "crashed", True),
    # Hard deaths, which are `crashed` in the plainest sense of the word. NOT retried: a process
    # that faulted on this tree will fault on it again, and an interpreter out of heap at this
    # concurrency will still be out of heap twenty seconds later.
    (re.compile(r"segmentation fault|core dumped|bus error|stack overflow|abort(?:ed)?\b|"
                r"out of memory|heap limit|fatal error|panic:", re.I), "crashed", False),
]

# One plain-English sentence per kind, written here and nowhere else. These are OUR words, which
# is what makes them safe to route into the rubric's unscored-criterion note -- see
# unavailable_reason().
_HUMAN = {
    "rate_limited": "the API throttled or capped the request",
    "auth": "authentication was refused",
    "model_unavailable": "the requested model was refused or does not exist",
    "timeout": "the census exceeded its time budget",
    "no_json": "the census returned no parseable JSON",
    "crashed": "the CLI died before producing a result",
    "cli_missing": "the CLI could not be found or started",
    "unknown": "the CLI failed for a reason it did not explain",
}

# The instruction below is fixed. Its wording was settled by measurement against repositories
# with known outcomes, and paraphrasing it changes what comes back. Edit only with new evidence.
#
# Two paragraphs have been edited since, both with evidence and both recorded here so the next
# reader does not have to guess which sentences are load-bearing measurement and which are not:
#
#   * the secrets guard now covers credential material arriving through a DIFF rather than only
#     through a filename, because the guard was written against filenames and the paragraph below
#     it asks the model to read history -- and a committed `.env` reaches it as a diff. The
#     mechanism behind that paragraph is `history_brief`, which withholds credential-shaped paths
#     by git pathspec; the sentences here are the second layer, not the first.
#   * the history paragraph no longer tells the model to run `git`. It has no shell: `Bash` was
#     withdrawn from this lane because `--add-dir` bounds Read/Grep/Glob and does not bound Bash,
#     so the grant was shell access to the operator's machine held in place by the model choosing
#     not to use it. The history it needed is pre-computed by `history_brief` and read as files.
#     Measured before and after on a real repository -- see the note in `collect()`.
#   * the secrets guard was reworded again, without changing what it forbids. "Skip them and say
#     nothing about it" / "do not note where it was" described the right behaviour in the wrong
#     voice: read by the repository owner -- who is the person running this -- it sounds like the
#     model is being told to notice their credentials and not mention it, which is close to a
#     complaint an owner actually made about an earlier version of this tool. The paragraph now
#     says the same thing as non-collection and states the mechanism and the disclosure, which is
#     what is true. Nothing about the counted material changed.
PROMPT = """\
You are producing an exhaustive technical inventory of ONE repository for due diligence. Report
what is actually in the codebase and its history. Inventory only -- do not grade the engineering.

LEAVE THE OWNER'S CREDENTIALS ALONE. Do not open environment files, credential files, key
material or certificates, do not read them, and do not inventory work about them. This inventory
is about engineering material; a repository's credentials are not ours to look at and never appear
in the output. If a file looks like it holds credentials, skip it and move on.

That applies just as much to credential material that reaches you by another route -- inside a
commit diff, a log, a fixture, or a file you opened for a different reason. This inventory asks you
to read history, and history contains whatever was once committed. If a key, a password, a token or
a connection string appears in front of you, do not copy it, paraphrase it, or note where it was:
none of it is a measurement, so none of it belongs in your answer.

To be plain about why: the point is that we do not COLLECT the owner's secrets, not that anything
is kept from them. The owner runs this themselves, the run reports how many files were withheld
from the history on this basis, and they read the whole artifact before deciding whether to send
it. Nothing here searches their code for credentials, and no credential-shaped string is ever
matched, counted or recorded.

Read the working tree AND the repository's development history. You have no shell and no `git`:
the history has been prepared for you as files, under {history}. Read
{history}/HISTORY-OVERVIEW.md first -- it lists every substantive commit across every ref with
its date, its changed files and its churn -- then open the full patches in
{history}/commit-diffs/ for the commits that look substantive. Open the densest source files in
the working tree as well. Budget 25-40 minutes and as many tool calls as the repository warrants
-- a large codebase deserves more looking than a small one.

COUNT ONLY WHAT IS SUBSTANTIAL. There is no cap and no round number to stop at, but there is a
bar, and it is high. An instance qualifies only if it is INTERESTING, NUANCED, COMPLEX, LARGE and
LONG-HORIZON -- work that would occupy a competent engineer across many steps, touch several parts
of the system, and demand real understanding before the first line is written. Ask of every
candidate: would solving this make a strong coding model measurably better at engineering? If the
answer is no, leave it out.

SIZE AND HORIZON ARE PART OF THE BAR, not separate from it. A subtle one-line fix can be difficult
and still fail this test, because there is no distance to travel: nothing to plan, no intermediate
state to hold, no sequence of decisions that build on each other. What qualifies is work with
extent -- several interacting pieces, a design that has to be worked out before it is written, and
a path from start to finish long enough that going astray early shows up late.

DISQUALIFY, no matter how much typing they represent: anything mechanical or repetitive; anything
a capable engineer would finish in one sitting without pausing to plan; renames, reformatting,
dependency bumps, configuration edits, boilerplate, scaffolding, generated code; adding a field,
an endpoint or a handler that follows an existing pattern; test-only changes that assert existing
behaviour; straightforward wiring between components that already have interfaces; and small
localised fixes, however clever, that touch one place and are done.

QUALIFY only where several of these hold together: the correct behaviour is genuinely non-obvious
and a plausible-looking solution would be quietly wrong; it requires holding several interacting
parts in mind at once; it turns on subtle invariants, ordering, boundary, precision or concurrency
properties; it demands understanding WHY the existing design is the way it is before changing it;
it spans multiple modules or layers; and finishing it well takes a sequence of decisions rather
than a single edit.

Most repositories hold a handful of these, not dozens. Three genuinely substantial, long-horizon
instances is a far better answer than thirty ordinary ones, and padding a category with routine or
trivially-scoped work is the single worst thing you can do here. A category with nothing that
clears the bar returns an empty list.

Report the following.

1. THEMES -- what this software does, in domain terms. Two to five short phrases, e.g.
   "payments", "fleet logistics", "identity and access", "medical billing". **Two to five words
   each, not a sentence, all lower case.** Describe the domain, not the stack, and never name a
   product or a company -- a theme that names one is discarded.

2. SELF-CONTAINMENT, 0-4 -- can this repository's behaviour be exercised inside itself? 4 = a
   standalone application. 0 = one slice of a mesh that needs several sibling services and a
   message broker before it does anything.

3. NINE CATEGORIES OF MATERIAL. For each, list only the instances that clear the bar
   above, most substantial first, one plain-English sentence each. The count for a category is simply how
   many sentences you listed for it -- do not report a number you did not enumerate. Each
   sentence must make the substance visible: say what is nuanced about it and how far it reaches,
   not merely what it is.

   a. COMPLEX LOGIC LOCI -- distinct places in the CURRENT tree holding substantial, non-obvious
      logic: invariants that must hold, state machines, monetary or tax arithmetic, scheduling,
      reconciliation, permission models, concurrency, parsing, retry and idempotency handling.
      Exclude CRUD handlers, glue, configuration, generated code and thin wrappers. A locus is
      one coherent unit -- a module or closely-coupled cluster -- not one file per count.

   b. SUBSTANTIAL FEATURES -- capabilities built across MULTIPLE commits that represent real,
      non-decomposable work. Not a rename, not a dependency bump, not a reformatting pass.

   c. NOTABLE DEFECT REPAIRS -- fixes that addressed something genuinely subtle: a race, an
      off-by-one at a boundary, a rounding or precision error, a broken invariant, a mishandled
      failure path. A typo fix or a version bump is not notable. Say which shipped a regression
      test in the same commit.

   d. SELF-CONTAINED UNITS OF WORK -- pieces of work this codebase could support as a stand-alone
      assignment, where the scope can be stated without reference to the rest of the system,
      completing it demands real reasoning rather than mechanical edits, and correctness can be
      settled by observing behaviour rather than by opinion.

   e. NET-NEW CAPABILITIES -- substantial things this codebase does not do yet but could be
      built here. Both kinds count equally, and the maturity of the codebase is NOT a
      qualification: an extension the existing abstractions are already shaped to support, and a
      genuinely new subsystem this codebase's purpose calls for, built on top of whatever is
      present. A thin or early-stage repository can hold as many of these as a mature one -- often
      more, because more of its intended surface is still unbuilt. Judge the task, not the state
      of the code around it.

      The only requirement is that the work belongs to THIS repository: it must fit the domain
      and purpose the code is evidently for, and be built on what is here. A task with no
      connection to what this codebase is trying to do is not a candidate no matter how large it
      is. Everything else about the difficulty bar still applies -- these must be substantial,
      long-horizon pieces of work, not a screen or an endpoint.

      List these as thoroughly as any other category; a repository is often shaped to support far
      more of them than it has already built.

   f. REPOSITORY EVOLUTIONS -- structural changes the history actually went through: a migration
      between frameworks, storage engines, protocols or languages; an architectural split or
      merge; an interface redesign carried across many files; a versioning or compatibility break.

   g. PERFORMANCE AND SCALE WORK -- changes made to make something faster, cheaper, or able to
      handle more: caching layers, query or index work, batching, pagination, memory reduction,
      concurrency introduced for throughput.

   h. CORRECTNESS AND SAFETY HARDENING -- validation, authorisation, input sanitisation, error
      propagation, transactional integrity, idempotency, rate limiting, audit logging, and fixes
      to security-relevant handling.

   i. INTEGRATION AND INTERFACE WORK -- third-party service integrations, API or schema changes,
      data-format handling, protocol adapters, and the compatibility work each demanded.

   Categories may overlap in subject matter -- one commit can be both a defect repair and
   hardening. Count it wherever it genuinely belongs; do not force a single home. But never list
   the same instance twice WITHIN one category.

FORMAT OF EVERY SENTENCE -- read this carefully. Every sentence is checked before it is kept, and
a sentence that breaks any rule below is DISCARDED WHOLE, taking its instance out of the count
with it. Nothing is patched up for you.

**Describe the behaviour. Never name the code.** No file names, no paths, no function, class,
method or variable names, no commit hashes, no code fragments, no backticks, no braces or arrows.
Write as though explaining the system to a non-engineer who will never see the source.

The mechanical rules, all of them enforced:

  * At most 40 words.
  * Plain lower case except the first word of a sentence. A capital anywhere else reads as a
    product, company or class name and is rejected.
  * No acronyms or all-capital words at all.
  * No quotation marks, backticks, parentheses, brackets, braces, slashes, equals signs or
    dotted.names of any kind.
  * No word containing an underscore, and no word with a capital in the middle.

  GOOD  "A settlement reconciler that matches ledger entries against bank statements with
         per-currency tolerance windows and flags unmatched residuals for review."
  GOOD  "Fare calculation applying tiered surge multipliers, minimum-fare floors and rounding
         rules that differ by market."
  GOOD  "A fix to a boundary condition where the last item in a paginated result was dropped
         whenever the page size exactly divided the total."

  BAD   "The reconcile.py module handles this."            (names a file)
  BAD   "calculateFare() applies the multipliers."          (names a function)
  BAD   "Fixed in commit a3f9c21."                          (names a commit)
  BAD   "See the OrderStateMachine class."                  (names a class)

Every BAD example above would be thrown away, and that instance would vanish from the count --
which is worse than a plainly-worded description.

Ground everything in what you actually opened. If a category is genuinely empty, return an empty
list -- zero is informative and legitimate, and padding is worse than a low count.

Return ONLY a JSON object, no prose around it. Every list is UNBOUNDED:
{{"themes": ["..."],
  "self_contained": <int 0-4>,
  "complex_logic": ["one sentence per locus", "..."],
  "substantial_features": ["one sentence per feature", "..."],
  "defect_repairs": ["one sentence per repair", "..."],
  "defect_repairs_with_regression_test": <int, how many of the above shipped a test>,
  "work_units": ["one sentence per unit of work", "..."],
  "net_new_capabilities": ["one sentence per capability", "..."],
  "repo_evolutions": ["one sentence per structural change", "..."],
  "performance_work": ["one sentence per change", "..."],
  "hardening_work": ["one sentence per change", "..."],
  "integration_work": ["one sentence per change", "..."],
  "material_summary": "two or three sentences on what this codebase holds, same rules",
  "what_i_could_not_assess": "one short sentence, same rules, or empty"}}

The last two fields obey every sentence rule above. `what_i_could_not_assess` is one sentence at
most: say what stopped you, not what you saw on the way.
"""



def _clean(examples, limit: int = MAX_SENTENCES_PER_CATEGORY) -> tuple[list[str], list[str]]:
    """Redact each example, then hold it to the emission boundary's sentence rule.

    Two stages, and the second one is a gate rather than a scrub. A sentence that still names a
    file, a symbol or a product after redaction is DROPPED whole: a description reading "the
    [identifier] module handles [path]" says nothing about the engineering and everything about
    the fact that we tried to launder it. The count is the length of the surviving list, so the
    number and the evidence for it still cannot disagree.

    Returns (kept, what_was_redacted).
    """
    kept: list[str] = []
    removed: list[str] = []
    for e in (examples or []):
        if not isinstance(e, str) or not e.strip():
            continue
        cleaned, gone = redact(e)
        if not cleaned or validate_sentence(cleaned):
            continue
        kept.append(cleaned)
        removed.extend(gone)
        if len(kept) >= limit:
            break
    return kept, sorted(set(removed))


def _clamp(v, lo: int, hi: int, default: int = 0) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


# What each category contributes to the headline total. Material in the present tree, and work
# the codebase could support as a standalone assignment, weigh most: they describe the codebase as
# it stands. The history categories corroborate that picture rather than establishing it, so they
# weigh less.
IDEA_WEIGHTS = {
    "complex_logic": 1.0,
    "work_units": 1.0,
    "net_new_capabilities": 1.0,
    "substantial_features": 0.7,
    "defect_repairs": 0.7,
    "repo_evolutions": 0.6,
    "hardening_work": 0.5,
    "integration_work": 0.5,
    "performance_work": 0.5,
}


def minable_ideas_total(counts: dict, with_test: int = 0) -> float:
    """The weighted total of everything the census enumerated.

    A defect repair that shipped its own regression test counts extra: a project whose fixes
    arrive with the test that demonstrates them is showing a stronger engineering practice.
    """
    total = sum(IDEA_WEIGHTS.get(k, 0.0) * int(counts.get(k) or 0) for k in IDEA_WEIGHTS)
    return total + 0.4 * min(int(with_test or 0), int(counts.get("defect_repairs") or 0))


def derive_logic_depth(total: float) -> int:
    """The 0-6 figure kept for continuity, derived from the unbounded total.

    The rubric scores the raw total, not this; the band survives because it is a compact way to
    say how dense a repository is without quoting a number whose scale is corpus-dependent.
    """
    for threshold, depth in ((0.5, 0), (3.0, 1), (7.0, 2), (14.0, 3), (28.0, 4), (55.0, 5)):
        if total < threshold:
            return depth
    return 6


# --- locating and running the CLI -----------------------------------------------------------

_BATCH_SUFFIXES = {".cmd", ".bat"}

# Recorded, not raised. A platform limitation the operator can read is a fact about the run; the
# same limitation discovered by a mangled prompt is a mystery about the repository.
_BATCH_SHIM_NOTE = (
    "the only entry point found on this machine is a shell shim, so the instruction is sent on "
    "standard input rather than on the command line, which that shim would otherwise reparse")


def resolve_cli(provider: str = "claude") -> tuple[str, bool, str]:
    """Find the requested provider's CLI and decide how the instruction can reach it.

    Returns (executable, prompt_on_stdin, platform_note) and raises ProviderUnavailable when the
    CLI is not installed. Three facts drive the rest and all three are Windows-only; on POSIX the
    answer is the plain result of a PATH lookup.

      * `shutil.which` honours PATHEXT and will happily hand back `claude.cmd`, which is what a
        Node install puts on PATH. CreateProcess does NOT honour PATHEXT, so passing the bare
        word "claude" as argv[0] fails on the very machine where which() just succeeded. The
        resolved absolute path is therefore what gets executed -- on every platform, which also
        removes a second PATH lookup that could resolve differently from the first.
      * A `.cmd`/`.bat` argv[0] runs under a command interpreter that reparses the command line
        with its own quoting rules. The instruction below contains double quotes, angle brackets
        and newlines; Python quotes argv for the C runtime, which the interpreter reads
        differently, and a newline ends a command outright. The instruction cannot survive that
        trip, so when a shim is the only entry point the instruction goes down stdin, which the
        interpreter never touches. The CLI reads it there: -p is documented as being for pipes.
      * A native install provides `claude.exe`, which has none of these problems. Prefer it.
    """
    candidates = {
        "claude": ("claude.exe", "claude.com", "claude"),
        "codex": ("codex.exe", "codex"),
        "gemini": ("gemini.exe", "gemini.cmd", "gemini"),
    }[provider]
    found = next((path for name in candidates if (path := shutil.which(name))), None)
    if not found:
        raise ProviderUnavailable(
            f"the `{provider}` CLI is not on PATH. It was requested explicitly, so this run "
            f"stops here rather than reporting a repository as unmeasured when the truth is "
            f"that the tool was missing. Install it and sign in, choose another --provider, or "
            f"pass --no-llm to run the deterministic collectors only."
        )
    prompt_on_stdin = provider == "codex" or Path(found).suffix.lower() in _BATCH_SUFFIXES
    note = _BATCH_SHIM_NOTE if Path(found).suffix.lower() in _BATCH_SUFFIXES else ""
    return found, prompt_on_stdin, note


def _safe_detail(text: str) -> str:
    """redact(), then prove it. A diagnostic must never become the reason a run aborts.

    redact() is one pass of substitutions and is not a guaranteed fixpoint on arbitrary CLI
    output -- removing a backtick can join two halves of something that was not an identifier
    until it was joined. A second pass settles every case observed. If one still survived we drop
    the text rather than let emit.audit() kill a run over a diagnostic: census_failure_kind still
    carries the classification, and losing the evidence beats losing the whole result.
    """
    cleaned, _ = redact(text)
    if contains_leak(cleaned):
        cleaned, _ = redact(cleaned)
    if contains_leak(cleaned):
        return "[detail withheld: it could not be redacted to the satisfaction of the audit]"
    return cleaned[:DETAIL_LIMIT]


def _tails(stdout, stderr) -> str:
    """A bounded, redacted tail of both streams -- the evidence behind census_failure_kind.

    The TAIL, because a CLI states its diagnosis last and its banner first. BOTH streams,
    because the transport errors land on stderr while the CLI's own error envelope arrives on
    stdout, and the operator does not know in advance which one they are chasing.

    This text comes from the CLI and not from the repository, but the CLI cheerfully echoes the
    directory it was pointed at, so it goes through redact() like everything else. It is
    deliberately NOT exempted in emit.py: an unaudited free-text field is precisely the hole this
    collector promises not to have, and a field that only ever appears on failure is the last
    place anyone would think to look for a leak.
    """
    parts = []
    for label, stream in (("standard error", stderr), ("standard output", stdout)):
        text = (stream or "").strip()
        if text:
            parts.append(f"{label} tail: {text[-DETAIL_TAIL:]}")
    return _safe_detail(" | ".join(parts)) if parts else ""


def _envelope_error(stdout: str) -> str:
    """The CLI's own error text when it exited zero but reported failure in its JSON envelope.

    Worth the extra parse: a throttle that arrives this way carries exit status 0, and treating
    it as merely unparseable would file a rate limit under `no_json` and decline to retry it.
    """
    try:
        outer = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(outer, dict):
        return ""
    if outer.get("is_error"):                      # claude's envelope
        result = outer.get("result")
        return result if isinstance(result, str) else json.dumps(outer)[:DETAIL_LIMIT]
    error = outer.get("error")                     # gemini's envelope
    if isinstance(error, dict):
        message = error.get("message")
        return message if isinstance(message, str) else json.dumps(error)[:DETAIL_LIMIT]
    return ""


def _classify(returncode: int | None, stdout: str, stderr: str) -> tuple[str, bool]:
    """(kind, retryable) from the CLI's own words, falling back to how it exited.

    Text matching is a heuristic and is documented as one; census_error_detail carries the
    evidence so an operator can overrule any single verdict. The classification exists to make
    278 rows sortable, not to be the last word on one of them.
    """
    blob = f"{stderr}\n{stdout}"[-4000:]
    for pattern, kind, retryable in _KIND_RULES:
        if pattern.search(blob):
            return kind, retryable
    if _died_on_a_signal(returncode):
        return "crashed", False
    return "unknown", False


def _died_on_a_signal(returncode: int | None) -> bool:
    """Whether the exit status itself says the process was killed rather than that it returned.

    Three encodings, because the number reaching us depends on who reported it:

      * Python's own POSIX convention: negative, the signal number negated (-11 for a fault, -9
        for the OOM killer, which is the one to expect at this concurrency).
      * The shell convention, 128 + signal, which is what surfaces whenever anything between us
        and the process went through a shell -- 139 for a segmentation fault. Text matching alone
        misses this whenever the fault is reported by the kernel rather than by the CLI, and a
        silent `unknown` on a crash is a lost diagnosis.
      * Windows NT exception statuses, which arrive as large unsigned values (0xC0000005 for an
        access violation).
    """
    if returncode is None:
        return False
    return returncode < 0 or 128 < returncode < 166 or returncode >= 0xC0000000


def _attempt(exe: str, prompt: str, repo: Path, provider: str, model: str,
             prompt_on_stdin: bool, budget: float,
             history: Path | None = None) -> tuple[dict | None, str | None, bool, str, int | None]:
    """One invocation. Returns (payload, kind, retryable, detail, returncode).

    `payload` is non-None, and `kind` None, only when a census actually came back parseable.

    Two things about this argv are load-bearing rather than incidental:

      * `childenv.isolation_flags` is what stops the repository under measurement configuring
        the agent that measures it. Without it a committed `.claude/settings.json` runs its
        hooks here, on this machine, with the provider credential in scope.
      * There is no `Bash` in the tool grant. `--add-dir` bounds Read/Grep/Glob only, so a shell
        was confinement by the model's own judgement rather than by anything enforceable. The
        history the lane needs arrives instead as files under `history`, which is why that
        directory is also handed to `--add-dir`.
    """
    output_path = None
    isolation = childenv.isolation_flags(provider, exe)
    if provider == "claude":
        cmd = [
            exe, "-p", "--output-format", "json", "--add-dir", str(repo), "--model", model,
            "--allowedTools", TOOLS, *isolation,
        ]
        if history is not None:
            cmd += ["--add-dir", str(history)]
    elif provider == "codex":
        handle = tempfile.NamedTemporaryFile(prefix="repo-quality-codex-", suffix=".txt", delete=False)
        handle.close()
        output_path = Path(handle.name)
        # No `--add-dir` for the history: codex's `--add-dir` grants WRITE access alongside the
        # workspace, and `--sandbox read-only` already lets it read any path. The prompt names
        # the directory; that is all codex needs. `--ephemeral` is the transcript control here.
        cmd = [
            exe, "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
            *isolation, "--model", model, "--output-last-message", str(output_path), "-",
        ]
    else:
        # Unreachable while `gemini` has no isolation control -- isolation_flags raises above.
        # Kept correct rather than deleted so that adding gemini back is one table entry.
        cmd = [
            exe, "-p", prompt, "--output-format", "json", "--approval-mode", "default",
            *isolation, "--model", model,
        ]
        if history is not None:
            cmd += ["--include-directories", str(history)]
    kwargs: dict = {
        "cwd": str(repo), "capture_output": True, "encoding": "utf-8", "errors": "replace",
        # MODEL trust domain: the provider CLI gets its own auth variables and nothing else.
        # No cloud, VCS, database or SSH credential enters a model subprocess.
        "timeout": max(1.0, budget),
        "env": childenv.build_env(childenv.MODEL, provider=provider,
                                  passthrough=("HOME", "USERPROFILE", "XDG_CONFIG_HOME")),
    }
    if prompt_on_stdin:
        kwargs["input"] = prompt
    elif provider == "claude":
        cmd.insert(2, prompt)
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    try:
        p = subprocess.run(cmd, **kwargs)
        if output_path and output_path.exists():
            p.stdout = output_path.read_text(encoding="utf-8")
    except subprocess.TimeoutExpired as e:
        # The partial output is the most valuable thing we hold here: a CLI that spent the whole
        # budget retrying a throttle internally says so on stderr long before the clock expires.
        # And when there is nothing, SAY there was nothing -- an empty diagnostic field reads as
        # "nobody looked", which is the state this lane was in before any of this was written.
        if output_path:
            output_path.unlink(missing_ok=True)
        detail = _tails(e.stdout, e.stderr) or _safe_detail(
            f"the CLI produced no output at all before the deadline of "
            f"{max(1.0, budget):.0f} seconds, so there is nothing to attribute the stall to; a "
            "silent stall this long is usually a wait on a credential prompt or on the network")
        return None, "timeout", False, detail, None
    except OSError as e:
        if output_path:
            output_path.unlink(missing_ok=True)
        # strerror rather than str(e): the latter appends the path it tried to execute.
        kind = "cli_missing" if isinstance(e, FileNotFoundError) else "crashed"
        return None, kind, False, _safe_detail(f"{type(e).__name__}: {e.strerror or ''}"), None

    if output_path:
        output_path.unlink(missing_ok=True)
    if p.returncode != 0:
        kind, retryable = _classify(p.returncode, p.stdout, p.stderr)
        return None, kind, retryable, _tails(p.stdout, p.stderr), p.returncode

    payload = _extract_json(p.stdout, provider)
    if payload is None:
        kind, retryable = "no_json", False
        envelope = _envelope_error(p.stdout)
        if envelope:
            classified, retryable = _classify(0, envelope, "")
            # An envelope error we cannot name is still an unusable result, so `no_json` stands.
            if classified != "unknown":
                kind = classified
        return None, kind, retryable, _tails(p.stdout, p.stderr), p.returncode
    return payload, None, False, "", p.returncode


def _error_line(kind: str, returncode: int | None, attempts: int, budget: int,
                provider: str = "claude") -> str:
    """The human sentence. Keeps the wording earlier runs were read with, adds the kind."""
    bits = [f"census failed ({kind}): {_HUMAN.get(kind, _HUMAN['unknown'])}"]
    if returncode is not None:
        bits.append(f"{provider} exited {returncode}")
    if kind == "timeout":
        bits.append(f"timed out after {budget}s")
    bits.append(f"{attempts} attempt(s)")
    bits.append("criteria unscored")
    return "; ".join(bits)


def unmeasured() -> dict:
    """Every measurement this lane exists to produce, explicitly NULL.

    A lane that could not finish emits this. Not an omitted key, and above all not a zero: a zero
    says "we looked and the repository holds nothing", which is a verdict about the repository,
    while what actually happened is a verdict about the run. The census is the single largest
    dimension in the ext rubric, so a zero here is the difference between a strong repository and
    a rejected one -- which is precisely how a repository with ten thousand commits came back
    scoring in the low teens because our own collector ran out of clock.
    """
    return {
        "logic_depth": None,
        "self_contained": None,
        "minable_ideas_total": None,
        "n_minable_ideas": None,
        "defect_repairs_with_regression_test": None,
        **{f"n_{cat}": None for cat in CATEGORIES},
    }


def unavailable_reason(result: dict) -> str:
    """Plain-English WHY for the rubric's unscored line, in our words only.

    Deliberately not the CLI's text. This string reaches score_explanation and
    criteria_unscored, both of which emit.py exempts from the leak audit, so anything routed
    through here would travel unaudited. A closed vocabulary cannot leak.
    """
    kind = str(result.get("census_failure_kind") or "unknown")
    return "material census unavailable: " + _HUMAN.get(kind, _HUMAN["unknown"])


def _min_useful_attempt(budget: float) -> float:
    """How much clock a retry must have in front of it to be worth starting.

    The absolute figure is what a real census needs; the fraction is there because the caller's
    budget is the definition of the job. Whichever is SMALLER wins, so a 1500s lane still refuses
    to start a census with two minutes left, while a 60s lane -- a test, a smoke run, an operator
    probing the failure path -- can still retry, instead of being told by a constant it never
    chose that no attempt it could possibly make is worth making.
    """
    return min(MIN_USEFUL_ATTEMPT, MIN_USEFUL_FRACTION * max(1.0, budget))


def _backoff(attempt: int, already_slept: float) -> float:
    """Exponential, jittered, and capped in total.

    The jitter is the point at this concurrency. 278 containers throttled inside the same second
    and backing off by the same fixed interval simply reconvene on the far side of the sleep and
    throttle each other again; spreading each one over half to one and a half of its nominal
    delay is what breaks the convoy.
    """
    delay = min(RETRY_MAX_SLEEP, RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    delay *= 0.5 + random.random()
    return max(0.0, min(delay, RETRY_TOTAL_SLEEP_CAP - already_slept))


def collect(repo: Path, model: str = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT,
            provider: str = "claude") -> dict:
    """Run the census.

    The lane no longer hands the agent a shell. `history_brief` writes the development history
    into a scratch directory first, the agent reads it there, and the directory is deleted when
    the lane returns -- so the only place the history lands is one this function created and
    removed.

    What that cost was measured, not assumed: five census runs and three mining runs over the same
    real repository, same model, same isolation flags, differing only in the tool grant. Mining is
    unchanged in count and strictly better grounded; this lane loses about 14% of its weighted
    total, all of it in `defect_repairs`, and `logic_depth` fell one band on two of three runs. The
    full table is in `references/client-runbook.md` section 2.

    THAT SHIFT IS A RE-BASELINING EVENT FOR ANY CORPUS. It applies to every repository equally, so
    ranking should survive it, but a `minable_ideas_total` measured before this change and one
    measured after are not the same number and must not be pooled or compared across the boundary.
    """
    out: dict = {"probe": "material_census", "ok": False, "provider": provider, "model": model}

    # Raises ProviderUnavailable if the requested CLI is absent, and ProviderNotIsolated if it is
    # present but cannot be stopped from honouring the measured tree's own configuration. Neither
    # is caught: both are facts about this machine that the operator has to act on, and a run that
    # continues past either produces a number nobody should trust.
    exe, prompt_on_stdin, platform_note = resolve_cli(provider)
    childenv.isolation_flags(provider, exe)
    if platform_note:
        out["census_platform_note"] = platform_note

    # Deliberately not recorded in `out`: the emission boundary refuses keys it was not told to
    # expect, and how many patches we materialised is a fact about the run rather than about the
    # repository. It goes to stderr, where the operator can see it, and no further.
    with tempfile.TemporaryDirectory(prefix="repo-quality-history-") as scratch:
        history = Path(scratch)
        brief = history_brief.write_brief(repo, history)
        print(f"census: prepared {brief['patches_written']} of {brief['commits_substantive']} "
              f"substantive commits as read-only patches"
              + (f"; {brief['credential_paths_excluded']} had credential-shaped files withheld"
                 if brief["credential_paths_excluded"] else ""),
              file=sys.stderr, flush=True)
        return _run_census(out, exe, repo, history, provider, model, prompt_on_stdin, timeout)


def _run_census(out: dict, exe: str, repo: Path, history: Path, provider: str, model: str,
                prompt_on_stdin: bool, timeout: int) -> dict:
    prompt = PROMPT.format(history=history)   # the doubled JSON braces unescape here too

    # The caller's timeout is the budget for the LANE, not for one invocation: retries and
    # backoff are spent out of it and never on top of it. Each attempt is handed whatever is
    # left, so the deadline holds however many attempts happen.
    budget = float(max(1, int(timeout)))
    deadline = time.monotonic() + budget
    floor = _min_useful_attempt(budget)
    slept = 0.0
    attempts = 0
    while True:
        attempts += 1
        payload, kind, retryable, detail, rc = _attempt(
            exe, prompt, repo, provider, model, prompt_on_stdin,
            budget=deadline - time.monotonic(), history=history)
        if payload is not None:
            break

        # Decide, then say what was decided. Three separate things can forbid a retry and they
        # have three different remedies -- a wrong classification, a raised ceiling, a longer
        # budget -- so collapsing them into "it did not retry" would leave the operator guessing
        # exactly as they were before this lane could explain itself at all.
        delay = _backoff(attempts, slept)
        after_sleeping = deadline - time.monotonic() - delay
        declined = ""
        if not retryable:
            pass                                  # not transient; the kind already says so
        elif attempts >= MAX_ATTEMPTS:
            declined = f"the ceiling of {MAX_ATTEMPTS} attempts was reached"
        elif after_sleeping < floor:
            declined = (f"only {max(0.0, after_sleeping):.0f} of the {budget:.0f} second budget "
                        f"would remain after backing off, below the {floor:.0f} seconds a census "
                        "needs to be worth starting")
        else:
            time.sleep(delay)                     # the only wait this lane ever performs
            slept += delay
            continue
        if declined:
            detail = _safe_detail(" | ".join(
                p for p in (detail, f"no further attempt: {declined}") if p))
        out.update({
            "error": _error_line(kind or "unknown", rc, attempts, int(budget), provider),
            "census_failure_kind": kind or "unknown",
            "census_retryable": bool(retryable),
            "census_attempts": attempts,
            "census_error_detail": detail,
            # NULL, never zero, and never simply absent -- see unmeasured().
            **unmeasured(),
        })
        out["timed_out"] = kind == "timeout"
        return out

    # Every category is a LIST of sentences; its count is the length of that list after
    # redaction, so the number and its evidence can never disagree.
    ideas: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    redacted: list[str] = []
    hit_cap = False
    for cat in CATEGORIES:
        raw = payload.get(cat) or []
        if len(raw) >= SANITY_CAP:
            hit_cap = True
        kept, gone = _clean(raw)
        ideas[cat] = kept
        counts[cat] = len(kept)
        redacted.extend(gone)

    with_test = _clamp(payload.get("defect_repairs_with_regression_test"),
                       0, counts["defect_repairs"])
    total = minable_ideas_total(counts, with_test)

    themes = []
    for t in (payload.get("themes") or [])[:5]:
        cleaned, _ = redact(str(t))
        if cleaned:
            themes.append(cleaned[:70])
    summary, summary_redacted = redact(str(payload.get("material_summary") or ""))
    if summary_redacted:
        redacted.extend(summary_redacted)

    out.update({
        "themes": themes,
        # counts -- one per category, unbounded
        **{f"n_{k}": v for k, v in counts.items()},
        "defect_repairs_with_regression_test": with_test,
        "minable_ideas_total": round(total, 1),
        "n_minable_ideas": sum(counts.values()),
        # the sentences behind every count
        "minable_ideas": ideas,
        "self_contained": _clamp(payload.get("self_contained"), 0, 4),
        "logic_depth": derive_logic_depth(total),
        "material_summary": summary,
        "census_hit_cap": hit_cap,
        "timed_out": False,
        # Reported on success too: a census that needed three attempts took minutes longer than
        # one that needed none, and a run whose duration cannot be accounted for cannot be
        # capacity-planned. This is also the only place a retried-but-successful lane is visible.
        "census_attempts": attempts,
        "redacted_from_examples": sorted(set(redacted)),
        "what_i_could_not_assess":
            " ".join(str(payload.get("what_i_could_not_assess") or "").split())[:300],
        "ok": True,
    })
    return out


def _extract_json(stdout: str, provider: str = "claude"):
    """Unwrap the provider's JSON envelope; the payload inside may also be fenced.

    `claude -p --output-format json` puts the model's text under `result`, `gemini
    --output-format json` under `response`, and codex writes it to the file named by
    --output-last-message, so by the time it reaches here it is already bare.
    """
    key = {"claude": "result", "gemini": "response"}.get(provider)
    try:
        outer = json.loads(stdout)
        inner = outer.get(key) if isinstance(outer, dict) and key else None
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

#!/usr/bin/env python3
"""
candidates.py — the task-candidate record the external mining lane fills in.

measure-ext runs ONE mining lane: a headless agent reads the code and the
development history and enumerates the task ideas the repository could support.
This module is the in-process shape those ideas are held in between the agent
answering and `measure.py` counting them. Nothing here reaches an artifact: the
emitted document carries per-type COUNTS plus one validated sentence per idea,
and `collectors/emit_allowlist.py` is what decides that.

Each idea is classified into one of the four declared task types:

    net_new        a capability the codebase does not have yet
    bug_repair     a defect the history actually fixed, reproduced and repaired
    repo_evolution a structural change: a migration, a split, an interface redesign
    agentic        a long-horizon task that fits none of the above

`agentic` is also the honest bucket for an answer whose type is not one of the
four: "mined by the diving agent, type not established" is a true statement,
where filing it under a type nobody chose is not.

The record is deliberately flat and stdlib-only (dataclasses + hashlib) so both
the collectors and the miner subprocess can depend on it without pulling anything
else in the skill along with it.
"""
from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field

STRATEGIES = ("net_new", "agentic", "bug_repair", "repo_evolution")
COMPLEXITY_BANDS = ("Small", "Medium", "Large")


@dataclass
class DifficultySignals:
    """How much work the idea represents. Every field is optional, because a
    collector fills only what it can measure without executing anything."""
    complexity: str = "Small"          # Small | Medium | Large
    agent_score: float = 0.0           # effective-LOC difficulty score (absolute)
    logic_density: float = 0.0         # dense new logic / true_loc  (0..1)
    context_burden: float = 0.0        # code that must be read first / true_loc (0..1)
    atomic_interdependence: float = 0.0  # non-decomposable / can't-checkpoint (0..1)
    n_subsystems: int = 0              # cross-module spread
    true_loc: int = 0                  # collapsed real size of the change
    rewrite_scope_loc: int = 0         # lines that would have to be written
    est_tokens: int = 0
    est_steps: int = 0


@dataclass
class TestEvidence:
    """What the repository's own tests say about the idea.

    `test_files` are paths that EXIST at HEAD and `assertion_count` counts what
    they assert; `breadth` (0..1) is how completely they cover the behaviour.
    `acceptance_criteria` is the other case — an idea for a capability the
    codebase does not have yet has no tests by definition, so it carries
    individually checkable statements instead, alongside the real `harness_files`
    at HEAD that a check would run under.

    Never put an aspirational path in `test_files`. Writing
    "tests/test_my_new_feature.py" there claims evidence that is not on disk."""
    test_files: list[str] = field(default_factory=list)
    assertion_count: int = 0
    deterministic: bool = False
    offline: bool = False
    breadth: float = 0.0
    acceptance_criteria: list[str] = field(default_factory=list)
    harness_files: list[str] = field(default_factory=list)


@dataclass
class Locus:
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    subsystems: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    strategy: str
    source_commit_shas: list[str] = field(default_factory=list)
    locus: Locus = field(default_factory=Locus)
    difficulty: DifficultySignals = field(default_factory=DifficultySignals)
    test_evidence: TestEvidence = field(default_factory=TestEvidence)
    why_interesting: str = ""
    provenance: dict = field(default_factory=dict)  # {repo, head_sha, mined_from}
    candidate_id: str = ""

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown task type {self.strategy!r}; use one of {STRATEGIES}")
        if self.difficulty.complexity not in COMPLEXITY_BANDS:
            raise ValueError(f"unknown complexity {self.difficulty.complexity!r}")
        if not self.candidate_id:
            self.candidate_id = make_candidate_id(
                self.strategy, self.source_commit_shas, self.locus.files
            )

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def make_candidate_id(strategy: str, shas: list[str], files: list[str]) -> str:
    """Deterministic id from content (no randomness — keeps runs reproducible and
    lets the same idea de-dup across lanes). sha1 is used only as a content
    fingerprint, never for security."""
    payload = "|".join([
        strategy,
        ",".join(sorted(shas)),
        ",".join(sorted(files)),
    ])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"{strategy}:{digest}"

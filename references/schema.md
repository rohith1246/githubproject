# Schema reference — measure-ext

This skill emits **exactly** the MEASURE-stage schema defined in the shared contract:

    ../../CONTRACT.md      (skills/repo-quality/CONTRACT.md)

That file is the source of truth. Read it before changing any field. This page only records how
this skill maps the deterministic collectors onto that schema, and the null-vs-0 decisions.

## Outputs

`python measure.py <repo> --out <dir>` writes into `<dir>`:

- `codebase_repos.json` / `codebase_repos.csv` — the flat promoted row: the 38 CONTRACT fields
  (the enumerated 37 + legacy `quality_score`) plus the three MEASURE additions
  `build_ok`, `testable_at_head`, `capacity`. 41 columns total.
- `measurement.json` — the full doc: `tree`, `git`, `classification` blocks, plus an additive
  `ext_signals` block and a `material` block (see below). Also carries `measurer_version`,
  `measured_at`, `repo_digest`, `variant: "ext"`.
- `codebase_repo_mining.json` — the task-type census (see below).

`--sink csv` writes only the row files; `--sink db` is a stub that raises `NotImplementedError`
(the platform ingests the JSON/CSV — there is no direct DB writer here).

## Block sources

| block | produced by | notes |
|---|---|---|
| `tree` (~67) | `collectors/repo_stats.py` (subprocess, stdlib-only) | verbatim, then held to the emission allowlist |
| `git` (~31) | `collectors/git_stats.py` `repo_stats` sub-block + its full-history class tallies | the per-class commit LISTS are never copied into the block, and are declared non-emittable so a change of mind fails the run; `top_authors`, `head_sha`, `effective_tip_sha`, `anonymizer_commit`, `latest_tag` dropped; `commits_by_month` + `active_days` computed here via fixed-argv `git log` |
| `classification` (5) | `collectors/classify_repo.py` + `demo_signals` from `repo_stats.py` | `primary_class`, `class_confidence`, `is_monorepo`, `is_likely_demo`, `demo_reasoning` |
| `ext_signals` (additive) | `code_structure.py` (allow_agentic=False), `git_history.py` (agentic=False), `build_probe.py` (agentic=False, only with `--build`) | deterministic paths only; no model is ever called |
| `material` (additive) | `material_census.py` | logic depth, and the only model-assisted block in `measurement.json`. Runs by default; `--no-llm` records it unscored (see below). A missing provider CLI is an error, not an unscored lane |

The other model-assisted lane, task-type mining, writes its own document and is described at the
bottom of this page.

## The `material` block (logic depth)

`material_census.collect(repo, model, timeout, provider)` invokes the requested provider CLI (fixed
argv, `shell=False`, auth from the environment) to inventory the substantial, minable material the
repo holds, then derives a compact logic-depth band. Each sentence is redacted AND held to
`validate_sentence()` where it is parsed, so a sentence that still names a file, a symbol or a
product is dropped whole rather than patched — and because a category's count is the length of its
surviving list, the number and its evidence still cannot disagree. The block then passes the
emission allowlist and the `redact_tree()` / `audit_no_leak()` backstop like every other block.

- **scored** (`scored: true`): `logic_depth` (0-6), `self_contained` (0-4), per-category counts
  (`n_complex_logic`, `n_work_units`, `n_net_new_capabilities`, `n_substantial_features`,
  `n_defect_repairs`, `n_repo_evolutions`, `n_performance_work`, `n_hardening_work`,
  `n_integration_work`), `n_minable_ideas`, `minable_ideas_total`, the `minable_ideas` sentence
  lists behind those counts, `themes`, `material_summary`, `census_attempts`, and `model`.
- **skipped** (`--no-llm`): `scored: false`, `skip_reason: "llm_disabled"`,
  `logic_depth_unavailable_reason`. `logic_depth` / `self_contained` are absent (unscored, not 0).
- **unavailable** (throttle, timeout, refusal, crash): `scored: false`, `ok: false`,
  `census_failure_kind`, `census_retryable`, `census_attempts`, `census_error_detail` (the only
  census string NOT vocab-exempt — it carries CLI text and is scrubbed + audited),
  `logic_depth_unavailable_reason`. The run still completes and writes everything else.

`logic_depth` is surfaced inside this block, NOT promoted onto the flat `codebase_repos` row — the
row's column set is unchanged (still 41). Own-vocabulary literals (`probe`, `skip_reason`,
`census_failure_kind`) and the `model` provenance id are closed-vocabulary values and pass the
backstop unmangled; census prose (`themes`, `minable_ideas`, `material_summary`,
`census_error_detail`) is sentence-validated and dropped rather than partially scrubbed.

## MEASURE additions

- `capacity` — deterministic count of mineable commits across history, taken from `git_history`'s
  `mineable_commits`, and **null** when that lane could not run. This is the same source
  measure-int promotes, deliberately: it used to be git_stats' `confirmed_candidate_count`, a
  different quantity under the same column name, and since grade multiplies capacity into
  `mining_rank` the two instruments produced rankings that looked comparable and were not.
- `build_ok`, `testable_at_head` — from the deterministic build probe, and therefore **null**
  unless `--build` is passed (null = unmeasured, per the CONTRACT null-vs-0 rule).
- `ext_signals.build.build_level` / `.build_level_requested` / `.build_level_fallback_reason` —
  which level the probe ACTUALLY ran at, which one was asked for, and why they differ. The default
  is `full`; a `full` run that does not finish inside `--full-attempt-seconds` completes at
  `discover` and says so here. Without the pair, a `discover` record a vendor chose is
  indistinguishable from a `full` attempt this run could not finish, and only the second leaves
  `observed_runnability` unscored for a reason the operator can act on (raise the budget).
- `ext_signals.build.discover_runnability` — integer 0–3, `install_ok + build_ok +
  tests_discovered`. All three terms are EXECUTED at `--build discover`, so this is the index a run
  that never ran a suite can still honestly report, and it is what keeps a fallen-back grade
  comparable. **Null** — never a lower count — when `install_ok` or `build_ok` is null, which is
  what the probe reports when the runner or an incomplete scan was the limit. Graded by
  `A_discover` at 8 of 100 in `ext-diligence-v5` ≥ 5.2.0. Measured on the 278 TRAIN repositories:
  install_ok rho +0.141, build_ok +0.131, tests_discovered +0.156.
- `ext_signals.build.observed_runnability` — integer 0–4, unchanged, and graded by `A0` as a step
  at band 4 (14 of 100 in ≥ 5.2.0, down from 22 to fund `A_discover`). It is a real low number when
  the REPOSITORY is why no suite ran — a build that genuinely fails, a project the runner found no
  tests in — and that is meant to cost points. It is **null with a reason** when WE are why: the
  clock, a missing runtime, an unreachable registry, or a build verdict that could not be
  established across the tree. `observed_runnability_reason` carries which, derived from the
  `attribution` every phase records. Summing our absences as zeros is the arithmetic that graded a
  repository which installed, built, discovered 441 tests and passed all 441 at 13.7 out of 100.
- `ext_signals.build.run_budget_exhausted` — bool. True when the probe's own clock, rather than the
  cap on how many roots may be probed, stopped work. It is the one fact that distinguishes a tree a
  cheaper level would measure MORE of from one it would not, and it is what `measure.py`'s
  `build_lane` reads to decide whether to fall back.
- `tree.jvm_dotnet_loc_share` — float 0–1, the share of counted LOC written in the JVM/.NET
  family `{Java, Kotlin, Scala, C#}`, computed in `collectors/repo_stats.py` from
  `loc_by_language` over `total_loc` and declared as a `NUMBER` in `emit_allowlist._TREE`. It is
  **null** only when nothing was counted at all, and `0.0` — a real measurement — otherwise. That
  distinction is the reason the field exists at all: a rubric gate cannot be written against
  `tree.loc_by_language.Java`, because an absent language key resolves as *unmeasured* rather
  than zero and would drop every non-Java repository into a PARTIAL gate verdict, making the
  scores non-comparable. One always-emitted scalar has no such hole. Groovy is deliberately
  excluded: `LANGUAGE_BY_EXT` has no Groovy entry, so a `.groovy` file is never counted anywhere
  in `loc_by_language`, and naming it would imply a measurement that is not made. No rubric
  currently gates on this field — see `grade-ext/rubrics/ext-diligence-v5.json`'s `pending_gates`
  for the measured reason why not.

## null vs 0 (honored per CONTRACT)

- `test_loc` — null: test LOC is not separately summed. `0` would falsely claim "measured none".
- `excluded_loc` — null: generated files are counted, not summed as LOC.
- `zip_bytes` — null: no bundle is produced.
- `frontend_pct` / `backend_pct` — null: no clean deterministic LOC split is measured.
- `pr_count` / `issue_count` — null: provider (GitHub/GitLab) tables are not consulted.
- `id` / `codebase_id` / `service_id` — null: assigned by the platform on ingest.

## Privacy (EXTERNAL variant) — the emission allowlist

`collectors/emit_allowlist.py` is the boundary. Every leaf key of every emitted document is
declared there with the kind of thing it is allowed to be, and `measure.py` runs `enforce()` over
all three documents immediately before writing.

| kind | means | example fields |
|---|---|---|
| number / boolean | a measurement | `total_loc`, `commit_count`, `ci_present` |
| closed-vocabulary enum | a value matched against one of OUR tables of public technology names | `primary_language`, `detected_frameworks`, `toolchain`, `census_failure_kind` |
| digest / handle | the content digest and the `repo-<digest[:12]>` display handle | `repo_digest`, `fake_repo_name`, `repo_id` |
| sentence | ONE line describing the SHAPE of a piece of work | `mined_task_summaries[]`, `minable_ideas.*[]`, `themes[]` |
| not collected | a retained schema key, permanently null/empty, with a stated reason | `hardcoded_secret_hits`, `env_vars_referenced_in_source`, `top_largest_files[].path` |

Rules that follow from that:

- **Undeclared is fatal.** A field a collector grows later raises `EmissionRefused` and the run
  writes nothing. The previous boundary was a denylist of eight key names, which let anything new
  through by default.
- **Keys are validated, not only values.** A dynamic map (`loc_by_language`, `iac_loc_by_type`,
  `linters_and_formatters`, `class_confidence`) may only use keys from its declared vocabulary;
  anything else is folded into `other`.
- **Sentences are gated, not scrubbed.** `validate_sentence()` rejects path separators, dotted
  identifiers, snake_case / camelCase tokens, file extensions, quoted strings, acronyms,
  capitalised proper nouns, and anything over 40 words. A rejected sentence is DROPPED and the
  count keeps its place — never emitted with a placeholder where the interesting noun was.
- **Never collected, and it says so.** There is no credential scan of any kind in this skill; no
  author name or address; no commit subject or message; no branch or tag name; no file or
  directory path; no environment-variable name. Those keys stay in the schema as null/empty and
  `--review` prints the reason for each.
- **Backstop.** `redact.py`'s scrub — now applied to undeclared dict KEYS as well as to string
  values — and the `audit_no_leak` write-boundary audit still run behind the allowlist and raise
  `LeakDetected`, a subclass of `EmissionRefused`. They are defence in depth, not the boundary.

`real_repo_name` is always null; the display handle is `fake_repo_name = "repo-<digest[:12]>"`.

## Review before you send

Every run prints a REVIEW section: how many numbers, booleans, enums and timestamps the artifact
holds, the digest and handle in full, every one-line description verbatim, and the fields that
are never collected with the reason for each. `--review` additionally prints every field and its
value. Nothing is uploaded, and this skill never deletes anything it wrote — the files belong to
whoever ran it, to read, keep, or send.

## The task-type census (`codebase_repo_mining.json`)

Keys: `repo_id` (= `fake_repo_name`), `measurer_version`, `provider`, `model`, `mined_at`,
`total_candidates`, the four `n_*` counts, `scored`, `mined_task_summaries`, and — when the lane
did not produce measurements — `skip_reason` and/or `mine_unavailable_reason`.

The mining lane is a CENSUS, not a shortlist. The agent enumerates every task idea the repository
can support, up to `--mine-n` (default 40), and classifies each into one of the four declared
types: `net_new`, `agentic`, `bug_repair`, `repo_evolution`. Each type's `n_*` column is the number of ideas of that type, and every surviving
sentence is emitted as `"<task_type>: <sentence>"` in `mined_task_summaries`, so a count can be
audited by reading the sentences carrying its tag — the same "the count IS the list" property the
material census has.

A sentence describes the engineering SHAPE of the work ("add cursor pagination to a list endpoint
backed by a relational store"). One that names a file, path, symbol, product or company is dropped
whole; the idea is still counted, it arrives without a description. An unknown tag is not
dropped — it stops the run, because a type nobody declared is a count nobody can read.

### Three states, and why the counts differ between them

| what happened | `total_candidates` and every `n_*` | how a reader tells |
|---|---|---|
| the lane ran | numeric; `0` means measured-none | neither reason key is present |
| the operator deselected it (`--no-llm`, `--no-mine`) | `0` | `skip_reason` names the flag, and `mine_unavailable_reason` says the zeros mean not-run |
| the lane could not finish (timeout, dead CLI, unparseable output) | **null** | `mine_unavailable_reason` carries the failure |

The verdict comes from the miner's own completion marker, not from its exit status. Every failure
mode inside the miner degrades to an empty candidate list plus a note, so the exit status cannot
distinguish "looked and found nothing" from "never finished looking". The marker can.

The flat row's `status` is `measured` only when every lane ran, and `partial` otherwise —
including when the operator deselected a lane. `skip_reason` says which kind of gap it is:
`llm_disabled` / `mine_disabled` for a lane you turned off, `lanes_unavailable` for one that could
not run, `lanes_timed_out` for one that hit its ceiling. This is one step stricter than
measure-int, which does not count a deselected lane against the run; the reason is the audience.
measure-int's row is read by our own orchestrator, which knows what it asked for. This row is read
by a buyer, and `measured` on a record whose largest measurement is absent is a claim we have not
earned.

## Providers

`--provider claude|codex|gemini`, each with a real modern invocation:

| provider | invocation | answer read from |
|---|---|---|
| claude | `claude -p <prompt> --output-format json --add-dir <repo> --add-dir <history> --allowedTools "Read Grep Glob" --safe-mode --strict-mcp-config --no-session-persistence --model <id>` | `.result` |
| codex | `codex exec --ephemeral --sandbox read-only --skip-git-repo-check --ignore-rules -c project_doc_max_bytes=0 --model <id> --output-last-message <file> -` (prompt on stdin) | that file |
| gemini | refused for the model lanes | n/a |

No `Bash`. `--add-dir` bounds `Read`, `Grep` and `Glob` and never bounded `Bash`, so that grant
was a shell on the operator's machine confined by the model's own judgement. `<history>` is the
scratch directory `collectors/history_brief.py` writes the development history into before the
session starts, and deletes when the lane returns.

The isolation flags come from `childenv.isolation_flags`, which checks them against the installed
binary's own `--help` rather than assuming them. They exist because a `.claude/settings.json`
committed to the measured repository supplies hooks and the CLI runs them. A CLI that does not
advertise them raises `ProviderNotIsolated` and the run stops.

`gemini` is refused rather than degraded. Verified against the gemini CLI source at v0.54.4: its
only mechanism for ignoring repository-supplied configuration is folder trust, which is
all-or-nothing and makes a headless `-p` run refuse to start when the folder is untrusted, and
there is no `--safe-mode`, no `--setting-sources`, no way to disable `GEMINI.md` discovery from
the command line and no way to stop it writing a session transcript.

A requested provider whose CLI is not installed raises `ProviderUnavailable` **before any
measurement runs** and exits non-zero. `provider` / `model` record what was REQUESTED; nothing
ever substitutes another provider or model, so they are also what was used whenever the lane's
`scored` (or `ok`) flag is true, and nothing was used when it is false.

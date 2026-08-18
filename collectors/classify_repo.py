#!/usr/bin/env python3
"""
classify_repo.py — assign a repository (or a sub-tree of a monorepo) to one or
more of the eight quality classes, for the repo-quality-score skill.

Usage:
    python classify_repo.py <repo-stats-json> [--git-stats <git-stats-json>]
                            [--threshold FLOAT]

Positional arguments:
    repo-stats-json     Path to a JSON file produced by repo_stats.py (use "-" to
                        read from stdin). The `class_signals` block is the input.

Options:
    --git-stats PATH    Optional path to a git_stats.py JSON file. Currently only
                        used to enrich the output metadata; classification is based
                        on the static tree signals.
    --threshold FLOAT   Minimum normalized confidence for a class to be "detected"
                        (default 0.18). A class also needs a minimum absolute signal
                        strength (raw >= 2.0) and either two independent supporting
                        signal families or one strong family, so weak noise doesn't
                        register.

Environment variables: none. Read-only, no network, no secrets.

The eight classes: frontend, backend, fullstack, ml, ai_research,
data_engineering, security, infra.

Output (JSON to stdout):
    {
      "class_confidence": {class: 0-1, ...},     # normalized over atomic classes
      "raw_scores": {class: float, ...},
      "primary_class": "...",
      "suggested_classes": ["..."],              # after the fullstack-collapse rule
      "is_monorepo": bool,                        # >= 2 suggested classes
      "notes": ["..."]
    }

Evidence rules (a single keyword must never name a repository):
  * frontend requires frontend material — component files, real CSS weight, or a
    meaningful share of web-language LOC. Without it the frontend signals are
    dropped outright.
  * the primary class must be supported by two independent signal families, or by
    one family strong enough to outweigh a corroborated rival. Otherwise the
    strongest corroborated class wins, or the repo falls back to the
    general-purpose 'backend' default.

ml vs ai_research and the fullstack collapse are heuristic starting points. The
skill's agent confirms or overrides them, and for true monorepos re-runs
repo_stats.py / classify_repo.py per sub-directory (apps/*, packages/*, services/*)
to get a real per-component breakdown.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ATOMIC_CLASSES = [
    "frontend", "backend", "ml", "ai_research",
    "data_engineering", "security", "infra",
]

FRONTEND_FRAMEWORK_MARKERS = {
    "React", "Vue", "Vue CLI", "Angular", "SvelteKit", "Astro", "Gatsby",
    "Next.js", "Nuxt", "Remix", "Tailwind CSS",
}
BACKEND_FRAMEWORK_MARKERS = {
    "Express", "NestJS", "FastAPI", "Flask", "Django", "Fastify", "Hono",
    "Koa", "Maven (Java)", "Ruby (Bundler)", "WSGI (Flask/Django)",
    "ASGI (FastAPI/Django Channels)", "tRPC",
}

# Languages a frontend is necessarily written in. CSS and HTML are not in
# repo_stats' CODE_EXTENSIONS, so CSS arrives separately as css_loc_ratio.
WEB_LANGUAGES = {"TypeScript", "JavaScript", "Vue", "Svelte"}

# A frontend needs frontend material: component files, real CSS weight, or a
# meaningful share of web-language LOC. Below all three, keyword and framework
# hits are noise — this is what stopped pytest (100% Python, zero components)
# being called a frontend off one dependency-keyword hit.
MIN_FRONTEND_WEB_LOC_SHARE = 0.10
MIN_FRONTEND_CSS_RATIO = 0.02

# A term counts as independent support only if it is worth at least this much,
# so a rounding-error contribution (css_loc_ratio of 0.004) is not "a second signal".
SUPPORT_TERM_FLOOR = 0.5

# A class supported by exactly ONE signal family must clear this to name the
# primary class over a corroborated rival.
MIN_SINGLE_FAMILY_RAW = 4.0

# Below this the strongest class is not evidence of anything.
MIN_PRIMARY_RAW = 1.0


def capped(value: float, per: float, cap: float) -> float:
    """Linear contribution `value * per`, clamped to `cap`. Keeps any single
    high-count signal from dominating the classification."""
    return min(value * per, cap)


def has_frontend_material(stats: dict) -> bool:
    """Does the repo actually contain frontend code?"""
    cs = stats.get("class_signals", {}) or {}
    loc_by_language = stats.get("loc_by_language", {}) or {}
    total_loc = sum(loc_by_language.values())
    web_loc = sum(v for k, v in loc_by_language.items() if k in WEB_LANGUAGES)
    web_share = (web_loc / total_loc) if total_loc > 0 else 0.0
    return (
        cs.get("ui_component_file_count", 0) > 0
        or web_share >= MIN_FRONTEND_WEB_LOC_SHARE
        or cs.get("css_loc_ratio", 0.0) >= MIN_FRONTEND_CSS_RATIO
    )


def compute_class_terms(stats: dict) -> dict[str, dict[str, float]]:
    """Per-class score broken into named terms.

    Kept as terms rather than a single sum so the classifier can see HOW MANY
    independent signal families back a class, not just how big the total is.
    """
    cs = stats.get("class_signals", {}) or {}
    hits = cs.get("dep_keyword_hits", {}) or {}
    frameworks = set(stats.get("detected_frameworks", []) or [])
    project_type = stats.get("project_type", "")

    def n(group: str) -> int:
        return len(hits.get(group, []))

    fe_markers = len(FRONTEND_FRAMEWORK_MARKERS & frameworks)
    be_markers = len(BACKEND_FRAMEWORK_MARKERS & frameworks)

    frontend = {
        "dep_keywords": capped(n("frontend_frameworks"), 1.5, 6.0),
        "ui_components": capped(cs.get("ui_component_file_count", 0), 0.1, 5.0),
        "css": capped(cs.get("css_loc_ratio", 0.0) * 20.0, 1.0, 3.0),
        "frameworks": capped(fe_markers, 1.0, 3.0),
    }

    backend = {
        "dep_keywords": capped(n("backend_frameworks"), 1.5, 6.0),
        "orm_db": capped(n("orm_db"), 1.0, 4.0),
        "frameworks": capped(be_markers, 1.0, 3.0),
        "project_type": 3.0 if project_type == "API service" else 0.0,
    }

    ml = {
        "dep_keywords": capped(n("ml_libs"), 2.0, 8.0),
        "notebooks": capped(cs.get("notebook_count", 0), 0.3, 4.0),
        "experiment_tracking": capped(n("experiment_tracking"), 1.0, 4.0),
    }

    ai_research = {
        "experiment_tracking": capped(n("experiment_tracking"), 2.0, 6.0),
        "notebooks": capped(cs.get("notebook_count", 0), 0.5, 5.0),
        "ml_libs": capped(n("ml_libs"), 1.0, 4.0),
    }

    data_engineering = {
        "dep_keywords": capped(n("data_eng"), 2.5, 9.0),
        "sql_files": capped(cs.get("sql_file_count", 0), 0.3, 4.0),
        "sql_loc": capped(cs.get("sql_loc", 0) / 200.0, 1.0, 3.0),
        "data_files": capped(cs.get("data_file_count", 0), 0.5, 2.0),
    }

    security = {"dep_keywords": capped(n("security_libs"), 3.0, 10.0)}

    # IaC dominance: what fraction of the repo's LOC is Infrastructure-as-Code.
    # Excludes Dockerfiles from the numerator — a Dockerfile is ubiquitous in app
    # repos and is a weak infra signal (it keeps its small count-based term below).
    total_loc = stats.get("total_loc", 0) or 0
    iac_by_type = cs.get("iac_loc_by_type", {}) or {}
    non_docker_iac = sum(v for k, v in iac_by_type.items() if k != "Dockerfile")
    iac_share = (non_docker_iac / total_loc) if total_loc > 0 else 0.0

    infra = {
        "terraform": (4.0 if cs.get("terraform_present") else 0.0)
        + capped(cs.get("terraform_file_count", 0), 0.3, 4.0),
        "k8s": capped(cs.get("k8s_manifest_count", 0), 0.5, 4.0),
        "helm": 2.0 if cs.get("helm_present") else 0.0,
        "pulumi": 2.0 if cs.get("pulumi_present") else 0.0,
        "ansible": 2.0 if cs.get("ansible_present") else 0.0,
        "dep_keywords": capped(n("infra_libs"), 1.0, 3.0),
        "dockerfiles": capped(cs.get("dockerfile_count", 0), 0.5, 1.5),
        # IaC volume as a SHARE of the repo: strong only when manifests/HCL are the
        # bulk of the code (compose-only, k8s-only, terraform-only repos → ~1.0),
        # negligible for an app repo that merely ships a few deploy manifests.
        "iac_share": capped(iac_share * 8.0, 1.0, 6.0),
        "cloudformation": capped(cs.get("cloudformation_file_count", 0), 0.5, 2.0),
    }

    terms = {
        "frontend": frontend,
        "backend": backend,
        "ml": ml,
        "ai_research": ai_research,
        "data_engineering": data_engineering,
        "security": security,
        "infra": infra,
    }
    return {c: {k: round(v, 3) for k, v in t.items()} for c, t in terms.items()}



# The evidence total below which we do not claim to know the class. Chosen as the weakest single
# corroborated signal the scorer can emit; anything under it is one trace term, not a verdict.
MIN_CONFIDENCE_EVIDENCE = 1.0

def choose_primary(
    raw: dict[str, float], support: dict[str, int], notes: list[str]
) -> str | None:
    """The strongest class that is actually evidenced, or None if none is.

    One dependency keyword is not a classification. A class named primary must
    either be supported by two independent signal families or be strong enough on
    its own family (MIN_SINGLE_FAMILY_RAW) to outweigh a corroborated rival.
    """
    ranked = sorted(ATOMIC_CLASSES, key=lambda c: raw[c], reverse=True)

    def evidenced(c: str) -> bool:
        return raw[c] >= MIN_PRIMARY_RAW and (
            support[c] >= 2 or raw[c] >= MIN_SINGLE_FAMILY_RAW
        )

    top = ranked[0]
    if raw[top] < MIN_PRIMARY_RAW:
        return None
    if evidenced(top):
        return top

    corroborated = next((c for c in ranked[1:] if evidenced(c)), None)
    if corroborated:
        notes.append(
            f"'{top}' scored highest ({raw[top]}) but on a single weak signal; "
            f"'{corroborated}' is corroborated by independent signals and was made "
            "primary instead."
        )
        return corroborated

    notes.append(
        f"Primary class '{top}' rests on a single weak signal (raw {raw[top]}). "
        "Low confidence — the agent should confirm it against the source."
    )
    return top


def classify(stats: dict, threshold: float) -> dict:
    notes: list[str] = []
    terms = compute_class_terms(stats)

    # Physical prerequisite: no frontend code, no frontend class. Dependency
    # keywords and framework markers alone cannot conjure a UI, so they are dropped
    # when the tree holds no frontend material.
    conjured = terms["frontend"]["dep_keywords"] + terms["frontend"]["frameworks"]
    if conjured > 0 and not has_frontend_material(stats):
        terms["frontend"]["dep_keywords"] = 0.0
        terms["frontend"]["frameworks"] = 0.0
        notes.append(
            "Frontend dependency/framework signals were discarded: the repo has no "
            "component files, negligible CSS and almost no web-language LOC."
        )

    raw = {c: round(sum(t.values()), 3) for c, t in terms.items()}
    support = {
        c: sum(1 for v in t.values() if v >= SUPPORT_TERM_FLOOR) for c, t in terms.items()
    }
    total = sum(raw.values())

    if total <= 0:
        confidence = {c: 0.0 for c in ATOMIC_CLASSES}
        primary = "backend"
        notes.append(
            "No strong class signals found — likely a general-purpose library/CLI. "
            "Defaulted primary to 'backend' (general code). Agent should confirm."
        )
        suggested = ["backend"]
        return {
            "class_confidence": confidence,
            "raw_scores": raw,
            "primary_class": primary,
            "suggested_classes": suggested,
            "is_monorepo": False,
            "notes": notes,
        }

    # Normalising by the total makes a single trace signal look certain: psf/requests scored
    # frontend 0.016 from its documentation theme's CSS and nothing else, and came out
    # {"frontend": 1.0}. Both graders consume this -- grade-ext writes it into class_scores,
    # grade-int multiplies by it -- so a rounding artefact was reweighting real scores. Confidence
    # is now the share of a floor, so a corpus of noise reports near-zero confidence in everything.
    denominator = max(total, MIN_CONFIDENCE_EVIDENCE)
    confidence = {c: round(v / denominator, 4) for c, v in raw.items()}
    primary = choose_primary(raw, support, notes)

    if primary is None:
        notes.append(
            "No class cleared the minimum evidence bar — likely a general-purpose "
            "library/CLI. Defaulted primary to 'backend' (general code). Agent "
            "should confirm."
        )
        return {
            "class_confidence": confidence,
            "raw_scores": raw,
            "primary_class": "backend",
            "suggested_classes": ["backend"],
            "is_monorepo": False,
            "notes": notes,
        }

    # Detected = normalized confidence over threshold, meaningful raw strength, and
    # more than one supporting signal family unless the one family is strong.
    detected = [
        c for c in ATOMIC_CLASSES
        if confidence[c] >= threshold and raw[c] >= 2.0
        and (support[c] >= 2 or raw[c] >= MIN_SINGLE_FAMILY_RAW)
    ]
    if primary not in detected:
        detected = sorted(set(detected) | {primary}, key=ATOMIC_CLASSES.index)

    # Fullstack-collapse rule: a repo with strong frontend AND backend and nothing
    # else is one full-stack app, not a two-component monorepo.
    suggested = list(detected)
    if set(detected) == {"frontend", "backend"}:
        suggested = ["fullstack"]
        notes.append(
            "Strong frontend + backend signals with no other class — collapsed to "
            "a single 'fullstack' class."
        )
    elif {"frontend", "backend"}.issubset(set(detected)):
        notes.append(
            "Frontend + backend both present alongside other classes — treated as a "
            "monorepo. Re-run per sub-directory for a true component breakdown."
        )

    if "ml" in detected and "ai_research" in detected:
        notes.append(
            "Both ml and ai_research signals present — these overlap. Pick one as the "
            "dominant class per component based on whether the emphasis is "
            "production/serving (ml) or experiments/reproduction (ai_research)."
        )

    is_monorepo = len(suggested) >= 2

    return {
        "class_confidence": confidence,
        "raw_scores": raw,
        "primary_class": primary,
        "suggested_classes": suggested,
        "is_monorepo": is_monorepo,
        "notes": notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Classify a repo into one or more quality classes."
    )
    parser.add_argument(
        "repo_stats_json",
        help="Path to repo_stats.py JSON output, or '-' for stdin.",
    )
    parser.add_argument("--git-stats", help="Optional git_stats.py JSON path.")
    parser.add_argument("--threshold", type=float, default=0.18)
    args = parser.parse_args()

    if args.repo_stats_json == "-":
        stats = json.load(sys.stdin)
    else:
        path = Path(args.repo_stats_json)
        if not path.exists():
            print(json.dumps({"error": f"path not found: {path}"}))
            return 1
        stats = json.loads(path.read_text(encoding="utf-8", errors="ignore"))

    result = classify(stats, args.threshold)
    result["repo_name"] = stats.get("repo_name")
    result["repo_path"] = stats.get("repo_path")
    result["primary_language"] = stats.get("primary_language")
    result["total_loc"] = stats.get("total_loc")

    if args.git_stats:
        gpath = Path(args.git_stats)
        if gpath.exists():
            result["git_stats_path"] = str(gpath)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

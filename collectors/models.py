#!/usr/bin/env python3
"""models.py -- the one place the model is named.

Four lanes are model-assisted: the material census, the build probe, the two history judgements
and the structure lane's zero-code challenge. Each of them used to carry its own default, and
they had already drifted -- two lanes on a small model, two on a large one -- which meant a run
reported a single `census_model` while four different models had in fact contributed to the
score. That is not a reproducibility footnote; scores produced under different models are not
comparable, and a run that cannot say which model produced it cannot be compared to anything.

So there is one default here and every lane imports it. `run.py --model` overrides it for the
whole run, and `RQE_MODEL` overrides it for a machine that wants a different default without
editing a command line. A lane may still be handed a different model explicitly by a caller who
means it -- what is no longer possible is a lane quietly choosing one of its own.

Why a pinned id and not an alias: an alias like `opus` resolves to whatever the newest Opus
happens to be on the day it runs, so a rubric calibrated in one week silently scores against a
different model the next. The exact id is recorded in the output for the same reason.
"""
from __future__ import annotations

import os

# The default for every model-assisted lane. Pinned deliberately -- see the docstring.
DEFAULT_MODEL = os.environ.get("RQE_MODEL", "claude-opus-5")


def resolve(model: str | None = None) -> str:
    """The model a lane should use: what it was handed, else the one default."""
    return model or DEFAULT_MODEL

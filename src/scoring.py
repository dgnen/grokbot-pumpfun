"""Scoring matrix. Code, no LLM.

Folds four components — audit, narrative, timing, metrics — into one
0..1 number with weights from the config. This is a cheap gate before
the expensive checker: anything that did not reach
`filter.min_total_score` is logged as a skip with a per-component
breakdown and never reaches grok-4.

Weights from the config are normalized: if the user writes
0.5/0.5/0.5/0.5, the total still stays in 0..1, and the ratios hold.
"""

from __future__ import annotations

from .models import Analysis, Config, Scores, ScoringWeights

# Components that are missing (the agent did not run) count as zero —
# not as the average and not as "skip the component". Absence of a
# signal is not an argument for.
MISSING_COMPONENT = 0.0


def normalized_weights(weights: ScoringWeights) -> dict[str, float]:
    raw = {
        "audit": max(0.0, weights.audit),
        "narrative": max(0.0, weights.narrative),
        "timing": max(0.0, weights.timing),
        "metrics": max(0.0, weights.metrics),
    }
    total = sum(raw.values())
    if total <= 0:
        # Degenerate config: equal weights beat a divide by zero.
        return dict.fromkeys(raw, 0.25)
    return {key: value / total for key, value in raw.items()}


def compute_scores(analysis: Analysis, config: Config) -> Scores:
    """Broken-out scoring by component plus the total."""
    weights = normalized_weights(config.scoring.weights)

    components = {
        "audit": analysis.audit.score if analysis.audit else MISSING_COMPONENT,
        "narrative": analysis.narrative.score if analysis.narrative else MISSING_COMPONENT,
        "timing": analysis.timing.score if analysis.timing else MISSING_COMPONENT,
        "metrics": analysis.metrics.quality,
    }
    components = {key: _clamp(value) for key, value in components.items()}

    total = sum(components[key] * weights[key] for key in components)

    return Scores(
        audit=round(components["audit"], 4),
        narrative=round(components["narrative"], 4),
        timing=round(components["timing"], 4),
        metrics=round(components["metrics"], 4),
        total=round(_clamp(total), 4),
    )


def passes_threshold(scores: Scores, config: Config) -> tuple[bool, str]:
    """Did the token reach the trip to the adversarial checker."""
    threshold = config.filter.min_total_score
    if scores.total < threshold:
        return False, f"score_below_threshold ({scores.total:.3f} < {threshold:.3f})"
    return True, "ok"


def weakest_component(scores: Scores) -> tuple[str, float]:
    """The weakest component — goes into the log as skip-reason detail."""
    named = {
        "audit": scores.audit,
        "narrative": scores.narrative,
        "timing": scores.timing,
        "metrics": scores.metrics,
    }
    name = min(named, key=lambda key: named[key])
    return name, named[name]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))

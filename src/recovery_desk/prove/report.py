"""Rendering: what a judge sees in the terminal, and what the UI reads from disk.

Deliberately stdlib-only. A repository that needs a package installed before it
prints its headline number has already lost the sixty seconds it had.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..config import Policy
from ..contract import (
    build_audit_rows,
    build_decisions,
    build_run,
    metrics_dict,
)
from ..fixtures.generator import Fixture
from ..models import DecisionStatus
from .harness import ArmResult, ablation_delta

RESULTS_DIR = Path("results")
REPORTS_DIR = RESULTS_DIR / "reports"
RUNS_CSV = RESULTS_DIR / "runs.csv"

RUNS_COLUMNS = [
    "recorded_at",
    "fixture_id",
    "policy_version",
    "arm",
    "budget",
    "gross_recovered",
    "net_recovered",
    "recovery_rate",
    "wasted_spend",
    "chase_precision",
    "contacts_per_rupee_recovered",
    "cost_per_rupee_recovered",
    "p95_decision_latency_ms",
    "policy_violations",
    "items_chased",
    "items_suppressed",
    "note",
]


# --------------------------------------------------------------------------
# Terminal rendering
# --------------------------------------------------------------------------


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    align_right: Sequence[int] = (),
) -> str:
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))

    def fmt(row: Sequence[Any]) -> str:
        parts = []
        for index, cell in enumerate(row):
            text = str(cell)
            parts.append(
                text.rjust(widths[index])
                if index in align_right
                else text.ljust(widths[index])
            )
        return "  ".join(parts).rstrip()

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)


def rupees(value: float) -> str:
    return "{:,.0f}".format(value)


def headline(results: Sequence[ArmResult], fixture: Fixture, policy: Policy) -> str:
    """The table a judge sees. Every arm, every metric that matters, no spin."""
    right = (2, 3, 4, 5, 6, 7, 8, 9, 10)
    rows = []
    for result in results:
        m = result.metrics
        rows.append(
            [
                result.arm_id,
                result.name,
                rupees(m.gross_recovered),
                rupees(m.net_recovered),
                "%.1f%%" % (m.recovery_rate * 100),
                rupees(m.wasted_spend),
                "%.1f%%" % (m.chase_precision * 100),
                rupees(m.total_spend),
                "%.4f" % m.cost_per_rupee_recovered,
                "%.2f" % m.p95_decision_latency_ms,
                m.policy_violations,
            ]
        )

    header = (
        "RECOVERY DESK  ---  %s\n"
        "%d at-risk items worth %s, recovery budget %s, policy %s\n"
        % (
            fixture.id,
            fixture.size,
            rupees(fixture.total_at_risk),
            rupees(policy.budget),
            policy.version,
        )
    )
    table = render_table(
        [
            "arm",
            "policy",
            "gross",
            "net",
            "rate",
            "wasted",
            "precision",
            "spend",
            "cost/Re",
            "p95ms",
            "viol",
        ],
        rows,
        right,
    )
    return header + "\n" + table


def suppression_summary(result: ArmResult) -> str:
    """Deliberate non-action, counted and priced. A headline, not a footnote."""
    counts: dict[str, int] = {}
    for decision in result.decisions:
        if decision.status is DecisionStatus.SUPPRESSED and decision.suppression_reason:
            key = decision.suppression_reason.value
            counts[key] = counts.get(key, 0) + 1

    rows = [[reason, count] for reason, count in sorted(counts.items(), key=lambda kv: -kv[1])]
    total = sum(counts.values())
    return (
        "DELIBERATELY NOT CHASED  ---  %d of %d items (%s)\n"
        % (total, len(result.decisions), result.arm_id)
        + render_table(["reason", "items"], rows, (1,))
    )


def decision_trace(result: ArmResult, item_id: str) -> str:
    """One item, every action it could have taken, priced. The signature view."""
    decision = next((d for d in result.decisions if d.item_id == item_id), None)
    if decision is None:
        return "no decision for %s" % item_id

    diagnosis = result.diagnoses[item_id]
    rows = []
    for evaluation in decision.ev_table:
        b = evaluation.breakdown
        rows.append(
            [
                evaluation.candidate.action_type.value,
                "%.3f" % b.p_recover,
                rupees(b.gross_value),
                "%.2f" % b.cost,
                "%.2f" % b.fatigue_penalty,
                "%.2f" % b.ev,
                "yes" if evaluation.eligible else (evaluation.block_reason or "no"),
                "<- chosen" if evaluation.candidate.action_type is decision.chosen_action
                and decision.status is DecisionStatus.CHASE
                else "",
            ]
        )

    checks = "\n".join(
        "    [%s] %-22s %s" % ("pass" if c.passed else "FAIL", c.name, c.detail)
        for c in decision.policy_checks
    )
    return (
        "DECISION TRACE  ---  %s\n"
        "  failure     %s (confidence %.2f, %s)\n"
        "  evidence    %s\n"
        "  status      %s\n"
        "  rationale   %s\n\n"
        % (
            item_id,
            diagnosis.failure_class.value,
            diagnosis.confidence,
            diagnosis.classifier_provenance,
            diagnosis.evidence,
            decision.status.value,
            decision.rationale,
        )
        + render_table(
            ["action", "p", "gross", "cost", "fatigue", "EV", "eligible", ""],
            rows,
            (1, 2, 3, 4, 5),
        )
        + ("\n  policy checks\n" + checks if checks else "")
    )


def ablation_summary(results: Sequence[ArmResult]) -> str:
    delta = ablation_delta(list(results))
    if delta is None:
        return (
            "ABLATION  ---  not run.\n"
            "  No B3 arm in this run. Run `eval` (the ablation is always attempted\n"
            "  there) or pass --model to `demo` to measure it."
        )

    by_id = {r.arm_id: r for r in results}
    stats = by_id["B3"].classifier_stats or {}
    calls = stats.get("calls_made", 0)
    fallbacks = stats.get("fallbacks_used", 0)
    rejected = stats.get("rejected_outputs", 0)
    had_key = stats.get("had_api_key", False)

    header = (
        "ABLATION  ---  B3 minus B3* (model classifier vs deterministic)\n"
        "  classifier: %d calls made, %d fallbacks, %d rejected, api key %s\n"
        % (calls, fallbacks, rejected, "present" if had_key else "absent")
    )
    if calls == 0:
        header += (
            "  0 calls succeeded: every item fell back to rules, so B3 is\n"
            "  identical to B3* by construction. This is a measured result, not\n"
            "  a skipped one -- the model was genuinely attempted %d times.\n"
            % fallbacks
        )
    return header + (
        "  net recovered    %+.2f\n"
        "  gross recovered  %+.2f\n"
        "  wasted spend     %+.2f\n"
        "  chase precision  %+.4f"
        % (
            delta["net_recovered"],
            delta["gross_recovered"],
            delta["wasted_spend"],
            delta["chase_precision"],
        )
    )


# --------------------------------------------------------------------------
# Artefacts on disk
# --------------------------------------------------------------------------


def _slug(value: str) -> str:
    """Run ids carry characters a filesystem will not take -- ``:`` and ``*``."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)


def write_run(
    fixture: Fixture,
    results: Sequence[ArmResult],
    policy: Policy,
    primary_arm_id: str | None = None,
    reports_dir: Path = REPORTS_DIR,
) -> Path:
    """Write the three documents the UI reads. Returns the run directory."""
    by_id = {r.arm_id: r for r in results}
    arm = by_id.get(primary_arm_id or "") or by_id.get("B3") or by_id["B3*"]

    run_dir = reports_dir / _slug(arm.run.id)
    run_dir.mkdir(parents=True, exist_ok=True)

    run_doc = build_run(fixture, results, policy, primary_arm_id)
    (run_dir / "run.json").write_text(
        json.dumps(run_doc, indent=1), encoding="utf-8"
    )
    (run_dir / "decisions.json").write_text(
        json.dumps(build_decisions(fixture, arm), indent=1), encoding="utf-8"
    )
    (run_dir / "audit.json").write_text(
        json.dumps(build_audit_rows(arm), indent=1), encoding="utf-8"
    )

    # A stable pointer so the UI and the CLI can find the newest run without
    # guessing at directory names.
    (reports_dir / "latest.json").write_text(
        json.dumps({"run_dir": run_dir.name, "run_id": arm.run.id}, indent=1),
        encoding="utf-8",
    )
    return run_dir


def append_runs_row(
    results: Sequence[ArmResult],
    policy: Policy,
    fixture: Fixture,
    note: str = "",
    path: Path = RUNS_CSV,
) -> None:
    """Append every arm of this run to the versioned results table.

    Including the runs that got worse. A monotonic improvement curve reads as
    fabricated; a real one with visible regressions and an explanation of each
    reads as engineering.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUNS_COLUMNS)
        if not exists:
            writer.writeheader()
        for result in results:
            m = metrics_dict(result.metrics)
            writer.writerow(
                {
                    "recorded_at": recorded_at,
                    "fixture_id": fixture.id,
                    "policy_version": policy.version,
                    "arm": result.arm_id,
                    "budget": policy.budget,
                    "gross_recovered": m["gross_recovered"],
                    "net_recovered": m["net_recovered"],
                    "recovery_rate": m["recovery_rate"],
                    "wasted_spend": m["wasted_spend"],
                    "chase_precision": m["chase_precision"],
                    "contacts_per_rupee_recovered": m[
                        "contacts_per_rupee_recovered"
                    ],
                    "cost_per_rupee_recovered": m["cost_per_rupee_recovered"],
                    "p95_decision_latency_ms": m["p95_decision_latency_ms"],
                    "policy_violations": m["policy_violations"],
                    "items_chased": m["items_chased"],
                    "items_suppressed": m["items_suppressed"],
                    "note": note,
                }
            )

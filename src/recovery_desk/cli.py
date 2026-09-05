"""One command in, one result table out.

A judge triaging hundreds of repositories spends sixty to ninety seconds
deciding whether to watch the video. This entry point exists so that in those
sixty seconds the repository clones, runs, prints the headline result and
exits cleanly, with no install step and no configuration.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DEFAULT_BATCH_SIZE, DEFAULT_SEED, Policy
from .contract import build_decisions, build_workspace
from .diagnose.classifier import DeterministicClassifier, ModelClassifier
from .fixtures.generator import generate
from .models import DecisionStatus
from .prove import report as report_module
from .prove.harness import ArmResult, run_arm, run_all
from .decide.strategies import RecoveryDesk

#: Defaults tuned so the demo shows every decision state at once: chased items
#: spanning high and low EV, negative-EV suppression, unrecoverable-class
#: suppression, and a budget tight enough that budget-exhausted suppression
#: actually binds. The default policy's Rs2,500 budget does not bind until the
#: pool is much larger, so the UI demo uses its own smaller, tighter pair.
UI_DEFAULT_SIZE = 350
UI_DEFAULT_BUDGET = 900.0


def _policy(args: argparse.Namespace) -> Policy:
    return Policy(budget=args.budget) if args.budget else Policy()


def _classifier(args: argparse.Namespace) -> ModelClassifier | None:
    if not args.model:
        return None
    classifier = ModelClassifier()
    if not classifier.available:
        print(
            "  note: --model was requested but ANTHROPIC_API_KEY is not set.\n"
            "        Running the deterministic arms only; no claim will be made\n"
            "        about what the model contributes.\n",
            file=sys.stderr,
        )
        return None
    return classifier


def exception_list(fixture, arm: ArmResult) -> str:
    """Where the desk still gets it wrong, by failure class.

    Two distinct failures, and conflating them would hide both. A *miss* is an
    item the desk chased that was genuinely recoverable and did not come back --
    a timing or channel error. A *skip* is a recoverable item the desk never
    touched, which is either correct triage under budget or a pricing error.
    """
    truth = fixture.ground_truth
    rows: dict[str, list[int]] = {}

    recovered = {
        a.item_id for a in arm.attempts if a.outcome.value == "recovered"
    }
    for decision in arm.decisions:
        item_truth = truth[decision.item_id]
        if not item_truth.is_recoverable:
            continue
        failure_class = arm.diagnoses[decision.item_id].failure_class.value
        entry = rows.setdefault(failure_class, [0, 0, 0])
        entry[0] += 1
        if decision.status is DecisionStatus.CHASE:
            if decision.item_id not in recovered:
                entry[1] += 1
        else:
            entry[2] += 1

    table = [
        [
            failure_class,
            counts[0],
            counts[1],
            "%.0f%%" % (100 * counts[1] / counts[0]) if counts[0] else "-",
            counts[2],
            "%.0f%%" % (100 * counts[2] / counts[0]) if counts[0] else "-",
        ]
        for failure_class, counts in sorted(rows.items(), key=lambda kv: -kv[1][0])
    ]
    return (
        "UNRESOLVED EXCEPTIONS  ---  recoverable items the desk did not recover\n"
        + report_module.render_table(
            ["failure class", "recoverable", "chased+missed", "miss%", "skipped", "skip%"],
            table,
            (1, 2, 3, 4, 5),
        )
    )


def regression_table(path: Path, limit: int = 12) -> str:
    """The tail of the versioned results table, regressions included."""
    if not path.exists():
        return "REGRESSION TABLE  ---  no runs recorded yet."
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    if len(lines) < 2:
        return "REGRESSION TABLE  ---  no runs recorded yet."
    header = lines[0].split(",")
    keep = ["recorded_at", "arm", "net_recovered", "wasted_spend", "chase_precision",
            "policy_violations", "note"]
    indices = [header.index(k) for k in keep if k in header]
    rows = [
        [line.split(",")[i] for i in indices] for line in lines[1:][-limit:]
    ]
    return "REGRESSION TABLE  ---  last %d recorded rows\n" % len(rows) + (
        report_module.render_table([header[i] for i in indices], rows)
    )


def command_demo(args: argparse.Namespace) -> int:
    policy = _policy(args)
    fixture = generate(seed=args.seed, size=args.size)
    results = run_all(fixture, policy, _classifier(args))

    print(report_module.headline(results, fixture, policy))
    print()

    desk = next(r for r in results if r.arm_id in ("B3", "B3*"))
    print(report_module.suppression_summary(desk))
    print()

    trace_item = args.item or _pick_trace_item(desk)
    if trace_item:
        print(report_module.decision_trace(desk, trace_item))
        print()

    print(report_module.ablation_summary(results))

    if not args.no_write:
        run_dir = report_module.write_run(fixture, results, policy)
        report_module.append_runs_row(results, policy, fixture, note=args.note)
        print("\nwritten  %s" % run_dir)
        print("appended %s" % report_module.RUNS_CSV)

    return 0


def command_eval(args: argparse.Namespace) -> int:
    policy = _policy(args)
    seeds = args.seeds or [args.seed]
    last_results: list[ArmResult] = []
    last_fixture = None

    for seed in seeds:
        fixture = generate(seed=seed, size=args.size)
        results = run_all(fixture, policy, _classifier(args))
        print(report_module.headline(results, fixture, policy))
        print()
        if not args.no_write:
            report_module.append_runs_row(
                results, policy, fixture, note=args.note or "eval"
            )
        last_results, last_fixture = results, fixture

    desk = next(r for r in last_results if r.arm_id in ("B3", "B3*"))
    print(exception_list(last_fixture, desk))
    print()
    print(report_module.ablation_summary(last_results))
    print()
    print(regression_table(report_module.RUNS_CSV))

    if not args.no_write:
        run_dir = report_module.write_run(last_fixture, last_results, policy)
        print("\nwritten  %s" % run_dir)

    violations = sum(r.metrics.policy_violations for r in last_results)
    if violations:
        print("\nFAIL: %d policy violations. This is a build failure." % violations)
        return 1
    return 0


WEB_DIR = Path(__file__).resolve().parents[2] / "web"
WEB_DATA_DIR = WEB_DIR / "data"


def command_ui(args: argparse.Namespace) -> int:
    """Generate the allocation-workspace data and, unless told not to, serve it.

    Runs the desk on its own deterministic classifier -- no API key is needed to
    see the workspace -- and writes exactly two files the page fetches:
    ``web/data/run.json`` (overview + queue) and ``web/data/decisions.json``
    (the full priced table per item, for the detail panel). Both come straight
    out of ``contract.py``; nothing here computes a decision.
    """
    policy = Policy(budget=args.budget, version="policy-v1")
    fixture = generate(seed=args.seed, size=args.size)
    arm = run_arm(fixture, RecoveryDesk(), policy, DeterministicClassifier(),
                   arm_id="B3*", name="Recovery Desk")

    workspace = build_workspace(fixture, arm, policy)
    decisions = build_decisions(fixture, arm)

    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (WEB_DATA_DIR / "run.json").write_text(
        json.dumps(workspace, indent=1), encoding="utf-8"
    )
    (WEB_DATA_DIR / "decisions.json").write_text(
        json.dumps(decisions, indent=1), encoding="utf-8"
    )

    m = arm.metrics
    print(
        "workspace data written  %s\n"
        "  %d items, Rs%.0f at risk, budget Rs%.0f\n"
        "  chased %d  suppressed %d  recovered %d (%.1f%% of recoverable)\n"
        "  wrote %s\n"
        "  wrote %s"
        % (
            fixture.id, fixture.size, fixture.total_at_risk, policy.budget,
            m.items_chased, m.items_suppressed, m.items_recovered,
            m.recovery_rate * 100,
            WEB_DATA_DIR / "run.json", WEB_DATA_DIR / "decisions.json",
        )
    )

    if args.no_serve:
        return 0

    import functools
    import http.server

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(WEB_DIR)
    )
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as httpd:
        print("\nserving  http://127.0.0.1:%d/  (Ctrl+C to stop)" % args.port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def _pick_trace_item(arm: ArmResult) -> str | None:
    """A chased item with a full table beats a suppressed one for a first look."""
    chased = [
        d for d in arm.decisions
        if d.status is DecisionStatus.CHASE and d.ev_table
    ]
    if chased:
        return max(chased, key=lambda d: d.ev).item_id
    return arm.decisions[0].item_id if arm.decisions else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recovery-desk",
        description="Decide which at-risk revenue is worth recovering, and prove it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--seed", type=int, default=DEFAULT_SEED)
        p.add_argument("--size", type=int, default=DEFAULT_BATCH_SIZE)
        p.add_argument("--budget", type=float, default=None)
        p.add_argument("--model", action="store_true",
                       help="add the B3 model arm (needs ANTHROPIC_API_KEY)")
        p.add_argument("--note", default="", help="note recorded in results/runs.csv")
        p.add_argument("--no-write", action="store_true")

    demo = sub.add_parser("demo", help="seeded batch, all baselines, results table")
    common(demo)
    demo.add_argument("--item", default=None, help="item id to trace in detail")
    demo.set_defaults(func=command_demo)

    evaluate = sub.add_parser(
        "eval", help="full harness, regression table, exception list"
    )
    common(evaluate)
    evaluate.add_argument("--seeds", type=int, nargs="*", default=None)
    evaluate.set_defaults(func=command_eval)

    ui = sub.add_parser(
        "ui", help="generate the allocation-workspace data and serve it"
    )
    ui.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ui.add_argument("--size", type=int, default=UI_DEFAULT_SIZE)
    ui.add_argument("--budget", type=float, default=UI_DEFAULT_BUDGET)
    ui.add_argument("--port", type=int, default=8756)
    ui.add_argument("--no-serve", action="store_true",
                     help="write web/data/*.json and exit without starting a server")
    ui.set_defaults(func=command_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

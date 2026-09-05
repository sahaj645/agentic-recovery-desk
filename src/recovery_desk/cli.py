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

from .config import DEFAULT_BATCH_SIZE, DEFAULT_POLICY, DEFAULT_SEED, Policy
from .contract import build_decisions, build_evidence, build_exceptions, build_workspace, waterline_density
from .diagnose.classifier import DeterministicClassifier, ModelClassifier
from .fixtures.generator import generate
from .fixtures.scenario import HERO_LABELS, generate_scenario
from .models import DecisionStatus
from .prove import report as report_module
from .prove.cases import find_cases
from .prove.harness import ArmResult, run_arm, run_all
from .decide.strategies import RecoveryDesk

#: The scenario the workspace and the video run on. 600 items with a fat
#: high-value tail (~Rs2.6M at risk) and a Rs900 budget: tight enough that ~90
#: positive-value opportunities are outbid at the waterline, so the allocation -- not the
#: detection -- is the visible behaviour. See fixtures/scenario.py for why this
#: is a constructed batch rather than an unbiased draw.
UI_DEFAULT_SIZE = 600
UI_DEFAULT_BUDGET = 900.0


def _policy(args: argparse.Namespace) -> Policy:
    return Policy(budget=args.budget) if args.budget else Policy()


def _classifier(args: argparse.Namespace) -> ModelClassifier | None:
    """Build the B3 classifier if --model was asked for, and let it actually run.

    Previously this returned None outright when no API key was present, which
    meant the B3 arm was silently skipped rather than measured -- there was
    never a real "0 calls, N fallbacks" number to report, only an absent arm.
    The classifier's own classify() already falls back safely per item when it
    has no key, so B3 now genuinely runs: every item is a real attempt, and the
    ablation reports the true, measured result of that attempt -- which, with
    no key, is that B3 is identical to B3* by construction, not by omission.
    """
    if not args.model:
        return None
    classifier = ModelClassifier()
    if not classifier.available:
        print(
            "  note: --model was requested but ANTHROPIC_API_KEY is not set.\n"
            "        The B3 arm will still run -- every item attempts the model\n"
            "        and falls back to rules, and that is reported as a measured\n"
            "        result (0 calls, 100% fallback), not skipped.\n",
            file=sys.stderr,
        )
    return classifier


def exception_list(fixture, arm: ArmResult) -> str:
    """Where the desk still gets it wrong, by failure class.

    Two distinct failures, and conflating them would hide both. A *miss* is an
    item the desk chased that was genuinely recoverable and did not come back --
    a timing or channel error. A *skip* is a recoverable item the desk never
    touched, which is either correct triage under budget or a pricing error.
    The counting lives in ``contract.build_exceptions`` so the CLI's text table
    and the evaluation-replay page can never drift apart on the numbers.
    """
    rows = build_exceptions(fixture, arm)
    table = [
        [
            row["failure_class"],
            row["recoverable"],
            row["chased_but_missed"],
            "%.0f%%" % (row["miss_rate"] * 100) if row["miss_rate"] is not None else "-",
            row["skipped"],
            "%.0f%%" % (row["skip_rate"] * 100) if row["skip_rate"] is not None else "-",
        ]
        for row in rows
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

    # The three-arm ablation the design asks for -- rules-only, the desk
    # without AI, the desk with AI -- runs every time eval runs, not only when
    # --model is passed. It is the point of the harness, not an optional extra,
    # and a real, honestly-measured "0 calls, 100% fallback" result when no key
    # is present is still the correct thing to report every time.
    args.model = True
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

    # The "smaller but better" case needs a budget that genuinely binds against
    # more than one competing item; an unbiased draw at this evaluation's budget
    # does not (see docs/failures.md, F7). find_cases() falls back to the
    # already-committed scarcity scenario for that one case only, and labels it.
    scenario_fixture = generate_scenario(size=UI_DEFAULT_SIZE)
    scenario_result = run_arm(
        scenario_fixture, RecoveryDesk(), Policy(budget=UI_DEFAULT_BUDGET),
        DeterministicClassifier(), arm_id="B3*", name="Recovery Desk",
    )
    cases = find_cases(
        last_fixture, last_results,
        scenario_fixture=scenario_fixture, scenario_result=scenario_result,
    )
    print("\nREPRESENTATIVE CASES  ---  %d found from this run\n" % len(cases))
    for case in cases:
        print("  [%s] %s" % (case.arm_id, case.title))
        print("    item %s  Rs%.2f" % (case.item_id, case.amount))

    if not args.no_write:
        run_dir = report_module.write_run(last_fixture, last_results, policy)
        evidence = build_evidence(last_fixture, last_results, policy, cases)
        evidence_path = report_module.REPORTS_DIR / "evidence.json"
        evidence_path.write_text(json.dumps(evidence, indent=1), encoding="utf-8")
        print("\nwritten  %s" % run_dir)
        print("written  %s" % evidence_path)

    violations = sum(r.metrics.policy_violations for r in last_results)
    if violations:
        print("\nFAIL: %d policy violations. This is a build failure." % violations)
        return 1
    return 0


WEB_DIR = Path(__file__).resolve().parents[2] / "web"
WEB_DATA_DIR = WEB_DIR / "data"


def command_ui(args: argparse.Namespace) -> int:
    """Generate data for both web pages and, unless told not to, serve them.

    Runs the desk on its own deterministic classifier -- no API key is needed to
    see either page -- and writes the files each page fetches. Both come
    straight out of ``contract.py``; nothing here computes a decision.

    Allocation workspace (``index.html``): ``web/data/run.json`` (overview and
    queue) and ``web/data/decisions.json`` (the full priced table per item), from
    the constructed scarcity scenario.

    Evaluation replay (``evaluate.html``): ``web/data/evidence.json`` (the B0-B3
    comparison, the exception list and the representative cases), from the same
    1,000-item unbiased fixture and Rs2,500 budget the README's numbers come
    from -- comparable conditions, not the scarcity scenario.
    """
    policy = Policy(budget=args.budget)
    fixture = generate_scenario(seed=args.seed, size=args.size)
    arm = run_arm(fixture, RecoveryDesk(), policy, DeterministicClassifier(),
                   arm_id="B3*", name="Recovery Desk")

    water = waterline_density(arm)
    workspace = build_workspace(fixture, arm, policy, hero_labels=HERO_LABELS)
    decisions = build_decisions(
        fixture, arm, waterline=water, hero_labels=HERO_LABELS
    )

    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (WEB_DATA_DIR / "run.json").write_text(
        json.dumps(workspace, indent=1), encoding="utf-8"
    )
    (WEB_DATA_DIR / "decisions.json").write_text(
        json.dumps(decisions, indent=1), encoding="utf-8"
    )

    m = arm.metrics
    sc = workspace["overview"]["scarcity"]
    print(
        "workspace data written  %s\n"
        "  %d items, Rs%.0f at risk, budget Rs%.0f\n"
        "  demanded spend Rs%.0f -> budget funds %d of %d worth chasing (%d outbid)\n"
        "  recovered Rs%.0f net on Rs%.0f spent; waterline density Rs%.1f per rupee\n"
        "  wrote %s\n  wrote %s"
        % (
            fixture.id, fixture.size, fixture.total_at_risk, policy.budget,
            sc["demanded_spend"], sc["items_funded"], sc["items_worth_chasing"],
            sc["items_outbid"], m.net_recovered, m.total_spend,
            sc["waterline_density"],
            WEB_DATA_DIR / "run.json", WEB_DATA_DIR / "decisions.json",
        )
    )

    eval_policy = DEFAULT_POLICY
    eval_fixture = generate(seed=DEFAULT_SEED, size=DEFAULT_BATCH_SIZE)
    # Always attempt the model arm here too -- same reasoning as command_eval:
    # a real, measured "0 calls" is honest; silently omitting the arm is not.
    eval_results = run_all(eval_fixture, eval_policy, ModelClassifier())
    eval_cases = find_cases(
        eval_fixture, eval_results, scenario_fixture=fixture, scenario_result=arm
    )
    evidence = build_evidence(eval_fixture, eval_results, eval_policy, eval_cases)
    (WEB_DATA_DIR / "evidence.json").write_text(
        json.dumps(evidence, indent=1), encoding="utf-8"
    )
    desk_eval = next(r for r in eval_results if r.arm_id in ("B3", "B3*"))
    print(
        "\nevaluation data written  %s\n"
        "  B3* recovers %.1f%% of recoverable value vs B2's %.1f%% (%d cases found)\n"
        "  wrote %s"
        % (
            eval_fixture.id, desk_eval.metrics.recovery_rate * 100,
            next(r for r in eval_results if r.arm_id == "B2").metrics.recovery_rate * 100,
            len(eval_cases),
            WEB_DATA_DIR / "evidence.json",
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

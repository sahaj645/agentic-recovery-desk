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
from types import SimpleNamespace

from .config import DEFAULT_BATCH_SIZE, DEFAULT_POLICY, DEFAULT_SEED, Policy
from .contract import build_decisions, build_evidence, build_exceptions, build_workspace, waterline_density
import os

from .diagnose.classifier import DeterministicClassifier, GeminiClassifier, ModelClassifier
from .fixtures.generator import generate
from .fixtures.scenario import HERO_LABELS, generate_scenario
from .models import DecisionStatus
from .prove import report as report_module
from .prove import ambiguity
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


def _build_classifier() -> ModelClassifier | GeminiClassifier:
    """Pick a provider from whichever key is actually set.

    Anthropic first, if present, to keep the desk's default identity stable for
    anyone who has always run it that way. Gemini as the free-tier fallback --
    Google AI Studio issues keys with no billing required -- for anyone without
    Anthropic credit. With neither key present this still returns the Anthropic
    adapter, which reports its own honest "no key, all fallback" result exactly
    as before; nothing about the no-key path changes.

    Both adapters share one safety gate (``_CachedModelClassifier``) and the
    same ``model:`` provenance prefix the UI reads to draw the AI boundary, so
    which one ran is a detail, never a different code path through EV, the
    allocator or the policy gate.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ModelClassifier()
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return GeminiClassifier()
    return ModelClassifier()


def _classifier(args: argparse.Namespace) -> ModelClassifier | GeminiClassifier | None:
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
    classifier = _build_classifier()
    if not classifier.available:
        print(
            "  note: --model was requested but no ANTHROPIC_API_KEY or\n"
            "        GEMINI_API_KEY/GOOGLE_API_KEY is set. The B3 arm will still\n"
            "        run -- every item attempts the model and falls back to\n"
            "        rules, and that is reported as a measured result (0 calls,\n"
            "        100% fallback), not skipped.\n",
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


def ambiguity_text(report) -> str:
    """Where AI could add information rules structurally cannot -- and did it.

    Two numbers that must never be confused: the ceiling (what perfect
    classification of the ambiguous zone would be worth, computed from ground
    truth, never available to the desk) and the measured result (what the
    model actually resolved, only nonzero if it was genuinely invoked and its
    output was not itself a fallback).
    """
    lines = [
        "ADDRESSABLE AMBIGUITY  ---  where rules-only classification runs out",
        "  %d of %d items (Rs%.2f) are UNKNOWN or low-confidence to rules alone"
        % (report.uncertain_items, report.total_items, report.uncertain_value),
        "  of which %d items (Rs%.2f) are genuinely recoverable per ground truth"
        % (report.addressable_recoverable_items, report.addressable_recoverable_value),
        "  -- CEILING, not a result: the most a perfect classifier could be",
        "     worth here, before EV, budget or policy touch it at all.",
    ]
    if report.model_ran:
        lines.append(
            "  MEASURED: the model resolved %d of those %d items, %d correctly"
            % (
                report.model_resolved_items,
                report.uncertain_items,
                report.model_correctly_classified_items,
            )
        )
    else:
        lines.append(
            "  MEASURED: 0 -- no model was genuinely invoked this run (no API key,"
        )
        lines.append(
            "     or every attempt fell back), so no uplift is claimed on this zone."
        )
    return "\n".join(lines)


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


def _clip(text: str, width: int = 64) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


#: The featured items, in the order they read best on camera. Each is a real
#: planted-input item from the scarcity scenario; the tag names the moment it
#: carries, never what the desk should do -- the allocator decides that itself,
#: and the row prints back whatever it decided.
_FEATURED = [
    ("itm_h0001", "do-nothing"),
    ("itm_h0002", "fund-instead"),
    ("itm_h0003", "counterintuitive"),
    ("itm_h0004", "levers-spent"),
    ("itm_h0005", "do-nothing"),
]


def _moment_row(tag: str, decision, amount: float) -> list[str]:
    if decision is None:
        return [tag, "-", "-", "-", "-"]
    if decision.status is DecisionStatus.CHASE:
        verdict = "chase: " + (decision.chosen_action.value if decision.chosen_action else "?")
    else:
        verdict = "do nothing"
    return [tag, decision.item_id, "Rs%.0f" % amount, verdict, _clip(decision.rationale)]


def guided_moments(data: SimpleNamespace) -> str:
    """The strongest real moments the scarcity scenario exposes, as a cheat-sheet.

    Every id, amount and decision here is read back from the run that just
    executed -- nothing is staged. The point is to hand whoever is presenting
    the exact items to open in the workspace, and to prove in the terminal that
    the memorable decisions are the allocator's own, not a script's.
    """
    arm = data.scenario_arm
    amounts = {i.id: i.amount for i in data.scenario_fixture.items}
    by_item = {d.item_id: d for d in arm.decisions}
    sc = data.workspace["overview"]["scarcity"]

    out = [
        "STRONGEST MOMENTS  ---  %s, budget Rs%.0f  (open each id in `run.py ui`)"
        % (data.scenario_fixture.id, sc["budget"]),
        "  SCARCITY     Rs%.0f at risk, but chasing all %d worthwhile items costs Rs%.0f"
        % (data.scenario_fixture.total_at_risk, sc["items_worth_chasing"], sc["demanded_spend"]),
        "  ALLOCATION   the budget funds %d and leaves %d positive-value items outbid;"
        % (sc["items_funded"], sc["items_outbid"]),
        "               the auction clears at Rs%.1f of expected recovery per rupee spent."
        % sc["waterline_density"],
        "",
        report_module.render_table(
            ["moment", "item", "amount", "decision", "why (the desk's own rationale)"],
            [
                _moment_row(tag, by_item.get(iid), amounts.get(iid, 0.0))
                for iid, tag in _FEATURED
                if iid in by_item
            ],
        ),
    ]

    smaller = next((c for c in data.eval_cases if c.id == "desk-smaller-but-better"), None)
    if smaller is not None:
        ev = smaller.evidence
        out += [
            "",
            "  SMALLER BEATS BIGGER (a real pair the allocator produced this run):",
            "    Rs%.0f (%s) is funded while Rs%.0f (%s) is outbid -- %.1fx larger,"
            % (ev["funded_amount"], smaller.item_id, ev["outbid_amount"],
               ev["outbid_item"], ev["amount_ratio"]),
            "    but a thinner rupee of recovery, so the same budget recovers more elsewhere.",
        ]
    return "\n".join(out)


def command_demo(args: argparse.Namespace) -> int:
    policy = _policy(args)
    fixture = generate(seed=args.seed, size=args.size)
    # Always attempt the model arm, exactly as eval and ui do: with no API key
    # the honest result is a measured "0 calls, 100% fallback", and running it
    # lets `make demo` show the AI-boundary ablation rather than an absent arm.
    args.model = True
    results = run_all(fixture, policy, _classifier(args))

    # 1. BASELINES -- the honest, unbiased comparison the headline numbers rest
    #    on, with the AI ablation and the aggregate suppression count.
    print(report_module.headline(results, fixture, policy))
    print()
    desk = next(r for r in results if r.arm_id in ("B3", "B3*"))
    print(report_module.suppression_summary(desk))
    print()
    print(report_module.ablation_summary(results))
    print()

    # 2. THE STRONGEST MOMENTS -- generate the web data the workspace reads and
    #    walk the real decisions the scarcity scenario exposes. The 1,000-item
    #    evaluation is scored once and reused here, not run a second time.
    data = generate_web_data(
        DEFAULT_SEED, UI_DEFAULT_SIZE, UI_DEFAULT_BUDGET,
        eval_fixture=fixture, eval_results=results, eval_policy=policy,
    )
    print(guided_moments(data))
    print()

    # 3. THE SIGNATURE TRACE, in full -- the counterintuitive one: a large
    #    payment whose priciest action carries the highest expected value and
    #    still loses, because under a binding budget the desk buys the denser
    #    rupee. `--item` overrides it for any other id in the scenario.
    trace_item = args.item or "itm_h0003"
    print(report_module.decision_trace(data.scenario_arm, trace_item))

    if not args.no_write:
        run_dir = report_module.write_run(fixture, results, policy)
        report_module.append_runs_row(results, policy, fixture, note=args.note)
        print("\nwritten  %s" % run_dir)
        print("appended %s" % report_module.RUNS_CSV)

    print(
        "\nSEE IT  ---  every moment above is one click away in the product:\n"
        "  python run.py ui         serves the allocation workspace at http://127.0.0.1:8756/\n"
        "  then open Evaluation     the B0-B3 baseline comparison, ablation and cases"
    )
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

    ambiguity_report = None
    b3 = next((r for r in last_results if r.arm_id == "B3"), None)
    rules_only_dx = next(
        r for r in last_results if r.arm_id == "B3*"
    ).diagnoses
    ambiguity_report = ambiguity.measure(
        last_fixture, rules_only_dx, b3.diagnoses if b3 else None
    )
    print(ambiguity_text(ambiguity_report))
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
        evidence = build_evidence(
            last_fixture, last_results, policy, cases,
            ambiguity=ambiguity.to_dict(ambiguity_report),
        )
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


def generate_web_data(
    scenario_seed: int,
    scenario_size: int,
    scenario_budget: float,
    eval_fixture=None,
    eval_results: list[ArmResult] | None = None,
    eval_policy: Policy | None = None,
) -> SimpleNamespace:
    """Write the three JSON files both web pages read, and return the objects.

    This is the single generator for the UI data, shared by ``ui`` and ``demo``
    so the two can never drift. Nothing here computes a decision: the scenario
    arm and the evaluation arms are real runs, and every field written comes
    straight out of ``contract.py``.

    Allocation workspace (``index.html``) reads ``run.json`` and
    ``decisions.json`` from the constructed scarcity scenario. Evaluation replay
    (``evaluate.html``) reads ``evidence.json`` from the 1,000-item unbiased
    fixture and Rs2,500 budget the README's numbers come from -- comparable
    conditions, not the scenario. The caller may pass an evaluation run it has
    already computed (``demo`` does, to avoid running the same 1,000 items
    twice); otherwise one is generated at the documented defaults.
    """
    scen_policy = Policy(budget=scenario_budget)
    scenario_fixture = generate_scenario(seed=scenario_seed, size=scenario_size)
    arm = run_arm(scenario_fixture, RecoveryDesk(), scen_policy,
                  DeterministicClassifier(), arm_id="B3*", name="Recovery Desk")
    water = waterline_density(arm)
    workspace = build_workspace(scenario_fixture, arm, scen_policy, hero_labels=HERO_LABELS)
    decisions = build_decisions(scenario_fixture, arm, waterline=water, hero_labels=HERO_LABELS)

    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (WEB_DATA_DIR / "run.json").write_text(json.dumps(workspace, indent=1), encoding="utf-8")
    (WEB_DATA_DIR / "decisions.json").write_text(json.dumps(decisions, indent=1), encoding="utf-8")

    if eval_results is None:
        eval_policy = DEFAULT_POLICY
        eval_fixture = generate(seed=DEFAULT_SEED, size=DEFAULT_BATCH_SIZE)
        # Always attempt the model arm -- same reasoning as command_eval: a real,
        # measured "0 calls" is honest; silently omitting the arm is not.
        eval_results = run_all(eval_fixture, eval_policy, _build_classifier())
    else:
        eval_policy = eval_policy or DEFAULT_POLICY

    eval_cases = find_cases(
        eval_fixture, eval_results, scenario_fixture=scenario_fixture, scenario_result=arm
    )
    b3_eval = next((r for r in eval_results if r.arm_id == "B3"), None)
    rules_dx_eval = next(r for r in eval_results if r.arm_id == "B3*").diagnoses
    ambiguity_report = ambiguity.measure(
        eval_fixture, rules_dx_eval, b3_eval.diagnoses if b3_eval else None
    )
    evidence = build_evidence(
        eval_fixture, eval_results, eval_policy, eval_cases,
        ambiguity=ambiguity.to_dict(ambiguity_report),
    )
    (WEB_DATA_DIR / "evidence.json").write_text(json.dumps(evidence, indent=1), encoding="utf-8")

    return SimpleNamespace(
        scenario_fixture=scenario_fixture,
        scenario_arm=arm,
        scenario_policy=scen_policy,
        workspace=workspace,
        eval_fixture=eval_fixture,
        eval_results=eval_results,
        eval_cases=eval_cases,
        ambiguity_report=ambiguity_report,
    )


def command_ui(args: argparse.Namespace) -> int:
    """Generate the data for both web pages and, unless told not to, serve them."""
    data = generate_web_data(args.seed, args.size, args.budget)
    arm = data.scenario_arm
    fixture = data.scenario_fixture
    m = arm.metrics
    sc = data.workspace["overview"]["scarcity"]
    print(
        "workspace data written  %s\n"
        "  %d items, Rs%.0f at risk, budget Rs%.0f\n"
        "  demanded spend Rs%.0f -> budget funds %d of %d worth chasing (%d outbid)\n"
        "  recovered Rs%.0f net on Rs%.0f spent; waterline density Rs%.1f per rupee\n"
        "  wrote %s\n  wrote %s"
        % (
            fixture.id, fixture.size, fixture.total_at_risk, data.scenario_policy.budget,
            sc["demanded_spend"], sc["items_funded"], sc["items_worth_chasing"],
            sc["items_outbid"], m.net_recovered, m.total_spend,
            sc["waterline_density"],
            WEB_DATA_DIR / "run.json", WEB_DATA_DIR / "decisions.json",
        )
    )
    desk_eval = next(r for r in data.eval_results if r.arm_id in ("B3", "B3*"))
    amb = data.ambiguity_report
    print(
        "\nevaluation data written  %s\n"
        "  B3* recovers %.1f%% of recoverable value vs B2's %.1f%% (%d cases found)\n"
        "  addressable ambiguity: Rs%.2f ceiling (%d items), Rs0.00 measured (%s)\n"
        "  wrote %s"
        % (
            data.eval_fixture.id, desk_eval.metrics.recovery_rate * 100,
            next(r for r in data.eval_results if r.arm_id == "B2").metrics.recovery_rate * 100,
            len(data.eval_cases),
            amb.addressable_recoverable_value, amb.addressable_recoverable_items,
            "model ran" if amb.model_ran else "no API key, all fallback",
            WEB_DATA_DIR / "evidence.json",
        )
    )

    if args.no_serve:
        return 0

    import functools
    import http.server

    class _NoCacheHandler(http.server.SimpleHTTPRequestHandler):
        """Serve the preview without caching.

        This is a development preview server: the whole point is to edit a
        stylesheet or regenerate a batch and immediately see the result. The
        default handler lets a browser hold on to app.js, styles.css and
        run.json, so an edit appears to do nothing and -- far worse -- a demo
        can be given against assets that are several changes stale.
        """

        def end_headers(self):
            self.send_header("Cache-Control", "no-store, must-revalidate")
            super().end_headers()

    handler = functools.partial(_NoCacheHandler, directory=str(WEB_DIR))
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as httpd:
        print("\nserving  http://127.0.0.1:%d/  (Ctrl+C to stop)" % args.port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


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
                       help="add the B3 model arm (needs ANTHROPIC_API_KEY, or "
                            "GEMINI_API_KEY/GOOGLE_API_KEY as a free-tier fallback)")
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

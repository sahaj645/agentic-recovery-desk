/* Evaluation replay -- reads web/data/evidence.json and renders it.
 *
 * This page computes nothing. Every number, every case, every rationale is a
 * field already produced by a real run of the harness and the case-finder in
 * src/recovery_desk/prove/cases.py. If a figure looks wrong, the fix is in the
 * engine or in what data was generated, never in this file.
 */

(function () {
  "use strict";

  const ARM_ORDER = ["B0", "B1", "B2", "B3*", "B3"];
  const METRIC_COLUMNS = [
    ["gross_recovered", "Gross recovered", "money"],
    ["net_recovered", "Net recovered", "money"],
    ["recovery_rate", "Recovery rate", "pct"],
    ["wasted_spend", "Wasted spend", "money"],
    ["chase_precision", "Chase precision", "pct"],
    ["contacts_per_rupee_recovered", "Contacts / ₹ recovered", "num4"],
    ["cost_per_rupee_recovered", "Cost / ₹ recovered", "num4"],
    ["p95_decision_latency_ms", "p95 latency (ms)", "num2"],
    ["policy_violations", "Policy violations", "int"],
  ];

  const rupees = (v) => "₹" + (Number(v) || 0).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  const fmt = (value, kind) => {
    switch (kind) {
      case "money": return rupees(value);
      case "pct": return (Number(value) * 100).toFixed(1) + "%";
      case "num4": return Number(value).toFixed(4);
      case "num2": return Number(value).toFixed(2);
      case "int": return String(value);
      default: return String(value);
    }
  };

  // A ratio with nothing in its denominator is undefined, not zero. An arm that
  // never chased anything has no chase precision, and one that never recovered
  // anything has no cost per rupee recovered -- B0 is exactly that arm. Printing
  // 0.0000 there reads as a measured result, so these fall back to the same "—"
  // the README uses for the identical cells.
  const UNDEFINED_WHEN = {
    chase_precision: (m) => !m.items_chased,
    contacts_per_rupee_recovered: (m) => !m.items_recovered,
    cost_per_rupee_recovered: (m) => !m.items_recovered,
  };

  const el = (tag, attrs, children) => {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key in attrs) {
        if (key === "class") node.className = attrs[key];
        else if (key === "text") node.textContent = attrs[key];
        else if (key === "html") node.innerHTML = attrs[key];
        else node.setAttribute(key, attrs[key]);
      }
    }
    (children || []).forEach((c) => c && node.appendChild(c));
    return node;
  };

  async function load() {
    const res = await fetch("data/evidence.json");
    if (!res.ok) throw new Error("evidence.json " + res.status);
    return res.json();
  }

  function renderHeader(doc) {
    document.getElementById("brand-sub").textContent =
      doc.fixture.id + " · " + doc.fixture.size + " items · policy " + doc.policy.version;
    document.getElementById("chip-policy").textContent =
      "budget " + rupees(doc.policy.budget);
  }

  function renderArmTable(doc) {
    const arms = doc.proof.arms.slice().sort(
      (a, b) => ARM_ORDER.indexOf(a.arm_id) - ARM_ORDER.indexOf(b.arm_id)
    );
    const table = document.getElementById("arm-table");
    const thead = table.querySelector("thead");
    const tbody = table.querySelector("tbody");
    thead.innerHTML = "";
    tbody.innerHTML = "";

    const headRow = el("tr", {}, [el("th", { text: "Arm" })]);
    METRIC_COLUMNS.forEach(([, label]) => headRow.appendChild(el("th", { class: "num", text: label })));
    thead.appendChild(headRow);

    arms.forEach((arm) => {
      const isPrimary = arm.arm_id === doc.proof.primary_arm;
      const row = el("tr", { class: isPrimary ? "primary" : "" }, [
        el("td", { class: "label", text: arm.arm_id + "  " + arm.name }),
      ]);
      METRIC_COLUMNS.forEach(([key, , kind]) => {
        const undefinedHere = UNDEFINED_WHEN[key] && UNDEFINED_WHEN[key](arm.metrics);
        row.appendChild(
          el("td", {
            class: "num" + (undefinedHere ? " undefined-metric" : ""),
            text: undefinedHere ? "—" : fmt(arm.metrics[key], kind),
          })
        );
      });
      tbody.appendChild(row);
    });

    const totalViolations = arms.reduce((sum, a) => sum + (a.metrics.policy_violations || 0), 0);
    const badge = document.getElementById("violations-badge");
    badge.className = "violations-badge " + (totalViolations === 0 ? "zero" : "nonzero");
    badge.textContent = totalViolations === 0
      ? "0 policy violations across every arm"
      : totalViolations + " policy violations — this is a build failure, not a metric";
  }

  function renderDeltas(doc) {
    const wrap = document.getElementById("deltas");
    wrap.innerHTML = "";
    const primary = doc.proof.primary_arm;
    const labels = { B1: "Blanket retry ×3", B2: "Rules-only heuristic" };

    Object.entries(doc.proof.deltas || {}).forEach(([ref, delta]) => {
      const pct = delta.net_recovered_pct;
      wrap.appendChild(
        el("div", { class: "delta-card" }, [
          el("div", { class: "vs", text: primary + " vs " + ref + " (" + (labels[ref] || ref) + ")" }),
          el("div", {
            class: "headline",
            text: (delta.net_recovered >= 0 ? "+" : "") + rupees(delta.net_recovered) +
              (pct != null ? "  (" + (pct >= 0 ? "+" : "") + pct.toFixed(1) + "%)" : ""),
          }),
          el("div", {
            class: "sub",
            text: "net recovered, more than " + ref + ". Wasted spend " +
              (delta.wasted_spend >= 0 ? "+" : "") + rupees(delta.wasted_spend) +
              ", chase precision " + (delta.chase_precision >= 0 ? "+" : "") +
              (delta.chase_precision * 100).toFixed(1) + "pts.",
          }),
        ])
      );
    });

    if (doc.proof.ablation) {
      const a = doc.proof.ablation;
      wrap.appendChild(
        el("div", { class: "delta-card" }, [
          el("div", { class: "vs", text: "B3 vs B3* (model contribution)" }),
          el("div", { class: "headline", text: (a.net_recovered >= 0 ? "+" : "") + rupees(a.net_recovered) }),
          el("div", { class: "sub", text: doc.proof.ablation_note || "" }),
        ])
      );
    } else {
      wrap.appendChild(
        el("div", { class: "delta-card" }, [
          el("div", { class: "vs", text: "B3 vs B3* (model contribution)" }),
          el("div", { class: "headline", text: "not run", style: "color:var(--ink-faint);font-size:15px;" }),
          el("div", {
            class: "sub",
            text: doc.proof.ablation_note ||
              "No model arm in this run, so no claim is made about what the model contributes.",
          }),
        ])
      );
    }
  }

  function renderAiBoundary(doc) {
    const wrap = document.getElementById("ai-boundary");
    wrap.innerHTML = "";

    const arms = doc.proof.arms;
    const b3 = arms.find((a) => a.arm_id === "B3");
    const stats = b3 && b3.classifier_stats;
    const amb = doc.ambiguity;

    // -- what the model was actually asked to do, and what happened --------
    const statusCard = el("div", { class: "ai-card" });
    statusCard.appendChild(el("div", { class: "ai-card-label", text: "AI understands (diagnosis only)" }));
    if (!b3 || !stats) {
      statusCard.appendChild(
        el("div", { class: "ai-card-body", text: "No B3 arm in this run. Diagnosis ran on rules alone." })
      );
    } else {
      const line = (label, value) =>
        el("div", { class: "ai-stat-row" }, [
          el("span", { class: "ai-stat-label", text: label }),
          el("span", { class: "ai-stat-val", text: value }),
        ]);
      statusCard.appendChild(line("Model", stats.model));
      statusCard.appendChild(line("API key present", stats.had_api_key ? "yes" : "no"));
      statusCard.appendChild(line("Calls made", String(stats.calls_made)));
      statusCard.appendChild(line("Rejected (malformed output)", String(stats.rejected_outputs)));
      statusCard.appendChild(line("Fell back to rules", String(stats.fallbacks_used)));
      if (stats.calls_made === 0) {
        statusCard.appendChild(
          el("div", {
            class: "ai-verdict neutral",
            text: "Every item fell back to rules. B3 is identical to B3* by construction " +
              "— a measured result, not a skipped arm.",
          })
        );
      }
    }
    wrap.appendChild(statusCard);

    // -- economics decides: unaffected regardless of the above -------------
    const econCard = el("div", { class: "ai-card" });
    econCard.appendChild(el("div", { class: "ai-card-label", text: "Economics decides (always deterministic)" }));
    econCard.appendChild(
      el("div", {
        class: "ai-card-body",
        text: "EV, budget allocation and the policy gate never consult a model, in any arm, " +
          "regardless of the diagnosis stats above. Only the failure-class label and confidence " +
          "cross this boundary.",
      })
    );
    wrap.appendChild(econCard);

    // -- the ceiling vs the measured result, kept visually distinct ---------
    if (amb) {
      const ambCard = el("div", { class: "ai-card ceiling" });
      ambCard.appendChild(el("div", { class: "ai-card-label", text: "Addressable ambiguity" }));
      ambCard.appendChild(
        el("div", { class: "ai-stat-row" }, [
          el("span", { class: "ai-stat-label", text: "Unresolved by rules" }),
          el("span", { class: "ai-stat-val", text: amb.uncertain_items + " items · " + rupees(amb.uncertain_value) }),
        ])
      );
      ambCard.appendChild(
        el("div", { class: "ai-stat-row" }, [
          el("span", { class: "ai-stat-label", text: "Ceiling (genuinely recoverable, ground truth)" }),
          el("span", {
            class: "ai-stat-val ceiling-val",
            text: amb.addressable_recoverable_items + " items · " + rupees(amb.addressable_recoverable_value),
          }),
        ])
      );
      ambCard.appendChild(
        el("div", { class: "ai-stat-row" }, [
          el("span", { class: "ai-stat-label", text: "Measured (model actually resolved)" }),
          el("span", {
            class: "ai-stat-val " + (amb.model_ran ? "measured-val" : "measured-zero"),
            text: amb.model_ran
              ? amb.model_resolved_items + " resolved, " + amb.model_correctly_classified_items + " correct"
              : "0 — not genuinely invoked this run",
          }),
        ])
      );
      ambCard.appendChild(
        el("div", {
          class: "ai-verdict " + (amb.model_ran ? "positive" : "neutral"),
          text: amb.model_ran
            ? "AI resolved a measured share of the ambiguous zone this run."
            : "The ceiling is an upper bound, not a claim. No uplift is reported because no model " +
              "genuinely ran against this fixture.",
        })
      );
      wrap.appendChild(ambCard);
    }
  }

  function renderExceptions(doc) {
    const body = document.getElementById("exceptions-body");
    body.innerHTML = "";
    doc.exceptions.forEach((row) => {
      body.appendChild(
        el("tr", {}, [
          el("td", { class: "label", text: row.failure_class.replace(/_/g, " ") }),
          el("td", { class: "num", text: String(row.recoverable) }),
          el("td", { class: "num", text: String(row.chased_but_missed) }),
          el("td", { class: "num", text: row.miss_rate != null ? (row.miss_rate * 100).toFixed(0) + "%" : "—" }),
          el("td", { class: "num", text: String(row.skipped) }),
          el("td", { class: "num", text: row.skip_rate != null ? (row.skip_rate * 100).toFixed(0) + "%" : "—" }),
        ])
      );
    });
  }

  function renderCases(doc) {
    const wrap = document.getElementById("cases");
    wrap.innerHTML = "";
    doc.cases.forEach((c) => {
      const armClass = c.arm_id.toLowerCase().replace("*", "\\*");
      const head = el("div", { class: "case-head" }, [
        el("span", { class: "case-arm-badge " + armClass, text: c.arm_id }),
        el("span", { class: "case-title", text: c.title }),
        el("span", { class: "case-meta", text: c.item_id + "  ·  " + rupees(c.amount) }),
      ]);
      const evidence = el("div", { class: "case-evidence" });
      Object.entries(c.evidence || {}).forEach(([key, value]) => {
        evidence.appendChild(
          el("div", {}, [
            el("span", { class: "ev-key", text: key.replace(/_/g, " ") }),
            el("span", { class: "ev-val", text: String(value) }),
          ])
        );
      });
      wrap.appendChild(
        el("div", { class: "case-card" }, [
          head,
          el("div", { class: "case-narrative", text: c.narrative }),
          evidence,
        ])
      );
    });
  }

  function showState(kind, title, detail) {
    const main = document.querySelector("main");
    const block = el("div", { class: "state-block" + (kind === "error" ? " is-error" : "") });
    if (kind === "loading") block.appendChild(el("div", { class: "state-progress" }));
    block.appendChild(el("div", { class: "state-title", text: title }));
    const detailEl = el("div", { class: "state-detail" });
    detailEl.innerHTML = detail;
    block.appendChild(detailEl);
    main.innerHTML = "";
    main.appendChild(block);
  }

  async function init() {
    const shell = document.querySelector("main").innerHTML;
    showState("loading", "Loading evidence", "Reading the scored comparison for this run.");
    let doc;
    try {
      doc = await load();
    } catch (err) {
      showState(
        "error",
        "Could not load evaluation data",
        "<code>" + err.message + "</code><br>Run <code>python run.py ui</code> or " +
          "<code>python run.py eval</code> to generate it, then reload."
      );
      return;
    }
    document.querySelector("main").innerHTML = shell;
    renderHeader(doc);
    renderArmTable(doc);
    renderDeltas(doc);
    renderAiBoundary(doc);
    renderExceptions(doc);
    renderCases(doc);
  }

  init();
})();

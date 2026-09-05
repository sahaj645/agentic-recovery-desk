/* Recovery Desk -- allocation workspace.
 *
 * Reads two files a real run produced -- data/run.json (overview + queue) and
 * data/decisions.json (the full priced table per item) -- and renders them.
 * Nothing in this file computes a decision, ranks an opportunity, or invents a
 * number: every value on screen traces back to a field in one of those two
 * documents.
 */

(function () {
  "use strict";

  const ACTION_LABEL = {
    do_nothing: "Do nothing",
    retry_now: "Retry now",
    retry_scheduled: "Retry (scheduled)",
    alternate_rail: "Alternate rail",
    nudge_sms: "SMS nudge",
    nudge_whatsapp: "WhatsApp nudge",
    voice_call: "Voice call",
  };

  const FAILURE_LABEL = {
    bank_psp_timeout: "Bank / PSP timeout",
    wrong_pin_or_attempts_exceeded: "Wrong PIN / attempts exceeded",
    insufficient_balance: "Insufficient balance",
    network_connectivity: "Network / connectivity",
    account_blocked_or_frozen: "Account blocked or frozen",
    unknown: "Unknown",
  };

  const SUPPRESSION_LABEL = {
    negative_ev: "Negative EV",
    unrecoverable_class: "Unrecoverable class",
    budget_exhausted: "Outbid on budget",
    contact_cap: "Contact cap reached",
    retry_cap: "Levers spent",
    policy_blocked: "Policy blocked",
    baseline_no_action: "No action taken",
  };

  let RUN = null;
  let DECISIONS = null;
  let currentFilter = "all";
  let currentSearch = "";
  let selectedItemId = null;

  const rupees = (value, opts) => {
    opts = opts || {};
    const n = Number(value) || 0;
    const digits = opts.decimals != null ? opts.decimals : 0;
    return (
      "₹" +
      n.toLocaleString("en-IN", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      })
    );
  };

  const pct = (value, digits) => (Number(value) * 100).toFixed(digits != null ? digits : 1) + "%";

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
    const [run, decisions] = await Promise.all([
      fetch("data/run.json").then((r) => {
        if (!r.ok) throw new Error("run.json " + r.status);
        return r.json();
      }),
      fetch("data/decisions.json").then((r) => {
        if (!r.ok) throw new Error("decisions.json " + r.status);
        return r.json();
      }),
    ]);
    RUN = run;
    DECISIONS = decisions;
    HEROES = {};
    (run.heroes || []).forEach((h) => {
      HEROES[h.item_id] = h.storyline;
    });
  }
  let HEROES = {};

  // ---------------------------------------------------------------- header

  function renderHeader() {
    document.getElementById("brand-sub").textContent =
      RUN.fixture.id + " · " + RUN.fixture.size + " items · " + RUN.arm.name;
    document.getElementById("chip-policy").innerHTML =
      "<span>" + RUN.policy.version + "</span>";
  }

  // ---------------------------------------------------------------- funnel

  function renderFunnel() {
    const ov = RUN.overview;
    const chaseValue = RUN.queue
      .filter((r) => r.status === "chase")
      .reduce((sum, r) => sum + r.amount, 0);

    const stage = (label, value, sub, cls) =>
      el("div", { class: "funnel-stage" }, [
        el("span", { class: "label", text: label }),
        el("span", { class: "value tabular" + (cls ? " " + cls : ""), text: rupees(value) }),
        el("span", { class: "sub", text: sub }),
      ]);

    const arrow = () => el("div", { class: "funnel-arrow", html: "&rarr;" });

    const wrap = document.getElementById("funnel");
    wrap.innerHTML = "";
    wrap.appendChild(stage("Money at risk", ov.total_at_risk, ov.items_at_risk + " items"));
    wrap.appendChild(arrow());
    wrap.appendChild(
      stage("Worth chasing", chaseValue, ov.items_chased + " items", "chase")
    );
    wrap.appendChild(arrow());
    wrap.appendChild(
      stage(
        "Not worth chasing",
        ov.suppressed.value,
        ov.suppressed.count + " items · " + pct(ov.suppressed.share_of_pool) + " of pool",
        "suppress"
      )
    );
    wrap.appendChild(arrow());
    wrap.appendChild(
      stage(
        "Recovered",
        ov.recovered_revenue,
        pct(ov.recovered_revenue / (chaseValue || 1)) + " of chased value",
        "chase"
      )
    );
  }

  // ------------------------------------------------------------- scarcity

  function renderScarcity() {
    const sc = RUN.overview.scarcity;
    const wrap = document.getElementById("scarcity");
    if (!sc) {
      wrap.hidden = true;
      return;
    }
    const binds = !sc.budget_covers_demand;
    wrap.className = "scarcity-banner" + (binds ? " binds" : "");
    wrap.innerHTML = "";
    const line = el("div", { class: "scarcity-line" });
    line.appendChild(
      el("strong", {
        text: binds ? "It cannot chase everyone." : "The budget covers all demand.",
      })
    );
    const detail = binds
      ? "Chasing every worthwhile opportunity would cost " +
        rupees(sc.demanded_spend) +
        "; the budget is " +
        rupees(sc.budget) +
        ". The desk funds " +
        sc.items_funded +
        " of " +
        sc.items_worth_chasing +
        " worth chasing and leaves " +
        sc.items_outbid +
        " positive-value opportunities unfunded — not because they can't be recovered, but because the same rupees recover more elsewhere. The auction clears at " +
        rupees(sc.waterline_density) +
        " of expected recovery per rupee spent."
      : "Every positive-value opportunity is funded; nothing is competing for scarce budget.";
    line.appendChild(el("span", { class: "scarcity-detail", text: " " + detail }));
    wrap.appendChild(line);
  }

  // -------------------------------------------------------------- overview

  function renderOverview() {
    const ov = RUN.overview;
    const stats = [
      ["At-risk revenue", rupees(ov.total_at_risk)],
      ["Recovery budget", rupees(ov.recovery_budget)],
      ["Expected recovery", rupees(ov.expected_recoverable_revenue)],
      ["Net expected recovery", rupees(ov.expected_net_recovery)],
      ["Selected opportunities", ov.items_chased.toLocaleString("en-IN")],
      ["Suppressed opportunities", ov.suppressed.count.toLocaleString("en-IN")],
    ];
    const wrap = document.getElementById("overview");
    wrap.innerHTML = "";
    stats.forEach(([label, value]) => {
      wrap.appendChild(
        el("div", { class: "stat" }, [
          el("span", { class: "label", text: label }),
          el("span", { class: "value tabular", text: value }),
        ])
      );
    });
  }

  // ----------------------------------------------------------------- queue

  function filteredRows() {
    const q = currentSearch.trim().toLowerCase();
    return RUN.queue.filter((row) => {
      if (currentFilter !== "all" && row.status !== currentFilter) return false;
      if (!q) return true;
      return (
        row.item_id.toLowerCase().includes(q) ||
        row.customer_id.toLowerCase().includes(q)
      );
    });
  }

  function renderQueue() {
    const rows = filteredRows();
    const body = document.getElementById("queue-body");
    body.innerHTML = "";

    const maxAbsEv = rows.reduce((m, r) => Math.max(m, Math.abs(r.expected_value)), 1);

    rows.forEach((row) => {
      const tr = el("tr", { "data-item": row.item_id });
      if (row.item_id === selectedItemId) tr.classList.add("selected");

      const idCell = el("td", {}, [
        el("div", { text: row.item_id }),
        el("div", {
          class: "action-tag",
          text: row.customer_id,
          style: "color:var(--ink-faint);font-size:11px;",
        }),
      ]);
      if (HEROES[row.item_id]) {
        idCell.querySelector("div").appendChild(el("span", { class: "hero-dot", title: HEROES[row.item_id] }));
      }
      tr.appendChild(idCell);
      tr.appendChild(el("td", { text: FAILURE_LABEL[row.failure_class] || row.failure_class }));
      tr.appendChild(el("td", { class: "num amount tabular", text: rupees(row.amount) }));
      tr.appendChild(el("td", { class: "num prob tabular", text: pct(row.recovery_probability) }));

      const actionCell = el("td", {});
      if (row.status === "chase") {
        actionCell.appendChild(
          el("span", { class: "action-tag", text: ACTION_LABEL[row.recommended_action] || row.recommended_action })
        );
      } else {
        actionCell.appendChild(
          el("span", { class: "action-tag none", text: "Not worth chasing" })
        );
      }
      tr.appendChild(actionCell);

      tr.appendChild(
        el("td", { class: "num cost tabular", text: row.status === "chase" ? rupees(row.action_cost, { decimals: 2 }) : "—" })
      );

      const evCell = el("td", { class: "num ev tabular" });
      const barWrap = el("div", { class: "ev-bar-cell" });
      const track = el("div", { class: "ev-bar-track" });
      const fill = el("div", { class: "ev-bar-fill" + (row.expected_value < 0 ? " negative" : "") });
      fill.style.width = Math.min(100, (Math.abs(row.expected_value) / maxAbsEv) * 100) + "%";
      track.appendChild(fill);
      barWrap.appendChild(track);
      barWrap.appendChild(el("span", { text: rupees(row.expected_value) }));
      evCell.appendChild(barWrap);
      tr.appendChild(evCell);

      const statusCell = el("td", {});
      if (row.status === "chase") {
        statusCell.appendChild(el("span", { class: "status-pill chase", text: "Chasing" }));
      } else {
        statusCell.appendChild(
          el("span", {
            class: "status-pill suppressed",
            text: SUPPRESSION_LABEL[row.suppression_reason] || "Not worth chasing",
          })
        );
      }
      tr.appendChild(statusCell);

      tr.addEventListener("click", () => openDetail(row.item_id));
      body.appendChild(tr);
    });

    document.getElementById("row-count").textContent =
      rows.length.toLocaleString("en-IN") + " of " + RUN.queue.length.toLocaleString("en-IN");
  }

  // ---------------------------------------------------------------- detail

  function candidateRow(candidate, isChosen) {
    const tr = el("tr");
    if (isChosen) tr.classList.add("selected");
    if (!candidate.eligible) tr.classList.add("ineligible");

    tr.appendChild(el("td", { text: ACTION_LABEL[candidate.action] || candidate.action }));
    tr.appendChild(el("td", { text: pct(candidate.probability) }));
    tr.appendChild(el("td", { text: rupees(candidate.gross_value) }));
    tr.appendChild(el("td", { text: rupees(candidate.cost, { decimals: 2 }) }));
    tr.appendChild(el("td", { text: rupees(candidate.fatigue_penalty, { decimals: 2 }) }));
    tr.appendChild(el("td", { text: rupees(candidate.expected_value) }));

    const why = el("td", { class: "why-not" });
    if (!candidate.eligible) {
      why.textContent = SUPPRESSION_LABEL[candidate.block_reason] || candidate.block_reason || "blocked";
    } else if (isChosen) {
      why.textContent = "selected";
      why.style.color = "var(--chase)";
    }
    tr.appendChild(why);
    return tr;
  }

  function openDetail(itemId) {
    const detail = DECISIONS[itemId];
    if (!detail) return;
    selectedItemId = itemId;
    renderQueue();

    const storyline = detail.storyline || HEROES[itemId];
    document.getElementById("detail-id").textContent =
      itemId + "  ·  " + detail.payment.customer_id;
    document.getElementById("detail-amount").textContent = rupees(detail.amount);
    const storyEl = document.getElementById("detail-storyline");
    if (storyEl) {
      storyEl.textContent = storyline || "";
      storyEl.hidden = !storyline;
    }

    const body = document.getElementById("detail-body");
    body.innerHTML = "";

    const chosen = detail.candidate_actions.find((c) => c.selected);
    const doNothing = detail.candidate_actions.find((c) => c.action === "do_nothing");
    const isChase = detail.status === "chase";

    // -- WHY THIS PAYMENT? --------------------------------------------
    const payWhy = el("div", { class: "why" });
    payWhy.appendChild(el("div", { class: "why-label", text: "Why this payment?" }));
    const payKv = el("dl", { class: "kv" });
    [
      ["Amount", rupees(detail.amount)],
      ["Occurred", new Date(detail.payment.occurred_at).toLocaleString("en-IN")],
      ["Source", detail.payment.source],
      ["Prior attempts", String(detail.payment.prior_attempts)],
      ["Prior contacts", String(detail.payment.prior_contacts)],
      ["Diagnosis", FAILURE_LABEL[detail.failure.class] || detail.failure.class],
      ["Confidence", pct(detail.failure.confidence)],
      ["Recovery prior", pct(detail.failure.recovery_prior)],
      ["Classifier", detail.failure.provenance],
    ].forEach(([k, v]) => {
      payKv.appendChild(el("dt", { text: k }));
      payKv.appendChild(el("dd", { class: "mono", text: v }));
    });
    payWhy.appendChild(payKv);
    payWhy.appendChild(
      el("div", { class: "evidence-box", text: "observed: " + detail.failure.raw_gateway_context })
    );
    payWhy.appendChild(
      el("div", { class: "evidence-box", text: "evidence: " + detail.failure.evidence })
    );
    body.appendChild(payWhy);

    // -- WHY THIS ACTION? ----------------------------------------------
    const actionWhy = el("div", { class: "why" });
    actionWhy.appendChild(
      el("div", {
        class: "why-label",
        text: isChase ? "Why this action?" : "Why no action?",
      })
    );
    const rationaleBox = el("div", {
      class: "rationale-box" + (isChase ? "" : " suppressed"),
      text: detail.rationale,
    });
    actionWhy.appendChild(rationaleBox);
    if (chosen) {
      const kv = el("dl", { class: "kv" });
      [
        ["Recovery p", pct(chosen.probability)],
        ["Gross value", rupees(chosen.gross_value)],
        ["Cost", rupees(chosen.cost, { decimals: 2 })],
        ["Contact fatigue", rupees(chosen.fatigue_penalty, { decimals: 2 })],
        ["Expected value", rupees(chosen.expected_value)],
      ].forEach(([k, v]) => {
        kv.appendChild(el("dt", { text: k }));
        kv.appendChild(el("dd", { class: "mono", text: v }));
      });
      actionWhy.appendChild(kv);
    }
    body.appendChild(actionWhy);

    // -- WHY NOT OTHER ACTIONS? -----------------------------------------
    const otherWhy = el("div", { class: "why" });
    otherWhy.appendChild(el("div", { class: "why-label", text: "Why not other actions?" }));
    const table = el("table", { class: "candidates" });
    const thead = el("tr", {}, [
      el("th", { text: "action" }),
      el("th", { text: "p" }),
      el("th", { text: "gross" }),
      el("th", { text: "cost" }),
      el("th", { text: "fatigue" }),
      el("th", { text: "EV" }),
      el("th", { text: "" }),
    ]);
    table.appendChild(el("thead", {}, [thead]));
    const tbody = el("tbody");
    detail.candidate_actions
      .slice()
      .sort((a, b) => b.expected_value - a.expected_value)
      .forEach((c) => tbody.appendChild(candidateRow(c, c.selected)));
    table.appendChild(tbody);
    otherWhy.appendChild(table);
    body.appendChild(otherWhy);

    // -- BUDGET COMPETITION --------------------------------------------
    // The step the design calls for between expected value and the final
    // decision: this item's best rupee, judged against the marginal funded one.
    const bc = detail.budget_competition;
    if (bc && bc.competitive_density != null) {
      const compWhy = el("div", { class: "why" });
      compWhy.appendChild(el("div", { class: "why-label", text: "Budget competition" }));
      const cleared = bc.cleared;
      const bar = el("div", { class: "waterline" });
      const you = el("div", {
        class: "waterline-row" + (cleared ? " win" : " lose"),
      });
      you.appendChild(el("span", { class: "wl-label", text: "This item's best rupee" }));
      you.appendChild(
        el("span", { class: "wl-val tabular", text: rupees(bc.competitive_density) + " / ₹1" })
      );
      const mark = el("div", { class: "waterline-row mark" });
      mark.appendChild(el("span", { class: "wl-label", text: "Budget cleared at (waterline)" }));
      mark.appendChild(
        el("span", { class: "wl-val tabular", text: rupees(bc.waterline_density) + " / ₹1" })
      );
      bar.appendChild(you);
      bar.appendChild(mark);
      compWhy.appendChild(bar);
      compWhy.appendChild(
        el("div", {
          class: "evidence-box",
          text: cleared
            ? "Funded: its best rupee returns more expected recovery than the marginal rupee the budget could afford elsewhere, so it clears the waterline."
            : "Outbid: its best rupee returns less than the marginal funded rupee. It is genuinely recoverable — it simply loses the auction for scarce budget, and chasing it would lower total recovery.",
        })
      );
      body.appendChild(compWhy);
    }

    // -- WHY NOT DO NOTHING? ---------------------------------------------
    const nothingWhy = el("div", { class: "why" });
    nothingWhy.appendChild(el("div", { class: "why-label", text: "Why not do nothing?" }));
    const bestEv = chosen ? chosen.expected_value : 0;
    const nothingText = isChase
      ? "Do nothing always scores exactly ₹0.00 expected value. " +
        (ACTION_LABEL[chosen.action] || chosen.action) +
        " scores " +
        rupees(bestEv) +
        " -- worth taking because it clears that floor."
      : "Every candidate action scored at or below ₹0.00 expected value once cost and contact " +
        "fatigue were priced in, so doing nothing is the highest-scoring option here. That is " +
        "the desk working as intended, not a gap.";
    nothingWhy.appendChild(el("div", { class: "evidence-box", text: nothingText }));
    body.appendChild(nothingWhy);

    // -- policy checks -----------------------------------------------------
    if (detail.policy_checks && detail.policy_checks.length) {
      const policyWhy = el("div", { class: "why" });
      policyWhy.appendChild(el("div", { class: "why-label", text: "Policy checks" }));
      const list = el("div", { class: "policy-checks" });
      detail.policy_checks.forEach((c) => {
        list.appendChild(
          el("div", { class: "policy-check " + (c.passed ? "pass" : "fail") }, [
            el("span", { class: "mark", text: c.passed ? "✓" : "✕" }),
            el("span", { text: c.name }),
            el("span", { class: "detail", text: "— " + c.detail }),
          ])
        );
      });
      policyWhy.appendChild(list);
      body.appendChild(policyWhy);
    }

    // -- outcome -------------------------------------------------------
    const outcomeWhy = el("div", { class: "why" });
    outcomeWhy.appendChild(el("div", { class: "why-label", text: "Outcome" }));
    const strip = el("div", { class: "outcome-strip" });
    const item = (label, value) =>
      el("div", { class: "item" }, [
        el("span", { class: "label", text: label }),
        el("span", { class: "val", text: value }),
      ]);
    strip.appendChild(item("State", detail.outcome.state.replace(/_/g, " ")));
    strip.appendChild(item("Amount recovered", rupees(detail.outcome.amount_recovered)));
    strip.appendChild(item("Cost incurred", rupees(detail.outcome.cost_incurred, { decimals: 2 })));
    strip.appendChild(
      item("Dispatched", detail.outcome.dispatched_at ? new Date(detail.outcome.dispatched_at).toLocaleString("en-IN") : "—")
    );
    strip.appendChild(item("Idempotency key", detail.outcome.idempotency_key || "—"));
    outcomeWhy.appendChild(strip);
    body.appendChild(outcomeWhy);

    document.getElementById("overlay").classList.add("open");
    document.getElementById("detail-panel").classList.add("open");
  }

  function closeDetail() {
    document.getElementById("overlay").classList.remove("open");
    document.getElementById("detail-panel").classList.remove("open");
    selectedItemId = null;
    renderQueue();
  }

  // ------------------------------------------------------------------ init

  function wireControls() {
    document.getElementById("status-filter").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-filter]");
      if (!btn) return;
      currentFilter = btn.getAttribute("data-filter");
      document
        .querySelectorAll("#status-filter button")
        .forEach((b) => b.classList.toggle("active", b === btn));
      renderQueue();
    });

    document.getElementById("search").addEventListener("input", (e) => {
      currentSearch = e.target.value;
      renderQueue();
    });

    document.getElementById("overlay").addEventListener("click", closeDetail);
    document.getElementById("detail-close").addEventListener("click", closeDetail);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDetail();
    });
  }

  async function init() {
    try {
      await load();
    } catch (err) {
      document.querySelector("main").innerHTML =
        '<div class="empty-state">Could not load workspace data (' +
        err.message +
        "). Run <code>python run.py ui</code> from the repository root first.</div>";
      return;
    }
    renderHeader();
    renderFunnel();
    renderScarcity();
    renderOverview();
    wireControls();
    renderQueue();
  }

  init();
})();

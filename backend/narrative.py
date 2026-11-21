"""Builds the final answer text: observed facts (grounded in computed
numbers only) kept separate from hypotheses/recommendations, per the
requirement to 'distinguish observed facts from hypotheses and
recommendations'.
"""

METRIC_LABEL = {
    "success_rate": "success rate",
    "revenue": "revenue (INR)",
    "txn_count": "transaction count",
    "settlement_delay": "average settlement delay (days)",
}


def build_narrative(intent, cols, rows, dq_warnings, decomposition, sql_source: str, access_note: str | None):
    facts = []
    hypotheses = []
    recommendations = []
    caveats = list(dq_warnings)

    metric_label = METRIC_LABEL.get(intent.metric, intent.metric)

    if access_note:
        caveats.append(access_note)

    if getattr(intent, "wants_bank_account_numbers", False):
        facts.append(
            f"Returned {len(rows)} merchant bank-account record(s) with full account numbers — "
            f"this bypasses the usual metric/dimension analysis path because the question asked for raw PII, "
            f"not an aggregate."
        )
        caveats.append(
            "This query exposes unmasked account numbers and is only permitted for the 'admin' role; "
            "analyst/viewer roles are blocked at the validator (see access-control note if present)."
        )
        for a in intent.assumptions:
            caveats.append(f"Assumption: {a}")
        caveats.append(f"SQL source: {sql_source}.")
        return {"facts": facts, "hypotheses": hypotheses, "recommendations": recommendations, "caveats": caveats}

    # Resolve column positions by NAME, never by fixed index — the LLM-drafted
    # SQL path can (and does) return columns in a different order/shape than
    # our own templater, so assuming rows[i][0]/[1] is wrong in general.
    def _find_col(name: str) -> int | None:
        if name in cols:
            return cols.index(name)
        # fuzzy: LLM-drafted SQL may name it e.g. 'success_rate_pct' instead
        # of 'success_rate' — match on substring rather than failing closed.
        for i, c in enumerate(cols):
            if name in c or c in name:
                return i
        return None

    dim_hit = _find_col(intent.dimension) if intent.dimension else None
    metric_hit = _find_col(intent.metric)
    metric_resolved = metric_hit is not None
    dim_resolved = (dim_hit is not None) if intent.dimension else True
    dim_idx = dim_hit if dim_hit is not None else (0 if cols else None)
    metric_idx = metric_hit if metric_hit is not None else (1 if len(cols) > 1 else 0)
    n_idx = cols.index("n") if "n" in cols else None
    if rows and not (metric_resolved and dim_resolved):
        caveats.append(
            f"The executed query's columns ({', '.join(cols)}) didn't exactly match the expected "
            f"'{intent.metric}'/'{intent.dimension}' names, so the narrative below is a best-effort "
            f"positional read — check the raw result table directly."
        )

    if decomposition:
        d = decomposition
        facts.append(
            f"Overall {metric_label} moved from {d['overall_rate_prior']}% "
            f"(period {d['period_prior'][0]} to {d['period_prior'][1]}) to {d['overall_rate_recent']}% "
            f"(period {d['period_recent'][0]} to {d['period_recent'][1]}), a change of "
            f"{d['total_change_pct_pts']:+.2f} percentage points."
        )
        facts.append(
            f"Decomposition: {d['mix_effect_pct_pts']:+.2f} pts is attributable to a shift in transaction MIX "
            f"across payment-method/region segments, and {d['rate_effect_pct_pts']:+.2f} pts is attributable to "
            f"CHANGE IN SUCCESS RATE within segments."
        )
        if abs(d["mix_effect_pct_pts"]) > abs(d["rate_effect_pct_pts"]):
            top = max(d["segments"], key=lambda r: r["share_recent"] - r["share_prior"])
            hypotheses.append(
                f"The decline looks driven mainly by MIX SHIFT, not by any single segment getting worse. "
                f"The segment with the largest share increase is {top['payment_method']}/{top['region']} "
                f"(share {top['share_prior']}% -> {top['share_recent']}%), which has a structurally lower "
                f"success rate ({top['rate_recent']}% recently vs the portfolio average of {d['overall_rate_recent']}%)."
            )
            recommendations.append(
                f"Investigate why {top['payment_method']} volume in the {top['region']} region is growing "
                f"(new merchant onboarding? a routing change?) and consider targeted success-rate improvements "
                f"for that corridor (alternate PSP routing, retry logic) rather than broad fixes."
            )
        else:
            hypotheses.append(
                "The decline looks driven mainly by within-segment rate deterioration rather than a change in "
                "transaction mix — worth checking with the relevant payment-method/region processor for outages."
            )
    elif getattr(intent, "cohort_values", None):
        row_map = {str(r[dim_idx]): r for r in rows}
        a_label, b_label = intent.cohort_values
        if a_label in row_map and b_label in row_map:
            ra, rb = row_map[a_label], row_map[b_label]
            a_n = f", n={ra[n_idx]}" if n_idx is not None else ""
            b_n = f", n={rb[n_idx]}" if n_idx is not None else ""
            diff = ra[metric_idx] - rb[metric_idx]
            facts.append(
                f"Cohort compare on '{intent.dimension}': {a_label} = {ra[metric_idx]} {metric_label}{a_n} "
                f"vs {b_label} = {rb[metric_idx]} {metric_label}{b_n}; "
                f"difference of {diff:+.2f}."
            )
        else:
            missing = [v for v in (a_label, b_label) if v not in row_map]
            facts.append(
                f"No rows found for cohort value(s) {', '.join(missing)} in the selected window — "
                f"they may not occur in this time range."
            )
    elif intent.dimension:
        if rows:
            ranked = sorted(rows, key=lambda r: r[metric_idx])
            worst, best = ranked[0], ranked[-1]
            facts.append(
                f"Across {len(rows)} '{intent.dimension}' values, {metric_label} ranges from "
                f"{worst[metric_idx]} ({worst[dim_idx]}) to {best[metric_idx]} ({best[dim_idx]})."
            )
        else:
            facts.append("No rows matched this question's filters in the selected window.")
    else:
        if rows:
            facts.append(f"{metric_label.capitalize()} over the selected window: {rows[0][metric_idx]}.")
        else:
            facts.append("No rows matched this question's filters in the selected window.")

    for a in intent.assumptions:
        caveats.append(f"Assumption: {a}")
    caveats.append(f"SQL source: {sql_source}.")

    return {
        "facts": facts,
        "hypotheses": hypotheses,
        "recommendations": recommendations,
        "caveats": caveats,
    }

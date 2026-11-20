"""Executes validated SQL, runs the data-quality checks, and — for "why did
X change" questions — does a mix-shift decomposition rather than naming a
single spurious root cause.
"""
import sqlite3
import datetime


def run_query(conn: sqlite3.Connection, sql: str, max_steps: int = 20_000_000, check_every: int = 500_000):
    """Executes sql with a step-count cap (a crude but real timeout/cost
    control — see 'A generated query is expensive or unsafe'). sqlite's
    progress handler fires every `check_every` VM instructions; we only
    abort once the running total crosses `max_steps`, so normal joins/
    aggregations over this dataset complete, while a runaway (e.g. an
    accidental cross join) gets cut off.
    """
    cur = conn.cursor()
    state = {"steps": 0, "tripped": False}

    def _handler():
        state["steps"] += check_every
        if state["steps"] >= max_steps:
            state["tripped"] = True
            return 1  # non-zero aborts the query
        return 0

    conn.set_progress_handler(_handler, check_every)
    try:
        cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        if state["tripped"] or "interrupted" in str(e).lower():
            raise RuntimeError(
                "Query exceeded the execution-step budget and was aborted as a cost-control measure. "
                "Try narrowing the time window or adding a filter."
            )
        raise
    finally:
        conn.set_progress_handler(None, 0)
    return cols, [list(r) for r in rows]


def data_quality_report(conn: sqlite3.Connection, since: str) -> list[str]:
    warnings = []
    cur = conn.cursor()

    cur.execute(
        "SELECT COUNT(*) FROM transactions WHERE created_at >= ? AND amount IS NULL", (since,)
    )
    missing = cur.fetchone()[0]
    if missing:
        warnings.append(
            f"{missing} transaction(s) in this window have a NULL amount (dropped/incomplete field) "
            f"and are excluded from revenue totals."
        )

    cur.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT external_ref, COUNT(*) c FROM transactions
            WHERE created_at >= ?
            GROUP BY external_ref HAVING c > 1
        )
        """,
        (since,),
    )
    dup_groups = cur.fetchone()[0]
    if dup_groups:
        warnings.append(
            f"{dup_groups} external_ref value(s) appear more than once in this window — likely a "
            f"duplicated callback/record. Counts and revenue may be slightly overstated if not de-duplicated."
        )
    return warnings


def decline_decomposition(conn: sqlite3.Connection, days: int):
    """Compares the most recent `days` window ('B') against the equal-length
    prior window ('A'), broken down by (payment_method, region), and
    decomposes the change in overall success rate into a MIX effect
    (segments shifting in volume share) vs a RATE effect (segments'
    own success rate changing).
    """
    today = datetime.date.today()
    b_start = today - datetime.timedelta(days=days)
    a_start = b_start - datetime.timedelta(days=days)

    def period_stats(start, end):
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.region, t.payment_method,
                   COUNT(*) AS n,
                   SUM(CASE WHEN t.status='SUCCESS' THEN 1 ELSE 0 END) AS succ
            FROM transactions t JOIN merchants m ON m.merchant_id = t.merchant_id
            WHERE t.created_at >= ? AND t.created_at < ?
            GROUP BY m.region, t.payment_method
            """,
            (start.isoformat(), end.isoformat()),
        )
        rows = cur.fetchall()
        total = sum(r[2] for r in rows) or 1
        stats = {}
        for region, pm, n, succ in rows:
            stats[(region, pm)] = {"n": n, "share": n / total, "rate": (succ / n if n else 0)}
        return stats, total

    stats_a, total_a = period_stats(a_start, b_start)
    stats_b, total_b = period_stats(b_start, today)

    overall_a = sum(s["share"] * s["rate"] for s in stats_a.values())
    overall_b = sum(s["share"] * s["rate"] for s in stats_b.values())

    mix_effect = 0.0
    rate_effect = 0.0
    segment_breakdown = []
    keys = set(stats_a) | set(stats_b)
    for key in keys:
        a = stats_a.get(key, {"n": 0, "share": 0.0, "rate": 0.0})
        b = stats_b.get(key, {"n": 0, "share": 0.0, "rate": 0.0})
        mix_effect += (b["share"] - a["share"]) * a["rate"]
        rate_effect += b["share"] * (b["rate"] - a["rate"])
        segment_breakdown.append({
            "region": key[0], "payment_method": key[1],
            "share_prior": round(a["share"] * 100, 2), "share_recent": round(b["share"] * 100, 2),
            "rate_prior": round(a["rate"] * 100, 2), "rate_recent": round(b["rate"] * 100, 2),
            "n_prior": a["n"], "n_recent": b["n"],
        })

    segment_breakdown.sort(key=lambda r: r["share_recent"] - r["share_prior"], reverse=True)

    return {
        "window_days": days,
        "period_prior": [a_start.isoformat(), b_start.isoformat()],
        "period_recent": [b_start.isoformat(), today.isoformat()],
        "overall_rate_prior": round(overall_a * 100, 2),
        "overall_rate_recent": round(overall_b * 100, 2),
        "total_change_pct_pts": round((overall_b - overall_a) * 100, 2),
        "mix_effect_pct_pts": round(mix_effect * 100, 2),
        "rate_effect_pct_pts": round(rate_effect * 100, 2),
        "segments": segment_breakdown[:8],
    }

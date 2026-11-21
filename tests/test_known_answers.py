"""Evaluates the generated-SQL pipeline against hand-computed "golden"
queries and known safety properties, rather than just eyeballing demo
output. Run with:

    pytest

Requires data/payments.db to exist (python -m backend.generate_data).
"""
import os
import sys
import sqlite3
import datetime
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import nl2sql, validator, analysis

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "payments.db")


@pytest.fixture(scope="module")
def conn():
    if not os.path.exists(DB_PATH):
        pytest.skip("data/payments.db not found — run `python -m backend.generate_data` first.")
    c = sqlite3.connect(DB_PATH)
    yield c
    c.close()


def run_pipeline(question: str, role: str = "admin"):
    intent = nl2sql.parse_question(question)
    sql, steps = nl2sql.build_sql(intent)
    safe_sql, warnings = validator.validate_sql(sql, role)
    return intent, safe_sql, warnings


# ---------------------------------------------------------------------------
# Known-answer tests: compare the pipeline's generated SQL result against an
# independently hand-written "golden" query over the same window.
# ---------------------------------------------------------------------------

def test_transaction_count_matches_golden(conn):
    intent, sql, _ = run_pipeline("How many transactions in the last 14 days?")
    cols, rows = analysis.run_query(conn, sql)

    since = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
    golden = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE created_at >= ?", (since,)
    ).fetchone()[0]

    assert intent.metric == "txn_count"
    n_idx = cols.index("txn_count")
    assert rows[0][n_idx] == golden


def test_success_rate_by_payment_method_matches_golden(conn):
    intent, sql, _ = run_pipeline("Success rate by payment method last 14 days")
    cols, rows = analysis.run_query(conn, sql)
    since = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()

    golden = dict(conn.execute(
        """
        SELECT payment_method, ROUND(100.0 * SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2)
        FROM transactions WHERE created_at >= ?
        GROUP BY payment_method
        """,
        (since,),
    ).fetchall())

    dim_idx, metric_idx = cols.index("payment_method"), cols.index("success_rate")
    assert len(rows) == len(golden)
    for row in rows:
        pm, rate = row[dim_idx], row[metric_idx]
        assert abs(rate - golden[pm]) < 0.01, f"{pm}: pipeline={rate} golden={golden[pm]}"


def test_cohort_compare_matches_golden(conn):
    intent, sql, _ = run_pipeline("Compare Card vs Wallet success rate")
    assert intent.cohort_values == ["Card", "Wallet"]
    cols, rows = analysis.run_query(conn, sql)

    since = (datetime.date.today() - datetime.timedelta(days=intent.days)).isoformat()
    golden = dict(conn.execute(
        """
        SELECT payment_method, ROUND(100.0 * SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2)
        FROM transactions WHERE created_at >= ? AND payment_method IN ('Card','Wallet')
        GROUP BY payment_method
        """,
        (since,),
    ).fetchall())

    assert len(rows) == 2
    dim_idx, metric_idx = cols.index("payment_method"), cols.index("success_rate")
    for row in rows:
        assert abs(row[metric_idx] - golden[row[dim_idx]]) < 0.01


def test_decomposition_is_internally_consistent(conn):
    d = analysis.decline_decomposition(conn, days=30)
    computed_total = round(d["overall_rate_recent"] - d["overall_rate_prior"], 2)
    assert abs(computed_total - d["total_change_pct_pts"]) < 0.01

    # mix + rate effect should approximately reconstruct the total change
    # (small cross-term is expected and fine).
    reconstructed = d["mix_effect_pct_pts"] + d["rate_effect_pct_pts"]
    assert abs(reconstructed - d["total_change_pct_pts"]) < 0.5


def test_cross_source_revenue_disagreement_is_detected(conn):
    since = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    note = analysis.cross_source_revenue_check(conn, since)
    # In this dataset settlement always lags by 1-3 days and some settlements
    # are DELAYED/FAILED, so the two ledgers must disagree for any recent window.
    assert note is not None
    assert "disagree" in note.lower()


# ---------------------------------------------------------------------------
# Safety / access-control known-answer tests.
# ---------------------------------------------------------------------------

def test_validator_blocks_write_statements():
    with pytest.raises(validator.ValidationError):
        validator.validate_sql("DROP TABLE transactions", role="admin")
    with pytest.raises(validator.ValidationError):
        validator.validate_sql("INSERT INTO transactions VALUES (1)", role="admin")


def test_validator_blocks_multiple_statements():
    with pytest.raises(validator.ValidationError):
        validator.validate_sql("SELECT 1; DROP TABLE transactions", role="admin")


def test_validator_allows_ctes():
    sql, warnings = validator.validate_sql(
        "WITH x AS (SELECT 1 AS v) SELECT v FROM x", role="admin"
    )
    assert "with x as" in sql.lower()


def test_access_control_blocks_bank_accounts_for_non_admin():
    sql = "SELECT account_number_full FROM bank_accounts"
    for role in ("analyst", "viewer"):
        with pytest.raises(validator.ValidationError):
            validator.validate_sql(sql, role=role)
    # admin is allowed
    safe_sql, _ = validator.validate_sql(sql, role="admin")
    assert "account_number_full" in safe_sql.lower()


def test_validator_injects_row_limit_when_missing():
    safe_sql, warnings = validator.validate_sql("SELECT * FROM transactions", role="admin")
    assert "limit" in safe_sql.lower()
    assert any("no limit" in w.lower() for w in warnings)


def test_ambiguous_time_period_defaults_with_assumption():
    intent = nl2sql.parse_question("What is the success rate?")
    assert intent.days == 30
    assert any("defaulting to the last 30 days" in a for a in intent.assumptions)

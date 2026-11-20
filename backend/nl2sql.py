"""Turns a natural-language question into (a) a structured intent, (b) SQL.

Two SQL-generation paths:
  - Deterministic templater (always available, no API key needed): maps the
    parsed intent onto a small set of parameterized SQL templates. Safe by
    construction because every identifier comes from a whitelist, never
    from the raw question text.
  - Optional LLM drafting (used only if ANTHROPIC_API_KEY is set): asks
    Claude to draft raw SQL against the documented schema. That draft is
    NEVER trusted directly — it is always run back through
    validator.validate_sql() before execution, and if it fails validation
    we fall back to the deterministic templater and surface why.
"""
import os
import re
import datetime
from dataclasses import dataclass, field

from .schema import SCHEMA

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

SCHEMA_DOC = """
Tables (SQLite):
  merchants(merchant_id, name, segment, region, onboarded_date, risk_tier)
  transactions(txn_id, merchant_id, payment_method, amount, currency, status, created_at, bank_id, external_ref)
    -- status in ('SUCCESS','FAILED','PENDING'); payment_method in ('Card','UPI','NetBanking','Wallet')
  bank_accounts(bank_id, merchant_id, bank_name, account_number_masked, account_number_full, ifsc)
  settlements(settlement_id, merchant_id, txn_id, settled_amount, settled_at, status)
    -- status in ('SETTLED','DELAYED','FAILED')
""".strip()

METRIC_KEYWORDS = {
    "success_rate": ["success rate", "success", "approval rate", "approved"],
    "revenue": ["revenue", "gmv", "volume", "amount", "value"],
    "txn_count": ["transaction count", "number of transactions", "count of transactions", "how many transactions"],
    "settlement_delay": ["settlement delay", "settlement time", "delayed settlement", "settle"],
}

DIMENSION_KEYWORDS = {
    "payment_method": ["payment method", "payment-method", "wallet", "card", "upi", "netbanking", "method"],
    "region": ["region", "north", "south", "east", "west"],
    "segment": ["segment", "industry", "category", "retail", "travel", "electronics", "subscription", "food"],
    "bank_name": ["bank"],
}


@dataclass
class Intent:
    metric: str = "success_rate"
    dimension: str | None = None
    days: int = 30
    compare_prior_period: bool = False
    is_decline_question: bool = False
    wants_bank_account_numbers: bool = False
    assumptions: list[str] = field(default_factory=list)


def _detect_metric(q: str) -> str:
    for metric, kws in METRIC_KEYWORDS.items():
        if any(kw in q for kw in kws):
            return metric
    return "success_rate"


def _detect_dimension(q: str) -> str | None:
    for dim, kws in DIMENSION_KEYWORDS.items():
        if any(kw in q for kw in kws):
            return dim
    return None


def _detect_days(q: str, assumptions: list[str]) -> int:
    m = re.search(r"last\s+(\d+)\s+day", q)
    if m:
        return int(m.group(1))
    m = re.search(r"last\s+(\d+)\s+week", q)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"last\s+(\d+)\s+month", q)
    if m:
        return int(m.group(1)) * 30
    if "last week" in q:
        return 7
    if "last month" in q or "this month" in q:
        return 30
    if "last quarter" in q:
        return 90
    assumptions.append("No explicit time window was given — defaulting to the last 30 days.")
    return 30


def parse_question(question: str) -> Intent:
    q = question.lower()
    intent = Intent()
    intent.wants_bank_account_numbers = "account number" in q or "account_number" in q or "bank account" in q
    intent.metric = _detect_metric(q)
    intent.dimension = _detect_dimension(q)
    intent.days = _detect_days(q, intent.assumptions)
    intent.is_decline_question = any(w in q for w in ["why", "decline", "dropped", "drop", "fell", "worse", "down"])
    intent.compare_prior_period = intent.is_decline_question or "compare" in q or "vs" in q or "versus" in q
    return intent


METRIC_EXPR = {
    "success_rate": "ROUND(100.0 * SUM(CASE WHEN t.status='SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2)",
    "revenue": "ROUND(SUM(CASE WHEN t.status='SUCCESS' THEN t.amount ELSE 0 END), 2)",
    "txn_count": "COUNT(*)",
    "settlement_delay": "ROUND(AVG(JULIANDAY(s.settled_at) - JULIANDAY(t.created_at)), 2)",
}

DIMENSION_COLUMN = {
    "payment_method": "t.payment_method",
    "region": "m.region",
    "segment": "m.segment",
    "bank_name": "b.bank_name",
}


def build_sql(intent: Intent) -> tuple[str, list[str]]:
    """Deterministic templater. Returns (sql, explanation_steps)."""
    steps = []

    if intent.wants_bank_account_numbers:
        steps.append("Question asks for raw bank account numbers -> targets the restricted bank_accounts table/column.")
        sql = (
            "SELECT m.name AS merchant, b.bank_name, b.account_number_full\n"
            "FROM bank_accounts b JOIN merchants m ON m.merchant_id = b.merchant_id"
        )
        return sql, steps

    since = (datetime.date.today() - datetime.timedelta(days=intent.days)).isoformat()
    metric_expr = METRIC_EXPR[intent.metric]

    joins = "FROM transactions t JOIN merchants m ON m.merchant_id = t.merchant_id"
    if intent.metric == "settlement_delay":
        joins += " JOIN settlements s ON s.txn_id = t.txn_id AND s.status='SETTLED'"
    if intent.dimension == "bank_name":
        joins += " JOIN bank_accounts b ON b.bank_id = t.bank_id"

    steps.append(f"Resolved metric '{intent.metric}' -> SQL expression `{metric_expr}`.")
    steps.append(f"Time window: created_at >= '{since}' ({intent.days} days).")

    if intent.dimension:
        dim_col = DIMENSION_COLUMN[intent.dimension]
        steps.append(f"Grouping by dimension '{intent.dimension}' -> `{dim_col}`.")
        sql = (
            f"SELECT {dim_col} AS {intent.dimension}, {metric_expr} AS {intent.metric}, COUNT(*) AS n\n"
            f"{joins}\n"
            f"WHERE t.created_at >= '{since}'\n"
            f"GROUP BY {dim_col}\n"
            f"ORDER BY {intent.metric} DESC"
        )
    else:
        sql = (
            f"SELECT {metric_expr} AS {intent.metric}, COUNT(*) AS n\n"
            f"{joins}\n"
            f"WHERE t.created_at >= '{since}'"
        )
    return sql, steps


def llm_available() -> bool:
    return bool(ANTHROPIC_API_KEY)


def llm_draft_sql(question: str, role: str) -> str | None:
    """Best-effort: ask Claude to draft SQL. Returns None on any failure
    (missing key, network error, etc.) so the caller can fall back.
    """
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = (
            f"{SCHEMA_DOC}\n\n"
            f"Write ONE read-only SQLite SELECT query (no comments, no markdown fences, "
            f"no semicolon) that answers this business question:\n\"{question}\"\n"
            f"The querying user's role is '{role}'. Respond with ONLY the SQL."
        )
        resp = client.messages.create(
            model="claude-sonnet-5-5",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        text = re.sub(r"^```sql\s*|```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
        return text
    except Exception:
        return None

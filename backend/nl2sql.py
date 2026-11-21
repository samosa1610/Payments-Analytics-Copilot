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
import re
import datetime
from dataclasses import dataclass, field

from .schema import SCHEMA
from . import llm_client

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

# Known categorical values per dimension, used to detect an explicit
# cohort-vs-cohort comparison ("compare Card vs Wallet", "North vs South")
# rather than a full breakdown by that dimension.
KNOWN_VALUES = {
    "payment_method": {"card": "Card", "upi": "UPI", "netbanking": "NetBanking", "wallet": "Wallet"},
    "region": {"north": "North", "south": "South", "east": "East", "west": "West"},
    "segment": {
        "retail": "Retail", "travel": "Travel", "food & beverage": "Food & Beverage",
        "food": "Food & Beverage", "electronics": "Electronics", "subscription": "Subscription",
    },
}


@dataclass
class Intent:
    metric: str = "success_rate"
    dimension: str | None = None
    days: int = 30
    compare_prior_period: bool = False
    is_decline_question: bool = False
    wants_bank_account_numbers: bool = False
    cohort_values: list[str] | None = None
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


def _detect_cohort(q: str) -> tuple[str, str, str] | None:
    """Detects an explicit cohort-vs-cohort comparison, e.g. 'compare Card
    vs Wallet' or 'North vs South'. Returns (dimension, valueA, valueB) or
    None if the question doesn't name two specific values of one dimension.
    """
    for dim, values in KNOWN_VALUES.items():
        found = []
        for key, canonical in values.items():
            if re.search(rf"\b{re.escape(key)}\b", q) and canonical not in found:
                found.append(canonical)
        if len(found) >= 2:
            return dim, found[0], found[1]
    return None


def parse_question(question: str) -> Intent:
    q = question.lower()
    intent = Intent()
    intent.wants_bank_account_numbers = "account number" in q or "account_number" in q or "bank account" in q
    intent.metric = _detect_metric(q)
    intent.dimension = _detect_dimension(q)
    intent.days = _detect_days(q, intent.assumptions)
    intent.is_decline_question = any(w in q for w in ["why", "decline", "dropped", "drop", "fell", "worse", "down"])

    cohort = _detect_cohort(q)
    if cohort and not intent.is_decline_question:
        intent.dimension = cohort[0]
        intent.cohort_values = [cohort[1], cohort[2]]

    intent.compare_prior_period = intent.is_decline_question or (
        intent.cohort_values is None and ("compare" in q or " vs " in q or "versus" in q)
    )
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
        cohort_filter = ""
        if intent.cohort_values:
            quoted = ", ".join(f"'{v}'" for v in intent.cohort_values)
            cohort_filter = f" AND {dim_col} IN ({quoted})"
            steps.append(f"Detected an explicit cohort comparison -> restricting to {dim_col} IN ({quoted}).")
        sql = (
            f"SELECT {dim_col} AS {intent.dimension}, {metric_expr} AS {intent.metric}, COUNT(*) AS n\n"
            f"{joins}\n"
            f"WHERE t.created_at >= '{since}'{cohort_filter}\n"
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
    return llm_client.available()


def llm_draft_sql(question: str, role: str) -> str | None:
    """Best-effort: ask Claude (via the configured wrapper) to draft SQL.
    Returns None on any failure (not configured, network error, etc.) so
    the caller can fall back to the deterministic templater.
    """
    prompt = (
        f"{SCHEMA_DOC}\n\n"
        f"Write ONE read-only SQLite SELECT query (no comments, no markdown fences, "
        f"no semicolon, no explanation — just the SQL) that answers this business "
        f"question:\n\"{question}\"\n"
        f"The querying user's role is '{role}'. Respond with ONLY the SQL."
    )
    text = llm_client.chat(prompt)
    if not text:
        return None
    text = re.sub(r"^```sql\s*|^```\s*|```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
    return text

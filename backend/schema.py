"""Schema registry: the single source of truth for what tables/columns exist
and what each role is allowed to touch. The validator and the SQL templater
both import from here instead of trusting anything that comes from a prompt.
"""

DB_PATH_DEFAULT = "data/payments.db"

# table -> set of column names
SCHEMA = {
    "merchants": {
        "merchant_id", "name", "segment", "region", "onboarded_date", "risk_tier",
    },
    "transactions": {
        "txn_id", "merchant_id", "payment_method", "amount", "currency",
        "status", "created_at", "bank_id", "external_ref",
    },
    "bank_accounts": {
        "bank_id", "merchant_id", "bank_name", "account_number_masked",
        "account_number_full", "ifsc",
    },
    "settlements": {
        "settlement_id", "merchant_id", "txn_id", "settled_amount",
        "settled_at", "status",
    },
}

# Columns that are considered sensitive / PII-adjacent and are stripped out
# (or the whole table blocked) for any role below "admin".
RESTRICTED_TABLES = {"bank_accounts"}
RESTRICTED_COLUMNS = {
    ("bank_accounts", "account_number_full"),
}

ROLES = {
    "admin": {
        "label": "Admin (Payments Ops)",
        "can_see_restricted_tables": True,
        "can_see_restricted_columns": True,
    },
    "analyst": {
        "label": "Business Analyst",
        "can_see_restricted_tables": False,
        "can_see_restricted_columns": False,
    },
    "viewer": {
        "label": "Executive Viewer (aggregates only)",
        "can_see_restricted_tables": False,
        "can_see_restricted_columns": False,
    },
}

ALL_TABLE_NAMES = set(SCHEMA.keys())

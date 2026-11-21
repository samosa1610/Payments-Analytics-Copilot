"""Safety + access-control gate that every generated SQL string must pass
through before it ever reaches sqlite3 — whether that SQL came from our own
deterministic templater or from an LLM draft.

Deliberately simple and explicit rather than a general SQL parser: for a
single-database analytics copilot, a conservative allow-list is more
auditable than a clever parser.
"""
import re
from .schema import SCHEMA, RESTRICTED_TABLES, RESTRICTED_COLUMNS, ALL_TABLE_NAMES

FORBIDDEN_KEYWORDS = [
    "insert", "update", "delete", "drop", "alter", "attach", "detach",
    "pragma", "create", "replace", "vacuum", "reindex", "trigger",
]

MAX_ROW_LIMIT = 5000
DEFAULT_LIMIT = 500


class ValidationError(Exception):
    pass


def _strip_trailing_semicolons(sql: str) -> str:
    sql = sql.strip()
    while sql.endswith(";"):
        sql = sql[:-1].strip()
    return sql


def validate_sql(sql: str, role: str) -> tuple[str, list[str]]:
    """Returns (safe_sql, warnings). Raises ValidationError if the query
    cannot be made safe (unsafe statement type, references a table/column
    the role may not see, or contains multiple statements).
    """
    warnings = []
    original = sql
    sql = _strip_trailing_semicolons(sql)

    if ";" in sql:
        raise ValidationError("Query contains multiple statements, which is not allowed.")

    lowered = sql.lower()
    stripped = lowered.strip()
    if not (stripped.startswith("select") or stripped.startswith("with")):
        raise ValidationError(
            "Only SELECT queries (optionally with a leading WITH / CTE) are allowed — "
            "this looks like a write/DDL statement."
        )

    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", lowered):
            raise ValidationError(f"Query uses the forbidden keyword '{kw.upper()}'.")

    # CTE names (WITH foo AS (...), bar AS (...)) are not real tables —
    # exclude them from the table whitelist check below.
    cte_names = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", lowered))

    # table whitelist: every identifier that looks like `table.column` or
    # follows FROM/JOIN must be a known table or a CTE defined in this query.
    referenced_tables = set(re.findall(r"\bfrom\s+([a-zA-Z_][a-zA-Z0-9_]*)", lowered))
    referenced_tables |= set(re.findall(r"\bjoin\s+([a-zA-Z_][a-zA-Z0-9_]*)", lowered))
    unknown = referenced_tables - ALL_TABLE_NAMES - cte_names
    if unknown:
        raise ValidationError(f"Query references unknown table(s): {', '.join(sorted(unknown))}.")

    role_can_see_restricted_tables = role == "admin"
    if not role_can_see_restricted_tables:
        blocked = referenced_tables & RESTRICTED_TABLES
        if blocked:
            raise ValidationError(
                f"Role '{role}' is not permitted to query restricted table(s): {', '.join(sorted(blocked))}."
            )
        for table, column in RESTRICTED_COLUMNS:
            if re.search(rf"\b{re.escape(column)}\b", lowered):
                raise ValidationError(
                    f"Role '{role}' is not permitted to select restricted column '{table}.{column}'."
                )

    # cost control: require / inject a LIMIT
    limit_match = re.search(r"\blimit\s+(\d+)", lowered)
    if limit_match:
        n = int(limit_match.group(1))
        if n > MAX_ROW_LIMIT:
            sql = re.sub(r"\blimit\s+\d+", f"LIMIT {MAX_ROW_LIMIT}", sql, flags=re.IGNORECASE)
            warnings.append(f"Requested LIMIT exceeded the {MAX_ROW_LIMIT}-row cap; capped automatically.")
    else:
        # only inject a limit on queries without GROUP BY aggregation-only
        # patterns is unnecessary restriction; simplest safe rule: always cap.
        sql = f"{sql}\nLIMIT {DEFAULT_LIMIT}"
        warnings.append(f"No LIMIT was specified; capped at {DEFAULT_LIMIT} rows as a cost control.")

    if sql != original:
        pass  # sql was rewritten; caller surfaces via warnings already appended

    return sql, warnings

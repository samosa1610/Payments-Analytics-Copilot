import os
import sqlite3
import datetime
from flask import Flask, request, jsonify, render_template

from .schema import ROLES, DB_PATH_DEFAULT
from . import nl2sql
from . import validator
from . import analysis
from .narrative import build_narrative

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "..", "data", "payments.db")

app = Flask(
    __name__,
    template_folder=os.path.join(HERE, "..", "templates"),
    static_folder=os.path.join(HERE, "..", "static"),
)


def get_conn():
    if not os.path.exists(DB_PATH):
        raise RuntimeError(
            "Database not found. Run `python -m backend.generate_data` first to create data/payments.db."
        )
    conn = sqlite3.connect(DB_PATH)
    return conn


@app.route("/")
def index():
    return render_template(
        "index.html",
        roles=[{"id": k, "label": v["label"]} for k, v in ROLES.items()],
        llm_available=nl2sql.llm_available(),
    )


@app.route("/api/ask", methods=["POST"])
def ask():
    body = request.get_json(force=True) or {}
    question = (body.get("question") or "").strip()
    role = body.get("role") or "analyst"

    if role not in ROLES:
        return jsonify({"error": f"Unknown role '{role}'."}), 400
    if not question:
        return jsonify({"error": "Question is required."}), 400

    intent = nl2sql.parse_question(question)
    sql_candidate, steps = nl2sql.build_sql(intent)
    sql_source = "Deterministic query templater (schema-constrained, no LLM key configured)."

    access_note = None

    if nl2sql.llm_available():
        draft = nl2sql.llm_draft_sql(question, role)
        if draft:
            try:
                safe_sql, warn = validator.validate_sql(draft, role)
                sql_candidate = safe_sql
                sql_source = "LLM-drafted SQL (validated and auto-corrected before execution)."
                steps = ["Claude drafted raw SQL against the documented schema.", *[f"Validator: {w}" for w in warn]]
            except validator.ValidationError as e:
                steps.append(f"LLM draft rejected by the safety validator ({e}); fell back to the safe templater.")
                sql_source = "Deterministic query templater (LLM draft failed validation — see steps)."

    try:
        safe_sql, warnings = validator.validate_sql(sql_candidate, role)
    except validator.ValidationError as e:
        # This is the "user requests data outside their access scope" /
        # "query is unsafe" failure path.
        return jsonify({
            "error": str(e),
            "blocked": True,
            "role": role,
            "attempted_sql": sql_candidate,
        }), 403

    conn = get_conn()
    try:
        cols, rows = analysis.run_query(conn, safe_sql)
    except RuntimeError as e:
        return jsonify({"error": str(e), "blocked": True}), 400
    except sqlite3.Error as e:
        return jsonify({"error": f"Query execution failed: {e}"}), 400

    since = (datetime.date.today() - datetime.timedelta(days=intent.days)).isoformat()
    dq_warnings = analysis.data_quality_report(conn, since)

    decomposition = None
    if intent.metric == "success_rate" and intent.compare_prior_period:
        decomposition = analysis.decline_decomposition(conn, intent.days)

    conn.close()

    narrative = build_narrative(intent, cols, rows, dq_warnings + warnings, decomposition, sql_source, access_note)

    return jsonify({
        "question": question,
        "role": role,
        "sql": safe_sql,
        "sql_source": sql_source,
        "explanation_steps": steps,
        "columns": cols,
        "rows": rows,
        "decomposition": decomposition,
        "narrative": narrative,
        "citation": {
            "dataset": "payments.db (transactions, merchants, settlements, bank_accounts)",
            "time_window_days": intent.days,
            "since": since,
        },
    })


if __name__ == "__main__":
    app.run(debug=True, port=5057)

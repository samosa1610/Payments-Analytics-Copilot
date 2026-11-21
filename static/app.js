let chartInstance = null;

const el = (id) => document.getElementById(id);

document.querySelectorAll(".example").forEach((btn) => {
  btn.addEventListener("click", () => {
    el("question").value = btn.textContent;
    ask();
  });
});

el("askBtn").addEventListener("click", ask);
el("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter") ask();
});

async function ask() {
  const question = el("question").value.trim();
  if (!question) return;
  const role = el("role").value;

  el("result").classList.add("hidden");
  el("errorBox").classList.add("hidden");

  let res, data;
  try {
    res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, role }),
    });
    data = await res.json();
  } catch (e) {
    showError("Request failed", String(e));
    return;
  }

  if (!res.ok) {
    showError(data.blocked ? "Blocked by access control / query validator" : "Error", data.error || "Unknown error");
    if (data.attempted_sql) {
      el("errorMsg").innerHTML += `<br><br><strong>Attempted query:</strong><pre>${escapeHtml(data.attempted_sql)}</pre>`;
    }
    return;
  }

  render(data);
}

function showError(title, msg) {
  el("errorTitle").textContent = title;
  el("errorMsg").textContent = msg;
  el("errorBox").classList.remove("hidden");
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function render(data) {
  el("result").classList.remove("hidden");
  el("sqlSource").textContent = data.sql_source + (data.cached ? "  [served from cache]" : "");
  el("sqlText").textContent = data.sql;

  const stepsEl = el("steps");
  stepsEl.innerHTML = "";
  (data.explanation_steps || []).forEach((s) => {
    const li = document.createElement("li");
    li.textContent = s;
    stepsEl.appendChild(li);
  });

  const factsEl = el("facts");
  factsEl.innerHTML = "";
  (data.narrative.facts || []).forEach((f) => appendLi(factsEl, f));

  const hypsEl = el("hyps");
  hypsEl.innerHTML = "";
  (data.narrative.hypotheses || []).forEach((h) => appendLi(hypsEl, "Hypothesis: " + h));
  (data.narrative.recommendations || []).forEach((r) => appendLi(hypsEl, "Recommendation: " + r));
  if (!data.narrative.hypotheses.length && !data.narrative.recommendations.length) {
    appendLi(hypsEl, "No hypotheses generated for this question type.");
  }

  const caveatsEl = el("caveats");
  caveatsEl.innerHTML = "";
  (data.narrative.caveats || []).forEach((c) => appendLi(caveatsEl, c));

  el("citation").textContent =
    `Dataset: ${data.citation.dataset} | Time window: last ${data.citation.time_window_days} days (since ${data.citation.since})`;

  const confidenceEl = el("confidence");
  confidenceEl.innerHTML = "";
  if (data.confidence && data.confidence.length) {
    data.confidence.forEach((c) => appendLi(confidenceEl, c));
  } else {
    appendLi(confidenceEl, "No specific confidence/uncertainty note for this query shape.");
  }

  renderTable(data.columns, data.rows);
  renderChart(data);
}

function appendLi(ul, text) {
  const li = document.createElement("li");
  li.textContent = text;
  ul.appendChild(li);
}

function renderTable(columns, rows) {
  const container = el("table");
  if (!rows || !rows.length) {
    container.innerHTML = "<p>No rows returned.</p>";
    return;
  }
  let html = "<table><thead><tr>" + columns.map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
  for (const row of rows.slice(0, 50)) {
    html += "<tr>" + row.map((v) => `<td>${v === null ? "—" : v}</td>`).join("") + "</tr>";
  }
  html += "</tbody></table>";
  if (rows.length > 50) html += `<p style="color:#93a2bd;font-size:0.8rem;">Showing first 50 of ${rows.length} rows.</p>`;
  container.innerHTML = html;
}

function renderChart(data) {
  const ctx = el("chart").getContext("2d");
  if (chartInstance) chartInstance.destroy();

  if (data.decomposition) {
    const d = data.decomposition;
    chartInstance = new Chart(ctx, {
      type: "bar",
      data: {
        labels: ["Prior period", "Mix effect", "Rate effect", "Recent period"],
        datasets: [{
          label: "Success rate contribution (pct points)",
          data: [d.overall_rate_prior, d.mix_effect_pct_pts, d.rate_effect_pct_pts, d.overall_rate_recent],
          backgroundColor: ["#5ea2ff", "#ffb648", "#3ddc97", "#5ea2ff"],
        }],
      },
      options: { plugins: { legend: { display: false } }, scales: { y: { ticks: { color: "#93a2bd" } }, x: { ticks: { color: "#93a2bd" } } } },
    });
    return;
  }

  const cols = data.columns;
  const rows = data.rows;
  if (!rows || !rows.length || cols.length < 2) {
    chartInstance = null;
    ctx.clearRect(0, 0, 400, 300);
    return;
  }
  const labels = rows.map((r) => String(r[0]));
  const values = rows.map((r) => r[1]);
  chartInstance = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [{ label: cols[1], data: values, backgroundColor: "#5ea2ff" }],
    },
    options: { plugins: { legend: { display: false } }, scales: { y: { ticks: { color: "#93a2bd" } }, x: { ticks: { color: "#93a2bd" } } } },
  });
}

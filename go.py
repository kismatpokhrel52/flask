import os
import csv
import json
import sqlite3
from datetime import datetime
from urllib.request import urlopen
from urllib.parse import quote
from flask import Flask, request, jsonify, send_file, Response

# Optional: use requests if available; otherwise fallback to urllib
USE_REQUESTS = False
try:
    import requests  # type: ignore
    USE_REQUESTS = True
except Exception:
    USE_REQUESTS = False

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    country TEXT NOT NULL,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    hs_code TEXT,
    quantity INTEGER NOT NULL,
    declared_value REAL NOT NULL,
    risk_level INTEGER NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL
);
"""

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute(SCHEMA_SQL)
    conn.commit()
    conn.close()

init_db()

def fetch_country_info(name: str):
    """
    Fetch basic country metadata from REST Countries API.
    Returns dict or None on failure.
    """
    url = f"https://restcountries.com/v3.1/name/{quote(name)}?fullText=true"
    try:
        if USE_REQUESTS:
            r = requests.get(url, timeout=8)
            if r.status_code != 200:
                return None
            data = r.json()
        else:
            with urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        if not data or not isinstance(data, list):
            return None
        c = data[0]
        currencies = c.get("currencies", {})
        currency_list = []
        for code, meta in currencies.items():
            currency_list.append(f"{code} ({meta.get('name','')})")
        return {
            "name": c.get("name", {}).get("common"),
            "region": c.get("region"),
            "population": c.get("population"),
            "capital": (c.get("capital") or [None])[0],
            "currencies": ", ".join(currency_list) if currency_list else None,
            "flag_png": (c.get("flags") or {}).get("png"),
            "cca2": c.get("cca2"),
        }
    except Exception:
        return None

def query_products(filters=None):
    conn = get_db()
    base = "SELECT * FROM products WHERE 1=1"
    params = []
    if filters:
        if filters.get("country"):
            base += " AND country = ?"
            params.append(filters["country"])
        if filters.get("category"):
            base += " AND category = ?"
            params.append(filters["category"])
        if filters.get("risk_min") is not None:
            base += " AND risk_level >= ?"
            params.append(int(filters["risk_min"]))
        if filters.get("risk_max") is not None:
            base += " AND risk_level <= ?"
            params.append(int(filters["risk_max"]))
    base += " ORDER BY created_at DESC"
    rows = conn.execute(base, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def kpis(rows):
    total_value = sum(r["declared_value"] for r in rows) if rows else 0.0
    total_qty = sum(r["quantity"] for r in rows) if rows else 0
    avg_risk = (sum(r["risk_level"] for r in rows) / len(rows)) if rows else 0.0
    # Aggregations
    by_country = {}
    by_category = {}
    risk_dist = {i: 0 for i in range(1, 6)}
    for r in rows:
        by_country[r["country"]] = by_country.get(r["country"], 0) + r["declared_value"]
        by_category[r["category"]] = by_category.get(r["category"], 0) + r["declared_value"]
        risk_dist[r["risk_level"]] += 1
    top_countries = sorted(by_country.items(), key=lambda x: x[1], reverse=True)[:10]
    top_categories = sorted(by_category.items(), key=lambda x: x[1], reverse=True)[:10]
    return {
        "total_value": round(total_value, 2),
        "total_quantity": total_qty,
        "avg_risk": round(avg_risk, 2),
        "top_countries": top_countries,
        "top_categories": top_categories,
        "risk_distribution": risk_dist,
    }

@app.route("/")
def home():
    return Response(HOME_HTML, mimetype="text/html")

@app.route("/api/country")
def api_country():
    name = request.args.get("name")
    if not name:
        return jsonify({"error": "name required"}), 400
    info = fetch_country_info(name)
    if not info:
        return jsonify({"error": "not found"}), 404
    return jsonify(info)

@app.route("/api/products", methods=["GET"])
def api_products_list():
    filters = {
        "country": request.args.get("country") or None,
        "category": request.args.get("category") or None,
        "risk_min": int(request.args.get("risk_min")) if request.args.get("risk_min") else None,
        "risk_max": int(request.args.get("risk_max")) if request.args.get("risk_max") else None,
    }
    rows = query_products(filters)
    return jsonify({"items": rows, "kpis": kpis(rows)})

@app.route("/api/products", methods=["POST"])
def api_products_add():
    data = request.get_json(force=True)
    required = ["country", "product_name", "category", "quantity", "declared_value", "risk_level"]
    for key in required:
        if key not in data:
            return jsonify({"error": f"missing {key}"}), 400
    try:
        quantity = int(data["quantity"])
        declared_value = float(data["declared_value"])
        risk_level = int(data["risk_level"])
        if risk_level < 1 or risk_level > 5:
            return jsonify({"error": "risk_level must be 1..5"}), 400
    except Exception:
        return jsonify({"error": "invalid numeric fields"}), 400
    conn = get_db()
    conn.execute(
        """
        INSERT INTO products (country, product_name, category, hs_code, quantity, declared_value, risk_level, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["country"].strip(),
            data["product_name"].strip(),
            data["category"].strip(),
            (data.get("hs_code") or "").strip(),
            quantity,
            declared_value,
            risk_level,
            (data.get("notes") or "").strip(),
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/upload_csv", methods=["POST"])
def api_upload_csv():
    """
    Accept a CSV with columns:
    country,product_name,category,hs_code,quantity,declared_value,risk_level,notes
    """
    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400
    f = request.files["file"]
    text = f.read().decode("utf-8").splitlines()
    reader = csv.DictReader(text)
    inserted = 0
    conn = get_db()
    for row in reader:
        try:
            conn.execute(
                """
                INSERT INTO products (country, product_name, category, hs_code, quantity, declared_value, risk_level, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (row.get("country") or "").strip(),
                    (row.get("product_name") or "").strip(),
                    (row.get("category") or "").strip(),
                    (row.get("hs_code") or "").strip(),
                    int(row.get("quantity") or 0),
                    float(row.get("declared_value") or 0.0),
                    int(row.get("risk_level") or 1),
                    (row.get("notes") or "").strip(),
                    datetime.utcnow().isoformat(),
                ),
            )
            inserted += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "inserted": inserted})

@app.route("/export.csv")
def export_csv():
    rows = query_products({})
    output = []
    header = ["id","country","product_name","category","hs_code","quantity","declared_value","risk_level","notes","created_at"]
    output.append(",".join(header))
    for r in rows:
        line = [
            str(r["id"]), r["country"], r["product_name"], r["category"], r["hs_code"],
            str(r["quantity"]), str(r["declared_value"]), str(r["risk_level"]),
            (r["notes"] or "").replace(",", ";"), r["created_at"]
        ]
        output.append(",".join(line))
    csv_data = "\n".join(output)
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=dataset.csv"}
    )

@app.route("/export.json")
def export_json():
    rows = query_products({})
    return Response(
        json.dumps(rows, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=dataset.json"}
    )
HS_LOOKUP = {
    "mobile phone": "8517.12",
    "rice": "1006.30",
    "electric car": "8703.80",
    "t-shirt": "6109.10",
}
@app.route("/api/products/<int:pid>", methods=["DELETE"])
def api_delete_product(pid):
    conn = get_db()
    conn.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


# ---------------- HTML/JS/CSS (inline for single-file simplicity) ----------------

HOME_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Nepal Product Inflow Evaluator</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0"></script>
<style>
  :root {
    --bg: #0e1a26;            /* light bluish-black */
    --panel: #132536;         /* slightly lighter */
    --accent: #3da0ff;        /* soft blue */
    --text: #e8f0f7;          /* light white */
    --muted: #b9c6d3;
    --danger: #ff6b6b;
    --success: #4cd4a3;
    --warning: #ffd166;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  }
  header {
    padding: 16px 24px;
    border-bottom: 1px solid #1e3246;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: linear-gradient(180deg, #0f1f2e, var(--bg));
  }
  .brand {
    font-weight: 600;
    letter-spacing: 0.3px;
  }
  .controls {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  button, .btn {
    background: #1a2f44;
    color: var(--text);
    border: 1px solid #284763;
    padding: 10px 14px;
    border-radius: 8px;
    cursor: pointer;
    transition: 120ms ease;
  }
  button:hover, .btn:hover { background: #214057; }
  .btn-accent { border-color: var(--accent); color: var(--text); }
  .btn-danger { border-color: var(--danger); color: var(--text); }
  .btn-success { border-color: var(--success); color: var(--text); }
  .container { padding: 20px; max-width: 1200px; margin: 0 auto; }
  .grid {
    display: grid;
    grid-template-columns: 1.2fr 1fr;
    gap: 20px;
  }
  .panel {
    background: var(--panel);
    border: 1px solid #22405a;
    border-radius: 12px;
    padding: 16px;
  }
  h2, h3 { margin: 6px 0 12px 0; }
  input, select, textarea {
    width: 100%;
    background: #0f1f2e;
    color: var(--text);
    border: 1px solid #2a4b67;
    padding: 10px;
    border-radius: 8px;
    margin: 6px 0 12px 0;
  }
  label { font-size: 13px; color: var(--muted); }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
  }
  th, td { padding: 10px; border-bottom: 1px solid #1e3246; }
  th { color: var(--muted); font-weight: 600; text-align: left; }
  .kpi {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin-bottom: 12px;
  }
  .kpi .tile {
    background: #0f1f2e;
    border: 1px solid #2a4b67;
    border-radius: 10px;
    padding: 12px;
  }
  .tile .value { font-size: 20px; margin-top: 4px; color: var(--text); }
  .pill {
    display: inline-block;
    padding: 4px 8px;
    border-radius: 999px;
    border: 1px solid #355a78;
    color: var(--muted);
    font-size: 12px;
  }
  .risk1 { border-color: #68d391; }
  .risk2 { border-color: #a3e635; }
  .risk3 { border-color: #ffd166; }
  .risk4 { border-color: #f6ad55; }
  .risk5 { border-color: #ff6b6b; }
  .footer {
    padding: 20px;
    text-align: center;
    color: var(--muted);
  }
  canvas { background: #0f1f2e; border: 1px solid #2a4b67; border-radius: 10px; }
</style>
</head>
<body>
<header>
  <div class="brand">Nepal Product Inflow Evaluator</div>
  <div class="controls">
    <button class="btn-accent" onclick="refreshData()">Refresh data</button>
    <button onclick="highlightHighRisk()">Highlight high risk</button>
    <button class="btn-success" onclick="exportCSV()">Export CSV</button>
    <button class="btn-success" onclick="exportJSON()">Export JSON</button>
    <label class="btn">
      Bulk upload CSV
      <input type="file" id="csvInput" style="display:none" accept=".csv" onchange="uploadCSV(this.files[0])"/>
    </label>
  </div>
</header>

<div class="container">
  <div class="grid">
    <div class="panel">
      <h2>Add incoming product</h2>
      <div>
        <label>Country</label>
        <input id="country" placeholder="e.g., China, India, USA"/>
        <button onclick="fetchCountry()">Fetch country info</button>
        <div id="countryInfo" style="margin-top:8px"></div>
      </div>

      <label>Product name</label>
      <input id="product" placeholder="e.g., Mobile phones"/>

      <label>Category</label>
      <select id="category">
        <option>Electronics</option>
        <option>Textiles</option>
        <option>Machinery</option>
        <option>Food & Beverages</option>
        <option>Chemicals</option>
        <option>Metals</option>
        <option>Others</option>
      </select>

      <label>HS code</label>
      <input id="hs" placeholder="e.g., 8517"/>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <div>
          <label>Quantity</label>
          <input id="qty" type="number" min="1" value="100"/>
        </div>
        <div>
          <label>Declared value (NPR)</label>
          <input id="val" type="number" min="0" step="0.01" value="100000"/>
        </div>
      </div>

      <label>Risk level (1-5)</label>
      <input id="risk" type="number" min="1" max="5" value="3"/>

      <label>Notes</label>
      <textarea id="notes" rows="3" placeholder="Optional notes..."></textarea>

      <div class="controls" style="margin-top:8px">
        <button class="btn-accent" onclick="addProduct()">Add product</button>
        <button onclick="clearForm()">Clear</button>
      </div>
    </div>

    <div class="panel">
      <h2>Filters</h2>
      <label>Filter by country</label>
      <input id="fCountry" placeholder="Exact match (e.g., India)"/>
      <label>Filter by category</label>
      <input id="fCategory" placeholder="Exact match (e.g., Electronics)"/>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <div>
          <label>Risk min</label>
          <input id="fRiskMin" type="number" min="1" max="5"/>
        </div>
        <div>
          <label>Risk max</label>
          <input id="fRiskMax" type="number" min="1" max="5"/>
        </div>
      </div>
      <div class="controls" style="margin-top:8px">
        <button class="btn-accent" onclick="applyFilters()">Apply filters</button>
        <button onclick="clearFilters()">Clear filters</button>
        <button class="btn-danger" onclick="highlightHighRisk()">High risk</button>
        <button onclick="topValue()">Top value</button>
        <button onclick="recentOnly()">Recent only</button>
      </div>
    </div>
  </div>

  <div class="panel" style="margin-top:20px">
    <h2>KPIs</h2>
    <div class="kpi">
      <div class="tile">
        <div>Total declared value</div>
        <div class="value" id="kpiValue">–</div>
      </div>
      <div class="tile">
        <div>Total quantity</div>
        <div class="value" id="kpiQty">–</div>
      </div>
      <div class="tile">
        <div>Average risk</div>
        <div class="value" id="kpiRisk">–</div>
      </div>
    </div>
  </div>

  <div class="grid" style="margin-top:20px">
    <div class="panel">
      <h3>Products</h3>
      <table id="tbl">
        <thead>
          <tr>
            <th>ID</th><th>Country</th><th>Product</th><th>Category</th><th>HS</th>
            <th>Qty</th><th>Value (NPR)</th><th>Risk</th><th>Notes</th><th>Created</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="panel">
      <h3>Charts</h3>
      <div style="display:grid;gap:12px">
        <canvas id="chartCountry" height="180"></canvas>
        <canvas id="chartRisk" height="180"></canvas>
      </div>
    </div>
  </div>
</div>

<div class="footer">
  Built for Nepal-focused evaluation of incoming products by country. Light bluish-black UI, bright white charts.
</div>

<script>
  let rowsCache = [];
  let chartCountry = null;
  let chartRisk = null;

  async function fetchCountry() {
    const name = document.getElementById('country').value.trim();
    if (!name) return;
    const el = document.getElementById('countryInfo');
    el.innerHTML = '<span class="pill">Fetching...</span>';
    try {
      const resp = await fetch('/api/country?name=' + encodeURIComponent(name));
      if (!resp.ok) throw new Error('Not found');
      const info = await resp.json();
      el.innerHTML = `
        <div style="display:flex;gap:10px;align-items:center">
          ${info.flag_png ? `<img src="${info.flag_png}" style="height:24px;border-radius:4px;border:1px solid #2a4b67"/>` : ''}
          <div>
            <div class="pill">${info.name || name}</div>
            <div style="font-size:12px;color:#b9c6d3">Region: ${info.region || '–'}, Capital: ${info.capital || '–'}, Population: ${info.population || '–'}</div>
            <div style="font-size:12px;color:#b9c6d3">Currencies: ${info.currencies || '–'}</div>
          </div>
        </div>
      `;
    } catch (e) {
      el.innerHTML = `<span class="pill">No info found</span>`;
    }
  }

  async function addProduct() {
    const country = document.getElementById('country').value.trim();
    const product = document.getElementById('product').value.trim();
    const category = document.getElementById('category').value.trim();
    const hs = document.getElementById('hs').value.trim();
    const qty = parseInt(document.getElementById('qty').value || '0', 10);
    const val = parseFloat(document.getElementById('val').value || '0');
    const risk = parseInt(document.getElementById('risk').value || '1', 10);
    const notes = document.getElementById('notes').value;

    if (!country || !product || !category || qty <= 0 || val < 0 || risk < 1 || risk > 5) {
      alert('Please fill required fields correctly.');
      return;
    }

    const resp = await fetch('/api/products', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        country, product_name: product, category, hs_code: hs,
        quantity: qty, declared_value: val, risk_level: risk, notes
      })
    });
    const data = await resp.json();
    if (resp.ok) {
      clearForm();
      refreshData();
    } else {
      alert(data.error || 'Error adding product');
    }
  }

  function clearForm() {
    ['country','product','hs','notes'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('qty').value = '100';
    document.getElementById('val').value = '100000';
    document.getElementById('risk').value = '3';
    document.getElementById('countryInfo').innerHTML = '';
  }

  async function refreshData(params = {}) {
    const qs = new URLSearchParams(params).toString();
    const resp = await fetch('/api/products' + (qs ? ('?' + qs) : ''));
    const data = await resp.json();
    rowsCache = data.items || [];
    renderTable(rowsCache);
    renderKPIs(data.kpis || {});
    renderCharts(rowsCache, data.kpis || {});
  }

  function renderKPIs(k) {
    document.getElementById('kpiValue').textContent = new Intl.NumberFormat('en-NP', {style:'currency', currency:'NPR', maximumFractionDigits:0}).format(k.total_value || 0);
    document.getElementById('kpiQty').textContent = (k.total_quantity || 0);
    document.getElementById('kpiRisk').textContent = (k.avg_risk || 0).toFixed(2);
  }

  function renderTable(rows) {
    const tbody = document.querySelector('#tbl tbody');
    tbody.innerHTML = '';
    for (const r of rows) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${r.id}</td>
        <td>${r.country}</td>
        <td>${r.product_name}</td>
        <td>${r.category}</td>
        <td>${r.hs_code || ''}</td>
        <td>${r.quantity}</td>
        <td>${r.declared_value}</td>
        <td><span class="pill risk${r.risk_level}">${r.risk_level}</span></td>
        <td>${(r.notes || '').slice(0,80)}</td>
        <td>${r.created_at}</td>
        <td><button onclick="deleteProduct(${r.id})">Delete</button></td>
        `;
        tbody.appendChild(tr);

    }
  }

  function renderCharts(rows, k) {
    const byCountry = {};
    const riskDist = {1:0,2:0,3:0,4:0,5:0};
    for (const r of rows) {
      byCountry[r.country] = (byCountry[r.country] || 0) + r.declared_value;
      riskDist[r.risk_level] = (riskDist[r.risk_level] || 0) + 1;
    }
    const countryLabels = Object.keys(byCountry);
    const countryValues = Object.values(byCountry);

    const riskLabels = Object.keys(riskDist);
    const riskValues = Object.values(riskDist);

    if (chartCountry) chartCountry.destroy();
    chartCountry = new Chart(document.getElementById('chartCountry').getContext('2d'), {
      type: 'bar',
      data: {
        labels: countryLabels,
        datasets: [{
          label: 'Declared value (NPR)',
          data: countryValues,
          backgroundColor: 'rgba(232, 240, 247, 0.7)',
          borderColor: 'rgba(232, 240, 247, 1)',
          borderWidth: 1
        }]
      },
      options: {
        plugins: { legend: { labels: { color: '#e8f0f7' } } },
        scales: {
          x: { ticks: { color: '#e8f0f7' }, grid: { color: '#1e3246' } },
          y: { ticks: { color: '#e8f0f7' }, grid: { color: '#1e3246' } }
        }
      }
    });

    if (chartRisk) chartRisk.destroy();
    chartRisk = new Chart(document.getElementById('chartRisk').getContext('2d'), {
      type: 'line',
      data: {
        labels: riskLabels,
        datasets: [{
          label: 'Count by risk',
          data: riskValues,
          tension: 0.3,
          fill: false,
          borderColor: 'rgba(232, 240, 247, 1)',
          backgroundColor: 'rgba(232, 240, 247, 0.7)'
        }]
      },
      options: {
        plugins: { legend: { labels: { color: '#e8f0f7' } } },
        scales: {
          x: { ticks: { color: '#e8f0f7' }, grid: { color: '#1e3246' } },
          y: { ticks: { color: '#e8f0f7' }, grid: { color: '#1e3246' } }
        }
      }
    });
  }

  function applyFilters() {
    const country = document.getElementById('fCountry').value.trim();
    const category = document.getElementById('fCategory').value.trim();
    const riskMin = document.getElementById('fRiskMin').value;
    const riskMax = document.getElementById('fRiskMax').value;
    const params = {};
    if (country) params.country = country;
    if (category) params.category = category;
    if (riskMin) params.risk_min = riskMin;
    if (riskMax) params.risk_max = riskMax;
    refreshData(params);
  }

  function clearFilters() {
    ['fCountry','fCategory','fRiskMin','fRiskMax'].forEach(id => document.getElementById(id).value = '');
    refreshData();
  }

  function highlightHighRisk() {
    document.getElementById('fRiskMin').value = '4';
    document.getElementById('fRiskMax').value = '5';
    applyFilters();
  }

  function topValue() {
    // Sort client-side by highest declared value
    const sorted = [...rowsCache].sort((a,b)=> b.declared_value - a.declared_value);
    renderTable(sorted.slice(0, 25));
  }

  function recentOnly() {
    // Already sorted by created_at desc in backend; just display first 25
    renderTable(rowsCache.slice(0,25));
  }

  function exportCSV() { window.location = '/export.csv'; }
  function exportJSON() { window.location = '/export.json'; }

  async function uploadCSV(file) {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    const resp = await fetch('/api/upload_csv', { method: 'POST', body: fd });
    if (resp.ok) {
      refreshData();
      alert('Uploaded successfully.');
    } else {
      alert('Upload failed.');
    }
    document.getElementById('csvInput').value = '';
  }
    async function deleteProduct(id) {
    if (!confirm("Delete item?")) return;
    await fetch("/api/products/" + id, { method: "DELETE" });
    refreshData();
}

  // Initial load
  refreshData();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
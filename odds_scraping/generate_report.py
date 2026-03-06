"""
generate_report.py
------------------
Read odds_history.jsonl and generate a self-contained HTML dashboard
showing current leaders and odds movement over time for each trophy market.

Usage:
    python generate_report.py              # generate and open odds_report.html
    python generate_report.py --no-browser # generate without opening
"""

import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JSONL_FILE = os.path.join(SCRIPT_DIR, "odds_history.jsonl")
OUTPUT_HTML = os.path.join(SCRIPT_DIR, "..", "index.html")

DASHBOARD_MARKET_META = {
    "Art Ross Trophy":  {"desc": "Most Regular-Season Points", "icon": "🏒"},
    "Hart Trophy":      {"desc": "Most Valuable Player",      "icon": "🏆"},
    "Vezina Trophy":    {"desc": "Best Goaltender",            "icon": "🥅"},
    "Norris Trophy":    {"desc": "Best Defenseman",            "icon": "🛡️"},
}

RAW_DATA_MARKET_META = {
    **DASHBOARD_MARKET_META,
    "Stanley Cup": {"desc": "Stanley Cup Winner", "icon": "🥇"},
}

PALETTE = [
    "#3b82f6", "#ef4444", "#22c55e", "#f59e0b", "#a855f7",
    "#06b6d4", "#ec4899", "#14b8a6", "#f97316", "#8b5cf6",
]


def load_snapshots(path: str) -> list[dict]:
    """Load all snapshots from the JSONL file, handling pretty-printed JSON."""
    if not os.path.exists(path):
        print(f"  No history file found at {path}")
        sys.exit(1)

    records = []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    # Try line-by-line first (proper JSONL)
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    # If that didn't work, try parsing as concatenated JSON objects
    if not records:
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(content):
            try:
                obj, end = decoder.raw_decode(content, idx)
                records.append(obj)
                idx = end
            except json.JSONDecodeError:
                idx += 1

    return records


def build_time_series(records: list[dict]) -> dict:
    """
    Build per-market time series data structure for the template.

    Returns: {
        "Art Ross Trophy": {
            "dates": ["2026-03-01", ...],
            "players": {
                "Connor McDavid": {"odds": ["+105", ...], "probs": [48.78, ...], "odds_int": [105, ...]},
                ...
            }
        }, ...
    }
    """
    series = {}
    for market_name in RAW_DATA_MARKET_META:
        dates = []
        player_data: dict[str, dict] = {}

        for rec in records:
            ts = rec["scraped_at"][:10]
            market = rec.get("markets", {}).get(market_name, [])
            if not market:
                continue
            dates.append(ts)
            for p in market:
                name = p["player"]
                if name not in player_data:
                    player_data[name] = {"odds": [], "probs": [], "odds_int": []}
                # Backfill nulls for dates this player wasn't present
                while len(player_data[name]["odds"]) < len(dates) - 1:
                    player_data[name]["odds"].append(None)
                    player_data[name]["probs"].append(None)
                    player_data[name]["odds_int"].append(None)
                player_data[name]["odds"].append(p["odds"])
                player_data[name]["probs"].append(round(p["implied_prob"] * 100, 2))
                player_data[name]["odds_int"].append(p["odds_int"])

            # Fill nulls for players not in this snapshot
            for name, d in player_data.items():
                while len(d["odds"]) < len(dates):
                    d["odds"].append(None)
                    d["probs"].append(None)
                    d["odds_int"].append(None)

        series[market_name] = {"dates": dates, "players": player_data}

    return series


def get_latest(records: list[dict]) -> dict[str, list[dict]]:
    """Get the most recent snapshot's market data."""
    if not records:
        return {}
    return records[-1].get("markets", {})


def build_change_log(records: list[dict]) -> dict[str, list[dict]]:
    """Build per-market list of odds changes between consecutive snapshots."""
    changes: dict[str, list[dict]] = {m: [] for m in RAW_DATA_MARKET_META}
    if len(records) < 2:
        return changes

    for i in range(1, len(records)):
        prev = records[i - 1]
        cur = records[i]
        ts = cur.get("scraped_at", "")[:16].replace("T", " ")

        for market_name in RAW_DATA_MARKET_META:
            prev_list = prev.get("markets", {}).get(market_name, [])
            cur_list = cur.get("markets", {}).get(market_name, [])
            if not prev_list or not cur_list:
                continue

            prev_map = {p.get("player"): p for p in prev_list if p.get("player")}
            cur_map = {p.get("player"): p for p in cur_list if p.get("player")}

            for player, cur_p in cur_map.items():
                prev_p = prev_map.get(player)
                if not prev_p:
                    changes[market_name].append(
                        {
                            "ts": ts,
                            "player": player,
                            "old_odds": None,
                            "new_odds": cur_p.get("odds"),
                            "old_prob": None,
                            "new_prob": round(float(cur_p.get("implied_prob", 0)) * 100, 2),
                            "delta_prob": None,
                        }
                    )
                    continue

                if cur_p.get("odds_int") != prev_p.get("odds_int"):
                    old_prob = round(float(prev_p.get("implied_prob", 0)) * 100, 2)
                    new_prob = round(float(cur_p.get("implied_prob", 0)) * 100, 2)
                    changes[market_name].append(
                        {
                            "ts": ts,
                            "player": player,
                            "old_odds": prev_p.get("odds"),
                            "new_odds": cur_p.get("odds"),
                            "old_prob": old_prob,
                            "new_prob": new_prob,
                            "delta_prob": round(new_prob - old_prob, 2),
                        }
                    )

    return changes


def generate_html(records: list[dict]) -> str:
    series = build_time_series(records)
    latest = get_latest(records)
    change_log = build_change_log(records)
    latest_ts = records[-1]["scraped_at"][:16].replace("T", " ") if records else "N/A"
    num_snapshots = len(records)

    # Build chart datasets JSON for each market
    charts_js = ""
    for idx, (market_name, meta) in enumerate(DASHBOARD_MARKET_META.items()):
        ms = series.get(market_name, {"dates": [], "players": {}})
        dates_json = json.dumps(ms["dates"])

        datasets = []
        for pidx, (player, data) in enumerate(ms["players"].items()):
            color = PALETTE[pidx % len(PALETTE)]
            datasets.append({
                "label": player,
                "data": data["probs"],
                "borderColor": color,
                "backgroundColor": color + "20",
                "borderWidth": 2.5,
                "pointRadius": 4,
                "pointHoverRadius": 6,
                "tension": 0.3,
                "fill": False,
                "spanGaps": True,
            })
        datasets_json = json.dumps(datasets)

        safe_id = market_name.replace(" ", "_").lower()
        charts_js += f"""
    new Chart(document.getElementById('chart_{safe_id}'), {{
        type: 'line',
        data: {{
            labels: {dates_json},
            datasets: {datasets_json}
        }},
        options: {{
            responsive: true,
            maintainAspectRatio: false,
            interaction: {{ mode: 'index', intersect: false }},
            plugins: {{
                legend: {{
                    labels: {{ color: '#e2e8f0', font: {{ size: 13 }}, usePointStyle: true, pointStyle: 'circle' }}
                }},
                tooltip: {{
                    backgroundColor: '#1e293b',
                    titleColor: '#e2e8f0',
                    bodyColor: '#e2e8f0',
                    borderColor: '#334155',
                    borderWidth: 1,
                    callbacks: {{
                        label: function(ctx) {{
                            return ctx.dataset.label + ': ' + (ctx.parsed.y !== null ? ctx.parsed.y.toFixed(1) + '%' : 'N/A');
                        }}
                    }}
                }}
            }},
            scales: {{
                x: {{
                    ticks: {{ color: '#94a3b8', maxRotation: 45 }},
                    grid: {{ color: '#1e293b' }}
                }},
                y: {{
                    title: {{ display: true, text: 'Implied Probability (%)', color: '#94a3b8' }},
                    ticks: {{ color: '#94a3b8', callback: function(v) {{ return v + '%'; }} }},
                    grid: {{ color: '#1e293b' }},
                    beginAtZero: true
                }}
            }}
        }}
    }});
"""

    # Build raw data views HTML
    raw_latest_html = ""
    raw_changes_html = ""
    raw_matrix_html = ""
    for market_name, meta in RAW_DATA_MARKET_META.items():
        ms = series.get(market_name, {"dates": [], "players": {}})
        dates = ms["dates"]
        players = ms["players"]

        # Build full timestamps (with time) for column headers
        full_timestamps = []
        for rec in records:
            ts = rec["scraped_at"][:16].replace("T", " ")
            if rec.get("markets", {}).get(market_name):
                full_timestamps.append(ts)

        if not dates or not players:
            raw_latest_html += f"""
        <div class="raw-table-section">
            <div class="raw-table-header">
                <span class="market-icon">{meta['icon']}</span>
                <h3>{market_name}</h3>
            </div>
            <p class="no-data">No data available</p>
        </div>"""
            raw_changes_html += f"""
        <div class="raw-table-section">
            <div class="raw-table-header">
                <span class="market-icon">{meta['icon']}</span>
                <h3>{market_name}</h3>
            </div>
            <p class="no-data">No data available</p>
        </div>"""
            raw_matrix_html += f"""
        <div class="raw-table-section">
            <div class="raw-table-header">
                <span class="market-icon">{meta['icon']}</span>
                <h3>{market_name}</h3>
            </div>
            <p class="no-data">No data available</p>
        </div>"""
            continue

        latest_list = latest.get(market_name, [])
        latest_rows = ""
        for i, p in enumerate(latest_list):
            prob = round(p["implied_prob"] * 100, 2)
            odds_color = "#22c55e" if p["odds_int"] < 0 else "#3b82f6"
            latest_rows += f"""
            <tr>
                <td class="rank-col">{i + 1}</td>
                <td class="player-col">{p['player']}</td>
                <td class="mono right" style="color:{odds_color}">{p['odds']}</td>
                <td class="right">{prob:.2f}%</td>
            </tr>"""

        raw_latest_html += f"""
        <div class="raw-table-section">
            <div class="raw-table-header">
                <span class="market-icon">{meta['icon']}</span>
                <h3>{market_name}</h3>
                <span class="raw-table-desc">Latest snapshot</span>
            </div>
            <div class="table-scroll">
                <table class="raw-table compact">
                    <thead>
                        <tr>
                            <th class="rank-col">#</th>
                            <th class="player-col">Player</th>
                            <th class="right">Odds</th>
                            <th class="right">Impl. %</th>
                        </tr>
                    </thead>
                    <tbody>
                        {latest_rows}
                    </tbody>
                </table>
            </div>
        </div>
"""

        market_changes = change_log.get(market_name, [])
        if market_changes:
            rows = ""
            for ch in reversed(market_changes[-250:]):
                delta = ch.get("delta_prob")
                if delta is None:
                    delta_html = "&mdash;"
                    delta_class = ""
                else:
                    delta_class = "delta-pos" if delta > 0 else ("delta-neg" if delta < 0 else "")
                    sign = "+" if delta > 0 else ""
                    delta_html = f"{sign}{delta:.2f}pp"

                old_odds = ch.get("old_odds") if ch.get("old_odds") is not None else "—"
                new_odds = ch.get("new_odds") if ch.get("new_odds") is not None else "—"
                old_prob = ch.get("old_prob")
                new_prob = ch.get("new_prob")

                old_prob_html = f"{old_prob:.2f}%" if old_prob is not None else "—"
                new_prob_html = f"{new_prob:.2f}%" if new_prob is not None else "—"

                rows += f"""
                <tr>
                    <td class="mono">{ch['ts']}</td>
                    <td class="player-col">{ch['player']}</td>
                    <td class="mono right">{old_odds}</td>
                    <td class="mono right">{new_odds}</td>
                    <td class="right">{old_prob_html}</td>
                    <td class="right">{new_prob_html}</td>
                    <td class="right {delta_class}">{delta_html}</td>
                </tr>"""
        else:
            rows = ""

        raw_changes_html += f"""
        <div class="raw-table-section">
            <div class="raw-table-header">
                <span class="market-icon">{meta['icon']}</span>
                <h3>{market_name}</h3>
                <span class="raw-table-desc">Odds changes (most recent first)</span>
            </div>
            <div class="table-scroll">
                <table class="raw-table compact">
                    <thead>
                        <tr>
                            <th class="mono">Time</th>
                            <th class="player-col">Player</th>
                            <th class="right">Old</th>
                            <th class="right">New</th>
                            <th class="right">Old %</th>
                            <th class="right">New %</th>
                            <th class="right">Δ</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows if rows else '<tr><td colspan="7" class="no-data">No odds changes detected</td></tr>'}
                    </tbody>
                </table>
            </div>
        </div>
"""

        # Header row
        date_headers = "".join(f'<th class="date-col">{d}</th>' for d in full_timestamps)
        header_row = f'<tr><th class="player-col">Player</th>{date_headers}</tr>'

        # Data rows — sort by latest implied prob descending
        sorted_players = sorted(
            players.items(),
            key=lambda x: (x[1]["probs"][-1] if x[1]["probs"][-1] is not None else -1),
            reverse=True,
        )

        body_rows = ""
        for player_name, data in sorted_players:
            cells = ""
            for i in range(len(dates)):
                odds = data["odds"][i]
                prob = data["probs"][i]
                odds_int = data["odds_int"][i]
                if odds is not None:
                    color = "#22c55e" if odds_int < 0 else "#3b82f6"
                    cells += f'<td><span class="odds-val" style="color:{color}">{odds}</span>'
                    cells += f'<span class="prob-val">{prob:.1f}%</span></td>'
                else:
                    cells += '<td class="na-cell">&mdash;</td>'
            body_rows += f'<tr><td class="player-col">{player_name}</td>{cells}</tr>'

        raw_matrix_html += f"""
        <div class="raw-table-section">
            <div class="raw-table-header">
                <span class="market-icon">{meta['icon']}</span>
                <h3>{market_name}</h3>
                <span class="raw-table-desc">{meta['desc']}</span>
            </div>
            <div class="table-scroll">
                <table class="raw-table">
                    <thead>{header_row}</thead>
                    <tbody>{body_rows}</tbody>
                </table>
            </div>
        </div>
"""

    # Build Stanley Cup tab HTML
    cup_meta = RAW_DATA_MARKET_META["Stanley Cup"]
    cup_players = latest.get("Stanley Cup", [])
    cup_safe_id = "stanley_cup"
    cup_rows_html = ""
    for i, p in enumerate(cup_players):
        prob = round(p["implied_prob"] * 100, 1)
        bar_width = min(prob, 100)
        odds_color = "#22c55e" if p["odds_int"] < 0 else "#3b82f6"
        cup_rows_html += f"""
            <div class=\"player-row\">
                <div class=\"player-rank\">{i + 1}</div>
                <div class=\"player-info\">
                    <div class=\"player-name\">{p['player']}</div>
                    <div class=\"prob-bar-bg\">
                        <div class=\"prob-bar\" style=\"width: {bar_width}%;\"></div>
                    </div>
                </div>
                <div class=\"player-odds\" style=\"color: {odds_color}\">{p['odds']}</div>
                <div class=\"player-prob\">{prob}%</div>
            </div>"""

    cup_tab_html = f"""
        <div class=\"market-section\">
            <div class=\"market-header\">
                <span class=\"market-icon\">{cup_meta['icon']}</span>
                <div>
                    <h2 class=\"market-title\">Stanley Cup — Winner</h2>
                    <p class=\"market-desc\">{cup_meta['desc']}</p>
                </div>
            </div>
            <div class=\"market-body\">
                <div class=\"leaderboard\">
                    <div class=\"leaderboard-header\">
                        <span class=\"lh-rank\">#</span>
                        <span class=\"lh-name\">Team</span>
                        <span class=\"lh-odds\">Odds</span>
                        <span class=\"lh-prob\">Impl. %</span>
                    </div>
                    {cup_rows_html}
                </div>
                <div class=\"chart-container\">
                    <canvas id=\"chart_{cup_safe_id}\"></canvas>
                </div>
            </div>
        </div>
"""

    # Add Stanley Cup chart JS
    cup_series = series.get("Stanley Cup", {"dates": [], "players": {}})
    cup_dates_json = json.dumps(cup_series.get("dates", []))
    cup_datasets = []
    for pidx, (player, data) in enumerate(cup_series.get("players", {}).items()):
        color = PALETTE[pidx % len(PALETTE)]
        cup_datasets.append(
            {
                "label": player,
                "data": data["probs"],
                "borderColor": color,
                "backgroundColor": color + "20",
                "borderWidth": 2.0,
                "pointRadius": 3,
                "pointHoverRadius": 5,
                "tension": 0.3,
                "fill": False,
                "spanGaps": True,
            }
        )
    cup_datasets_json = json.dumps(cup_datasets)
    charts_js += f"""
    new Chart(document.getElementById('chart_{cup_safe_id}'), {{
        type: 'line',
        data: {{
            labels: {cup_dates_json},
            datasets: {cup_datasets_json}
        }},
        options: {{
            responsive: true,
            maintainAspectRatio: false,
            interaction: {{ mode: 'index', intersect: false }},
            plugins: {{
                legend: {{
                    labels: {{ color: '#e2e8f0', font: {{ size: 12 }}, usePointStyle: true, pointStyle: 'circle' }}
                }},
                tooltip: {{
                    backgroundColor: '#1e293b',
                    titleColor: '#e2e8f0',
                    bodyColor: '#e2e8f0',
                    borderColor: '#334155',
                    borderWidth: 1,
                    callbacks: {{
                        label: function(ctx) {{
                            return ctx.dataset.label + ': ' + (ctx.parsed.y !== null ? ctx.parsed.y.toFixed(1) + '%' : 'N/A');
                        }}
                    }}
                }}
            }},
            scales: {{
                x: {{
                    ticks: {{ color: '#94a3b8', maxRotation: 45 }},
                    grid: {{ color: '#1e293b' }}
                }},
                y: {{
                    title: {{ display: true, text: 'Implied Probability (%)', color: '#94a3b8' }},
                    ticks: {{ color: '#94a3b8', callback: function(v) {{ return v + '%'; }} }},
                    grid: {{ color: '#1e293b' }},
                    beginAtZero: true
                }}
            }}
        }}
    }});
"""

    # Build market cards HTML (awards only)
    cards_html = ""
    for market_name, meta in DASHBOARD_MARKET_META.items():
        players = latest.get(market_name, [])
        safe_id = market_name.replace(" ", "_").lower()

        rows_html = ""
        for i, p in enumerate(players):
            prob = round(p["implied_prob"] * 100, 1)
            bar_width = min(prob, 100)
            rank_class = "rank-1" if i == 0 else ("rank-2" if i == 1 else ("rank-3" if i == 2 else ""))
            odds_color = "#22c55e" if p["odds_int"] < 0 else "#3b82f6"

            rows_html += f"""
            <div class="player-row">
                <div class="player-rank {rank_class}">{i + 1}</div>
                <div class="player-info">
                    <div class="player-name">{p['player']}</div>
                    <div class="prob-bar-bg">
                        <div class="prob-bar" style="width: {bar_width}%;"></div>
                    </div>
                </div>
                <div class="player-odds" style="color: {odds_color}">{p['odds']}</div>
                <div class="player-prob">{prob}%</div>
            </div>"""

        cards_html += f"""
        <div class="market-section">
            <div class="market-header">
                <span class="market-icon">{meta['icon']}</span>
                <div>
                    <h2 class="market-title">{market_name}</h2>
                    <p class="market-desc">{meta['desc']}</p>
                </div>
            </div>
            <div class="market-body">
                <div class="leaderboard">
                    <div class="leaderboard-header">
                        <span class="lh-rank">#</span>
                        <span class="lh-name">Player</span>
                        <span class="lh-odds">Odds</span>
                        <span class="lh-prob">Impl. %</span>
                    </div>
                    {rows_html}
                </div>
                <div class="chart-container">
                    <canvas id="chart_{safe_id}"></canvas>
                </div>
            </div>
        </div>
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NHL Trophy Odds Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: #0f172a;
        color: #e2e8f0;
        min-height: 100vh;
        padding: 0;
    }}
    .header {{
        background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
        border-bottom: 1px solid #1e293b;
        padding: 28px 32px;
        text-align: center;
    }}
    .header h1 {{
        font-size: 1.8rem;
        font-weight: 700;
        background: linear-gradient(90deg, #3b82f6, #06b6d4);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 6px;
    }}
    .header .subtitle {{
        color: #64748b;
        font-size: 0.9rem;
    }}
    .header .meta {{
        display: flex;
        justify-content: center;
        gap: 24px;
        margin-top: 12px;
        font-size: 0.82rem;
        color: #475569;
    }}
    .header .meta span {{
        display: flex;
        align-items: center;
        gap: 5px;
    }}
    .container {{
        max-width: 1100px;
        margin: 0 auto;
        padding: 24px 20px 48px;
    }}
    .market-section {{
        background: #1e293b;
        border: 1px solid #334155;
        border-radius: 12px;
        margin-bottom: 28px;
        overflow: hidden;
    }}
    .market-header {{
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 18px 24px;
        border-bottom: 1px solid #334155;
        background: linear-gradient(135deg, #1e293b 0%, #172033 100%);
    }}
    .market-icon {{ font-size: 1.6rem; }}
    .market-title {{
        font-size: 1.2rem;
        font-weight: 600;
        color: #f1f5f9;
    }}
    .market-desc {{
        font-size: 0.8rem;
        color: #64748b;
        margin-top: 2px;
    }}
    .market-body {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 0;
    }}
    @media (max-width: 800px) {{
        .market-body {{ grid-template-columns: 1fr; }}
    }}
    .leaderboard {{
        padding: 16px 20px;
        border-right: 1px solid #334155;
    }}
    @media (max-width: 800px) {{
        .leaderboard {{ border-right: none; border-bottom: 1px solid #334155; }}
    }}
    .leaderboard-header {{
        display: grid;
        grid-template-columns: 32px 1fr 64px 64px;
        gap: 8px;
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: #475569;
        padding-bottom: 8px;
        border-bottom: 1px solid #283040;
        margin-bottom: 6px;
    }}
    .lh-odds, .lh-prob {{ text-align: right; }}
    .player-row {{
        display: grid;
        grid-template-columns: 32px 1fr 64px 64px;
        gap: 8px;
        align-items: center;
        padding: 8px 0;
        border-bottom: 1px solid #283040;
        transition: background 0.15s;
    }}
    .player-row:last-child {{ border-bottom: none; }}
    .player-row:hover {{ background: #283040; border-radius: 6px; }}
    .player-rank {{
        width: 26px;
        height: 26px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 0.8rem;
        font-weight: 700;
        border-radius: 6px;
        background: #283040;
        color: #64748b;
    }}
    .player-rank.rank-1 {{ background: #854d0e; color: #fbbf24; }}
    .player-rank.rank-2 {{ background: #374151; color: #d1d5db; }}
    .player-rank.rank-3 {{ background: #7c2d12; color: #fb923c; }}
    .player-name {{
        font-size: 0.9rem;
        font-weight: 500;
        color: #f1f5f9;
        margin-bottom: 4px;
    }}
    .prob-bar-bg {{
        height: 4px;
        background: #283040;
        border-radius: 2px;
        overflow: hidden;
    }}
    .prob-bar {{
        height: 100%;
        background: linear-gradient(90deg, #3b82f6, #06b6d4);
        border-radius: 2px;
        transition: width 0.4s;
    }}
    .player-odds {{
        text-align: right;
        font-size: 0.9rem;
        font-weight: 600;
        font-family: 'SF Mono', 'Menlo', monospace;
    }}
    .player-prob {{
        text-align: right;
        font-size: 0.82rem;
        color: #94a3b8;
    }}
    .chart-container {{
        padding: 16px 20px;
        height: 260px;
        position: relative;
    }}
    .footer {{
        text-align: center;
        padding: 20px;
        color: #475569;
        font-size: 0.75rem;
    }}

    /* ── Tabs ── */
    .tab-bar {{
        display: flex;
        justify-content: center;
        gap: 4px;
        padding: 16px 20px 0;
        max-width: 1100px;
        margin: 0 auto;
    }}
    .tab-btn {{
        padding: 10px 28px;
        border: 1px solid #334155;
        border-bottom: none;
        border-radius: 8px 8px 0 0;
        background: #1e293b;
        color: #94a3b8;
        font-size: 0.9rem;
        font-weight: 500;
        cursor: pointer;
        transition: all 0.15s;
    }}
    .tab-btn:hover {{ color: #e2e8f0; background: #283040; }}
    .tab-btn.active {{
        background: #0f172a;
        color: #e2e8f0;
        border-color: #3b82f6;
        border-bottom: 1px solid #0f172a;
        position: relative;
        z-index: 1;
    }}
    .tab-content {{ display: none; }}
    .tab-content.active {{ display: block; }}

    .raw-subtabs {{
        display: flex;
        gap: 8px;
        padding: 0 4px 16px;
        align-items: center;
        flex-wrap: wrap;
    }}
    .raw-subtab-btn {{
        padding: 8px 12px;
        border: 1px solid #334155;
        border-radius: 10px;
        background: #1e293b;
        color: #94a3b8;
        font-size: 0.85rem;
        cursor: pointer;
        transition: all 0.15s;
    }}
    .raw-subtab-btn:hover {{ color: #e2e8f0; background: #283040; }}
    .raw-subtab-btn.active {{
        color: #e2e8f0;
        border-color: #3b82f6;
        background: #0f172a;
    }}
    .raw-view {{ display: none; }}
    .raw-view.active {{ display: block; }}

    /* ── Raw Data Tables ── */
    .raw-table-section {{
        background: #1e293b;
        border: 1px solid #334155;
        border-radius: 12px;
        margin-bottom: 24px;
        overflow: hidden;
    }}
    .raw-table-header {{
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 16px 24px;
        border-bottom: 1px solid #334155;
        background: linear-gradient(135deg, #1e293b 0%, #172033 100%);
    }}
    .raw-table-header h3 {{
        font-size: 1.1rem;
        font-weight: 600;
        color: #f1f5f9;
    }}
    .raw-table-desc {{
        font-size: 0.8rem;
        color: #64748b;
        margin-left: auto;
    }}
    .table-scroll {{
        overflow-x: auto;
    }}
    .raw-table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 0.85rem;
    }}
    .raw-table.compact {{
        font-size: 0.86rem;
    }}
    .raw-table thead th {{
        background: #172033;
        color: #94a3b8;
        font-weight: 600;
        font-size: 0.75rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        padding: 10px 14px;
        text-align: center;
        border-bottom: 1px solid #334155;
        white-space: nowrap;
        position: sticky;
        top: 0;
    }}
    .raw-table thead th.player-col {{
        text-align: left;
        position: sticky;
        left: 0;
        z-index: 2;
        background: #172033;
        min-width: 160px;
    }}
    .raw-table tbody td {{
        padding: 10px 14px;
        text-align: center;
        border-bottom: 1px solid #283040;
        white-space: nowrap;
    }}
    .raw-table tbody td.player-col {{
        text-align: left;
        font-weight: 500;
        color: #f1f5f9;
        position: sticky;
        left: 0;
        background: #1e293b;
        z-index: 1;
        min-width: 160px;
    }}
    .raw-table tbody tr:hover td {{
        background: #283040;
    }}
    .raw-table tbody tr:hover td.player-col {{
        background: #283040;
    }}
    .odds-val {{
        font-family: 'SF Mono', 'Menlo', monospace;
        font-weight: 600;
        font-size: 0.88rem;
        display: block;
    }}
    .prob-val {{
        color: #64748b;
        font-size: 0.75rem;
        display: block;
        margin-top: 1px;
    }}
    .na-cell {{
        color: #475569;
    }}
    .no-data {{
        padding: 24px;
        color: #475569;
        text-align: center;
    }}
    .date-col {{
        min-width: 110px;
    }}
    .rank-col {{
        width: 44px;
        text-align: center;
    }}
    .mono {{
        font-family: 'SF Mono', 'Menlo', monospace;
    }}
    .right {{
        text-align: right;
    }}
    .delta-pos {{
        color: #22c55e;
        font-weight: 600;
    }}
    .delta-neg {{
        color: #ef4444;
        font-weight: 600;
    }}
</style>
</head>
<body>

<div class="header">
    <h1>NHL Trophy Odds Dashboard</h1>
    <p class="subtitle">FanDuel Sportsbook — 2025-26 Season</p>
    <div class="meta">
        <span>📅 Last updated: {latest_ts}</span>
        <span>📊 {num_snapshots} snapshot{"s" if num_snapshots != 1 else ""} collected</span>
        <span>🏒 {len(RAW_DATA_MARKET_META)} markets tracked</span>
    </div>
</div>

<div class="tab-bar">
    <button class="tab-btn active" onclick="switchTab('dashboard')">Dashboard</button>
    <button class="tab-btn" onclick="switchTab('cup')">Stanley Cup</button>
    <button class="tab-btn" onclick="switchTab('rawdata')">Raw Data</button>
</div>

<div class="container">
    <div id="tab-dashboard" class="tab-content active">
        {cards_html}
    </div>
    <div id="tab-cup" class="tab-content">
        {cup_tab_html}
    </div>
    <div id="tab-rawdata" class="tab-content">
        <div class="raw-subtabs">
            <button class="raw-subtab-btn active" onclick="switchRawView('latest')">Latest</button>
            <button class="raw-subtab-btn" onclick="switchRawView('changes')">Changes</button>
            <button class="raw-subtab-btn" onclick="switchRawView('matrix')">Matrix</button>
        </div>
        <div id="raw-view-latest" class="raw-view active">
            {raw_latest_html}
        </div>
        <div id="raw-view-changes" class="raw-view">
            {raw_changes_html}
        </div>
        <div id="raw-view-matrix" class="raw-view">
            {raw_matrix_html}
        </div>
    </div>
</div>

<div class="footer">
    Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} &middot; Data from FanDuel Sportsbook
</div>

<script>
function switchTab(tabId) {{
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    document.getElementById('tab-' + tabId).classList.add('active');
    event.target.classList.add('active');
}}

function switchRawView(viewId) {{
    document.querySelectorAll('.raw-view').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.raw-subtab-btn').forEach(el => el.classList.remove('active'));
    document.getElementById('raw-view-' + viewId).classList.add('active');
    event.target.classList.add('active');
}}

Chart.defaults.color = '#94a3b8';
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
{charts_js}
</script>
</body>
</html>"""

    return html


def main():
    parser = argparse.ArgumentParser(description="Generate NHL trophy odds HTML dashboard.")
    parser.add_argument("--no-browser", action="store_true", help="Don't open the report in a browser.")
    args = parser.parse_args()

    print("  Loading odds history...")
    records = load_snapshots(JSONL_FILE)
    print(f"  ✓ {len(records)} snapshot(s) loaded")

    print("  Generating HTML report...")
    html = generate_html(records)

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ Report saved: {OUTPUT_HTML}")

    if not args.no_browser:
        webbrowser.open(f"file://{OUTPUT_HTML}")
        print("  ✓ Opened in browser")


if __name__ == "__main__":
    main()

"""
reporter.py — Test sonuçlarını HTML ve CSV olarak raporlar.
"""

import csv
import json
import logging
import os
from datetime import datetime
from typing import List, Dict
from jinja2 import Environment, FileSystemLoader, Template

logger = logging.getLogger(__name__)

# Rapor şablonu (Jinja2)
REPORT_TEMPLATE = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mail Otomasyon Test Raporu - {{ run_id }}</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono&display=swap" rel="stylesheet">
    <style>
        :root {
            --primary: #6366f1; --primary-dark: #4f46e5;
            --success: #10b981; --warning: #f59e0b; --danger: #ef4444;
            --bg: #f8fafc; --surface: #ffffff; --text: #1e293b; --muted: #64748b;
            --border: #e2e8f0;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }
        
        .header { background: #0f172a; color: white; padding: 40px; border-bottom: 4px solid var(--primary); }
        .header-content { max-width: 1200px; margin: 0 auto; display: flex; justify-content: space-between; align-items: flex-end; }
        .header h1 { font-size: 32px; font-weight: 800; letter-spacing: -0.025em; }
        .header .meta { color: #94a3b8; font-family: 'JetBrains Mono', monospace; font-size: 14px; margin-top: 8px; }
        
        .summary-bar { max-width: 1200px; margin: -30px auto 30px; display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; padding: 0 20px; }
        .stat-card { background: var(--surface); border-radius: 16px; padding: 24px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.1); border: 1px solid var(--border); }
        .stat-card .val { font-size: 36px; font-weight: 800; display: block; }
        .stat-card .lbl { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
        .stat-card.pass .val { color: var(--success); }
        .stat-card.fail .val { color: var(--danger); }
        .stat-card.warn .val { color: var(--warning); }
        
        .content { max-width: 1200px; margin: 0 auto; padding: 0 20px 60px; }
        .combo-section { margin-bottom: 40px; }
        .combo-header { background: #f1f5f9; padding: 16px 24px; border-radius: 12px; display: flex; justify-content: space-between; align-items: center; border: 1px solid var(--border); margin-bottom: 16px; }
        .combo-header h2 { font-size: 18px; font-weight: 700; color: #334155; }
        
        .results-grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
        .result-item { background: var(--surface); border-radius: 12px; border: 1px solid var(--border); overflow: hidden; transition: transform 0.2s; }
        .result-item:hover { transform: translateY(-2px); box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
        .item-main { padding: 16px 24px; display: flex; align-items: center; gap: 20px; cursor: pointer; }
        
        .badge { padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 700; text-transform: uppercase; }
        .badge.pass { background: #d1fae5; color: #065f46; }
        .badge.fail { background: #fee2e2; color: #991b1b; }
        .badge.skip { background: #f1f5f9; color: #475569; }
        
        .scenario-name { flex: 1; font-weight: 600; font-size: 15px; }
        .summary-text { color: var(--muted); font-size: 14px; max-width: 400px; }
        
        .item-details { padding: 0 24px 24px; display: none; border-top: 1px solid var(--border); background: #fafafa; }
        .item-details.active { display: block; }
        
        .checks-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-top: 20px; }
        .check-box, .issue-box { padding: 12px; border-radius: 8px; border: 1px solid var(--border); font-size: 13px; }
        .check-box.ok { border-left: 4px solid var(--success); }
        .check-box.err { border-left: 4px solid var(--danger); }
        .issue-box { border-left: 4px solid var(--warning); background: #fffbeb; }
        
        .screenshot-box { margin-top: 20px; border-radius: 12px; overflow: hidden; border: 1px solid var(--border); }
        .screenshot-box img { max-width: 100%; display: block; }
        
        .confidence-label { font-size: 11px; font-weight: 800; color: var(--muted); background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
        
        @media print { .header { color: black; background: white; border-bottom: 2px solid black; } .stat-card { box-shadow: none; border: 1px solid #ccc; } .item-details { display: block !important; } }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-content">
            <div>
                <p class="meta">#{{ run_id }}</p>
                <h1>Mail Otomasyon Raporu</h1>
            </div>
            <div class="meta" style="text-align: right">
                {{ report_date }}<br>
                Analiz: Claude AI Engine
            </div>
        </div>
    </div>

    <div class="summary-bar">
        <div class="stat-card total"><span class="val">{{ total }}</span><span class="lbl">Toplam Test</span></div>
        <div class="stat-card pass"><span class="val">{{ passed }}</span><span class="lbl">Başarılı</span></div>
        <div class="stat-card fail"><span class="val">{{ failed }}</span><span class="lbl">Hatalı</span></div>
        <div class="stat-card warn"><span class="val">{{ pass_rate }}%</span><span class="lbl">Başarı Oranı</span></div>
    </div>

    <div class="content">
        {% for combo, results in groups.items() %}
        <div class="combo-section">
            <div class="combo-header">
                <h2>🔀 {{ combo }}</h2>
                <span class="badge {{ 'pass' if results|sum(attribute='analysis.passed') == results|length else 'fail' }}">
                    {{ results|sum(attribute='analysis.passed') }} / {{ results|length }}
                </span>
            </div>
            
            <div class="results-grid">
                {% for r in results %}
                <div class="result-item">
                    <div class="item-main" onclick="this.nextElementSibling.classList.toggle('active')">
                        <span class="badge {{ 'pass' if r.analysis.passed else 'fail' }}">
                            {{ 'PASS' if r.analysis.passed else 'FAIL' }}
                        </span>
                        <div class="scenario-name">{{ r.scenario_type }} <span class="confidence-label">{{ r.analysis.confidence }}</span></div>
                        <div class="summary-text">{{ r.analysis.summary }}</div>
                        <div style="color: var(--primary)">▼</div>
                    </div>
                    <div class="item-details">
                        <div class="checks-grid">
                            {% for check in r.analysis.checks %}
                            <div class="check-box {{ 'ok' if check.passed else 'err' }}">
                                <b>{{ check.name }}</b>: {{ check.detail }}
                            </div>
                            {% endfor %}
                            
                            {% for issue in r.analysis.issues %}
                            <div class="issue-box">
                                ⚠️ <b>Hata:</b> {{ issue }}
                            </div>
                            {% endfor %}
                        </div>

                        {% if r.analysis.screenshot %}
                        <div class="checks-grid" style="grid-template-columns: 1fr {{ '1fr 1fr' if r.analysis.baseline else '' }};">
                            <div class="screenshot-box">
                                <p style="padding:10px; font-weight:700; border-bottom:1px solid #eee">📸 Mevcut Render</p>
                                <img src="{{ r.analysis.screenshot|replace('reports/', '', 1) }}" alt="Screenshot">
                            </div>
                            {% if r.analysis.baseline %}
                            <div class="screenshot-box">
                                <p style="padding:10px; font-weight:700; border-bottom:1px solid #eee">🖼️ Baseline</p>
                                <img src="/api/visual/diff-info?path={{ r.analysis.baseline|urlencode }}" alt="Baseline">
                            </div>
                            <div class="screenshot-box">
                                <p style="padding:10px; font-weight:700; border-bottom:1px solid #eee">🔍 Fark (%{{ r.analysis.diff_percent|round(2) }})</p>
                                <img src="/api/visual/diff-info?path={{ r.analysis.diff_screenshot|urlencode }}" alt="Diff">
                            </div>
                            {% endif %}
                        </div>
                        {% endif %}
                        
                        <div style="margin-top:20px; font-size:12px; color:var(--muted)">
                            <b>Test Zamanı:</b> {{ r.test_time }} | <b>İstemci:</b> {{ r.send_meta.sender_client }}
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endfor %}
    </div>

    <footer style="text-align: center; padding: 40px; color: var(--muted); font-size: 13px; border-top: 1px solid var(--border)">
        Mail Otomasyon Test Sistemi &copy; 2026
    </footer>
</body>
</html>
"""

def generate_html_report(results: List[dict], output_path: str):
    """Test sonuçlarından profesyonel HTML rapor üretir (Jinja2)."""
    if not results:
        logger.warning("Rapor üretilecek sonuç yok.")
        return

    total = len(results)
    passed = sum(1 for r in results if r.get("analysis", {}).get("passed") is True)
    failed = sum(1 for r in results if r.get("analysis", {}).get("passed") is False)
    pass_rate = round((passed / total * 100) if total > 0 else 0, 1)

    # Gruplandırma
    groups = {}
    for r in results:
        combo = r.get("combination", "Genel")
        groups.setdefault(combo, []).append(r)

    # Jinja2 rendering
    template = Template(REPORT_TEMPLATE)
    html_content = template.render(
        run_id=results[0].get("run_id", "Manual Run"),
        report_date=datetime.now().strftime("%d %B %Y, %H:%M"),
        total=total,
        passed=passed,
        failed=failed,
        pass_rate=pass_rate,
        groups=groups
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info(f"Yüksek kaliteli HTML raporu oluşturuldu: {output_path}")


def generate_csv_results(results: List[dict], output_path: str):
    """Test sonuçlarını CSV olarak kaydeder."""
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Kombinasyon", "Senaryo Tipi", "Sonuç", "Güven Seviyesi",
            "Özet", "Sorunlar", "Öneriler", "Test Zamanı", "Ekran Görüntüsü"
        ])
        for r in results:
            analysis = r.get("analysis", {})
            writer.writerow([
                r.get("combination", ""),
                r.get("scenario_type", ""),
                "PASS" if analysis.get("passed") else "FAIL",
                analysis.get("confidence", ""),
                analysis.get("summary", ""),
                " | ".join(analysis.get("issues", [])),
                " | ".join(analysis.get("recommendations", [])),
                r.get("test_time", ""),
                analysis.get("screenshot", ""),
            ])
    logger.info(f"CSV sonuçları oluşturuldu: {output_path}")

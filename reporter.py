"""
reporter.py — Test sonuçlarını HTML ve CSV olarak raporlar.
"""

import csv
import json
import logging
from datetime import datetime
from typing import List, Dict

logger = logging.getLogger(__name__)


def generate_html_report(results: List[dict], output_path: str):
    """Test sonuçlarından profesyonel HTML rapor üretir."""
    total = len(results)
    passed = sum(1 for r in results if r.get("analysis", {}).get("passed", False))
    failed = total - passed
    pass_rate = round((passed / total * 100) if total > 0 else 0, 1)

    # Kombinasyon bazlı grupla
    by_combo = {}
    for r in results:
        key = r["combination"]
        by_combo.setdefault(key, []).append(r)

    combo_rows = ""
    for combo, combo_results in by_combo.items():
        c_total = len(combo_results)
        c_passed = sum(1 for r in combo_results if r.get("analysis", {}).get("passed", False))
        c_rate = round((c_passed / c_total * 100) if c_total > 0 else 0, 1)
        status_class = "pass" if c_rate == 100 else ("partial" if c_rate > 0 else "fail")

        scenario_rows = ""
        for r in combo_results:
            analysis = r.get("analysis", {})
            sc_passed = analysis.get("passed", False)
            sc_class = "pass" if sc_passed else "fail"
            checks_html = ""
            for ch in analysis.get("checks", []):
                ch_icon = "✅" if ch.get("passed") else "❌"
                checks_html += f'<li>{ch_icon} <b>{ch.get("name","")}</b>: {ch.get("detail","")}</li>'
            issues = analysis.get("issues", [])
            issues_html = "".join(f"<li>⚠️ {i}</li>" for i in issues) if issues else ""

            scenario_rows += f"""
            <tr class="{sc_class}-row">
              <td><span class="badge {sc_class}">{('✅ PASS' if sc_passed else '❌ FAIL')}</span></td>
              <td>{r['scenario_type']}</td>
              <td class="summary-cell">{analysis.get('summary','')}</td>
              <td>
                <details>
                  <summary>Kontroller ({len(analysis.get('checks',[]))})</summary>
                  <ul class="checks-list">{checks_html}</ul>
                  {f'<ul class="issues-list">{issues_html}</ul>' if issues_html else ''}
                </details>
              </td>
              <td><span class="confidence confidence-{analysis.get('confidence','LOW').lower()}">{analysis.get('confidence','?')}</span></td>
            </tr>"""

        combo_rows += f"""
        <div class="combo-card {status_class}-card">
          <div class="combo-header">
            <h3>🔀 {combo}</h3>
            <div class="combo-stats">
              <span class="stat-badge">{c_passed}/{c_total} PASS</span>
              <span class="rate-badge {status_class}">{c_rate}%</span>
            </div>
          </div>
          <table class="results-table">
            <thead>
              <tr>
                <th>Sonuç</th><th>Senaryo</th><th>Özet</th><th>Kontrol Detayı</th><th>Güven</th>
              </tr>
            </thead>
            <tbody>{scenario_rows}</tbody>
          </table>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mail Otomasyon Test Raporu</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #f0f2f5; color: #1a1a2e; }}
  .header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
             color: white; padding: 32px 40px; }}
  .header h1 {{ font-size: 28px; font-weight: 700; }}
  .header .meta {{ color: #a8b2d8; margin-top: 8px; font-size: 14px; }}
  .summary-bar {{ display: flex; gap: 20px; padding: 24px 40px; background: white;
                  border-bottom: 1px solid #e2e8f0; flex-wrap: wrap; }}
  .stat-card {{ background: #f8fafc; border-radius: 12px; padding: 16px 24px;
                border-left: 4px solid #e2e8f0; min-width: 140px; }}
  .stat-card.total {{ border-left-color: #6366f1; }}
  .stat-card.pass  {{ border-left-color: #10b981; }}
  .stat-card.fail  {{ border-left-color: #ef4444; }}
  .stat-card.rate  {{ border-left-color: #f59e0b; }}
  .stat-card .val {{ font-size: 32px; font-weight: 800; }}
  .stat-card .lbl {{ font-size: 13px; color: #64748b; margin-top: 4px; }}
  .content {{ padding: 32px 40px; }}
  .combo-card {{ background: white; border-radius: 16px; margin-bottom: 24px;
                 box-shadow: 0 2px 8px rgba(0,0,0,0.08); overflow: hidden; }}
  .combo-header {{ display: flex; justify-content: space-between; align-items: center;
                   padding: 16px 24px; border-bottom: 1px solid #f1f5f9; }}
  .combo-header h3 {{ font-size: 16px; color: #1e293b; }}
  .combo-stats {{ display: flex; gap: 10px; align-items: center; }}
  .stat-badge {{ background: #e2e8f0; padding: 4px 12px; border-radius: 999px;
                 font-size: 13px; font-weight: 600; }}
  .rate-badge {{ padding: 4px 12px; border-radius: 999px; font-size: 13px; font-weight: 700; }}
  .rate-badge.pass {{ background: #d1fae5; color: #065f46; }}
  .rate-badge.partial {{ background: #fef3c7; color: #92400e; }}
  .rate-badge.fail {{ background: #fee2e2; color: #991b1b; }}
  .results-table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  .results-table th {{ background: #f8fafc; padding: 10px 16px; text-align: left;
                       font-size: 12px; color: #64748b; text-transform: uppercase;
                       letter-spacing: 0.5px; border-bottom: 1px solid #e2e8f0; }}
  .results-table td {{ padding: 12px 16px; border-bottom: 1px solid #f1f5f9; vertical-align: top; }}
  .pass-row {{ background: #f0fdf4; }}
  .fail-row {{ background: #fff5f5; }}
  .badge {{ padding: 3px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; }}
  .badge.pass {{ background: #d1fae5; color: #065f46; }}
  .badge.fail {{ background: #fee2e2; color: #991b1b; }}
  .summary-cell {{ max-width: 240px; }}
  details summary {{ cursor: pointer; color: #6366f1; font-size: 13px; font-weight: 500; }}
  .checks-list, .issues-list {{ margin-top: 8px; padding-left: 16px; font-size: 13px;
                                 color: #374151; line-height: 1.8; }}
  .issues-list {{ color: #b45309; }}
  .confidence {{ padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }}
  .confidence-high {{ background: #d1fae5; color: #065f46; }}
  .confidence-medium {{ background: #fef3c7; color: #92400e; }}
  .confidence-low {{ background: #fee2e2; color: #991b1b; }}
  .pass-card .combo-header {{ background: #f0fdf4; }}
  .partial-card .combo-header {{ background: #fffbeb; }}
  .fail-card .combo-header {{ background: #fff5f5; }}
  @media print {{ body {{ background: white; }} .combo-card {{ box-shadow: none; }} }}
</style>
</head>
<body>
<div class="header">
  <h1>📧 Mail Servis Otomasyon Test Raporu</h1>
  <div class="meta">Oluşturulma: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')} &nbsp;|&nbsp; 
  Claude API Destekli Analiz</div>
</div>

<div class="summary-bar">
  <div class="stat-card total"><div class="val">{total}</div><div class="lbl">Toplam Senaryo</div></div>
  <div class="stat-card pass"><div class="val" style="color:#10b981">{passed}</div><div class="lbl">✅ Başarılı</div></div>
  <div class="stat-card fail"><div class="val" style="color:#ef4444">{failed}</div><div class="lbl">❌ Başarısız</div></div>
  <div class="stat-card rate"><div class="val" style="color:#f59e0b">{pass_rate}%</div><div class="lbl">Başarı Oranı</div></div>
  <div class="stat-card"><div class="val" style="font-size:20px">{len(by_combo)}</div><div class="lbl">Kombinasyon</div></div>
</div>

<div class="content">
{combo_rows}
</div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"HTML rapor oluşturuldu: {output_path}")


def generate_csv_results(results: List[dict], output_path: str):
    """Test sonuçlarını CSV olarak kaydeder (Sheets'e aktarım için)."""
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Kombinasyon", "Senaryo Tipi", "Sonuç", "Güven Seviyesi",
            "Özet", "Sorunlar", "Öneriler", "Test Zamanı"
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
            ])
    logger.info(f"CSV sonuçları oluşturuldu: {output_path}")

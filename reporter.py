"""
reporter.py — Test sonuçlarını HTML ve CSV olarak raporlar.
"""

import csv
import html
import logging
from datetime import datetime
from typing import List

logger = logging.getLogger(__name__)


def _esc(value) -> str:
    """Mail içeriğinden / Claude çıktısından gelen metni HTML için kaçırır."""
    return html.escape(str(value)) if value is not None else ""


def _text(value) -> str:
    """Yazılabilir metin: None → "", tek başına surrogate (ör. LLM JSON'undaki
    "\\ud800") → U+FFFD. Eskiden UnicodeEncodeError tüm raporu düşürüyordu."""
    if value is None:
        return ""
    return str(value).encode("utf-8", "replace").decode("utf-8")


def _normalize(result) -> dict:
    """Rapor girişini güvenli biçime getirir — tek bozuk satır (analysis=None,
    checks=None, eksik anahtar, metin olmayan issue) eskiden raporun tamamını
    düşürüyordu."""
    r = result if isinstance(result, dict) else {}
    a = r.get("analysis") if isinstance(r.get("analysis"), dict) else {}

    def str_list(v):
        if v is None:
            return []
        if not isinstance(v, (list, tuple)):
            v = [v]
        return [_text(x) for x in v if x not in (None, "")]

    checks = []
    raw_checks = a.get("checks")
    for ch in (raw_checks if isinstance(raw_checks, (list, tuple)) else []):
        if isinstance(ch, dict):
            checks.append({"name": _text(ch.get("name")), "passed": bool(ch.get("passed")),
                           "detail": _text(ch.get("detail"))})
        elif ch not in (None, ""):
            checks.append({"name": _text(ch), "passed": False, "detail": ""})
    passed = a.get("passed")
    return {
        **r,
        "combination": _text(r.get("combination")) or "?",
        "scenario_type": _text(r.get("scenario_type") or r.get("scenario_key")) or "?",
        "test_time": _text(r.get("test_time")),
        "analysis": {
            "passed": passed if passed is None or isinstance(passed, bool) else bool(passed),
            "confidence": _text(a.get("confidence")) or "LOW",
            "summary": _text(a.get("summary")),
            "checks": checks,
            "issues": str_list(a.get("issues")),
            "recommendations": str_list(a.get("recommendations")),
        },
    }


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value) -> str:
    """CSV/formül enjeksiyonu: = + - @ ile başlayan hücre Excel/Sheets'te formül
    olarak çalışır (konu/özet mail içeriğinden ve LLM'den gelir). Başına ' eklenir."""
    text = _text(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _status_of(result: dict) -> str:
    """PASS / FAIL / SKIP — analysis.passed None ise senaryo atlanmıştır."""
    analysis = result.get("analysis") if isinstance(result, dict) else None
    passed = analysis.get("passed") if isinstance(analysis, dict) else False
    if passed is None:
        return "SKIP"
    return "PASS" if passed else "FAIL"


_MANUAL_BADGES = {
    "pass": ("pass", "👁 GEÇTİ"),
    "fail": ("fail", "👁 KALDI"),
    "na": ("skip", "👁 UYGULANAMAZ"),
}


def _manual_entry(manual, result) -> dict:
    """Sonucun manuel doğrulama kaydı (yoksa boş sözlük)."""
    if not isinstance(manual, dict) or not isinstance(result, dict):
        return {}
    rid = str(result.get("result_id") or "")
    if not rid:
        return {}
    entry = manual.get(rid)
    if isinstance(entry, dict):
        return entry
    # Plan kipi: istemci başına ayrı kayıt ("<rid>:<istemci>") → tek hücrede birleştir.
    parts = [e for k, e in manual.items()
             if str(k).startswith(rid + ":") and isinstance(e, dict)]
    if not parts:
        return {}
    overalls = [e.get("overall") for e in parts]
    steps, notes = [], []
    for e in parts:
        name = _text(e.get("client_name") or e.get("client") or "?")
        label = {"pass": "GEÇTİ", "fail": "KALDI", "na": "UYGULANAMAZ"}.get(e.get("overall"), "?")
        notes.append(f"{name}: {label}" + (f" ({_text(e.get('note'))})" if e.get("note") else ""))
        for s in (e.get("steps") if isinstance(e.get("steps"), list) else []):
            if isinstance(s, dict):
                steps.append({**s, "text": f"{name} · {_text(s.get('text'))}"})
    return {"overall": "fail" if "fail" in overalls else "pass" if "pass" in overalls else "na",
            "note": "; ".join(notes), "steps": steps}


def _manual_note(entry: dict) -> str:
    parts = [_text(entry.get("note"))]
    steps = entry.get("steps") if isinstance(entry.get("steps"), list) else []
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        if step.get("note") or step.get("status") == "fail":
            label = {"pass": "GEÇTİ", "fail": "KALDI", "na": "UYGULANAMAZ"}.get(
                step.get("status"), "?")
            parts.append(f"[{i}. {label}] {_text(step.get('text'))}"
                         + (f" — {_text(step.get('note'))}" if step.get("note") else ""))
    return " | ".join(p for p in parts if p)


def _agreement_label(status: str, entry: dict) -> str:
    """Otomatik ve manuel karar uyumu — aracın kendi doğrulaması."""
    overall = entry.get("overall")
    if overall not in ("pass", "fail") or status == "SKIP":
        return ""
    if (overall == "pass") == (status == "PASS"):
        return "UYUMLU"
    return "ARAÇ KAÇIRDI" if status == "PASS" else "YANLIŞ ALARM"


def generate_html_report(results: List[dict], output_path: str, note: str = "",
                         manual: dict = None):
    """Test sonuçlarından profesyonel HTML rapor üretir.

    ``note`` doluysa (ör. koşu durduruldu) raporun başında uyarı olarak gösterilir.
    ``manual``: result_id → manuel doğrulama kaydı (manual_verify.py).
    """
    note_html = (
        '<div style="margin:16px 0;padding:12px 16px;border-radius:8px;background:#fff4e5;'
        'border:1px solid #f0a500;color:#7a4b00;font-weight:600">⚠️ ' + _esc(note) + '</div>'
    ) if note else ""
    results = [_normalize(r) for r in results or []]
    total = len(results)
    passed = sum(1 for r in results if _status_of(r) == "PASS")
    skipped = sum(1 for r in results if _status_of(r) == "SKIP")
    failed = total - passed - skipped
    effective = total - skipped
    pass_rate = round((passed / effective * 100) if effective > 0 else 0, 1)

    # Kombinasyon bazlı grupla
    by_combo = {}
    for r in results:
        key = r["combination"]
        by_combo.setdefault(key, []).append(r)

    combo_rows = ""
    for combo, combo_results in by_combo.items():
        c_passed = sum(1 for r in combo_results if _status_of(r) == "PASS")
        c_skipped = sum(1 for r in combo_results if _status_of(r) == "SKIP")
        c_effective = len(combo_results) - c_skipped
        c_rate = round((c_passed / c_effective * 100) if c_effective > 0 else 0, 1)
        if c_effective == 0:
            status_class = "partial"
        else:
            status_class = "pass" if c_rate == 100 else ("partial" if c_rate > 0 else "fail")

        scenario_rows = ""
        for r in combo_results:
            analysis = r.get("analysis", {})
            status = _status_of(r)
            sc_class = {"PASS": "pass", "FAIL": "fail", "SKIP": "skip"}[status]
            badge_label = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "SKIP": "⏭ ATLANDI"}[status]
            checks_html = ""
            checks = analysis.get("checks", [])
            for ch in checks if isinstance(checks, list) else []:
                if not isinstance(ch, dict):
                    ch = {"name": str(ch), "passed": False, "detail": ""}
                ch_icon = "✅" if ch.get("passed") else "❌"
                checks_html += (
                    f'<li>{ch_icon} <b>{_esc(ch.get("name", ""))}</b>: '
                    f'{_esc(ch.get("detail", ""))}</li>'
                )
            issues = analysis.get("issues", [])
            if not isinstance(issues, list):
                issues = [issues] if issues else []
            issues_html = "".join(f"<li>⚠️ {_esc(i)}</li>" for i in issues) if issues else ""

            entry = _manual_entry(manual, r)
            if entry.get("overall") in _MANUAL_BADGES:
                m_class, m_label = _MANUAL_BADGES[entry["overall"]]
                agree = _agreement_label(status, entry)
                m_note = _manual_note(entry)
                manual_html = (
                    f'<span class="badge {m_class}">{m_label}</span>'
                    + (f'<div class="agree agree-{"ok" if agree == "UYUMLU" else "bad"}">'
                       f'{_esc(agree)}</div>' if agree else "")
                    + (f'<div class="manual-note">{_esc(m_note)}</div>' if m_note else ""))
            else:
                manual_html = '<span class="manual-pending">—</span>'

            scenario_rows += f"""
            <tr class="{sc_class}-row">
              <td><span class="badge {sc_class}">{badge_label}</span></td>
              <td>{_esc(r['scenario_type'])}</td>
              <td class="summary-cell">{_esc(analysis.get('summary', ''))}</td>
              <td>
                <details>
                  <summary>Kontroller ({len(analysis.get('checks', []))})</summary>
                  <ul class="checks-list">{checks_html}</ul>
                  {f'<ul class="issues-list">{issues_html}</ul>' if issues_html else ''}
                </details>
              </td>
              <td><span class="confidence confidence-{_esc(analysis.get('confidence', 'LOW')).lower()}">{_esc(analysis.get('confidence', '?'))}</span></td>
              <td class="manual-cell">{manual_html}</td>
            </tr>"""

        combo_rows += f"""
        <div class="combo-card {status_class}-card">
          <div class="combo-header">
            <h3>🔀 {_esc(combo)}</h3>
            <div class="combo-stats">
              <span class="stat-badge">{c_passed}/{c_effective} PASS</span>
              <span class="rate-badge {status_class}">{c_rate}%</span>
            </div>
          </div>
          <table class="results-table">
            <thead>
              <tr>
                <th>Sonuç</th><th>Senaryo</th><th>Özet</th><th>Kontrol Detayı</th><th>Güven</th><th>Manuel Doğrulama</th>
              </tr>
            </thead>
            <tbody>{scenario_rows}</tbody>
          </table>
        </div>"""

    m_entries = [(r, _manual_entry(manual, r)) for r in results]
    m_entries = [(r, e) for r, e in m_entries if e.get("overall") in _MANUAL_BADGES]
    m_labels = [_agreement_label(_status_of(r), e) for r, e in m_entries]
    m_missed = m_labels.count("ARAÇ KAÇIRDI")
    m_alarm = m_labels.count("YANLIŞ ALARM")
    manual_cards = (
        f'<div class="stat-card manual"><div class="val" style="color:#6366f1">'
        f'{len(m_entries)}/{total}</div><div class="lbl">👁 Manuel Doğrulanan</div></div>'
        f'<div class="stat-card manual"><div class="val" style="color:'
        f'{"#ef4444" if m_missed else "#10b981"}">{m_missed}</div>'
        f'<div class="lbl">Aracın Kaçırdığı (oto. PASS / manuel KALDI)</div></div>'
        f'<div class="stat-card manual"><div class="val" style="color:#f59e0b">{m_alarm}</div>'
        f'<div class="lbl">Yanlış Alarm (oto. FAIL / manuel GEÇTİ)</div></div>'
    ) if m_entries else ""

    html_doc = f"""<!DOCTYPE html>
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
  .stat-card.skip  {{ border-left-color: #94a3b8; }}
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
  .skip-row {{ background: #f8fafc; }}
  .badge {{ padding: 3px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; }}
  .badge.pass {{ background: #d1fae5; color: #065f46; }}
  .badge.fail {{ background: #fee2e2; color: #991b1b; }}
  .badge.skip {{ background: #e2e8f0; color: #475569; }}
  .summary-cell {{ max-width: 240px; }}
  details summary {{ cursor: pointer; color: #6366f1; font-size: 13px; font-weight: 500; }}
  .checks-list, .issues-list {{ margin-top: 8px; padding-left: 16px; font-size: 13px;
                                 color: #374151; line-height: 1.8; }}
  .issues-list {{ color: #b45309; }}
  .confidence {{ padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }}
  .confidence-high {{ background: #d1fae5; color: #065f46; }}
  .confidence-medium {{ background: #fef3c7; color: #92400e; }}
  .confidence-low {{ background: #fee2e2; color: #991b1b; }}
  .confidence-n\\/a {{ background: #e2e8f0; color: #475569; }}
  .stat-card.manual {{ border-left-color: #6366f1; }}
  .manual-cell {{ max-width: 260px; font-size: 12px; }}
  .manual-note {{ margin-top: 6px; color: #475569; line-height: 1.5; }}
  .manual-pending {{ color: #94a3b8; }}
  .agree {{ margin-top: 6px; font-size: 11px; font-weight: 700; }}
  .agree-ok {{ color: #065f46; }}
  .agree-bad {{ color: #991b1b; }}
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
{note_html}

<div class="summary-bar">
  <div class="stat-card total"><div class="val">{total}</div><div class="lbl">Toplam Senaryo</div></div>
  <div class="stat-card pass"><div class="val" style="color:#10b981">{passed}</div><div class="lbl">✅ Başarılı</div></div>
  <div class="stat-card fail"><div class="val" style="color:#ef4444">{failed}</div><div class="lbl">❌ Başarısız</div></div>
  <div class="stat-card skip"><div class="val" style="color:#94a3b8">{skipped}</div><div class="lbl">⏭ Atlanan</div></div>
  <div class="stat-card rate"><div class="val" style="color:#f59e0b">{pass_rate}%</div><div class="lbl">Başarı Oranı</div></div>
  <div class="stat-card"><div class="val" style="font-size:20px">{len(by_combo)}</div><div class="lbl">Kombinasyon</div></div>
  {manual_cards}
</div>

<div class="content">
{combo_rows}
</div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(html_doc)
    logger.info(f"HTML rapor oluşturuldu: {output_path}")


def generate_csv_results(results: List[dict], output_path: str, manual: dict = None):
    """Test sonuçlarını CSV olarak kaydeder (Sheets'e aktarım için).

    Son dört sütun manuel doğrulama içindir; kayıt yoksa boş kalır."""
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Kombinasyon", "Senaryo Tipi", "Sonuç", "Güven Seviyesi",
            "Özet", "Sorunlar", "Öneriler", "Test Zamanı",
            "Run ID", "Manuel Sonuç", "Manuel Not", "Oto/Manuel Uyumu",
        ])
        for r in (_normalize(x) for x in results or []):
            analysis = r["analysis"]
            entry = _manual_entry(manual, r)
            manual_label = {"pass": "GEÇTİ", "fail": "KALDI", "na": "UYGULANAMAZ"}.get(
                entry.get("overall"), "")
            writer.writerow([_csv_cell(v) for v in (
                r["combination"],
                r["scenario_type"],
                _status_of(r),
                analysis["confidence"],
                analysis["summary"],
                " | ".join(analysis["issues"]),
                " | ".join(analysis["recommendations"]),
                r["test_time"],
                r.get("result_id") or "",
                manual_label,
                _manual_note(entry) if manual_label else "",
                _agreement_label(_status_of(r), entry),
            )])
    logger.info(f"CSV sonuçları oluşturuldu: {output_path}")

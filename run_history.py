"""
run_history.py — Koşu geçmişi, kanıt paketi ve sürümler arası fark.

Her koşu ``reports/runs/<kimlik>/`` altında kalıcıdır:

  manifest.json    araç sürümü, etiketler (ör. "EMS 9.5.0"), plan/kademe/etki,
                   sunucu karşılama bilgileri, özet sayılar, kanıt özetleri
  results.json     tüm sonuçlar (kontroller dahil)
  report.html / results.csv
  evidence/        gönderilen ve her kanaldan okunan ham mesajlar (.eml)

Bir koşu **referans (baseline)** işaretlenir; sonraki koşular onunla
karşılaştırılır: yeni hatalar, düzelenler, değişen kontroller. Sürüm
güncellemesinde "güncellemeden önce de böyleydi / değildi" sorusunun kanıtı
budur.

Karşılaştırma yalnızca deterministik kontrollere ([Kesin]/[Bilgi]) bakar;
LLM kontrollerinin adları koşudan koşuya değiştiği için onlarda yalnızca
senaryonun genel sonucu karşılaştırılır.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

TOOL_VERSION = "0.9.0"
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
EVIDENCE_NAME_RE = re.compile(r"^[0-9a-f]{8}\.[a-z0-9]+\.eml$")
DEFAULT_EVIDENCE_MAX_MB = 2.0
_DET_PREFIXES = ("[Kesin]", "[Bilgi]")


def runs_dir(reports_dir) -> Path:
    return Path(reports_dir) / "runs"


def new_run_id() -> str:
    """Zaman sıralı kimlik: son ekin ilk üç hanesi milisaniyedir, böylece aynı
    saniyede başlayan koşular da adlarına göre doğru sıralanır."""
    now = datetime.now()
    return (now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03x}"
            + uuid.uuid4().hex[:3])


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", errors="replace") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
    tmp.replace(path)


def _read_json(path: Path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return fallback
    return data if isinstance(data, type(fallback)) else fallback


def clean_labels(labels) -> list:
    """Sürüm etiketleri: kısa, tek satır, tekrarsız."""
    out = []
    for label in labels or []:
        text = " ".join(str(label).split())[:60]
        if text and text not in out:
            out.append(text)
    return out[:10]


# ────────────────────────────────────────────────────────────────────
#  Kanıt
# ────────────────────────────────────────────────────────────────────
def save_evidence(run_dir, result_id: str, messages: dict,
                  max_mb: float = DEFAULT_EVIDENCE_MAX_MB) -> dict:
    """Ham mesajları kanıt olarak yazar.

    ``messages``: kanal → bayt ("sent", "imap", "pop3", "activesync").
    Sınırı aşan mesaj yazılmaz ama SHA-256 özeti yine kaydedilir — içerik
    saklanmasa da "aynı mesaj mıydı" sorusu yanıtlanabilir.
    """
    out: dict = {}
    limit = int(max(0.0, float(max_mb)) * 1024 * 1024)
    for channel, raw in (messages or {}).items():
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            continue
        entry = {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
        if len(raw) <= limit:
            name = f"{result_id}.{channel}.eml"
            path = Path(run_dir) / "evidence" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(raw))
            entry["file"] = name
        out[channel] = entry
    return out


# ────────────────────────────────────────────────────────────────────
#  Koşu kaydı
# ────────────────────────────────────────────────────────────────────
def status_of(result: dict) -> str:
    analysis = result.get("analysis") if isinstance(result, dict) else None
    passed = analysis.get("passed") if isinstance(analysis, dict) else False
    if passed is None:
        return "SKIP"
    return "PASS" if passed else "FAIL"


def totals(results: list) -> dict:
    statuses = [status_of(r) for r in results or []]
    return {"total": len(statuses), "passed": statuses.count("PASS"),
            "failed": statuses.count("FAIL"), "skipped": statuses.count("SKIP")}


def finalize_run(run_dir, results: list, run_info: dict, files: dict) -> dict:
    """Koşuyu kalıcılaştırır: sonuçlar + manifest + rapor kopyaları.

    ``files``: {"report.html": kaynak yol, "results.csv": kaynak yol}
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    slim = []
    for r in results or []:
        if isinstance(r, dict):
            item = dict(r)
            meta = item.get("send_meta")
            if isinstance(meta, dict):
                item["send_meta"] = {k: v for k, v in meta.items()
                                     if k not in ("raw_bytes", "expect", "check_prompt")}
            slim.append(item)
    _write_json(run_dir / "results.json", {"run": run_info, "results": slim})
    for name, source in (files or {}).items():
        try:
            if source and Path(source).is_file():
                shutil.copyfile(source, run_dir / name)
        except OSError:
            pass
    manifest = {
        "run_id": run_dir.name,
        "tool_version": TOOL_VERSION,
        **{k: v for k, v in (run_info or {}).items() if k != "clients"},
        "clients": (run_info or {}).get("clients") or [],
        "totals": totals(slim),
        "evidence_files": sum(1 for r in slim for e in (r.get("evidence") or {}).values()
                              if isinstance(e, dict) and e.get("file")),
    }
    _write_json(run_dir / "manifest.json", manifest)
    return manifest


def list_runs(reports_dir) -> list:
    """Koşular, yeniden eskiye (manifest özetleri)."""
    base = runs_dir(reports_dir)
    baseline = get_baseline(reports_dir)
    out = []
    if not base.is_dir():
        return out
    for path in sorted(base.iterdir(), reverse=True):
        if not (path.is_dir() and RUN_ID_RE.match(path.name)):
            continue
        manifest = _read_json(path / "manifest.json", {})
        if not manifest:
            continue
        out.append({"run_id": path.name, "started_at": manifest.get("started_at", ""),
                    "finished_at": manifest.get("finished_at", ""),
                    "labels": manifest.get("labels") or [],
                    "plan": manifest.get("plan", ""), "tier": manifest.get("tier", ""),
                    "impact": manifest.get("impact", ""),
                    "note": manifest.get("note", ""),
                    "totals": manifest.get("totals") or {},
                    "evidence_files": manifest.get("evidence_files", 0),
                    "tool_version": manifest.get("tool_version", ""),
                    "baseline": path.name == baseline})
    return out


def load_run(reports_dir, run_id: str) -> Optional[dict]:
    if not RUN_ID_RE.match(str(run_id)):
        return None
    path = runs_dir(reports_dir) / run_id
    data = _read_json(path / "results.json", {})
    if not data:
        return None
    return {"run_id": run_id, "manifest": _read_json(path / "manifest.json", {}),
            "results": [r for r in data.get("results") or [] if isinstance(r, dict)]}


def get_baseline(reports_dir) -> str:
    try:
        value = (runs_dir(reports_dir) / "baseline.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return value if RUN_ID_RE.match(value) else ""


def set_baseline(reports_dir, run_id: str) -> bool:
    if load_run(reports_dir, run_id) is None:
        return False
    path = runs_dir(reports_dir) / "baseline.txt"
    path.write_text(run_id, encoding="utf-8")
    return True


# ────────────────────────────────────────────────────────────────────
#  Fark
# ────────────────────────────────────────────────────────────────────
def _key(result: dict) -> tuple:
    return (str(result.get("combination", "")), str(result.get("scenario_key", "")))


def _det_checks(result: dict) -> dict:
    checks = (result.get("analysis") or {}).get("checks") or []
    return {c["name"]: c for c in checks
            if isinstance(c, dict) and str(c.get("name", "")).startswith(_DET_PREFIXES)}


def _check_changes(old: dict, new: dict) -> list:
    a, b = _det_checks(old), _det_checks(new)
    changes = []
    for name in list(a) + [n for n in b if n not in a]:
        before, after = a.get(name), b.get(name)
        # Bazı kontroller yalnızca başarısızken raporlanır (ör. ikincil kanaldaki
        # beklenti hatası): başarısız olarak belirmesi "bozuldu", başarısızken
        # kaybolması "düzeldi" demektir.
        if before is None:
            changes.append({"check": name,
                            "kind": "yeni kontrol" if after.get("passed") else "bozuldu",
                            "before": "", "after": after.get("detail", "")})
        elif after is None:
            changes.append({"check": name,
                            "kind": "kontrol kayboldu" if before.get("passed") else "düzeldi",
                            "before": before.get("detail", ""), "after": ""})
        elif bool(before.get("passed")) != bool(after.get("passed")):
            changes.append({"check": name,
                            "kind": "bozuldu" if before.get("passed") else "düzeldi",
                            "before": before.get("detail", ""), "after": after.get("detail", "")})
        elif name.startswith("[Bilgi]") and before.get("detail") != after.get("detail"):
            # Karar değişmedi ama sunucunun davranışı değişti (ör. artık 8bit'i
            # quoted-printable'a çeviriyor) — sürüm notunda görülmesi gereken şey.
            changes.append({"check": name, "kind": "davranış değişti",
                            "before": before.get("detail", ""), "after": after.get("detail", "")})
    return changes


def diff_runs(base: dict, new: dict) -> dict:
    """İki koşuyu (load_run çıktıları) karşılaştırır."""
    a = {_key(r): r for r in base.get("results") or []}
    b = {_key(r): r for r in new.get("results") or []}
    rows = []
    for key in list(b) + [k for k in a if k not in b]:
        old, cur = a.get(key), b.get(key)
        row = {"combination": key[0], "scenario_key": key[1],
               "before": status_of(old) if old else "—",
               "after": status_of(cur) if cur else "—",
               "result_id": (cur or {}).get("result_id", ""),
               "summary": ((cur or old).get("analysis") or {}).get("summary", ""),
               "changes": _check_changes(old, cur) if old and cur else []}
        if old is None:
            row["kind"] = "new"
        elif cur is None:
            row["kind"] = "missing"
        elif row["before"] == row["after"]:
            row["kind"] = "changed" if row["changes"] else "same"
        elif row["after"] == "FAIL":
            row["kind"] = "regression"
        elif row["after"] == "PASS":
            row["kind"] = "fixed"
        else:
            row["kind"] = "changed"
        rows.append(row)
    order = {"regression": 0, "changed": 1, "fixed": 2, "new": 3, "missing": 4, "same": 5}
    rows.sort(key=lambda r: (order[r["kind"]], r["combination"], r["scenario_key"]))
    counts = {k: sum(1 for r in rows if r["kind"] == k) for k in order}
    return {"base": base.get("run_id"), "new": new.get("run_id"),
            "base_labels": (base.get("manifest") or {}).get("labels") or [],
            "new_labels": (new.get("manifest") or {}).get("labels") or [],
            "counts": counts, "rows": rows,
            "identical": counts["same"] == len(rows)}


def diff_summary(diff: dict) -> str:
    c = diff["counts"]
    if diff["identical"]:
        return f"Referans koşuyla ({diff['base']}) fark yok — {c['same']} senaryo aynı."
    return (f"Referans koşuya ({diff['base']}) göre: {c['regression']} YENİ HATA, "
            f"{c['fixed']} düzelen, {c['changed']} kontrol/davranış değişikliği, "
            f"{c['new']} yeni, {c['missing']} eksik, {c['same']} aynı.")

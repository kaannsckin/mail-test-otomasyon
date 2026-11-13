"""
manual_verify.py — Manuel (insan gözüyle) doğrulama kayıtları.

Otomasyon mesajı SMTP ile gönderip IMAP ile geri okur; alıcı **istemcinin**
(iOS Mail, Outlook, ...) mesajı ekranda nasıl gösterdiğini göremez. Bu modül
o boşluğu kapatır: her senaryo için istemcide bakılacak adımlar kullanıcıya
tek tek sorulur, her adımın geçti/kaldı durumu ve notu saklanır.

İkinci amaç otomasyonun kendisini doğrulamaktır: otomatik karar ile insan
kararı karşılaştırılır (``agreement``). "Otomatik PASS, manuel KALDI" sayısı
aracın kaçırdığı hatalardır — bu sayı sıfıra yaklaştıkça manuel adım
seyreltilebilir.

Kayıtlar ``reports/manual_verification.json`` içinde, sonucun ``result_id``
değeriyle (konu satırındaki 8 haneli Run ID) anahtarlanır.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

STATUSES = ("pass", "fail", "na")
STATUS_LABELS = {"pass": "GEÇTİ", "fail": "KALDI", "na": "UYGULANAMAZ"}
MAX_NOTE = 2000

_lock = threading.Lock()


# ────────────────────────────────────────────────────────────────────
#  Dosya yolları ve G/Ç
# ────────────────────────────────────────────────────────────────────
def results_json_path(results_csv: str) -> Path:
    """Makine tarafından okunan tam sonuç dosyası (CSV'nin yanında)."""
    return Path(str(results_csv)).with_suffix(".json")


def store_path(results_csv: str) -> Path:
    return Path(str(results_csv)).parent / "manual_verification.json"


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", errors="replace") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, path)


def _load_json(path: Path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return fallback
    return data if isinstance(data, type(fallback)) else fallback


def write_results_json(results: list, path, run_info: Optional[dict] = None) -> None:
    """Tüm sonuçları JSON olarak yazar (her senaryodan sonra çağrılır — koşu
    sürerken Manuel Doğrulama sihirbazı biten maddeleri gösterebilsin)."""
    slim = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        item = dict(r)
        meta = item.get("send_meta")
        if isinstance(meta, dict):
            item["send_meta"] = {k: v for k, v in meta.items()
                                 if k not in ("raw_bytes", "expect", "check_prompt")}
        slim.append(item)
    _atomic_write(Path(path), {"run": run_info or {}, "results": slim})


def load_results_json(path) -> dict:
    data = _load_json(Path(path), {})
    results = data.get("results")
    return {"run": data.get("run") if isinstance(data.get("run"), dict) else {},
            "results": [r for r in results if isinstance(r, dict)]
            if isinstance(results, list) else []}


def load_store(path) -> dict:
    return _load_json(Path(path), {})


def save_entry(path, result_id: str, entry: dict) -> dict:
    """Bir sonucun manuel doğrulama kaydını yazar; güncel depoyu döndürür."""
    with _lock:
        store = load_store(path)
        store[str(result_id)] = entry
        _atomic_write(Path(path), store)
        return store


# ────────────────────────────────────────────────────────────────────
#  Kayıt kurma ve değerlendirme
# ────────────────────────────────────────────────────────────────────
def auto_status(result: dict) -> str:
    analysis = result.get("analysis") if isinstance(result, dict) else None
    passed = analysis.get("passed") if isinstance(analysis, dict) else False
    if passed is None:
        return "SKIP"
    return "PASS" if passed else "FAIL"


def is_eligible(result: dict) -> bool:
    """Manuel doğrulanabilir mi? Mesaj gönderilmiş olmalı (istemcide bakılacak
    bir şey var); atlanan ve hiç gönderilemeyen senaryolar hariç."""
    return (isinstance(result, dict) and bool(result.get("result_id"))
            and not result.get("skipped") and isinstance(result.get("send_meta"), dict)
            and result.get("manual_scope") != "none")


def slots(result: dict) -> list:
    """Bir sonucun manuel doğrulama yuvaları: (kayıt kimliği, istemci | None).

    Plan kipinde aynı mesaja her istemcide ayrı bakılır → istemci başına bir
    yuva (``<result_id>:<istemci>``). CSV kipinde tek yuva (``<result_id>``).
    """
    rid = str(result.get("result_id") or "")
    clients = result.get("clients")
    if isinstance(clients, list) and clients:
        return [(f"{rid}:{c.get('key')}", c) for c in clients if isinstance(c, dict)]
    return [(rid, None)]


def for_results(store: dict, results: list) -> dict:
    """Depodan yalnızca verilen sonuçlara ait kayıtları süzer."""
    ids = {str(r.get("result_id")) for r in results or [] if isinstance(r, dict)}
    return {k: v for k, v in (store or {}).items() if str(k).split(":", 1)[0] in ids}


def overall_of(steps: list) -> str:
    statuses = [s.get("status") for s in steps]
    if "fail" in statuses:
        return "fail"
    if "pass" in statuses:
        return "pass"
    return "na"


def build_entry(result: dict, step_inputs: list, note: str = "",
                client: Optional[dict] = None) -> dict:
    """Kullanıcı girdisinden kayıt üretir. Her adım için durum ZORUNLUDUR.

    Raises: ValueError — adım sayısı uyuşmuyor ya da geçersiz durum.
    """
    texts = [str(t) for t in (result.get("manual_steps") or [])]
    if len(step_inputs) != len(texts):
        raise ValueError(f"{len(texts)} adım bekleniyor, {len(step_inputs)} geldi")
    steps = []
    for text, given in zip(texts, step_inputs):
        status = str((given or {}).get("status") or "").lower()
        if status not in STATUSES:
            raise ValueError(f"Her adım için durum seçilmeli (geçti / kaldı / uygulanamaz): {text}")
        steps.append({"text": text, "status": status,
                      "note": str((given or {}).get("note") or "").strip()[:MAX_NOTE]})
    return {
        "steps": steps,
        "overall": overall_of(steps),
        "note": str(note or "").strip()[:MAX_NOTE],
        "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "auto_status": auto_status(result),
        "combination": result.get("combination", ""),
        "scenario_key": result.get("scenario_key", ""),
        **({"client": client.get("key"), "client_name": client.get("name")} if client else {}),
    }


def entry_note(entry: dict) -> str:
    """Rapor hücresi için: genel not + adım notları tek satırda."""
    parts = [entry.get("note") or ""]
    for i, step in enumerate(entry.get("steps") or [], start=1):
        if step.get("note") or step.get("status") == "fail":
            parts.append(f"[{i}. {STATUS_LABELS.get(step.get('status'), '?')}] "
                         f"{step.get('text', '')}"
                         + (f" — {step['note']}" if step.get("note") else ""))
    return " | ".join(p for p in parts if p)


def agreement(results: list, store: dict) -> dict:
    """Otomatik karar ile manuel kararın uyumu (aracın kendi doğrulaması)."""
    stats = {"eligible": 0, "verified": 0, "pending": 0, "agree": 0,
             "false_pass": 0, "false_alarm": 0, "manual_pass": 0, "manual_fail": 0}
    for r in results or []:
        if not is_eligible(r):
            continue
        for slot_id, _client in slots(r):
            stats["eligible"] += 1
            entry = store.get(slot_id)
            if not isinstance(entry, dict) or entry.get("overall") not in STATUSES:
                stats["pending"] += 1
                continue
            stats["verified"] += 1
            manual, auto = entry["overall"], auto_status(r)
            if manual == "na":
                continue
            stats["manual_pass" if manual == "pass" else "manual_fail"] += 1
            if (manual == "pass") == (auto == "PASS"):
                stats["agree"] += 1
            elif auto == "PASS":
                stats["false_pass"] += 1      # araç geçti dedi, insan kaldı dedi
            else:
                stats["false_alarm"] += 1     # araç kaldı dedi, insan geçti dedi
    compared = stats["agree"] + stats["false_pass"] + stats["false_alarm"]
    stats["agreement_rate"] = round(stats["agree"] / compared * 100, 1) if compared else None
    return stats


# ────────────────────────────────────────────────────────────────────
#  CLI: her senaryodan sonra sor (python main.py --manual)
# ────────────────────────────────────────────────────────────────────
_KEYS = {"g": "pass", "k": "fail", "u": "na"}


def prompt_cli(result: dict, input_fn: Optional[Callable[[str], str]] = None,
               out: Callable[[str], None] = print,
               client: Optional[dict] = None) -> Optional[dict]:
    """Bir senaryonun manuel adımlarını terminalde tek tek sorar.

    Dönen değer kayıt sözlüğüdür; kullanıcı maddeyi atlarsa (a) None.
    EOFError çağırana bırakılır (etkileşimsiz ortamda sormayı kesmek için).
    """
    input_fn = input_fn or input
    steps = [str(t) for t in (result.get("manual_steps") or [])]
    if not steps:
        return None
    out("")
    out("─" * 72)
    out(f"MANUEL DOĞRULAMA · {result.get('combination', '?')} · "
        f"{result.get('scenario_type') or result.get('scenario_key')}")
    where = (f"{client.get('name')} ({client.get('access')})" if client
             else (result.get("receiver_client") or "Alıcı istemci"))
    out(f"  İstemci: {where} — konuda arayın: #{result.get('result_id')}")
    if result.get("subject"):
        out(f"  Konu: {result['subject']}")
    summary = (result.get("analysis") or {}).get("summary", "")
    out(f"  Otomatik sonuç: {auto_status(result)} — {summary}")
    out("  Her adım için: (g)eçti · (k)aldı · (u)ygulanamaz · (a) bu maddeyi atla")

    inputs = []
    for i, text in enumerate(steps, start=1):
        out(f"\n  [{i}/{len(steps)}] {text}")
        while True:
            answer = input_fn("      Durum [g/k/u/a]: ").strip().lower()[:1]
            if answer == "a":
                out("  ⏭ Madde atlandı (daha sonra arayüzden doğrulanabilir).")
                return None
            if answer in _KEYS:
                break
            out("      Lütfen g, k, u veya a girin.")
        note = input_fn("      Not (boş bırakılabilir): ").strip()
        inputs.append({"status": _KEYS[answer], "note": note})
    overall_note = input_fn("\n  Madde için genel not (boş bırakılabilir): ").strip()
    entry = build_entry(result, inputs, overall_note, client=client)
    out(f"  ✔ Kaydedildi: manuel sonuç {STATUS_LABELS[entry['overall']]}")
    return entry

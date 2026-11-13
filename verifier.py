"""
verifier.py — Alınan ham mesajı gönderim beklentisiyle DETERMİNİSTİK karşılaştırır.

LLM analizi yorum üretir; aynı mesaj için iki koşuda farklı karar verebilir.
Regresyon testinde "sürüm güncellemesinden sonra hiçbir şey değişmedi"
iddiasını kanıtlamak için karar tekrarlanabilir olmalıdır. Bu modül yalnızca
bayt/metin eşitliğine bakar: gövde metni, ek SHA-256 özeti, başlık değerleri.

Kontrol adları iki önek taşır:
  [Kesin]  başarısızsa senaryo FAIL olur
  [Bilgi]  karar değiştirmez; sunucunun yaptığı meşru dönüşümleri kaydeder
           (ör. 8bit → quoted-printable). Sürümler arası fark izlemek içindir.
"""
from __future__ import annotations

import email
import hashlib
import html as html_lib
import re
from email.utils import getaddresses
from typing import Optional

from receiver import MailReceiver

_decode = MailReceiver._decode_header
_unfold = MailReceiver._unfold

HARD = "[Kesin] "
INFO = "[Bilgi] "

# LLM'e ulaşılamadığında analyzer'ın ürettiği kontrol adları (karar değil, arıza).
_LLM_FAILURE_CHECKS = {"LLM Analizi", "Parse Hatası"}


def _check(name: str, passed: bool, detail: str, hard: bool = True) -> dict:
    return {"name": (HARD if hard else INFO) + name, "passed": bool(passed), "detail": detail}


def _norm_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def _header(msg, name: str) -> str:
    return _unfold(_decode(msg.get(name, "")))


def _disp_type(part) -> str:
    return _unfold(part.get("Content-Disposition", "")).split(";", 1)[0].strip().lower()


def _part_text(part) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _first_text(msg, subtype: str):
    """Ek olmayan ilk text/<subtype> parçası (iletilmiş mesajın içi hariç)."""
    for part in _walk_top(msg):
        if part.get_content_type() == f"text/{subtype}" and _disp_type(part) != "attachment":
            return part
    return None


def _walk_top(msg):
    """msg.walk() gibi ama message/rfc822 kapsülünün içine girmez."""
    yield msg
    if msg.is_multipart() and msg.get_content_type() != "message/rfc822":
        for sub in msg.get_payload():
            yield from _walk_top(sub)


def _first_diff(expected: str, got: str) -> str:
    limit = min(len(expected), len(got))
    pos = next((i for i in range(limit) if expected[i] != got[i]), limit)
    return (f"ilk fark {pos}. karakterde — beklenen {expected[pos:pos + 30]!r}, "
            f"alınan {got[pos:pos + 30]!r}")


# ────────────────────────────────────────────────────────────────────
#  Tekil kontroller
# ────────────────────────────────────────────────────────────────────
def _check_subject(msg, expected: str) -> dict:
    got = _header(msg, "Subject")
    want = " ".join(expected.split())
    if got == want:
        return _check("Konu korunumu", True, f"Konu birebir aynı ({len(want)} karakter)")
    return _check("Konu korunumu", False, f"Konu değişmiş: {_first_diff(want, got)}")


def _check_body(msg, expected: str) -> dict:
    part = _first_text(msg, "plain")
    if part is None:
        return _check("Gövde metni", False, "text/plain parçası bulunamadı")
    got, want = _norm_text(_part_text(part)), _norm_text(expected)
    if got == want:
        return _check("Gövde metni", True,
                      f"Çözülen metin birebir aynı ({len(want)} karakter)")
    if want in got:
        return _check("Gövde metni", True,
                      f"Metin korunmuş; sunucu {len(got) - len(want)} karakter ek içerik "
                      "eklemiş (ör. yasal uyarı / imza)")
    return _check("Gövde metni", False, f"Metin değişmiş: {_first_diff(want, got)}")


def _check_declared(msg, declared: dict) -> dict:
    part = _first_text(msg, "plain")
    if part is None:
        return _check("Kodlama beyanı", True, "text/plain parçası yok", hard=False)
    got_cs = (part.get_content_charset() or "belirtilmemiş").lower()
    got_cte = (_unfold(part.get("Content-Transfer-Encoding", "")) or "belirtilmemiş").lower()
    sent = f"charset={declared.get('charset')}, CTE={declared.get('cte')}"
    got = f"charset={got_cs}, CTE={got_cte}"
    same = got_cs == str(declared.get("charset")).lower() and got_cte == declared.get("cte")
    note = "değişmeden iletildi" if same else "sunucu yeniden kodlamış (içerik korunduysa sorun değil)"
    return _check("Kodlama beyanı", True, f"gönderilen {sent} → alınan {got} — {note}",
                  hard=False)


_PRIORITY_READERS = {
    "X-Priority": lambda v: ("high" if v[:1] in "12" else "low" if v[:1] in "45" else "normal")
    if v[:1].isdigit() else None,
    "Importance": lambda v: v if v in ("high", "low", "normal") else None,
    "X-MSMail-Priority": lambda v: v if v in ("high", "low", "normal") else None,
    "Priority": lambda v: {"urgent": "high", "non-urgent": "low", "normal": "normal"}.get(v),
}


def _check_priority(msg, level: str) -> list:
    tr = {"high": "yüksek", "low": "düşük", "normal": "normal"}
    seen, wrong, missing = [], [], []
    for name, reader in _PRIORITY_READERS.items():
        raw = _header(msg, name)
        if not raw:
            missing.append(name)
            continue
        got = reader(raw.lower())
        (seen if got == level else wrong).append(f"{name}: {raw}")
    checks = []
    if wrong:
        checks.append(_check("Öncelik düzeyi", False,
                             f"Beklenen '{tr[level]}' ama: " + "; ".join(wrong)))
    elif not seen:
        checks.append(_check("Öncelik düzeyi", False,
                             "Öncelik başlıklarının TAMAMI silinmiş — istemci öncelik "
                             "simgesi gösteremez"))
    else:
        checks.append(_check("Öncelik düzeyi", True,
                             f"'{tr[level]}' öncelik korunmuş: " + "; ".join(seen)))
    checks.append(_check("Öncelik başlıkları", True,
                         f"korunan {len(seen)}/4"
                         + (f" — silinen: {', '.join(missing)}" if missing else ""),
                         hard=False))
    return checks


def _check_headers_equal(msg, expected: dict) -> list:
    checks = []
    for name, want in expected.items():
        got = _header(msg, name)
        want_n = " ".join(str(want).split())
        if not got:
            checks.append(_check(f"Başlık: {name}", False, "Başlık silinmiş"))
        elif got.lower() == want_n.lower():
            checks.append(_check(f"Başlık: {name}", True, f"Korunmuş: {got[:120]}"))
        else:
            checks.append(_check(f"Başlık: {name}", False,
                                 f"Değişmiş — beklenen {want_n[:120]!r}, alınan {got[:120]!r}"))
    return checks


def _addresses(msg, name: str) -> list:
    values = [str(v) for v in (msg.get_all(name) or [])]
    return [(" ".join(_decode(display).split()), addr.lower())
            for display, addr in getaddresses(values) if addr]


def _check_addresses(msg, expected: dict) -> list:
    checks = []
    for name, pairs in expected.items():
        got = _addresses(msg, name)
        if not got:
            checks.append(_check(f"Adres başlığı: {name}", False, "Başlık silinmiş / boş"))
            continue
        problems = []
        for display, addr in pairs:
            match = next((g for g in got if g[1] == str(addr).lower()), None)
            if match is None:
                problems.append(f"{addr} adresi yok")
            elif display and match[0] != " ".join(display.split()):
                problems.append(f"görünen ad değişmiş: beklenen {display!r}, alınan {match[0]!r}")
        if len(got) != len(pairs):
            problems.append(f"adres sayısı {len(pairs)} yerine {len(got)}")
        checks.append(_check(f"Adres başlığı: {name}", not problems,
                             "; ".join(problems) if problems else
                             "Korunmuş: " + ", ".join(f"{d} <{a}>".strip() for d, a in got)))
    return checks


def _attachment_parts(msg) -> list:
    found = []
    for part in _walk_top(msg):
        if part.is_multipart():
            continue
        if _disp_type(part) == "attachment" or (part.get_filename() and not part.get("Content-ID")):
            data = part.get_payload(decode=True) or b""
            found.append({"filename": _decode(part.get_filename() or ""),
                          "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    return found


def _check_attachments(msg, expected: list) -> list:
    got = _attachment_parts(msg)
    checks = []
    for want in expected:
        name = want["filename"]
        by_hash = next((g for g in got if g["sha256"] == want["sha256"]), None)
        if by_hash is None:
            by_name = next((g for g in got if g["filename"] == name), None)
            if by_name:
                detail = (f"İçerik DEĞİŞMİŞ — beklenen {want.get('size')} bayt, "
                          f"alınan {by_name['size']} bayt; SHA-256 uyuşmuyor")
            else:
                detail = f"Ek bulunamadı (alınan ek sayısı: {len(got)})"
            checks.append(_check(f"Ek bütünlüğü: {name}", False, detail))
            continue
        checks.append(_check(f"Ek bütünlüğü: {name}", True,
                             f"SHA-256 eşleşti ({by_hash['size']} bayt)"))
        same_name = by_hash["filename"] == name
        checks.append(_check(f"Ek dosya adı: {name}", same_name,
                             "Dosya adı korunmuş" if same_name else
                             f"Dosya adı değişmiş: alınan {by_hash['filename']!r}"))
    return checks


def _check_inline(msg, expected: list) -> list:
    parts = {}
    for part in _walk_top(msg):
        cid = _unfold(part.get("Content-ID", "")).strip("<>")
        if cid and not part.is_multipart():
            parts[cid] = hashlib.sha256(part.get_payload(decode=True) or b"").hexdigest()
    checks = []
    for want in expected:
        cid = want["cid"]
        if cid not in parts:
            checks.append(_check("Gömülü görsel (CID)", False,
                                 f"Content-ID {cid} taşıyan parça yok"))
        else:
            ok = parts[cid] == want["sha256"]
            checks.append(_check("Gömülü görsel (CID)", ok,
                                 "Görsel bayt düzeyinde aynı" if ok else
                                 "Görsel içeriği değişmiş (yeniden kodlanmış / küçültülmüş)"))
    return checks


def _html_text(msg) -> Optional[str]:
    part = _first_text(msg, "html")
    return _part_text(part) if part is not None else None


_HREF_RE = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def _check_html(msg, expect: dict) -> list:
    html = _html_text(msg)
    if html is None:
        return [_check("HTML gövde", False, "text/html parçası bulunamadı")]
    checks = []
    missing = [frag for frag in expect.get("html_contains", []) if frag not in html]
    if expect.get("html_contains"):
        checks.append(_check("HTML içerik", not missing,
                             "Beklenen tüm parçalar HTML'de mevcut" if not missing else
                             "HTML'de bulunamayan: " + ", ".join(repr(m) for m in missing)))
    if expect.get("html_hrefs"):
        got = {html_lib.unescape(a or b) for a, b in _HREF_RE.findall(html)}
        lost = [u for u in expect["html_hrefs"] if u not in got]
        checks.append(_check("Bağlantı bütünlüğü (href)", not lost,
                             f"{len(expect['html_hrefs'])} bağlantının tamamı değişmeden iletildi"
                             if not lost else
                             "Değişen/kaybolan bağlantılar: " + ", ".join(lost)
                             + " — alınan href'ler: " + ", ".join(sorted(got))[:400]))
    return checks


def _check_leaf_types(msg, expected: list) -> dict:
    got = [p.get_content_type() for p in _walk_top(msg) if not p.is_multipart()]
    missing = [t for t in expected if t not in got]
    tree = " › ".join(p.get_content_type() for p in _walk_top(msg) if p.is_multipart())
    return _check("MIME parçaları", not missing,
                  (f"Tüm parçalar mevcut ({', '.join(got)}); kapsayıcılar: {tree or 'yok'}")
                  if not missing else
                  f"Eksik parça: {', '.join(missing)} — alınan: {', '.join(got)}")


# ────────────────────────────────────────────────────────────────────
#  Genel giriş noktaları
# ────────────────────────────────────────────────────────────────────
def verify(expect: dict, raw: bytes) -> list:
    """Beklentiyi ham mesajla karşılaştırır; kontrol listesi döndürür."""
    if not raw:
        return [_check("Ham mesaj", False, "Alınan mesajın ham içeriği yok")]
    msg = email.message_from_bytes(raw)
    checks: list = []
    if "subject" in expect:
        checks.append(_check_subject(msg, expect["subject"]))
    if "body_text" in expect:
        checks.append(_check_body(msg, expect["body_text"]))
    if "priority_level" in expect:
        checks += _check_priority(msg, expect["priority_level"])
    if expect.get("headers_equal"):
        checks += _check_headers_equal(msg, expect["headers_equal"])
    if expect.get("address_headers"):
        checks += _check_addresses(msg, expect["address_headers"])
    if expect.get("attachments"):
        checks += _check_attachments(msg, expect["attachments"])
    if expect.get("inline"):
        checks += _check_inline(msg, expect["inline"])
    if expect.get("html_contains") or expect.get("html_hrefs"):
        checks += _check_html(msg, expect)
    if expect.get("leaf_types"):
        checks.append(_check_leaf_types(msg, expect["leaf_types"]))
    if expect.get("declared"):
        checks.append(_check_declared(msg, expect["declared"]))
    return checks


def merge_verdicts(det_checks: list, llm: Optional[dict]) -> dict:
    """Deterministik kontrolleri (bağlayıcı) LLM yorumuyla (danışma) birleştirir.

    Karar deterministik kontrollere aittir. LLM farklı düşünüyorsa karar
    değişmez ama güven MEDIUM'a iner ve görüşü 'issues' altında gösterilir —
    böylece insan (Manuel Doğrulama) hakemlik edebilir.
    """
    failed = [c for c in det_checks if c["name"].startswith(HARD) and not c["passed"]]
    passed = not failed
    hard_total = sum(1 for c in det_checks if c["name"].startswith(HARD))
    checks = list(det_checks)
    issues = [f"{c['name']}: {c['detail']}" for c in failed]
    recommendations: list = []
    confidence = "HIGH"
    llm_note = ""

    llm_checks = (llm or {}).get("checks") or []
    llm_broken = any(c.get("name") in _LLM_FAILURE_CHECKS for c in llm_checks
                     if isinstance(c, dict))
    if llm is None:
        llm_note = " LLM analizi kapalı."
    elif llm_broken:
        llm_note = " LLM yorumu alınamadı: " + str(llm.get("summary") or "")[:200]
    else:
        for c in llm_checks:
            if isinstance(c, dict):
                checks.append({"name": "[LLM] " + str(c.get("name") or "Kontrol"),
                               "passed": bool(c.get("passed")),
                               "detail": str(c.get("detail") or "")})
        recommendations = list(llm.get("recommendations") or [])
        if passed and llm.get("passed") is False:
            confidence = "MEDIUM"
            issues.append("LLM farklı görüşte (karar deterministik kontrollere göre "
                          "verildi; elle doğrulayın): " + str(llm.get("summary") or ""))
            issues += [f"[LLM] {i}" for i in (llm.get("issues") or [])]
        elif not passed:
            issues += [f"[LLM] {i}" for i in (llm.get("issues") or [])]

    if passed:
        summary = f"{hard_total} kesin kontrolün tamamı geçti (bayt düzeyinde doğrulandı)."
    else:
        summary = (f"{len(failed)}/{hard_total} kesin kontrol başarısız: "
                   + "; ".join(c["name"].replace(HARD, "") for c in failed))
    return {
        "passed": passed,
        "confidence": confidence,
        "checks": checks,
        "summary": summary + llm_note,
        "issues": issues,
        "recommendations": recommendations,
        "verdict_source": "deterministic",
    }

#!/usr/bin/env python3
"""
live_check.py — Yayındaki (deploy edilmiş) örneğe karşı fonksiyonel ve
son kullanıcı testleri.

Aşamalar:
  A) Fonksiyonel kontroller (her zaman): health, arayüz, auth, maskeleme,
     kombinasyonlar, path traversal, MFA/durum/rapor endpointleri
  B) Son kullanıcı akışı (--config verilirse): config yükle → dry-run →
     gerçek plain_text koşusu (Gmail→Gmail) → rapor/sonuç kontrolü
  C) MEB/harici alıcı gönderimi (--to verilirse): test_address'i geçici
     değiştirir, gönderir (alım adımı bilerek zaman aşımına düşer —
     alıcı kutuyu SEN kontrol edersin), sonra geri alır
  D) Temizlik (--scrub): sitedeki config secret'larını placeholder ile ezer

Kullanım:
  python scripts/live_check.py https://site.onrender.com
  python scripts/live_check.py https://site --password 'UI_PASSWORD'
  python scripts/live_check.py https://site --password '...' \\
      --config config.yaml --to alici@ornek.com --scrub
"""

import argparse
import copy
import sys
import time
from pathlib import Path

import httpx
import yaml

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    icon = "✅" if ok else "❌"
    print(f"{icon} {name}" + (f" — {detail}" if detail else ""))


class Client:
    def __init__(self, base: str, username: str, password: str):
        auth = (username, password) if password else None
        self.http = httpx.Client(base_url=base.rstrip("/"), auth=auth,
                                 timeout=90.0, follow_redirects=True)

    def get(self, path, **kw):
        return self.http.get(path, **kw)

    def post(self, path, **kw):
        return self.http.post(path, **kw)


def wait_cold_start(c: Client) -> bool:
    """Render free plan uykudan ~50 sn'de uyanır."""
    print("⏳ Servis uyandırılıyor (soğuk başlatma 60 sn sürebilir)...")
    for attempt in range(6):
        try:
            r = c.get("/api/health")
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(10)
    return False


def poll_run(c: Client, timeout_s: int = 300) -> dict:
    """Koşu bitene kadar bekler, tüm logları toplar."""
    offset, lines = 0, []
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            logs = c.get(f"/api/run/logs", params={"offset": offset}).json()
            lines += logs.get("lines", [])
            offset = logs.get("next_offset", offset)
            status = c.get("/api/run/status").json()
            if not status.get("running") and offset > 0:
                # kuyruktaki son satırları da al
                logs = c.get(f"/api/run/logs", params={"offset": offset}).json()
                lines += logs.get("lines", [])
                return {"status": status, "lines": lines}
            if not status.get("running") and status.get("finished_at"):
                return {"status": status, "lines": lines}
        except httpx.HTTPError as e:
            print(f"   (poll hatası, yeniden denenecek: {e})")
        time.sleep(4)
    return {"status": {"running": True, "timeout": True}, "lines": lines}


# ────────────────────────────────────────────────────────────────────
#  A) Fonksiyonel kontroller
# ────────────────────────────────────────────────────────────────────
def phase_functional(c: Client, password: str, secrets: list[str]):
    print("\n━━━ A) FONKSİYONEL KONTROLLER ━━━")

    r = c.get("/api/health")
    record("Health endpoint", r.status_code == 200 and r.json().get("ok") is True,
           f"serverless={r.json().get('serverless')}" if r.status_code == 200 else f"HTTP {r.status_code}")

    r = c.get("/")
    record("Arayüz açılıyor", r.status_code == 200 and "Mail" in r.text,
           f"{len(r.text)} bayt HTML")

    # Auth kontrolü: parola verildiyse parolasız istek 401 olmalı
    noauth = httpx.Client(base_url=str(c.http.base_url), timeout=60.0)
    r = noauth.get("/api/run/status")
    if password:
        record("Basic Auth aktif (parolasız istek reddediliyor)", r.status_code == 401,
               f"HTTP {r.status_code}")
    else:
        record("Basic Auth KAPALI — UI_PASSWORD ayarlanmalı!", False,
               "site parolasız internete açık")

    r = c.get("/api/combinations")
    combos = r.json().get("combinations", []) if r.status_code == 200 else []
    record("Kombinasyonlar yükleniyor", len(combos) == 18, f"{len(combos)} kombinasyon")

    # Path traversal
    r = c.get("/api/reports/..%2fconfig.yaml")
    record("Path traversal engelli", r.status_code == 404, f"HTTP {r.status_code}")

    r = c.get("/api/mfa/status")
    record("MFA durumu", r.status_code == 200 and r.json().get("pending") is False)

    r = c.get("/api/reports")
    record("Rapor listesi", r.status_code == 200 and r.json().get("ok") is True)

    # Secret sızıntısı: bilinen secret'lar hiçbir GET yanıtında görünmemeli
    if secrets:
        leaked = []
        for path in ("/api/config", "/api/reports", "/api/results/latest"):
            try:
                body = c.get(path).text
                leaked += [s for s in secrets if s and s in body]
            except httpx.HTTPError:
                pass
        record("Secret maskeleme (GET yanıtlarında sızıntı yok)", not leaked,
               "SIZINTI VAR!" if leaked else "")

    return combos


# ────────────────────────────────────────────────────────────────────
#  B) Son kullanıcı akışı
# ────────────────────────────────────────────────────────────────────
def find_gmail_combo(combos) -> int | None:
    for combo in combos:
        if (combo["sender_server"].lower() == "gmail"
                and combo["receiver_server"].lower() == "gmail"):
            return combo["index"]
    return None


def phase_user_flow(c: Client, cfg: dict, combos) -> None:
    print("\n━━━ B) SON KULLANICI AKIŞI (gerçek Gmail hesabıyla) ━━━")

    cfg = copy.deepcopy(cfg)
    cfg.setdefault("test", {}).update(
        {"wait_seconds": 20, "max_retries": 2, "retry_interval": 8}
    )
    r = c.post("/api/config", json={"config": cfg})
    record("Config siteye yüklendi", r.status_code == 200 and r.json().get("ok"))

    idx = find_gmail_combo(combos)
    if idx is None:
        record("Gmail→Gmail kombinasyonu", False, "bulunamadı")
        return
    print(f"   Kombinasyon #{idx} kullanılacak (Gmail→Gmail)")

    # Dry-run: SMTP bağlantı + kimlik doğrulama
    r = c.post("/api/run/start", json={"dry_run": True, "combo": idx})
    record("Dry-run başlatıldı", r.status_code == 200 and r.json().get("ok"),
           r.json().get("error", ""))
    run = poll_run(c, timeout_s=180)
    log_text = "\n".join(run["lines"])
    smtp_ok = "SMTP OK" in log_text
    record("SMTP bağlantı + giriş (dry-run)", smtp_ok,
           "sunucu SMTP çıkışına izin veriyor" if smtp_ok else "loglara bakın")
    if not smtp_ok:
        print("   --- dry-run logları (son 10) ---")
        for ln in run["lines"][-10:]:
            print(f"   {ln}")
        return

    # Gerçek koşu: gönder → IMAP'te bul → analiz → rapor
    r = c.post("/api/run/start", json={"combo": idx, "scenario": "plain_text"})
    record("Gerçek koşu başlatıldı (plain_text)", r.status_code == 200 and r.json().get("ok"))
    run = poll_run(c, timeout_s=360)
    log_text = "\n".join(run["lines"])
    record("Mail gönderildi", "Gönderildi" in log_text)
    record("Mail IMAP'te bulundu (alım doğrulandı)", "Mesaj bulundu" in log_text,
           "uçtan uca iletim çalışıyor" if "Mesaj bulundu" in log_text else "")
    exit_code = run["status"].get("exit_code")
    record("Koşu tamamlandı", exit_code == 0, f"exit={exit_code}")
    print("   --- koşu logları (son 12) ---")
    for ln in run["lines"][-12:]:
        print(f"   {ln}")

    r = c.get("/api/results/latest")
    ok = r.status_code == 200 and r.json().get("ok")
    record("Sonuç CSV'si üretildi", bool(ok),
           f"{len(r.json().get('results', []))} satır" if ok else "")
    r = c.get("/api/reports")
    names = [f["name"] for f in r.json().get("reports", [])] if r.status_code == 200 else []
    record("HTML rapor üretildi", "test_report.html" in names)


# ────────────────────────────────────────────────────────────────────
#  C) Harici alıcıya gönderim (kullanıcı kutusunu elle kontrol eder)
# ────────────────────────────────────────────────────────────────────
def phase_external_send(c: Client, cfg: dict, combos, to_addr: str):
    print(f"\n━━━ C) HARİCİ ALICIYA GÖNDERİM ({to_addr}) ━━━")
    idx = find_gmail_combo(combos)
    if idx is None:
        record("Gmail kombinasyonu", False, "bulunamadı")
        return

    ext = copy.deepcopy(cfg)
    ext["gmail"]["test_address"] = to_addr
    ext.setdefault("test", {}).update(
        {"wait_seconds": 5, "max_retries": 1, "retry_interval": 3}
    )
    c.post("/api/config", json={"config": ext})

    r = c.post("/api/run/start", json={"combo": idx, "scenario": "plain_text"})
    record("Harici gönderim başlatıldı", r.status_code == 200 and r.json().get("ok"))
    run = poll_run(c, timeout_s=240)
    log_text = "\n".join(run["lines"])
    sent = "Gönderildi" in log_text
    record("Mail harici alıcıya gönderildi", sent,
           "alıcı kutusunu (spam dahil) elle kontrol edin" if sent else "")
    subject_lines = [ln for ln in run["lines"] if "run_id=" in ln or "Senaryo:" in ln]
    for ln in subject_lines[:3]:
        print(f"   {ln}")
    print("   Not: 'Mesaj bulunamadı' uyarısı BEKLENEN durumdur — alım kutusu")
    print("   farklı bir sunucuda olduğu için otomatik doğrulama yapılamaz.")

    # test_address'i eski haline getir
    c.post("/api/config", json={"config": cfg})


# ────────────────────────────────────────────────────────────────────
#  D) Temizlik
# ────────────────────────────────────────────────────────────────────
def phase_scrub(c: Client, cfg: dict):
    print("\n━━━ D) TEMİZLİK (secret'lar siteden siliniyor) ━━━")
    scrubbed = copy.deepcopy(cfg)
    for srv in ("ems", "gmail", "outlook"):
        if isinstance(scrubbed.get(srv), dict):
            for key in ("password", "totp_secret"):
                if scrubbed[srv].get(key):
                    scrubbed[srv][key] = "scrubbed"
    for section in ("anthropic", "gemini"):
        if isinstance(scrubbed.get(section), dict) and scrubbed[section].get("api_key"):
            scrubbed[section]["api_key"] = "scrubbed"
    r = c.post("/api/config", json={"config": scrubbed})
    record("Site config'indeki secret'lar temizlendi", r.status_code == 200,
           "gerçek kullanım için config'i arayüzden yeniden girin/yükleyin")


def main():
    ap = argparse.ArgumentParser(description="Canlı örnek fonksiyonel testi")
    ap.add_argument("base_url", help="ör. https://mail-test-otomasyon.onrender.com")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password", default="", help="UI_PASSWORD (ayarlıysa)")
    ap.add_argument("--config", help="Gerçek koşu için yerel config.yaml yolu")
    ap.add_argument("--to", help="Harici alıcıya da gönder (ör. MEB adresi)")
    ap.add_argument("--scrub", action="store_true",
                    help="Test sonrası sitedeki secret'ları temizle")
    args = ap.parse_args()

    c = Client(args.base_url, args.username, args.password)
    if not wait_cold_start(c):
        print("❌ Servis uyanmadı — Render panelinden durumu kontrol edin.")
        sys.exit(1)

    cfg, secrets = None, []
    if args.config:
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
        for srv in ("ems", "gmail", "outlook"):
            if isinstance(cfg.get(srv), dict):
                secrets += [cfg[srv].get("password", ""), cfg[srv].get("totp_secret", "")]
        for sec in ("anthropic", "gemini"):
            if isinstance(cfg.get(sec), dict):
                secrets.append(cfg[sec].get("api_key", ""))
        secrets = [s for s in secrets if s]

    combos = phase_functional(c, args.password, secrets)

    if cfg:
        phase_user_flow(c, cfg, combos)
        if args.to:
            phase_external_send(c, cfg, combos, args.to)
        if args.scrub:
            phase_scrub(c, cfg)

    print("\n" + "═" * 60)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"SONUÇ: {len(RESULTS) - len(failed)}/{len(RESULTS)} kontrol geçti")
    if failed:
        print("Başarısız:")
        for n in failed:
            print(f"  ✗ {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()

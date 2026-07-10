#!/usr/bin/env python3
"""
e2e_smoke.py — Gerçek sunucuya karşı uçtan uca duman testi.

config.yaml'daki bir gönderen hesaptan, verilen alıcı adrese 4 senaryoyu
(plain_text, attachment, inline_image, reply_chain) gönderir; ardından
gönderen hesabın gelen kutusunu IMAP ile tarayıp bounce (mailer-daemon)
olup olmadığını kontrol eder. Alıcı tarafındaki görsel doğrulama (resim
görünüyor mu, ek açılıyor mu, thread birleşti mi) insan tarafından yapılır.

Kullanım:
  cp config.yaml.example config.yaml   # gönderen hesabın bölümünü doldur
  python scripts/e2e_smoke.py --to alici@ornek.com
  python scripts/e2e_smoke.py --to alici@ornek.com --server gmail --scenarios plain_text,attachment
  python scripts/e2e_smoke.py --to alici@ornek.com --dry-run   # göndermeden mesajları hazırla
"""

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)  # test_files/ ve config.yaml repo köküne göre çözülür

import yaml  # noqa: E402

from main import (  # noqa: E402
    LENGTH_LABELS,
    _attachment_tag,
    _build_subject,
    _inline_image_tag,
    _resolve_attachment_paths,
    prepare_test_files,
)
from message_templates import get_reply_original, get_template, resolve_inline_html  # noqa: E402
from sender import MailSender  # noqa: E402

ALL_SCENARIOS = ["plain_text", "attachment", "inline_image", "reply_chain"]

# Alıcı istemcide insan gözüyle yapılacak kontroller
MANUAL_CHECKS = {
    "plain_text": "Gövde eksiksiz mi, Türkçe karakterler (ğüşıöçĞÜŞİÖÇ) bozulmamış mı?",
    "attachment": "Ek(ler) görünüyor mu, adı/boyutu doğru mu, açılabiliyor mu?",
    "inline_image": "Görsel gövde İÇİNDE görünüyor mu (ek olarak değil)? HTML düzeni bozulmamış mı?",
    "reply_chain": "İki mesaj aynı konuşma/thread altında birleşti mi? Alıntı '>' bölümü duruyor mu?",
}


def load_server_config(config_path: str, server_key: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if server_key not in config:
        raise SystemExit(f"HATA: '{server_key}' config.yaml'da tanımlı değil.")
    sc = config[server_key]
    missing = [k for k in ("smtp_host", "smtp_port", "username", "password", "test_address")
               if not sc.get(k)]
    if missing:
        raise SystemExit(f"HATA: config.yaml [{server_key}] eksik alanlar: {', '.join(missing)}")
    return sc


def send_scenario(scenario: str, sender: MailSender, to_address: str,
                  test_cfg: dict, dry_run: bool) -> dict:
    run_id = uuid.uuid4().hex[:8]
    prefix = test_cfg.get("subject_prefix", "[AUTO-TEST]")
    image_path = test_cfg.get("test_image_path", "test_files/test_image.png")
    attachment_paths = _resolve_attachment_paths(test_cfg)

    tmpl = get_template(scenario, 0)
    length_label = LENGTH_LABELS.get(tmpl.length, tmpl.length)
    combo_label = f"E2E Duman → {to_address}"

    detail = {
        "plain_text": "Eksiz",
        "attachment": _attachment_tag(attachment_paths),
        "inline_image": _inline_image_tag(image_path),
        "reply_chain": "Eksiz, Thread Testi",
    }[scenario]

    subject = _build_subject(prefix, run_id, scenario, length_label, combo_label, detail)
    body = f"{tmpl.body}\n\n[Run ID: {run_id}]"

    if dry_run:
        return {"run_id": run_id, "subject": subject, "sent": False}

    if scenario == "plain_text":
        sender.send_plain_text(to_address, subject, body)
    elif scenario == "attachment":
        sender.send_with_attachment(to_address, subject, body,
                                    attachment_paths or "test_files/test_document.pdf")
    elif scenario == "inline_image":
        html_body = resolve_inline_html(tmpl.body, "{{CID}}")
        sender.send_inline_image(to_address, subject, image_path, html_body=html_body)
    elif scenario == "reply_chain":
        orig_tmpl = get_reply_original(0)
        orig_subject = _build_subject(prefix, f"ORIG-{run_id}", "reply_chain",
                                      LENGTH_LABELS.get(orig_tmpl.length, orig_tmpl.length),
                                      combo_label, "Thread Başlangıç, Eksiz")
        orig_meta = sender.send_plain_text(to_address, orig_subject,
                                           f"{orig_tmpl.body}\n\n[Run ID: {run_id}]")
        time.sleep(3)
        sender.send_reply(to_address, orig_subject, orig_meta.get("msg_id", ""), "", body)

    return {"run_id": run_id, "subject": subject, "sent": True}


def check_bounces(server_cfg: dict, since_ts: float, wait_seconds: int) -> list[str]:
    """Gönderen hesabın gelen kutusunda mailer-daemon bounce'larını arar."""
    import email as email_lib
    import imaplib
    import ssl
    from email.utils import parsedate_to_datetime

    print(f"\n⏳ Bounce kontrolü için {wait_seconds} sn bekleniyor...")
    time.sleep(wait_seconds)

    bounces = []
    imap = imaplib.IMAP4_SSL(server_cfg["imap_host"], server_cfg.get("imap_port", 993),
                             ssl_context=ssl.create_default_context())
    try:
        imap.login(server_cfg["username"], server_cfg["password"])
        imap.select("INBOX")
        _, data = imap.search(None, 'FROM "mailer-daemon"')
        for mail_id in (data[0].split() or [])[-10:]:
            _, msg_data = imap.fetch(mail_id, "(RFC822.HEADER)")
            msg = email_lib.message_from_bytes(msg_data[0][1])
            try:
                received = parsedate_to_datetime(msg.get("Date", "")).timestamp()
            except Exception:
                received = 0
            if received >= since_ts - 60:
                bounces.append(msg.get("Subject", "(konu yok)"))
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return bounces


def main():
    parser = argparse.ArgumentParser(description="Gerçek sunucu E2E duman testi")
    parser.add_argument("--to", required=True, help="Alıcı e-posta adresi")
    parser.add_argument("--server", default="gmail",
                        help="config.yaml'daki gönderen sunucu anahtarı (varsayılan: gmail)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--scenarios", default=",".join(ALL_SCENARIOS),
                        help=f"Virgülle ayrılmış liste (varsayılan: hepsi). Seçenekler: {ALL_SCENARIOS}")
    parser.add_argument("--bounce-wait", type=int, default=90,
                        help="Bounce kontrolünden önce bekleme (sn, varsayılan 90; 0=atla)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Mesajları hazırla ama gönderme")
    args = parser.parse_args()

    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = [s for s in scenarios if s not in ALL_SCENARIOS]
    if unknown:
        raise SystemExit(f"HATA: bilinmeyen senaryo(lar): {unknown}")

    server_cfg = load_server_config(args.config, args.server)
    with open(args.config, "r", encoding="utf-8") as f:
        test_cfg = (yaml.safe_load(f) or {}).get("test", {})

    prepare_test_files()
    sender = MailSender(server_cfg)
    started = time.time()

    print("=" * 72)
    print(f"E2E Duman Testi | {server_cfg['username']} → {args.to}"
          + (" | DRY-RUN (gönderim yok)" if args.dry_run else ""))
    print("=" * 72)

    results = []
    for sc in scenarios:
        try:
            r = send_scenario(sc, sender, args.to, test_cfg, args.dry_run)
            status = "hazırlandı" if args.dry_run else "gönderildi ✓"
            print(f"\n[{sc}] {status}\n  Run ID : {r['run_id']}\n  Konu   : {r['subject']}")
            results.append((sc, r, None))
        except Exception as e:
            print(f"\n[{sc}] HATA ✗ — {type(e).__name__}: {e}")
            results.append((sc, None, e))
        if not args.dry_run:
            time.sleep(2)

    failed = [sc for sc, _, e in results if e]
    if not args.dry_run and args.bounce_wait > 0 and len(failed) < len(results):
        try:
            bounces = check_bounces(server_cfg, started, args.bounce_wait)
            if bounces:
                print(f"\n⚠️  {len(bounces)} bounce bulundu:")
                for b in bounces:
                    print(f"   - {b}")
            else:
                print("✓ Bounce yok — mesajlar alıcı sunucu tarafından kabul edilmiş görünüyor.")
        except Exception as e:
            print(f"⚠️  Bounce kontrolü yapılamadı: {type(e).__name__}: {e}")

    print("\n" + "=" * 72)
    print("ALICI TARAFINDA KONTROL LİSTESİ (spam klasörünü de kontrol et):")
    for sc, r, e in results:
        if e:
            continue
        print(f"  [{sc}] Run ID {r['run_id']}")
        print(f"     → {MANUAL_CHECKS[sc]}")
    print("=" * 72)
    if failed:
        print(f"✗ Gönderilemeyen senaryolar: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
zpush_check.py — Z-Push (ActiveSync) bağlantısını adım adım sınar.

ActiveSync istemcisi sahte bir sunucuya karşı test edildi; gerçek Z-Push ile
ilk deneme bu betikle yapılır. Her adım ayrı raporlanır, hangi adımda
takıldığı ve sunucunun ne döndürdüğü görünür.

Kullanım:
    python scripts/zpush_check.py                       # config.yaml → ems bölümü
    python scripts/zpush_check.py --server ems --debug  # sunucu yanıtlarını ağaç olarak dök
    python scripts/zpush_check.py --url https://posta.kurum.gov.tr/Microsoft-Server-ActiveSync \\
                                  --user test@kurum.gov.tr          # şifre sorulur
    python scripts/zpush_check.py --find "#a1b2c3d4"    # konusu eşleşen mesajı indir, göster
    python scripts/zpush_check.py --send-to ben@kurum.gov.tr        # Z-Push üzerinden test gönder

Adımlar: 1) OPTIONS (sürümler)  2) FolderSync (gelen kutusu, gerekirse cihaz
ilkesi kabulü)  3) Sync (son 3 günün mesajları)  4) isteğe bağlı: mesaj indir / gönder.

Betik posta kutusunda hiçbir şeyi silmez veya okundu işaretlemez. Sunucuda
"MailTestOtomasyon" türünde bir cihaz kaydı oluşur (Z-Push yönetiminde görünür).
"""
import argparse
import email
import getpass
import logging
import sys
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from activesync import EasAuthError, EasClient, EasError, WbxmlError  # noqa: E402
from sender import SMTP_POLICY  # noqa: E402
from verifier import eas_view_checks, fingerprint, visible_text  # noqa: E402

IMPORTANCE = {"0": "düşük", "1": "normal", "2": "YÜKSEK"}


def step(number: int, title: str) -> None:
    print(f"\n[{number}] {title}")


def fail(message: str, hint: str = "") -> None:
    print(f"    ❌ {message}")
    if hint:
        print(f"       ↳ {hint}")
    sys.exit(2)


def load_server(args) -> dict:
    cfg: dict = {}
    if Path(args.config).exists():
        from main import load_config
        cfg = dict(load_config(args.config).get(args.server) or {})
    if args.url:
        cfg["eas_url"] = args.url
    if args.user:
        cfg["username"] = args.user
        cfg.pop("eas_username", None)
        cfg["password"] = ""
    if args.version:
        cfg["eas_version"] = args.version
    if args.insecure:
        cfg["tls_verify"] = False
    if not cfg.get("eas_url"):
        print(f"HATA: eas_url yok. config.yaml → {args.server} bölümüne ekleyin ya da --url verin.")
        sys.exit(1)
    if not cfg.get("username"):
        print("HATA: kullanıcı adı yok (--user).")
        sys.exit(1)
    if not cfg.get("password"):
        cfg["password"] = getpass.getpass(f"{cfg['username']} şifresi: ")
    return cfg


def main() -> None:
    parser = argparse.ArgumentParser(description="Z-Push (ActiveSync) bağlantı sınaması")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--server", default="ems", help="config.yaml sunucu bölümü (varsayılan: ems)")
    parser.add_argument("--url", help="Z-Push adresi (…/Microsoft-Server-ActiveSync)")
    parser.add_argument("--user", help="Kullanıcı adı (şifre sorulur)")
    parser.add_argument("--version", help="Protokol sürümünü zorla (12.1 | 14.0 | 14.1)")
    parser.add_argument("--insecure", action="store_true",
                        help="Sertifika doğrulamasını kapat (yalnızca deneme)")
    parser.add_argument("--find", help="Konusunda bu metin geçen mesajı indir ve göster")
    parser.add_argument("--send-to", help="Z-Push üzerinden bu adrese test mesajı gönder")
    parser.add_argument("--from-address", help="Gönderen adres (varsayılan: test_address/kullanıcı)")
    parser.add_argument("--debug", action="store_true", help="Sunucu yanıtlarını (WBXML ağacı) dök")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    cfg = load_server(args)
    client = EasClient(cfg)
    print(f"Z-Push: {client.url}\nKullanıcı: {client.username} | Cihaz: {client.device_id} "
          f"({client.device_type})")

    try:
        step(1, "OPTIONS — sunucu sürümleri")
        info = client.options()
        if not info["versions"]:
            fail("Sunucu MS-ASProtocolVersions başlığı döndürmedi — bu adres ActiveSync uç noktası "
                 "olmayabilir.", "Adres genellikle …/Microsoft-Server-ActiveSync ile biter.")
        print(f"    ✅ Sunucu: {info['server'] or '?'} | sürümler: {', '.join(info['versions'])}")
        print(f"       Kullanılacak sürüm: {info['version']}")

        step(2, "FolderSync — klasörler (gerekirse cihaz ilkesi kabul edilir)")
        folders = client.folder_sync()
        if client.policy_key != "0":
            print(f"    ℹ️  Sunucu cihaz ilkesi istedi; kabul edildi (anahtar {client.policy_key}).")
        print(f"    ✅ {len(folders)} klasör | gelen kutusu: "
              f"{next(f['name'] for f in folders if f['id'] == client.inbox_id)!r} "
              f"(id {client.inbox_id})")

        step(3, "Sync — gelen kutusu (son 3 gün)")
        count = client.sync_inbox()
        print(f"    ✅ {count} mesaj")
        for sid, item in list(client.items.items())[-10:]:
            files = ", ".join(a["name"] for a in item["attachments"]) or "—"
            print(f"       {sid:>10} | {IMPORTANCE.get(item['importance'], '?'):7} | "
                  f"{item['subject'][:70]} | ekler: {files}")
        if count > 10:
            print(f"       … (son 10 gösterildi)")

        if args.find:
            step(4, f"Mesaj indir — konu içinde {args.find!r}")
            matches = [sid for sid, item in client.items.items() if args.find in item["subject"]]
            if not matches:
                fail("Eşleşen mesaj yok (son 3 gün içinde).")
            sid = matches[-1]
            raw = client.fetch_mime(sid)
            view = {**client.items[sid], **client.fetch_view(sid)}
            msg = email.message_from_bytes(raw)
            print(f"    ✅ MIME: {len(raw)} bayt | Message-ID: {msg.get('Message-ID')}")
            print("       Parçalar: " + ", ".join(t for t, _ in fingerprint(raw)))
            kind = {"1": "düz metin", "2": "HTML"}.get(view["body_type"], view["body_type"])
            print(f"       Telefonun gördüğü gövde ({kind}, {len(view['body'])} karakter"
                  f"{', KIRPILMIŞ' if view['truncated'] else ''}):")
            print("       " + visible_text(view["body"])[:300])
            print("       Görünüm kontrolleri (MIME ↔ telefona giden alanlar):")
            for check in eas_view_checks(view, raw, None):
                print(f"         {'✅' if check['passed'] else '❌'} {check['name']}: {check['detail']}")

        if args.send_to:
            step(5 if args.find else 4, f"SendMail — Z-Push üzerinden {args.send_to}")
            sender = args.from_address or cfg.get("test_address") or client.username
            msg = MIMEText("Z-Push gönderim sınaması.\nTürkçe: ğüşıöç ĞÜŞİÖÇ\n", "plain", "utf-8")
            msg["Message-ID"] = make_msgid()
            msg["Date"] = formatdate(localtime=True)
            msg["Subject"] = "[ZPUSH-TEST] Gönderim sınaması ğüşıöç"
            msg["From"], msg["To"] = sender, args.send_to
            client.send_mail(msg.as_bytes(policy=SMTP_POLICY))
            print(f"    ✅ Sunucu kabul etti | Message-ID: {msg['Message-ID']}")

    except EasAuthError as e:
        fail(str(e), "Kullanıcı adı/şifre; Z-Push tam e-posta adresi yerine kısa kullanıcı adı "
                     "bekliyor olabilir (--user).")
    except (EasError, WbxmlError) as e:
        fail(str(e), "--debug ile yeniden çalıştırıp çıktıyı paylaşın; sürüm sorunuysa "
                     "--version 12.1 deneyin.")
    except Exception as e:  # noqa: BLE001 — ağ/TLS hataları
        fail(f"{type(e).__name__}: {e}",
             "Adres/port/sertifika: kurum içi CA için config.yaml → tls_ca_file, "
             "yalnızca deneme için --insecure.")
    finally:
        client.close()

    print("\nTAMAM — ActiveSync kanalı çalışıyor. config.yaml'da eas_url tanımlıysa test planı "
          "koşularında Z-Push otomatik okunur.")


if __name__ == "__main__":
    main()

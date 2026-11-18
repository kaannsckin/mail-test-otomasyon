#!/usr/bin/env python3
"""
saha_kurulum.py — İlk saha denemesi için config.yaml'ı soru-cevapla oluşturur.

scripts/saha_denemesi.sh tarafından çağrılır; tek başına da çalışır:
    python scripts/saha_kurulum.py              # config.yaml yoksa oluşturur
    python scripts/saha_kurulum.py --yeniden    # varsa da baştan sorar

Şifreler ekranda görünmeden sorulur ve yalnızca bu makinedeki config.yaml'a
yazılır (dosya .gitignore'dadır, izinleri 600 yapılır).
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from mail_transport import control_chars, normalize_eas_url  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "config.yaml.example"


def ask(prompt: str, default: str = "", required: bool = False) -> str:
    while True:
        shown = f" [{default}]" if default else ""
        value = input(f"  {prompt}{shown}: ").strip() or default
        if value or not required:
            return value
        print("    (zorunlu)")


def ask_yes(prompt: str, default: bool) -> bool:
    value = input(f"  {prompt} [{'E/h' if default else 'e/H'}]: ").strip().lower()
    return default if not value else value in ("e", "evet", "y", "yes")


def ask_secret(prompt: str) -> str:
    while True:
        value = getpass.getpass(f"  {prompt}: ")
        bad = control_chars(value)
        if bad:
            print(f"    Şifrede görünmez karakter var ({', '.join(bad)}) — ok tuşu ya da yapıştırma "
                  "kaynaklı olabilir. Lütfen yeniden yazın (ok tuşlarını kullanmadan).")
            continue
        if value:
            return value
        print("    (zorunlu)")


def server_section(base: dict, title: str, host_hint: str) -> dict:
    print(f"\n── {title} ──")
    cfg = dict(base)
    host = ask("Sunucu adı (SMTP ve IMAP aynıysa tek ad)", host_hint, required=True)
    cfg["smtp_host"] = ask("SMTP sunucusu", host)
    cfg["smtp_port"] = int(ask("SMTP portu (587 = STARTTLS, 465 = SSL)", str(cfg.get("smtp_port") or 587)))
    cfg["smtp_use_tls"] = cfg["smtp_port"] != 465
    cfg["imap_host"] = ask("IMAP sunucusu", host)
    cfg["imap_port"] = int(ask("IMAP portu (993 = SSL)", str(cfg.get("imap_port") or 993)))
    cfg["username"] = ask("Kullanıcı adı (TEST hesabı)", required=True)
    cfg["password"] = ask_secret("Şifre (görünmez)")
    cfg["test_address"] = ask("Bu hesabın e-posta adresi", cfg["username"] if "@" in cfg["username"] else "",
                              required=True)
    cfg["auth_method"] = "password"
    return cfg


def ask_mfa(cfg: dict) -> None:
    """Hesapta iki adımlı doğrulama varsa yöntemi sorar."""
    if not ask_yes("Bu hesapta iki adımlı doğrulama (OTP / doğrulama kodu) açık mı?", False):
        cfg["auth_method"] = "password"
        cfg["totp_secret"] = ""
        return
    print("    Otomatik koşu için doğrulama uygulamasındaki Base32 gizli anahtar gerekir;")
    print("    yoksa test hesabında iki adımlı doğrulamanın kapatılması önerilir.")
    secret = getpass.getpass("  TOTP gizli anahtarı (Base32, görünmez; yoksa boş): ").strip().replace(" ", "")
    cfg["auth_method"] = "totp_password"
    cfg["mfa_method"] = "totp"
    cfg["totp_secret"] = secret


def fix_passwords(target: Path) -> None:
    if not target.exists():
        print(f"config.yaml yok: {target} — önce: python scripts/saha_kurulum.py")
        sys.exit(1)
    config = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    for name in ("ems", "gmail"):
        section = config.get(name) or {}
        if not section.get("username") or (name == "gmail" and not section.get("password")):
            continue
        print(f"\n── {name.upper()} ({section.get('username')}) ──")
        section["password"] = ask_secret("Şifre (görünmez)")
        if name == "ems":
            ask_mfa(section)
            if section.get("eas_url"):
                section["eas_url"] = normalize_eas_url(section["eas_url"])
        config[name] = section
    target.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    print(f"\n✔ Güncellendi: {target}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(REPO / "config.yaml"))
    parser.add_argument("--yeniden", action="store_true", help="config.yaml varsa da baştan sor")
    parser.add_argument("--sifre", action="store_true",
                        help="Yalnızca şifreleri (ve iki adımlı doğrulamayı) yeniden sor")
    args = parser.parse_args()
    target = Path(args.config)
    if args.sifre:
        fix_passwords(target)
        return
    if target.exists() and not args.yeniden:
        print(f"config.yaml zaten var, korunuyor: {target}")
        return

    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    print("\nSaha denemesi ayarları — yalnızca TEST hesaplarını kullanın.")
    print("Kutu senaryoları test kutusunda okundu/bayrak işaretler, mesaj taşır ve siler.")

    ems = server_section(config["ems"], "EMS (test edilen sunucu)", "")
    ems["label"] = "EMS"
    ask_mfa(ems)
    print("\n── EMS sertifikası ──")
    ca = ask("Kurum iç CA sertifikası (PEM) dosya yolu — yoksa boş bırakın", "")
    if ca:
        ems["tls_ca_file"] = ca
        ems["tls_verify"] = True
    else:
        ems["tls_verify"] = not ask_yes(
            "Sertifika doğrulaması kapatılsın mı? (kendinden imzalı sertifika hatası alırsanız; güvensiz)",
            False)
    print("\n── POP3 (Outlook masaüstü yolu) ──")
    pop_host = ask("POP3 sunucusu", ems["imap_host"])
    if pop_host != ems["imap_host"]:
        ems["pop_host"] = pop_host
    pop_port = ask("POP3 portu (995 = SSL)", "995")
    if pop_port != "995":
        ems["pop_port"] = int(pop_port)
    print("\n── Z-Push (iPad yolu) ──")
    eas = ask("Z-Push adresi (ör. https://posta.kurum.gov.tr/Microsoft-Server-ActiveSync) — "
              "boşsa Z-Push atlanır", "")
    if eas:
        ems["eas_url"] = normalize_eas_url(eas)
        if ems["eas_url"] != eas:
            print(f"    → {ems['eas_url']}")
        eas_user = ask("Z-Push kullanıcı adı farklıysa (ör. e-posta yerine kısa ad)", "")
        if eas_user:
            ems["eas_username"] = eas_user
    config["ems"] = ems

    print("\n── Gmail (dışarıdan gelen posta yolu, isteğe bağlı) ──")
    if ask_yes("Gmail test hesabı tanımlansın mı? (uygulama şifresi gerekir)", False):
        gmail = server_section(config["gmail"], "Gmail", "smtp.gmail.com")
        gmail.update({"imap_host": "imap.gmail.com", "auth_method": "app_password", "label": "Gmail"})
        config["gmail"] = gmail

    print("\n── Analiz ──")
    key = getpass.getpass("  Claude API key (boş bırakılırsa LLM'siz kip; görünmez): ").strip()
    if key:
        config["anthropic"]["api_key"] = key
        config["analysis"] = {"provider": "claude"}
    else:
        config["analysis"] = {"provider": "none"}

    test = config.setdefault("test", {})
    test["plan"] = "test_plan.yaml"
    test["wait_seconds"] = int(ask("Mesajın kutuya düşmesi için bekleme (sn)", "15"))
    test["max_retries"] = 4
    test["retry_interval"] = 10      # Z-Push değişiklikleri IMAP'ten periyodik okur

    target.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    print(f"\n✔ Yazıldı: {target} (şifreler bu dosyada; paylaşmayın)")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nİptal edildi.")
        sys.exit(1)

"""
mailbox_ops.py — Posta kutusu işlemleri ve kanallar arası durum eşitlemesi.

Mesaj teslim edildikten sonra kullanıcının kutuda yaptığı işlemler sınanır ve
sonucun DİĞER kanallarda görünüp görünmediği denetlenir:

  * Outlook (IMAP) okundu işaretledi → iPad'de (Z-Push) okundu görünüyor mu?
  * iPad'de okunmadı yapıldı → IMAP'te okunmamış mı?
  * Mesaj klasöre taşındı / silindi → iPad'in gelen kutusundan kalktı mı,
    hedef klasörde / Silinmiş Öğeler'de görünüyor mu?
  * Taslak sunucuya kaydedildi → iPad'in Taslaklar klasöründe görünüyor mu?
  * Sunucu tarafı arama Türkçe karakterlerle doğru sonuç veriyor mu?

Güvenlik: Posta kutusunda değişiklik yapılır (okundu/bayrak, taşıma, Çöp'e
atma, taslak ekleme, test klasörü oluşturma) — yalnızca TEST hesaplarında
kullanın. Düz EXPUNGE asla gönderilmez (kutudaki başka silinmiş mesajları da
kalıcı olarak yok ederdi); yalnızca UID MOVE ya da UIDPLUS ile UID EXPUNGE.
"""
from __future__ import annotations

import email
import re
import time
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from typing import Callable, Optional

import imap_folders
from scenario_catalog import mailbox_tokens
from verifier import _check, fingerprint

_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)", re.I)
_QUOTA_RE = re.compile(r"STORAGE\s+(\d+)\s+(\d+)", re.I)
EAS_TYPES = {"2": "Gelen Kutusu", "3": "Taslaklar", "4": "Silinmiş Öğeler", "5": "Gönderilmiş Öğeler"}


@dataclass
class MailboxContext:
    server_config: dict
    message_id: str
    run_id: str
    from_address: str
    connect: Callable                  # → oturum açılmış, INBOX seçili imaplib bağlantısı
    eas: object = None                 # EasReceiver (ActiveSync kanalı varsa)
    eas_server_id: Optional[str] = None
    pop3_read: bool = False            # POP3 kanalı mesajı indirmiş mi
    test_folder: str = "MTO-Test"
    retries: int = 3
    interval: float = 5.0
    notes: list = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
#  IMAP yardımcı sınıfı
# ────────────────────────────────────────────────────────────────────
class ImapBox:
    def __init__(self, imap):
        self.imap = imap
        self._folders: Optional[list] = None

    @property
    def caps(self) -> set:
        return {str(c).upper() for c in (getattr(self.imap, "capabilities", None) or ())}

    def folders(self, refresh: bool = False) -> list:
        if self._folders is None or refresh:
            self._folders = imap_folders.list_folders(self.imap)
        return self._folders

    def special(self, use: str) -> Optional[str]:
        return imap_folders.find_special(self.folders(), use)

    def uid(self, folder: str, message_id: str) -> Optional[bytes]:
        return imap_folders.uid_by_message_id(self.imap, folder, message_id)

    def select(self, folder: str) -> None:
        typ, data = self.imap.select(imap_folders.quote(folder))
        if typ != "OK":
            raise RuntimeError(f"Klasör seçilemedi: {folder} ({data})")

    def flags(self, folder: str, uid: bytes) -> set:
        self.select(folder)
        typ, data = self.imap.uid("FETCH", uid, "(FLAGS)")
        blob = b" ".join(part if isinstance(part, bytes) else b" ".join(
            p for p in part if isinstance(p, bytes)) for part in (data or []) if part)
        match = _FLAGS_RE.search(blob)
        if typ != "OK" or not match:
            raise RuntimeError(f"Bayraklar okunamadı (UID {uid!r})")
        return {f.decode("ascii", "replace") for f in match.group(1).split()}

    def store(self, folder: str, uid: bytes, op: str, flag: str) -> None:
        self.select(folder)
        typ, data = self.imap.uid("STORE", uid, op, f"({flag})")
        if typ != "OK":
            raise RuntimeError(f"Bayrak yazılamadı: {op} {flag} ({data})")

    def raw(self, folder: str, uid: bytes) -> bytes:
        self.select(folder)
        typ, data = self.imap.uid("FETCH", uid, "(BODY.PEEK[])")
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2:
                return item[1]
        raise RuntimeError(f"Mesaj indirilemedi (UID {uid!r})")

    def ensure_folder(self, name: str) -> bool:
        """Klasör yoksa oluşturur; oluşturulduysa True."""
        if any(f["name"] == name for f in self.folders()):
            return False
        typ, data = self.imap.create(imap_folders.quote(name))
        if typ != "OK":
            raise RuntimeError(f"Klasör oluşturulamadı: {name} ({data})")
        if hasattr(self.imap, "subscribe"):       # istemciler çoğunlukla yalnız abone klasörleri gösterir
            self.imap.subscribe(imap_folders.quote(name))
        self.folders(refresh=True)
        return True

    def move(self, uid: bytes, source: str, target: str) -> str:
        """Mesajı taşır; kullanılan yöntemi döndürür."""
        self.select(source)
        if "MOVE" in self.caps:
            typ, data = self.imap.uid("MOVE", uid, imap_folders.quote(target))
            if typ == "OK":
                return "UID MOVE"
        typ, data = self.imap.uid("COPY", uid, imap_folders.quote(target))
        if typ != "OK":
            raise RuntimeError(f"Kopyalanamadı: {target} ({data})")
        self.imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        if "UIDPLUS" in self.caps:
            self.imap.uid("EXPUNGE", uid)
            return "UID COPY + UID EXPUNGE"
        return "UID COPY (kaynakta \\Deleted işaretli kaldı — sunucu UIDPLUS desteklemiyor)"

    def search(self, folder: str, key: str, value: str) -> list:
        """Sunucu tarafı arama; ASCII dışı değer literal + CHARSET UTF-8 ile gönderilir."""
        self.select(folder)
        if value.isascii():
            typ, data = self.imap.uid("SEARCH", None, key, f'"{value}"')
        else:
            self.imap.literal = value.encode("utf-8")
            typ, data = self.imap.uid("SEARCH", "CHARSET", "UTF-8", key)
        if typ != "OK":
            raise RuntimeError(f"Arama reddedildi: {key} ({data})")
        return (data[0] or b"").split() if data else []

    def append(self, folder: str, raw: bytes, flags: str) -> None:
        typ, data = self.imap.append(imap_folders.quote(folder), flags, None, raw)
        if typ != "OK":
            raise RuntimeError(f"APPEND reddedildi: {folder} ({data})")

    def quota(self) -> Optional[tuple]:
        if "QUOTA" not in self.caps:
            return None
        typ, data = self.imap.getquotaroot("INBOX")
        match = _QUOTA_RE.search(repr(data))
        return (int(match.group(1)), int(match.group(2))) if typ == "OK" and match else None


# ────────────────────────────────────────────────────────────────────
#  ActiveSync yardımcıları
# ────────────────────────────────────────────────────────────────────
def _eas_poll(ctx: MailboxContext, condition: Callable[[], bool], sync: Callable[[], object]):
    """Z-Push değişikliği IMAP'ten periyodik olarak okur: koşul sağlanana kadar
    yeniden eşitlenir. (geçti mi, bekleme süresi sn) döndürür."""
    started = time.monotonic()
    for attempt in range(max(1, ctx.retries) + 1):
        sync()
        if condition():
            return True, round(time.monotonic() - started, 1)
        if attempt < ctx.retries:
            time.sleep(ctx.interval)
    return False, round(time.monotonic() - started, 1)


def _eas_item(ctx: MailboxContext) -> Optional[dict]:
    return ctx.eas.client.items.get(ctx.eas_server_id) if ctx.eas and ctx.eas_server_id else None


def _no_eas(name: str) -> dict:
    return _check(name, True, "ActiveSync kanalı yok (eas_url tanımlı değil) — iPad eşitlemesi sınanmadı",
                  hard=False)


def _eas_find_in(ctx: MailboxContext, folder_type: Optional[str] = None,
                 folder_name: Optional[str] = None):
    """Bir EAS klasöründe konusunda Run ID geçen öğe var mı? (bulundu, açıklama)"""
    client = ctx.eas.client
    client.folder_sync()
    folder = next((f for f in client.folders
                   if (folder_type and f["type"] == folder_type)
                   or (folder_name and f["name"] == folder_name)), None)
    if folder is None:
        return False, "klasör ActiveSync klasör listesinde yok"
    state_items = client._state(folder["id"])["items"]

    def found():
        return any(ctx.run_id in str(i.get("subject", "")) for i in state_items.values())
    ok, waited = _eas_poll(ctx, found, lambda: client.sync_collection(folder["id"]))
    return ok, (f"'{folder['name']}' klasöründe görünüyor ({waited} sn)" if ok
                else f"'{folder['name']}' klasöründe görünmüyor ({waited} sn beklendi)")


# ────────────────────────────────────────────────────────────────────
#  Senaryolar
# ────────────────────────────────────────────────────────────────────
def _require_inbox_uid(box: ImapBox, ctx: MailboxContext) -> bytes:
    uid = box.uid("INBOX", ctx.message_id)
    if not uid:
        raise RuntimeError("Mesaj gelen kutusunda bulunamadı (işlem öncesi)")
    return uid


def _search(box: ImapBox, ctx: MailboxContext) -> list:
    uid = _require_inbox_uid(box, ctx)
    tokens = mailbox_tokens(ctx.run_id)
    cases = [
        ("Arama: konu (ASCII)", "SUBJECT", tokens["subject_ascii"], True),
        ("Arama: konu (Türkçe)", "SUBJECT", tokens["subject_tr"], True),
        ("Arama: gövde (Türkçe)", "BODY", tokens["body_tr"], True),
        ("Arama: gönderen", "FROM", ctx.from_address, True),
        # Büyük harfle arama: 'I' Unicode'da 'i'ye küçülür, Türkçede 'ı'ya —
        # sunucunun Türkçe büyük/küçük harf duyarsızlığı burada görünür.
        ("Arama: gövde, BÜYÜK harf (Türkçe ı/I)", "BODY", tokens["body_tr_upper"], False),
    ]
    checks = []
    for name, key, value, hard in cases:
        try:
            hits = box.search("INBOX", key, value)
        except Exception as e:  # noqa: BLE001 — bir arama reddedilse diğerleri sürsün
            checks.append(_check(name, False, f"{key} {value!r}: {e}", hard=hard))
            continue
        ok = uid in hits
        checks.append(_check(name, ok, (f"{key} {value!r} → {len(hits)} sonuç, mesaj bulundu" if ok
                                        else f"{key} {value!r} → {len(hits)} sonuç, mesaj YOK"),
                             hard=hard))
    return checks


def _read_sync(box: ImapBox, ctx: MailboxContext) -> list:
    uid = _require_inbox_uid(box, ctx)
    checks = []
    initial = "\\Seen" in box.flags("INBOX", uid)
    state = "OKUNMUŞ" if initial else "okunmamış"
    if ctx.pop3_read:
        checks.append(_check("Okundu: POP3 indirmesinin etkisi", True,
                             f"POP3 ile indirildikten sonra IMAP'te {state} görünüyor — Outlook (POP3) "
                             "kullanıcısının indirmesi iPad'deki okunmamış durumunu "
                             + ("DEĞİŞTİRİYOR" if initial else "değiştirmiyor"), hard=False))
    else:
        checks.append(_check("Okundu: başlangıç durumu", True, f"Teslim edilen mesaj {state}",
                             hard=False))

    box.store("INBOX", uid, "-FLAGS", "\\Seen")
    box.store("INBOX", uid, "+FLAGS", "\\Seen")
    fresh = ImapBox(ctx.connect())
    try:
        kept = "\\Seen" in fresh.flags("INBOX", uid)
    finally:
        _logout(fresh.imap)
    checks.append(_check("Okundu: IMAP'te kalıcı", kept,
                         "Okundu işareti yeni oturumda da duruyor" if kept
                         else "Okundu işareti yeni oturumda KAYBOLMUŞ"))

    if ctx.eas is None:
        return checks + [_no_eas("Okundu: ActiveSync eşitlemesi")]
    if not ctx.eas_server_id:
        return checks + [_check("Okundu: ActiveSync eşitlemesi", True,
                                "Mesaj ActiveSync'te bulunamadığı için sınanmadı (kanal kontrolüne bakın)",
                                hard=False)]
    ok, waited = _eas_poll(ctx, lambda: (_eas_item(ctx) or {}).get("read") == "1",
                           ctx.eas.client.sync_inbox)
    checks.append(_check("Okundu: IMAP → ActiveSync", ok,
                         f"IMAP'te okundu işaretlenen mesaj iPad'e okundu olarak gitti ({waited} sn)"
                         if ok else f"iPad'de hâlâ okunmamış ({waited} sn beklendi)"))

    ctx.eas.client.set_read(ctx.eas_server_id, False)
    unseen, waited = _imap_poll(ctx, uid, lambda flags: "\\Seen" not in flags)
    checks.append(_check("Okundu: ActiveSync → IMAP", unseen,
                         f"iPad'de okunmadı yapılan mesaj IMAP'te okunmamış ({waited} sn)" if unseen
                         else f"IMAP'te hâlâ okunmuş ({waited} sn beklendi)"))
    box.store("INBOX", uid, "+FLAGS", "\\Seen")          # son durum: okunmuş (manuel adım buna bakar)
    return checks


def _imap_poll(ctx: MailboxContext, uid: bytes, condition: Callable[[set], bool]):
    started = time.monotonic()
    for attempt in range(max(1, ctx.retries) + 1):
        box = ImapBox(ctx.connect())
        try:
            if condition(box.flags("INBOX", uid)):
                return True, round(time.monotonic() - started, 1)
        finally:
            _logout(box.imap)
        if attempt < ctx.retries:
            time.sleep(ctx.interval)
    return False, round(time.monotonic() - started, 1)


def _flag_sync(box: ImapBox, ctx: MailboxContext) -> list:
    uid = _require_inbox_uid(box, ctx)
    box.store("INBOX", uid, "+FLAGS", "\\Flagged")
    fresh = ImapBox(ctx.connect())
    try:
        kept = "\\Flagged" in fresh.flags("INBOX", uid)
    finally:
        _logout(fresh.imap)
    checks = [_check("Bayrak: IMAP'te kalıcı", kept, "Bayrak (\\Flagged) yeni oturumda da duruyor"
                     if kept else "Bayrak yeni oturumda KAYBOLMUŞ")]
    if ctx.eas is None:
        return checks + [_no_eas("Bayrak: ActiveSync eşitlemesi")]
    if not ctx.eas_server_id:
        return checks + [_check("Bayrak: ActiveSync eşitlemesi", True,
                                "Mesaj ActiveSync'te bulunamadığı için sınanmadı", hard=False)]
    ok, waited = _eas_poll(ctx, lambda: (_eas_item(ctx) or {}).get("flag") == "2",
                           ctx.eas.client.sync_inbox)
    checks.append(_check("Bayrak: IMAP → ActiveSync", ok,
                         f"Bayrak iPad'e gitti (Flag/Status=2, {waited} sn)" if ok
                         else f"iPad'de bayrak görünmüyor (Flag/Status="
                              f"{(_eas_item(ctx) or {}).get('flag', '?')}, {waited} sn beklendi)"))
    return checks


def _moved_checks(box: ImapBox, ctx: MailboxContext, uid: bytes, target: str, label: str) -> list:
    original = box.raw("INBOX", uid)
    method = box.move(uid, "INBOX", target)
    checks = [_check(f"{label}: yöntem", True, method, hard=False)]
    new_uid = box.uid(target, ctx.message_id)
    same = bool(new_uid) and fingerprint(box.raw(target, new_uid)) == fingerprint(original)
    checks.append(_check(f"{label}: hedef klasörde", same,
                         f"Mesaj '{target}' klasöründe, içerik aynı" if same else
                         (f"Mesaj '{target}' klasöründe ama içerik DEĞİŞMİŞ" if new_uid
                          else f"Mesaj '{target}' klasöründe bulunamadı")))
    left = box.uid("INBOX", ctx.message_id)
    if left and "\\Deleted" in box.flags("INBOX", left):
        checks.append(_check(f"{label}: gelen kutusundan kalktı", True,
                             "Kaynakta \\Deleted işaretli (sunucu UID EXPUNGE desteklemediği için "
                             "kalıcı silme istemciye bırakıldı)", hard=False))
    else:
        checks.append(_check(f"{label}: gelen kutusundan kalktı", not left,
                             "Gelen kutusunda artık yok" if not left else "Gelen kutusunda HÂLÂ duruyor"))
    if ctx.eas is not None and ctx.eas_server_id:
        gone, waited = _eas_poll(ctx, lambda: ctx.eas_server_id not in ctx.eas.client.items,
                                 ctx.eas.client.sync_inbox)
        checks.append(_check(f"{label}: iPad gelen kutusundan kalktı", gone,
                             f"ActiveSync gelen kutusundan silindi ({waited} sn)" if gone
                             else f"iPad'in gelen kutusunda hâlâ görünüyor ({waited} sn beklendi)"))
    return checks


def _move(box: ImapBox, ctx: MailboxContext) -> list:
    uid = _require_inbox_uid(box, ctx)
    created = box.ensure_folder(ctx.test_folder)
    checks = [_check("Taşıma: test klasörü", True,
                     f"'{ctx.test_folder}' " + ("oluşturuldu" if created else "zaten vardı"), hard=False)]
    checks += _moved_checks(box, ctx, uid, ctx.test_folder, "Taşıma")
    if ctx.eas is None:
        return checks + [_no_eas("Taşıma: ActiveSync eşitlemesi")]
    leaf = ctx.test_folder.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    ok, detail = _eas_find_in(ctx, folder_name=leaf)
    checks.append(_check("Taşıma: iPad'de hedef klasörde", ok, detail))
    return checks


def _trash(box: ImapBox, ctx: MailboxContext) -> list:
    uid = _require_inbox_uid(box, ctx)
    trash = box.special("\\Trash")
    if not trash:
        return [_check("Silme: Çöp klasörü", False,
                       "Çöp/Silinmiş Öğeler klasörü bulunamadı (SPECIAL-USE \\Trash ya da yaygın ad yok) "
                       "— istemciler silinen mesajı nereye koyacağını bilemez")]
    checks = [_check("Silme: Çöp klasörü", True, f"'{trash}'")]
    checks += _moved_checks(box, ctx, uid, trash, "Silme")
    if ctx.eas is None:
        return checks + [_no_eas("Silme: ActiveSync eşitlemesi")]
    ok, detail = _eas_find_in(ctx, folder_type="4")
    checks.append(_check("Silme: iPad'de Silinmiş Öğeler'de", ok, detail))
    return checks


def draft_message(run_id: str, from_address: str) -> bytes:
    msg = MIMEText(f"Taslak eşitleme sınaması.\nTürkçe: ğüşıöç ĞÜŞİÖÇ\n\n[Run ID: {run_id}]",
                   "plain", "utf-8")
    msg["Message-ID"] = make_msgid()
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = f"[TASLAK] #{run_id} Taslak eşitleme ğüş"
    msg["From"] = from_address
    msg["To"] = from_address
    return msg.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _draft(box: ImapBox, ctx: MailboxContext) -> list:
    drafts = box.special("\\Drafts")
    if not drafts:
        return [_check("Taslak: Taslaklar klasörü", False,
                       "Taslaklar klasörü bulunamadı — istemciler taslağı sunucuya kaydedemez")]
    raw = draft_message(ctx.run_id, ctx.from_address)
    draft_id = " ".join(str(email.message_from_bytes(raw)["Message-ID"]).split())
    box.append(drafts, raw, "(\\Draft \\Seen)")
    uid = box.uid(drafts, draft_id)
    checks = [_check("Taslak: Taslaklar klasörü", True, f"'{drafts}'"),
              _check("Taslak: sunucuya kaydedildi", bool(uid),
                     "APPEND sonrası taslak bulundu" if uid else "APPEND sonrası taslak BULUNAMADI")]
    if uid:
        same = fingerprint(box.raw(drafts, uid)) == fingerprint(raw)
        checks.append(_check("Taslak: içerik aynı", same, "Taslak içeriği değişmeden saklandı" if same
                             else "Saklanan taslağın içeriği DEĞİŞMİŞ"))
        is_draft = "\\Draft" in box.flags(drafts, uid)
        checks.append(_check("Taslak: \\Draft bayrağı", is_draft, "Taslak bayrağı korunmuş" if is_draft
                             else "\\Draft bayrağı kaybolmuş — istemci mesajı taslak olarak göstermez"))
    if ctx.eas is None:
        return checks + [_no_eas("Taslak: ActiveSync eşitlemesi")]
    ok, detail = _eas_find_in(ctx, folder_type="3")
    checks.append(_check("Taslak: iPad'de Taslaklar'da", ok, detail))
    return checks


def _folders(box: ImapBox, ctx: MailboxContext) -> list:
    _require_inbox_uid(box, ctx)
    folders = box.folders()
    checks = [_check("Kutu yapısı: SPECIAL-USE", True,
                     "Sunucu özel klasörleri bayrakla bildiriyor (RFC 6154)"
                     if "SPECIAL-USE" in box.caps or any(
                         f2 in imap_folders.SPECIAL_USES for f in folders for f2 in f["flags"])
                     else "Sunucu SPECIAL-USE bildirmiyor — istemciler klasörleri adından tahmin eder",
                     hard=False)]
    for use, label, hard in (("\\Sent", "Gönderilmiş", True), ("\\Drafts", "Taslaklar", True),
                             ("\\Trash", "Çöp", True), ("\\Junk", "Gereksiz", False),
                             ("\\Archive", "Arşiv", False)):
        name = box.special(use)
        checks.append(_check(f"Kutu yapısı: {label} klasörü", bool(name),
                             f"'{name}'" if name else "bulunamadı", hard=hard))
    quota = box.quota()
    if quota:
        used, limit = quota
        pct = round(used / limit * 100, 1) if limit else 0
        checks.append(_check("Kutu yapısı: kota", True,
                             f"{used / 1024:.1f} MB / {limit / 1024:.1f} MB (%{pct})"
                             + (" — DOLMAK ÜZERE: yeni mesajlar reddedilebilir" if pct >= 90 else ""),
                             hard=False))
    else:
        checks.append(_check("Kutu yapısı: kota", True, "Sunucu QUOTA bildirmiyor", hard=False))
    if ctx.eas is None:
        return checks + [_no_eas("Kutu yapısı: ActiveSync klasörleri")]
    eas_folders = ctx.eas.client.folder_sync()
    types = {f["type"] for f in eas_folders}
    missing = [label for t, label in EAS_TYPES.items() if t not in types]
    checks.append(_check("Kutu yapısı: iPad'in gördüğü standart klasörler", not missing,
                         f"{len(eas_folders)} klasör; Gelen/Taslaklar/Silinmiş/Gönderilmiş tamam"
                         if not missing else "ActiveSync'te eksik: " + ", ".join(missing)))
    return checks


SCENARIOS = {
    "mbx_search": _search,
    "mbx_read_sync": _read_sync,
    "mbx_flag_sync": _flag_sync,
    "mbx_move": _move,
    "mbx_trash": _trash,
    "mbx_draft": _draft,
    "mbx_folders": _folders,
}


def _logout(imap) -> None:
    try:
        imap.logout()
    except Exception:  # noqa: BLE001
        pass


def run_mailbox_checks(scenario_key: str, ctx: MailboxContext) -> list:
    """Kutu senaryosunu çalıştırır; deterministik kontrol listesi döndürür.

    Beklenmeyen bir hata senaryoyu FAIL yapar ama koşuyu durdurmaz.
    """
    handler = SCENARIOS.get(scenario_key)
    if handler is None:
        return []
    imap = None
    try:
        imap = ctx.connect()
        return handler(ImapBox(imap), ctx)
    except Exception as e:  # noqa: BLE001
        return [_check("Kutu işlemi", False, f"{type(e).__name__}: {e}")]
    finally:
        if imap is not None:
            _logout(imap)

"""
test_mailbox_ops.py — Posta kutusu işlemleri, kanallar arası durum eşitlemesi
ve Junk tespiti.

Düzenek: bellek içi bir IMAP sunucusu (Backend + FakeImap) ve aynı kutuyu
ActiveSync olarak sunan sahte bir Z-Push (ZBack) — gerçek Z-Push gibi IMAP'in
ÜZERİNE kuruludur: IMAP'te değişen bayrak Z-Push'ta görünür, Z-Push'tan gelen
"okunmadı" komutu IMAP'e yazılır. Bozucu seçenekler (ignore_flags,
drop_flags...) sunucu hatalarını benzetir; her kontrolün hem sağlam durumda
geçtiği hem hatada FAIL verdiği kanıtlanır.
"""

import email
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import imap_folders  # noqa: E402
import main as main_mod  # noqa: E402
import mailbox_ops  # noqa: E402
from activesync import EasClient, EasReceiver, Node, wbxml_decode, wbxml_encode  # noqa: E402
from analyzer import NullAnalyzer  # noqa: E402
from plan_model import PlanError, load_plan, parse_plan  # noqa: E402
from receiver import MailReceiver  # noqa: E402
from sender import MailSender  # noqa: E402
from test_catalog_scenarios import Loopback, _server_cfg  # noqa: E402

CFG = {**_server_cfg("ems"), "eas_url": "https://zpush.test/Microsoft-Server-ActiveSync"}
MAILBOX_KEYS = ["mbx_search", "mbx_read_sync", "mbx_flag_sync", "mbx_move", "mbx_trash",
                "mbx_draft", "mbx_folders"]


# ═══════════════════════════════════════════════════════════════════
#  Bellek içi IMAP sunucusu
# ═══════════════════════════════════════════════════════════════════

def _decoded(raw: bytes, header: str) -> str:
    return " ".join(MailReceiver._decode_header(email.message_from_bytes(raw).get(header, "")).split())


def _body_text(raw: bytes) -> str:
    msg = email.message_from_bytes(raw)
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8")
    return ""


class Backend:
    def __init__(self, special_use=True, trash="Çöp Kutusu", sent="Sent", quota=(2048, 102400),
                 caps=("IMAP4REV1", "MOVE", "UIDPLUS", "SPECIAL-USE", "QUOTA")):
        self.folders = {"INBOX": [], "Drafts": [], "Junk": []}
        self.special = {"Drafts": "\\Drafts", "Junk": "\\Junk"}
        for name, use in ((trash, "\\Trash"), (sent, "\\Sent")):
            if name:
                self.folders[name] = []
                self.special[name] = use
        if not special_use:
            self.special = {}
        self.caps = tuple(caps)
        self.quota = quota
        self.next_uid = 1
        self.utf8_search = True          # False: CHARSET UTF-8 araması reddedilir
        self.drop_flags = set()          # bu bayraklar sessizce yazılmaz (kalıcılık hatası)
        self.strip_on_append = set()     # APPEND'de düşürülen bayraklar
        self.plain_expunge_calls = 0

    def deliver(self, raw: bytes, folder="INBOX", flags=()):
        msg = {"uid": self.next_uid, "raw": raw, "flags": set(flags)}
        self.next_uid += 1
        self.folders[folder].append(msg)
        return msg

    def find(self, message_id: str):
        core = " ".join(message_id.split())
        for folder, msgs in self.folders.items():
            for m in msgs:
                if " ".join(str(email.message_from_bytes(m["raw"])["Message-ID"]).split()) == core:
                    return folder, m
        return None, None


def _unquote(name):
    name = name.decode() if isinstance(name, bytes) else str(name)
    if name.startswith('"') and name.endswith('"'):
        name = re.sub(r'\\(.)', r"\1", name[1:-1])
    return imap_folders.utf7_decode(name)


class FakeImap:
    def __init__(self, backend: Backend):
        self.b = backend
        self.capabilities = backend.caps
        self.literal = None
        self.selected = "INBOX"
        self.logged_out = False

    def _msgs(self):
        return self.b.folders[self.selected]

    def _get(self, uid):
        return next((m for m in self._msgs() if m["uid"] == int(uid)), None)

    def select(self, mailbox="INBOX", readonly=False):
        name = _unquote(mailbox)
        if name not in self.b.folders:
            return "NO", [b"Mailbox does not exist"]
        self.selected = name
        return "OK", [str(len(self._msgs())).encode()]

    def list(self, directory='""', pattern="*"):
        lines = []
        for name in self.b.folders:
            flags = ["\\HasNoChildren"] + ([self.b.special[name]] if name in self.b.special else [])
            lines.append(f'({" ".join(flags)}) "/" "{imap_folders.utf7_encode(name)}"'.encode())
        return "OK", lines

    def create(self, mailbox):
        self.b.folders.setdefault(_unquote(mailbox), [])
        return "OK", [b"CREATE completed"]

    def subscribe(self, mailbox):
        return "OK", [b"SUBSCRIBE completed"]

    def _search(self, args):
        tokens = list(args)
        if tokens and tokens[0] is None:
            tokens = tokens[1:]
        if tokens[:2] == ["CHARSET", "UTF-8"]:
            if not self.b.utf8_search:
                self.literal = None
                return "NO", [b"[BADCHARSET] UTF-8 desteklenmiyor"]
            tokens = tokens[2:]
        key = tokens[0].upper()
        if key == "HEADER":
            value = tokens[2].strip('"')
            hits = [m for m in self._msgs() if value in str(email.message_from_bytes(m["raw"]).get(tokens[1], ""))]
        else:
            value = self.literal.decode("utf-8") if self.literal is not None else tokens[1].strip('"')
            self.literal = None
            field = {"SUBJECT": lambda r: _decoded(r, "Subject"), "FROM": lambda r: _decoded(r, "From"),
                     "BODY": _body_text, "TEXT": _body_text}[key]
            # Python lower(): 'I' → 'i' (Türkçe 'ı' değil) — gerçek sunucuların çoğu gibi
            hits = [m for m in self._msgs() if value.lower() in field(m["raw"]).lower()]
        return "OK", [b" ".join(str(m["uid"]).encode() for m in hits)]

    def uid(self, command, *args):
        command = command.upper()
        if command == "SEARCH":
            return self._search(args)
        if command == "FETCH":
            m = self._get(args[0])
            if m is None:
                return "OK", [None]
            if args[1] == "(FLAGS)":
                return "OK", [f'1 (UID {m["uid"]} FLAGS ({" ".join(sorted(m["flags"]))}))'.encode()]
            return "OK", [(f'1 (UID {m["uid"]} BODY[] {{{len(m["raw"])}}}'.encode(), m["raw"]), b")"]
        if command == "STORE":
            m = self._get(args[0])
            flags = set(args[2].strip("()").split()) - self.b.drop_flags
            if args[1].startswith("+"):
                m["flags"] |= flags
            else:
                m["flags"] -= flags
            return "OK", [b"STORE completed"]
        if command in ("MOVE", "COPY"):
            if command == "MOVE" and "MOVE" not in self.b.caps:
                return "BAD", [b"Unknown command"]
            m = self._get(args[0])
            target = _unquote(args[1])
            if target not in self.b.folders:
                return "NO", [b"[TRYCREATE]"]
            self.b.deliver(m["raw"], target, m["flags"] - {"\\Deleted"})
            if command == "MOVE":
                self._msgs().remove(m)
            return "OK", [b"done"]
        if command == "EXPUNGE":
            self.b.folders[self.selected] = [m for m in self._msgs()
                                             if not (m["uid"] == int(args[0]) and "\\Deleted" in m["flags"])]
            return "OK", [b"EXPUNGE completed"]
        raise AssertionError(f"beklenmeyen UID komutu {command}")

    def expunge(self):
        self.b.plain_expunge_calls += 1         # asla çağrılmamalı
        return "OK", [b""]

    def append(self, mailbox, flags, date_time, message):
        wanted = set(flags.strip("()").split()) - self.b.strip_on_append
        self.b.deliver(message, _unquote(mailbox), wanted)
        return "OK", [b"APPEND completed"]

    def getquotaroot(self, mailbox):
        used, limit = self.b.quota
        return "OK", [[b'"INBOX" ""'], [f'"" (STORAGE {used} {limit})'.encode()]]

    # MailReceiver'ın teslim yolu (Junk testi)
    def search(self, charset, *criteria):
        if criteria[0] == "SINCE":
            return "OK", [b""]
        return self._search((None, *criteria))

    def fetch(self, ids, parts):
        return self.uid("FETCH", ids, "(BODY.PEEK[])")

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]


# ═══════════════════════════════════════════════════════════════════
#  IMAP'in üzerine kurulu sahte Z-Push
# ═══════════════════════════════════════════════════════════════════

EAS_TYPE = {"\\Drafts": "3", "\\Trash": "4", "\\Sent": "5"}


class ZBack:
    def __init__(self, backend: Backend):
        self.b = backend
        self.snapshots: dict = {}       # SyncKey → {ServerId: (okundu, bayrak)}
        self.key = 0
        self.ignore_flags = False       # IMAP bayrak değişiklikleri Z-Push'a yansımıyor
        self.ignore_client_read = False  # iPad'in okundu komutu IMAP'e yazılmıyor
        self.requests: list = []

    def transport(self):
        return httpx.MockTransport(self.handle)

    def _folders(self):
        out = []
        for i, name in enumerate(self.b.folders):
            ftype = "2" if name == "INBOX" else EAS_TYPE.get(self.b.special.get(name), "12")
            out.append({"id": f"f{i}", "name": name.rsplit("/", 1)[-1], "type": ftype, "imap": name})
        return out

    def _folder(self, fid):
        return next(f for f in self._folders() if f["id"] == fid)

    def _state(self, fid):
        out = {}
        for m in self.b.folders[self._folder(fid)["imap"]]:
            if "\\Deleted" in m["flags"]:
                continue
            flags = set() if self.ignore_flags else m["flags"]
            out[f"{fid}:{m['uid']}"] = ("1" if "\\Seen" in flags else "0",
                                        "2" if "\\Flagged" in flags else "0")
        return out

    def _msg(self, sid):
        fid, uid = sid.split(":")
        return next(m for m in self.b.folders[self._folder(fid)["imap"]] if m["uid"] == int(uid))

    def _wbxml(self, node):
        return httpx.Response(200, content=wbxml_encode(node))

    def handle(self, request):
        self.requests.append(request)
        if request.method == "OPTIONS":
            return httpx.Response(200, headers={"MS-ASProtocolVersions": "12.1,14.0"})
        cmd = parse_qs(request.url.query.decode())["Cmd"][0]
        root = wbxml_decode(request.content)
        if cmd == "FolderSync":
            return self._wbxml(Node(
                "FolderHierarchy:FolderSync", Node("FolderHierarchy:Status", data="1"),
                Node("FolderHierarchy:SyncKey", data="1"),
                Node("FolderHierarchy:Changes", *[Node(
                    "FolderHierarchy:Add", Node("FolderHierarchy:ServerId", data=f["id"]),
                    Node("FolderHierarchy:ParentId", data="0"),
                    Node("FolderHierarchy:DisplayName", data=f["name"]),
                    Node("FolderHierarchy:Type", data=f["type"])) for f in self._folders()])))
        if cmd == "Sync":
            col = root.find("AirSync:Collections/AirSync:Collection")
            fid, key = col.value("AirSync:CollectionId"), col.value("AirSync:SyncKey")
            for change in (col.find("AirSync:Commands") or Node("x")).findall("AirSync:Change"):
                read = change.value("AirSync:ApplicationData/Email:Read")
                if read and not self.ignore_client_read:
                    m = self._msg(change.value("AirSync:ServerId"))
                    m["flags"] = (m["flags"] | {"\\Seen"}) if read == "1" else (m["flags"] - {"\\Seen"})
            prev = {} if key == "0" else self.snapshots.get(key)
            if prev is None:
                return self._wbxml(Node("AirSync:Sync", Node("AirSync:Collections", Node(
                    "AirSync:Collection", Node("AirSync:CollectionId", data=fid),
                    Node("AirSync:Status", data="3")))))
            current = {} if key == "0" else self._state(fid)
            commands = []
            for sid, (read, flag) in current.items():
                if sid not in prev:
                    raw = self._msg(sid)["raw"]
                    commands.append(Node("AirSync:Add", Node("AirSync:ServerId", data=sid), Node(
                        "AirSync:ApplicationData", Node("Email:Subject", data=_decoded(raw, "Subject")),
                        Node("Email:From", data=_decoded(raw, "From")),
                        Node("Email:Read", data=read),
                        Node("Email:Flag", Node("Email:Status", data=flag)) if flag == "2" else None)))
                elif prev[sid] != (read, flag):
                    commands.append(Node("AirSync:Change", Node("AirSync:ServerId", data=sid), Node(
                        "AirSync:ApplicationData", Node("Email:Read", data=read),
                        Node("Email:Flag", Node("Email:Status", data=flag)) if flag == "2"
                        else Node("Email:Flag"))))
            commands += [Node("AirSync:Delete", Node("AirSync:ServerId", data=sid))
                         for sid in prev if sid not in current]
            self.key += 1
            self.snapshots[str(self.key)] = current
            return self._wbxml(Node("AirSync:Sync", Node("AirSync:Collections", Node(
                "AirSync:Collection", Node("AirSync:SyncKey", data=str(self.key)),
                Node("AirSync:CollectionId", data=fid), Node("AirSync:Status", data="1"),
                Node("AirSync:Commands", *commands) if commands else None))))
        if cmd == "ItemOperations":
            fetch = root.find("ItemOperations:Fetch")
            m = self._msg(fetch.value("AirSync:ServerId"))
            mime = fetch.find("ItemOperations:Options").value("AirSync:MIMESupport") == "2"
            data = m["raw"] if mime else _body_text(m["raw"]).encode("utf-8")
            return self._wbxml(Node("ItemOperations:ItemOperations",
                                    Node("ItemOperations:Status", data="1"),
                                    Node("ItemOperations:Response", Node(
                                        "ItemOperations:Fetch", Node("ItemOperations:Status", data="1"),
                                        Node("ItemOperations:Properties", Node(
                                            "AirSyncBase:Body",
                                            Node("AirSyncBase:Type", data="4" if mime else "1"),
                                            Node("AirSyncBase:Data", data=data)))))))
        return httpx.Response(501)


# ═══════════════════════════════════════════════════════════════════
#  Düzenek: SMTP (loopback) → IMAP kutusu; aynı kutu Z-Push'tan da okunur
# ═══════════════════════════════════════════════════════════════════

class PopChannel:
    """POP3 indirmesini benzetir: bazı sunucular RETR'de mesajı \\Seen yapar."""
    def __init__(self, backend, marks_seen):
        self.b, self.marks_seen, self.last_error = backend, marks_seen, None

    def wait_for_message(self, expected_msg_id, **_kw):
        _, m = self.b.find(expected_msg_id)
        if m is None:
            return None
        if self.marks_seen:
            m["flags"].add("\\Seen")
        return MailReceiver._extract_details(MailReceiver(CFG), email.message_from_bytes(m["raw"]),
                                             m["raw"], "pop3")


def run(scenario, tmp_path, monkeypatch, backend=None, zpush=None, eas=True, pop=None,
        deliver_to="INBOX", connect_error=None):
    monkeypatch.chdir(tmp_path)
    main_mod.prepare_test_files()
    backend = backend or Backend()
    zpush = zpush or ZBack(backend)
    loop = Loopback()
    sender = MailSender(_server_cfg("gmail"))
    receiver = MailReceiver(CFG if eas else _server_cfg("ems"))
    extra = []
    if pop is not None:
        extra.append(("pop3", PopChannel(backend, pop)))
    if eas:
        extra.append(("activesync", EasReceiver(CFG, transport=zpush.transport())))
    receiver.extra_channels = extra

    def connect():
        if connect_error:
            raise connect_error
        return FakeImap(backend)

    def imap_wait(**kw):
        raw = loop.store.get(" ".join(kw["expected_msg_id"].split()))
        if raw is None:
            return None
        backend.deliver(raw, deliver_to)
        if deliver_to != "INBOX":
            return None
        return receiver._extract_details(email.message_from_bytes(raw), raw, "1")

    combo = SimpleNamespace(label="Gmail → EMS", receiver_server="ems", receiver_client="iPad",
                            sender_server="gmail", sender_client="SMTP", scenarios={})
    cfg = {"wait_seconds": 0, "max_retries": 2, "retry_interval": 0}
    with patch.object(MailSender, "_connect", side_effect=lambda: loop.smtp()), \
         patch.object(MailReceiver, "_connect", side_effect=connect), \
         patch.object(MailReceiver, "wait_for_message", side_effect=imap_wait), \
         patch.object(mailbox_ops.time, "sleep"), patch.object(main_mod.time, "sleep"):
        result = main_mod.run_scenario(scenario, combo, 0, sender, receiver, NullAnalyzer(), cfg)
    return result, backend, zpush


def checks(result):
    return {c["name"]: c for c in result["analysis"]["checks"]}


def failed(result):
    return [c["name"] for c in result["analysis"]["checks"] if not c["passed"]]


# ═══════════════════════════════════════════════════════════════════
#  Sağlam sunucu: her kutu senaryosu PASS (Z-Push dahil)
# ═══════════════════════════════════════════════════════════════════

class TestCleanServer:

    @pytest.mark.parametrize("scenario", MAILBOX_KEYS)
    def test_passes_with_activesync(self, scenario, tmp_path, monkeypatch):
        result, _, _ = run(scenario, tmp_path, monkeypatch)
        assert result["analysis"]["passed"] is True, failed(result)
        names = [c["name"] for c in result["analysis"]["checks"]]
        assert any("activesync" in n.lower() or "ipad" in n.lower() for n in names), names

    @pytest.mark.parametrize("scenario", MAILBOX_KEYS)
    def test_passes_without_activesync_and_says_so(self, scenario, tmp_path, monkeypatch):
        result, _, _ = run(scenario, tmp_path, monkeypatch, eas=False)
        assert result["analysis"]["passed"] is True, failed(result)
        if scenario != "mbx_search":
            assert any("ActiveSync kanalı yok" in c["detail"] for c in result["analysis"]["checks"])

    def test_read_sync_both_directions_and_final_state(self, tmp_path, monkeypatch):
        result, backend, _ = run("mbx_read_sync", tmp_path, monkeypatch)
        c = checks(result)
        assert c["[Kesin] Okundu: IMAP → ActiveSync"]["passed"]
        assert c["[Kesin] Okundu: ActiveSync → IMAP"]["passed"]
        assert c["[Bilgi] Okundu: başlangıç durumu"]["detail"] == "Teslim edilen mesaj okunmamış"
        _, m = backend.find(result["send_meta"]["msg_id"])
        assert "\\Seen" in m["flags"]                   # manuel adım "okunmuş" bekler

    def test_move_leaves_message_in_test_folder(self, tmp_path, monkeypatch):
        result, backend, _ = run("mbx_move", tmp_path, monkeypatch)
        folder, _ = backend.find(result["send_meta"]["msg_id"])
        assert folder == "MTO-Test"
        c = checks(result)
        assert c["[Bilgi] Taşıma: yöntem"]["detail"] == "UID MOVE"
        assert "oluşturuldu" in c["[Bilgi] Taşıma: test klasörü"]["detail"]
        assert c["[Kesin] Taşıma: iPad'de hedef klasörde"]["passed"]

    def test_trash_uses_turkish_special_use_folder(self, tmp_path, monkeypatch):
        result, backend, _ = run("mbx_trash", tmp_path, monkeypatch)
        assert backend.find(result["send_meta"]["msg_id"])[0] == "Çöp Kutusu"
        assert checks(result)["[Kesin] Silme: Çöp klasörü"]["detail"] == "'Çöp Kutusu'"
        assert "Çöp Kutusu" in checks(result)["[Kesin] Silme: iPad'de Silinmiş Öğeler'de"]["detail"]

    def test_draft_is_stored_with_flag_and_visible_on_ipad(self, tmp_path, monkeypatch):
        result, backend, _ = run("mbx_draft", tmp_path, monkeypatch)
        (draft,) = backend.folders["Drafts"]
        assert draft["flags"] == {"\\Draft", "\\Seen"}
        assert f"#{result['result_id']}" in _decoded(draft["raw"], "Subject")

    def test_folders_reports_quota(self, tmp_path, monkeypatch):
        result, _, _ = run("mbx_folders", tmp_path, monkeypatch)
        assert checks(result)["[Bilgi] Kutu yapısı: kota"]["detail"] == "2.0 MB / 100.0 MB (%2.0)"

    def test_mailbox_message_contains_search_tokens(self, tmp_path, monkeypatch):
        result, backend, _ = run("mbx_search", tmp_path, monkeypatch, eas=False)
        _, m = backend.find(result["send_meta"]["msg_id"])
        assert "Öğretmenoğlu" in _decoded(m["raw"], "Subject")
        assert f"çığır{result['result_id']}" in _body_text(m["raw"])


# ═══════════════════════════════════════════════════════════════════
#  Sunucu hataları: ilgili kontrol FAIL verir
# ═══════════════════════════════════════════════════════════════════

class TestServerFaults:

    def test_zpush_not_seeing_imap_read_flag(self, tmp_path, monkeypatch):
        backend = Backend()
        z = ZBack(backend)
        z.ignore_flags = True
        result, _, _ = run("mbx_read_sync", tmp_path, monkeypatch, backend, z)
        assert "[Kesin] Okundu: IMAP → ActiveSync" in failed(result)
        assert result["analysis"]["passed"] is False

    def test_ipad_unread_not_written_back(self, tmp_path, monkeypatch):
        backend = Backend()
        z = ZBack(backend)
        z.ignore_client_read = True
        result, _, _ = run("mbx_read_sync", tmp_path, monkeypatch, backend, z)
        assert failed(result) == ["[Kesin] Okundu: ActiveSync → IMAP"]

    def test_read_flag_not_persisted(self, tmp_path, monkeypatch):
        backend = Backend()
        backend.drop_flags = {"\\Seen"}
        result, _, _ = run("mbx_read_sync", tmp_path, monkeypatch, backend, eas=False)
        assert failed(result) == ["[Kesin] Okundu: IMAP'te kalıcı"]

    @pytest.mark.parametrize("marks_seen, word", [(True, "DEĞİŞTİRİYOR"), (False, "değiştirmiyor")])
    def test_pop3_download_effect_is_reported(self, tmp_path, monkeypatch, marks_seen, word):
        result, _, _ = run("mbx_read_sync", tmp_path, monkeypatch, pop=marks_seen)
        info = checks(result)["[Bilgi] Okundu: POP3 indirmesinin etkisi"]
        assert word in info["detail"] and result["analysis"]["passed"] is True

    def test_flag_not_synced_to_ipad(self, tmp_path, monkeypatch):
        backend = Backend()
        z = ZBack(backend)
        z.ignore_flags = True
        result, _, _ = run("mbx_flag_sync", tmp_path, monkeypatch, backend, z)
        assert failed(result) == ["[Kesin] Bayrak: IMAP → ActiveSync"]

    def test_turkish_search_rejected_by_server(self, tmp_path, monkeypatch):
        backend = Backend()
        backend.utf8_search = False
        result, _, _ = run("mbx_search", tmp_path, monkeypatch, backend, eas=False)
        assert failed(result) == ["[Kesin] Arama: konu (Türkçe)", "[Kesin] Arama: gövde (Türkçe)",
                                  "[Bilgi] Arama: gövde, BÜYÜK harf (Türkçe ı/I)"]
        assert "BADCHARSET" in checks(result)["[Kesin] Arama: konu (Türkçe)"]["detail"]

    def test_turkish_uppercase_i_is_info_only(self, tmp_path, monkeypatch):
        result, _, _ = run("mbx_search", tmp_path, monkeypatch, eas=False)
        upper = checks(result)["[Bilgi] Arama: gövde, BÜYÜK harf (Türkçe ı/I)"]
        assert upper["passed"] is False and result["analysis"]["passed"] is True

    def test_no_trash_folder(self, tmp_path, monkeypatch):
        result, _, _ = run("mbx_trash", tmp_path, monkeypatch, Backend(trash=None), eas=False)
        assert failed(result) == ["[Kesin] Silme: Çöp klasörü"]

    def test_trash_found_by_name_without_special_use(self, tmp_path, monkeypatch):
        backend = Backend(special_use=False, trash="Silinmiş Öğeler",
                          caps=("IMAP4REV1", "MOVE", "UIDPLUS"))
        result, _, _ = run("mbx_trash", tmp_path, monkeypatch, backend, eas=False)
        assert result["analysis"]["passed"] is True, failed(result)
        assert backend.find(result["send_meta"]["msg_id"])[0] == "Silinmiş Öğeler"

    def test_move_without_move_extension_uses_uid_expunge(self, tmp_path, monkeypatch):
        backend = Backend(caps=("IMAP4REV1", "UIDPLUS", "SPECIAL-USE"))
        result, _, _ = run("mbx_move", tmp_path, monkeypatch, backend)
        assert result["analysis"]["passed"] is True, failed(result)
        assert checks(result)["[Bilgi] Taşıma: yöntem"]["detail"] == "UID COPY + UID EXPUNGE"
        assert backend.plain_expunge_calls == 0

    def test_move_without_uidplus_never_expunges(self, tmp_path, monkeypatch):
        backend = Backend(caps=("IMAP4REV1", "SPECIAL-USE"))
        # Kullanıcının daha önce silmiş olduğu başka bir mesaj: dokunulmamalı
        other = backend.deliver(b"Message-ID: <baska@x>\r\nSubject: eski\r\n\r\nx", flags={"\\Deleted"})
        result, _, _ = run("mbx_move", tmp_path, monkeypatch, backend, eas=False)
        assert result["analysis"]["passed"] is True, failed(result)
        assert "UIDPLUS" in checks(result)["[Bilgi] Taşıma: yöntem"]["detail"]
        assert backend.plain_expunge_calls == 0 and other in backend.folders["INBOX"]

    def test_draft_flag_lost(self, tmp_path, monkeypatch):
        backend = Backend()
        backend.strip_on_append = {"\\Draft"}
        result, _, _ = run("mbx_draft", tmp_path, monkeypatch, backend, eas=False)
        assert failed(result) == ["[Kesin] Taslak: \\Draft bayrağı"]

    def test_missing_sent_folder(self, tmp_path, monkeypatch):
        result, _, _ = run("mbx_folders", tmp_path, monkeypatch, Backend(sent=None))
        assert failed(result) == ["[Kesin] Kutu yapısı: Gönderilmiş klasörü",
                                  "[Bilgi] Kutu yapısı: Arşiv klasörü",      # bilgi; kararı etkilemez
                                  "[Kesin] Kutu yapısı: iPad'in gördüğü standart klasörler"]

    def test_no_quota_capability_is_info(self, tmp_path, monkeypatch):
        backend = Backend(caps=("IMAP4REV1", "MOVE", "UIDPLUS", "SPECIAL-USE"))
        result, _, _ = run("mbx_folders", tmp_path, monkeypatch, backend, eas=False)
        assert checks(result)["[Bilgi] Kutu yapısı: kota"]["detail"] == "Sunucu QUOTA bildirmiyor"

    def test_nearly_full_quota_is_highlighted(self, tmp_path, monkeypatch):
        result, _, _ = run("mbx_folders", tmp_path, monkeypatch, Backend(quota=(95000, 100000)),
                           eas=False)
        assert "DOLMAK ÜZERE" in checks(result)["[Bilgi] Kutu yapısı: kota"]["detail"]

    def test_unexpected_error_fails_scenario_but_not_run(self, tmp_path, monkeypatch):
        result, _, _ = run("mbx_move", tmp_path, monkeypatch, eas=False,
                           connect_error=OSError("bağlantı koptu"))
        assert failed(result) == ["[Kesin] Kutu işlemi"]
        assert "bağlantı koptu" in checks(result)["[Kesin] Kutu işlemi"]["detail"]


# ═══════════════════════════════════════════════════════════════════
#  Junk tespiti
# ═══════════════════════════════════════════════════════════════════

class TestJunkDetection:

    def test_receiver_finds_junk_folder(self):
        backend = Backend()
        raw = b"Message-ID: <spam@x>\r\nSubject: s\r\n\r\nx"
        backend.deliver(raw, "Junk")
        receiver = MailReceiver(_server_cfg("ems"))
        with patch.object(MailReceiver, "_connect", side_effect=lambda: FakeImap(backend)), \
             patch("receiver.time.sleep"):
            assert receiver.wait_for_message("<spam@x>", "", wait_seconds=0, max_retries=1) is None
        assert receiver.junk_folder == "Junk" and receiver.last_error is None

    def test_junk_found_by_turkish_name(self):
        backend = Backend(special_use=False)
        backend.folders["Gereksiz E-posta"] = []
        backend.deliver(b"Message-ID: <spam@x>\r\n\r\nx", "Gereksiz E-posta")
        receiver = MailReceiver(_server_cfg("ems"))
        with patch.object(MailReceiver, "_connect", side_effect=lambda: FakeImap(backend)), \
             patch("receiver.time.sleep"):
            receiver.wait_for_message("<spam@x>", "", wait_seconds=0, max_retries=1)
        assert receiver.junk_folder == "Gereksiz E-posta"

    def test_orchestrator_reports_junk(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        main_mod.prepare_test_files()
        backend, loop = Backend(), Loopback()
        sender, receiver = MailSender(_server_cfg("gmail")), MailReceiver(_server_cfg("ems"))
        real_wait = MailReceiver.wait_for_message

        def deliver_to_junk(self, **kw):
            raw = loop.store[" ".join(kw["expected_msg_id"].split())]
            backend.deliver(raw, "Junk")
            return real_wait(self, **kw)
        combo = SimpleNamespace(label="Gmail → EMS", receiver_server="ems", receiver_client="iPad",
                                sender_server="gmail", sender_client="SMTP", scenarios={})
        with patch.object(MailSender, "_connect", side_effect=lambda: loop.smtp()), \
             patch.object(MailReceiver, "_connect", side_effect=lambda: FakeImap(backend)), \
             patch.object(MailReceiver, "wait_for_message", autospec=True, side_effect=deliver_to_junk), \
             patch("receiver.time.sleep"), patch.object(main_mod.time, "sleep"):
            result = main_mod.run_scenario("plain_text", combo, 0, sender, receiver, NullAnalyzer(),
                                           {"wait_seconds": 0, "max_retries": 1, "retry_interval": 0})
        assert result["analysis"]["passed"] is False and result["junk_folder"] == "Junk"
        assert failed(result) == ["Teslim (Gelen Kutusu)"]
        assert "'Junk' klasörüne düştü" in result["analysis"]["summary"]

    def test_not_found_anywhere_stays_timeout(self):
        receiver = MailReceiver(_server_cfg("ems"))
        with patch.object(MailReceiver, "_connect", side_effect=lambda: FakeImap(Backend())), \
             patch("receiver.time.sleep"):
            receiver.wait_for_message("<yok@x>", "", wait_seconds=0, max_retries=1)
        assert receiver.junk_folder is None


# ═══════════════════════════════════════════════════════════════════
#  Birimler: klasör adları, ActiveSync değişiklik komutları, plan
# ═══════════════════════════════════════════════════════════════════

class TestFolderNames:

    @pytest.mark.parametrize("name", ["Çöp Kutusu", "Gönderilmiş Öğeler", "A&B", "日本語", "INBOX"])
    def test_utf7_roundtrip(self, name):
        assert imap_folders.utf7_decode(imap_folders.utf7_encode(name)) == name

    def test_known_utf7_values(self):
        assert imap_folders.utf7_encode("Çöp Kutusu") == "&AMcA9g-p Kutusu"
        assert imap_folders.utf7_encode("A&B") == "A&-B"
        assert imap_folders.quote('a"b') == '"a\\"b"'

    def test_list_parsing_and_special_folders(self):
        folders = imap_folders.parse_list([
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Trash) "/" "&AMcA9g-p Kutusu"',
            b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
            b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
            b'(\\HasNoChildren) "." INBOX.Junk',
            (b'(\\HasNoChildren) "/" {8}', b'Taslakla'),
            b'(\\HasNoChildren) NIL "Drafts"', b"bozuk satir", None])
        assert [f["name"] for f in folders] == ["INBOX", "Çöp Kutusu", "[Gmail]", "[Gmail]/Sent Mail",
                                                "INBOX.Junk", "Taslakla", "Drafts"]
        assert imap_folders.find_special(folders, "\\Trash") == "Çöp Kutusu"
        assert imap_folders.find_special(folders, "\\Sent") == "[Gmail]/Sent Mail"
        assert imap_folders.find_special(folders, "\\Junk") == "INBOX.Junk"      # ada göre
        assert imap_folders.find_special(folders, "\\Drafts") == "Drafts"
        assert imap_folders.find_special(folders, "\\Archive") is None


class TestEasChanges:

    def _client(self, backend):
        z = ZBack(backend)
        return EasClient(CFG, transport=z.transport()), z

    def test_changes_update_read_and_flag(self):
        backend = Backend()
        m = backend.deliver(b"Message-ID: <a@x>\r\nSubject: s\r\n\r\nx")
        client, _ = self._client(backend)
        client.sync_inbox()
        (sid,) = client.items
        assert client.items[sid]["read"] == "0" and client.items[sid]["flag"] == "0"
        m["flags"] |= {"\\Seen", "\\Flagged"}
        client.sync_inbox()
        assert client.items[sid]["read"] == "1" and client.items[sid]["flag"] == "2"
        m["flags"] -= {"\\Flagged"}
        client.sync_inbox()
        assert client.items[sid]["flag"] == "0"          # boş <Flag/> = kaldırıldı

    def test_set_read_sends_change_command(self):
        backend = Backend()
        m = backend.deliver(b"Message-ID: <a@x>\r\nSubject: s\r\n\r\nx", flags={"\\Seen"})
        client, z = self._client(backend)
        client.sync_inbox()
        (sid,) = client.items
        client.set_read(sid, False)
        assert "\\Seen" not in m["flags"] and client.items[sid]["read"] == "0"
        sent = wbxml_decode(z.requests[-1].content)
        change = sent.find("AirSync:Collections/AirSync:Collection/AirSync:Commands/AirSync:Change")
        assert change.value("AirSync:ApplicationData/Email:Read") == "0"

    def test_other_folders_have_their_own_state(self):
        backend = Backend()
        backend.deliver(b"Message-ID: <a@x>\r\nSubject: gelen\r\n\r\nx")
        backend.deliver(b"Message-ID: <b@x>\r\nSubject: taslak\r\n\r\nx", "Drafts", {"\\Draft"})
        client, _ = self._client(backend)
        client.sync_inbox()
        drafts = client.folder_id("3")
        client.sync_collection(drafts)
        assert [i["subject"] for i in client._state(drafts)["items"].values()] == ["taslak"]
        assert [i["subject"] for i in client.items.values()] == ["gelen"]

    def test_rejected_command_raises(self):
        def handler(request):
            if request.method == "OPTIONS":
                return httpx.Response(200, headers={"MS-ASProtocolVersions": "14.0"})
            cmd = parse_qs(request.url.query.decode())["Cmd"][0]
            if cmd == "FolderSync":
                return ZBack(Backend()).handle(request)
            return httpx.Response(200, content=wbxml_encode(Node("AirSync:Sync", Node(
                "AirSync:Collections", Node(
                    "AirSync:Collection", Node("AirSync:SyncKey", data="9"),
                    Node("AirSync:CollectionId", data="f0"), Node("AirSync:Status", data="1"),
                    Node("AirSync:Responses", Node("AirSync:Change",
                                                   Node("AirSync:ServerId", data="f0:1"),
                                                   Node("AirSync:Status", data="6"))))))))
        client = EasClient(CFG, transport=httpx.MockTransport(handler))
        client.folder_sync()
        client._state(client.inbox_id)["key"] = "5"
        with pytest.raises(Exception, match="durum=6"):
            client.set_read("f0:1", True)


class TestPlanMailboxServers:

    def test_mailbox_scenarios_only_on_system_under_test(self):
        plan = load_plan(PROJECT_ROOT / "test_plan.yaml")
        for combo in plan.combinations("tam"):
            has_mbx = any(k.startswith("mbx_") for k in combo.scenarios)
            assert has_mbx == (combo.receiver_server == "ems"), combo.label

    def test_client_impact_keeps_the_filter(self):
        # Regresyon: istemci etkisi seçiliyken kutu filtresi eziliyordu.
        plan = parse_plan({"yollar": [{"gonderen": "ems", "alan": "gmail"}],
                           "istemciler": {"x": {"sunucu": "gmail", "erisim": "imap"}},
                           "istemci_senaryolari": ["plain_text", "mbx_move"]})
        (combo,) = plan.combinations("tam", "x")
        assert list(combo.scenarios) == ["plain_text"]

    def test_configurable_and_validated(self):
        plan = parse_plan({"yollar": [{"gonderen": "ems", "alan": "gmail"}],
                           "kutu_sunuculari": ["gmail"]})
        assert "mbx_move" in plan.combinations()[0].scenarios
        with pytest.raises(PlanError, match="kutu_sunuculari"):
            parse_plan({"yollar": [{"gonderen": "ems", "alan": "ems"}], "kutu_sunuculari": "ems"})
        with pytest.raises(PlanError, match="bilinmeyen sunucu"):
            parse_plan({"yollar": [{"gonderen": "ems", "alan": "ems"}], "kutu_sunuculari": ["yahoo"]})

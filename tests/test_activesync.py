"""
test_activesync.py — ActiveSync (Z-Push) istemcisi.

Gerçek Z-Push yerine bellek içi sahte bir sunucu kullanılır (httpx
MockTransport). Bu testler istemcinin KENDİ İÇİNDE tutarlı olduğunu ve
protokol akışını (ilke kabulü, eşitleme anahtarı, sayfalama) doğru yürüttüğünü
kanıtlar; gerçek Z-Push ile uyum scripts/zpush_check.py ile sahada sınanır.
"""

import email
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import main as main_mod  # noqa: E402
from activesync import (  # noqa: E402
    EasAuthError, EasClient, EasError, EasReceiver, EasSender, Node, WbxmlError,
    default_device_id, wbxml_decode, wbxml_encode,
)
from analyzer import NullAnalyzer  # noqa: E402
from receiver import MailReceiver, receiver_for  # noqa: E402
from sender import MailSender  # noqa: E402
from test_catalog_scenarios import Loopback, _server_cfg  # noqa: E402
from verifier import eas_view_checks  # noqa: E402

CFG = {**_server_cfg("ems"), "eas_url": "https://zpush.test/Microsoft-Server-ActiveSync"}


def make_raw(msg_id, subject="Konu ğüş", body="Merhaba ğüşıöç\n\n[Run ID: abcd1234]",
             importance=None, attachment=None):
    if attachment:
        msg = MIMEMultipart("mixed")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        part = MIMEText("ek içeriği", "plain", "utf-8")
        part.add_header("Content-Disposition", "attachment", filename=attachment)
        msg.attach(part)
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Message-ID"], msg["Subject"] = msg_id, subject
    msg["From"], msg["To"] = "gonderen@gmail.test", "kullanici@ems.test"
    if importance:
        msg["Importance"] = importance
    return msg.as_bytes().replace(b"\n", b"\r\n")


class FakeZPush:
    """En küçük ActiveSync sunucusu: Provision, FolderSync, Sync, ItemOperations, SendMail."""

    def __init__(self, messages=(), require_provision=False, window=2, view_mutator=None,
                 versions="2.5,12.0,12.1,14.0,14.1"):
        self.mailbox = {str(i + 1): raw for i, raw in enumerate(messages)}
        self.require_provision = require_provision
        self.window = window
        self.view_mutator = view_mutator
        self.versions = versions
        self.sync_state: dict = {}          # anahtar → istemciye gönderilmiş id'ler
        self.key = 0
        self.sent: list = []
        self.calls: list = []
        self.requests: list = []
        self.password = "x"

    def transport(self):
        return httpx.MockTransport(self.handle)

    # ── yardımcılar ──────────────────────────────────────────────────
    def _fields(self, raw):
        msg = email.message_from_bytes(raw)
        from receiver import MailReceiver as R
        fields = {
            "subject": R._decode_header(msg.get("Subject", "")), "from": msg.get("From", ""),
            "importance": {"high": "2", "low": "0"}.get(msg.get("Importance", ""), "1"),
            "attachments": [R._decode_header(p.get_filename()) for p in msg.walk()
                            if p.get_filename()],
            "body": next((p.get_payload(decode=True).decode("utf-8") for p in msg.walk()
                          if p.get_content_type() == "text/plain" and not p.get_filename()), ""),
        }
        return self.view_mutator(fields) if self.view_mutator else fields

    def _wbxml(self, node):
        return httpx.Response(200, content=wbxml_encode(node),
                              headers={"Content-Type": "application/vnd.ms-sync.wbxml"})

    # ── istek işleyici ───────────────────────────────────────────────
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        import base64
        user, _, password = base64.b64decode(
            request.headers["Authorization"].split()[1]).decode().partition(":")
        if password != self.password:
            return httpx.Response(401)
        if request.method == "OPTIONS":
            self.calls.append("OPTIONS")
            return httpx.Response(200, headers={
                "MS-ASProtocolVersions": self.versions,
                "MS-ASProtocolCommands": "Sync,SendMail,FolderSync,ItemOperations,Provision",
                "X-Z-Push-Version": "2.7.1"})
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        cmd = query["Cmd"]
        self.calls.append(cmd)
        assert query["DeviceId"] and query["DeviceType"] and query["User"] == user
        if cmd == "Provision":
            root = wbxml_decode(request.content)
            temp = root.value("Provision:Policies/Provision:Policy/Provision:PolicyKey")
            return self._wbxml(Node("Provision:Provision", Node("Provision:Status", data="1"), Node(
                "Provision:Policies", Node(
                    "Provision:Policy",
                    Node("Provision:PolicyType", data="MS-EAS-Provisioning-WBXML"),
                    Node("Provision:Status", data="1"),
                    Node("Provision:PolicyKey", data="777" if temp == "111" else "111")))))
        if self.require_provision and request.headers.get("X-MS-PolicyKey") != "777":
            return httpx.Response(449)
        if cmd == "SendMail":
            if request.headers["Content-Type"] == "message/rfc822":
                raw = request.content
            else:
                raw = wbxml_decode(request.content).find("ComposeMail:Mime").data
            self.sent.append(raw)
            self.mailbox[str(len(self.mailbox) + 1)] = raw
            return httpx.Response(200, content=b"")
        root = wbxml_decode(request.content)
        if cmd == "FolderSync":
            def folder(sid, name, ftype):
                return Node("FolderHierarchy:Add", Node("FolderHierarchy:ServerId", data=sid),
                            Node("FolderHierarchy:ParentId", data="0"),
                            Node("FolderHierarchy:DisplayName", data=name),
                            Node("FolderHierarchy:Type", data=ftype))
            return self._wbxml(Node(
                "FolderHierarchy:FolderSync", Node("FolderHierarchy:Status", data="1"),
                Node("FolderHierarchy:SyncKey", data="1"),
                Node("FolderHierarchy:Changes", Node("FolderHierarchy:Count", data="2"),
                     folder("f-sent", "Gönderilmiş", "5"), folder("f-inbox", "Gelen Kutusu", "2"))))
        if cmd == "Sync":
            col = root.find("AirSync:Collections/AirSync:Collection")
            assert col.value("AirSync:CollectionId") == "f-inbox"
            key = col.value("AirSync:SyncKey")
            if key != "0" and key not in self.sync_state:
                return self._wbxml(Node("AirSync:Sync", Node("AirSync:Collections", Node(
                    "AirSync:Collection", Node("AirSync:CollectionId", data="f-inbox"),
                    Node("AirSync:Status", data="3")))))
            seen = set(self.sync_state.get(key, ()))
            adds = []
            if key != "0":
                pending = [sid for sid in self.mailbox if sid not in seen]
                if not pending:
                    return httpx.Response(200, content=b"")       # değişiklik yok
                for sid in pending[:self.window]:
                    f = self._fields(self.mailbox[sid])
                    adds.append(Node("AirSync:Add", Node("AirSync:ServerId", data=sid), Node(
                        "AirSync:ApplicationData",
                        Node("Email:From", data=f["from"]),
                        Node("Email:Subject", data=f["subject"]),
                        Node("Email:Importance", data=f["importance"]),
                        Node("AirSyncBase:Attachments", *[Node(
                            "AirSyncBase:Attachment",
                            Node("AirSyncBase:DisplayName", data=name),
                            Node("AirSyncBase:IsInline", data="0")) for name in f["attachments"]])
                        if f["attachments"] else None)))
                    seen.add(sid)
                more = len(pending) > self.window
            self.key += 1
            self.sync_state[str(self.key)] = seen
            return self._wbxml(Node("AirSync:Sync", Node("AirSync:Collections", Node(
                "AirSync:Collection", Node("AirSync:SyncKey", data=str(self.key)),
                Node("AirSync:CollectionId", data="f-inbox"), Node("AirSync:Status", data="1"),
                Node("AirSync:Commands", *adds) if adds else None,
                Node("AirSync:MoreAvailable") if adds and more else None))))
        if cmd == "ItemOperations":
            fetch = root.find("ItemOperations:Fetch")
            sid = fetch.value("AirSync:ServerId")
            options = fetch.find("ItemOperations:Options")
            want_mime = options.value("AirSync:MIMESupport") == "2"
            raw = self.mailbox[sid]
            data = raw if want_mime else self._fields(raw)["body"].encode("utf-8")
            return self._wbxml(Node("ItemOperations:ItemOperations",
                                    Node("ItemOperations:Status", data="1"),
                                    Node("ItemOperations:Response", Node(
                                        "ItemOperations:Fetch",
                                        Node("ItemOperations:Status", data="1"),
                                        Node("AirSync:ServerId", data=sid),
                                        Node("ItemOperations:Properties", Node(
                                            "AirSyncBase:Body",
                                            Node("AirSyncBase:Type", data="4" if want_mime else "1"),
                                            Node("AirSyncBase:Data", data=data)))))))
        return httpx.Response(501)


def client_for(server, **cfg):
    return EasClient({**CFG, **cfg}, transport=server.transport())


# ═══════════════════════════════════════════════════════════════════
#  WBXML
# ═══════════════════════════════════════════════════════════════════

class TestWbxml:

    def test_foldersync_request_matches_protocol_document(self):
        # MS-ASCMD'deki örnek: <FolderSync><SyncKey>0</SyncKey></FolderSync>
        node = Node("FolderHierarchy:FolderSync", Node("FolderHierarchy:SyncKey", data="0"))
        assert wbxml_encode(node) == bytes.fromhex("03016a00000756520330000101")

    def test_initial_sync_request_bytes(self):
        node = Node("AirSync:Sync", Node("AirSync:Collections", Node(
            "AirSync:Collection", Node("AirSync:SyncKey", data="0"),
            Node("AirSync:CollectionId", data="1"))))
        # 45=Sync 5C=Collections 4F=Collection 4B=SyncKey 52=CollectionId (içerik biti 0x40)
        assert wbxml_encode(node) == bytes.fromhex("03016a00455c4f4b0330000152033100010101" + "01")

    def test_roundtrip_with_page_switches_empty_tags_and_binary(self):
        mime = b"From: a\r\n\r\n\x00\xff ikili \x01"
        tree = Node("AirSync:Sync", Node("AirSync:Collections", Node(
            "AirSync:Collection", Node("AirSync:GetChanges"),
            Node("AirSync:Options", Node("AirSyncBase:BodyPreference",
                                         Node("AirSyncBase:Type", data="4"))),
            Node("AirSync:ApplicationData", Node("Email:Subject", data="Şifre ğüş 🚀"),
                 Node("AirSyncBase:Body", Node("AirSyncBase:Data", data=mime))))))
        back = wbxml_decode(wbxml_encode(tree))
        col = back.find("AirSync:Collections/AirSync:Collection")
        assert col.find("AirSync:GetChanges").data is None
        assert col.value("AirSync:ApplicationData/Email:Subject") == "Şifre ğüş 🚀"
        assert col.find("AirSync:ApplicationData/AirSyncBase:Body/AirSyncBase:Data").data == mime
        assert col.value("AirSync:Options/AirSyncBase:BodyPreference/AirSyncBase:Type") == "4"

    def test_opaque_length_uses_multibyte_integer(self):
        blob = b"\x00" * 300
        raw = wbxml_encode(Node("ComposeMail:SendMail", Node("ComposeMail:Mime", data=blob)))
        assert bytes([0xC3, 0x82, 0x2C]) in raw           # 300 = 0x82 0x2C
        assert wbxml_decode(raw).find("ComposeMail:Mime").data == blob

    def test_unknown_tags_and_pages_decode_without_error(self):
        raw = bytes.fromhex("03016a00" "0063" "45" "7f0378000101")   # sayfa 99, etiketler 0x05, 0x3F
        root = wbxml_decode(raw)
        assert root.name == "P99:0x05" and root.children[0].name == "P99:0x3F"
        assert root.children[0].text == "x"

    def test_empty_body_is_none_and_garbage_raises(self):
        assert wbxml_decode(b"") is None
        with pytest.raises(WbxmlError):
            wbxml_decode(bytes.fromhex("03016a00450361"))      # sonlanmamış dizgi
        with pytest.raises(WbxmlError):
            wbxml_encode(Node("Yok:Etiket"))

    def test_dump_is_readable(self):
        text = Node("AirSync:Sync", Node("AirSync:SyncKey", data="7"),
                    Node("AirSync:GetChanges")).dump()
        assert "<AirSync:SyncKey> '7'" in text and "<AirSync:GetChanges/>" in text


# ═══════════════════════════════════════════════════════════════════
#  İstemci akışı
# ═══════════════════════════════════════════════════════════════════

class TestClient:

    def test_requires_eas_url(self):
        with pytest.raises(ValueError, match="eas_url"):
            EasClient(_server_cfg("ems"))

    def test_options_picks_14_0(self):
        client = client_for(FakeZPush())
        info = client.options()
        assert info["version"] == "14.0" and info["server"] == "2.7.1"
        assert "SendMail" in info["commands"]

    def test_version_falls_back_and_can_be_forced(self):
        assert client_for(FakeZPush(versions="2.5,12.1")).options()["version"] == "12.1"
        assert client_for(FakeZPush(), eas_version="14.1").options()["version"] == "14.1"

    def test_device_id_is_stable_per_user(self):
        a, b = default_device_id("Kullanici@ems.test"), default_device_id("kullanici@ems.test")
        assert a == b and a != default_device_id("baska@ems.test") and len(a) <= 32

    def test_folder_sync_finds_inbox(self):
        client = client_for(FakeZPush())
        folders = client.folder_sync()
        assert client.inbox_id == "f-inbox" and len(folders) == 2

    def test_sync_pages_through_all_messages(self):
        server = FakeZPush([make_raw(f"<{i}@x>", subject=f"Konu {i}") for i in range(5)], window=2)
        client = client_for(server)
        assert client.sync_inbox() == 5
        assert [f["subject"] for f in client.items.values()] == [f"Konu {i}" for i in range(5)]
        # İkinci çağrı yalnızca yeni geleni getirir (eşitleme anahtarı korunur)
        server.mailbox["6"] = make_raw("<6@x>", subject="Yeni")
        assert client.sync_inbox() == 1 and client.items["6"]["subject"] == "Yeni"
        assert client.sync_inbox() == 0

    def test_provisioning_is_done_once_when_demanded(self):
        server = FakeZPush([make_raw("<a@x>")], require_provision=True)
        client = client_for(server)
        client.sync_inbox()
        assert client.policy_key == "777"
        assert server.calls.count("Provision") == 2          # geçici anahtar + kabul
        assert server.calls[:4] == ["OPTIONS", "FolderSync", "Provision", "Provision"]

    def test_invalid_sync_key_restarts_sync(self):
        server = FakeZPush([make_raw("<a@x>")])
        client = client_for(server)
        client.sync_inbox()
        server.sync_state.clear()                            # sunucu durumu sıfırlandı
        server.mailbox["2"] = make_raw("<b@x>")
        client.sync_inbox()
        assert set(client.items) == {"1", "2"}

    def test_fetch_mime_returns_exact_bytes(self):
        raw = make_raw("<a@x>", attachment="Şubat raporu.txt")
        client = client_for(FakeZPush([raw]))
        client.sync_inbox()
        assert client.fetch_mime("1") == raw
        assert client.items["1"]["attachments"][0]["name"] == "Şubat raporu.txt"

    def test_wrong_password_raises_auth_error(self):
        client = client_for(FakeZPush(), password="yanlış")
        with pytest.raises(EasAuthError):
            client.options()

    def test_http_error_is_reported(self):
        client = EasClient(CFG, transport=httpx.MockTransport(
            lambda req: httpx.Response(451, headers={"X-MS-Location": "https://yeni.test/eas"})))
        with pytest.raises(EasError, match="yeni.test"):
            client.options()

    def test_send_mail_wbxml_for_14_and_raw_for_12(self):
        raw = make_raw("<gonder@x>")
        for versions, ctype in (("12.1,14.0", "application/vnd.ms-sync.wbxml"),
                                ("2.5,12.1", "message/rfc822")):
            server = FakeZPush(versions=versions)
            client_for(server).send_mail(raw)
            assert server.sent == [raw]
            assert server.requests[-1].headers["Content-Type"] == ctype

    def test_send_mail_rejection(self):
        def handler(request):
            if request.method == "OPTIONS":
                return httpx.Response(200, headers={"MS-ASProtocolVersions": "14.0"})
            return httpx.Response(200, content=wbxml_encode(Node(
                "ComposeMail:SendMail", Node("ComposeMail:Status", data="120"))))
        with pytest.raises(EasError, match="durum=120"):
            EasClient(CFG, transport=httpx.MockTransport(handler)).send_mail(b"x")

    def test_credentials_never_in_url(self):
        server = FakeZPush([make_raw("<a@x>")])
        client_for(server, password="x").sync_inbox()
        assert all("x" != v for r in server.requests
                   for v in parse_qs(r.url.query.decode()).get("Password", []))
        assert all("Authorization" in r.headers for r in server.requests)


# ═══════════════════════════════════════════════════════════════════
#  Alıcı / gönderici
# ═══════════════════════════════════════════════════════════════════

class TestReceiverAndSender:

    def test_receiver_finds_message_and_returns_view(self):
        target = make_raw("<hedef@x>", subject="[T] #abcd1234 Öncelik", importance="high",
                          attachment="rapor.txt")
        server = FakeZPush([make_raw("<a@x>"), target, make_raw("<c@x>")])
        receiver = EasReceiver(CFG, transport=server.transport())
        got = receiver.wait_for_message("<hedef@x>", wait_seconds=0, max_retries=1,
                                        subject_hint="abcd1234")
        assert got["raw_bytes"] == target
        assert got["eas_view"]["importance"] == "2"
        assert got["eas_view"]["subject"] == "[T] #abcd1234 Öncelik"
        assert "[Run ID: abcd1234]" in got["eas_view"]["body"]
        # İpucu sayesinde yalnızca konusu uyan mesajın MIME'ı indirildi (+1 görünüm)
        assert server.calls.count("ItemOperations") == 2

    def test_hint_disambiguates_by_message_id(self):
        # reply_chain: orijinal ve yanıt aynı Run ID'yi taşır.
        server = FakeZPush([make_raw("<orijinal@x>", subject="#ORIG-abcd1234"),
                            make_raw("<yanit@x>", subject="Re: #ORIG-abcd1234")])
        receiver = EasReceiver(CFG, transport=server.transport())
        got = receiver.wait_for_message("<orijinal@x>", wait_seconds=0, max_retries=1,
                                        subject_hint="abcd1234")
        assert got["headers"]["message_id"] == "<orijinal@x>"

    def test_not_found_and_auth_failure(self):
        receiver = EasReceiver(CFG, transport=FakeZPush([make_raw("<a@x>")]).transport())
        assert receiver.wait_for_message("<yok@x>", wait_seconds=0, max_retries=2,
                                         retry_interval=0) is None
        assert receiver.last_error is None
        bad = EasReceiver({**CFG, "password": "yanlış"}, transport=FakeZPush().transport())
        assert bad.wait_for_message("<a@x>", wait_seconds=0, max_retries=5, retry_interval=0) is None
        assert bad.is_auth_error(bad.last_error)

    def test_factory(self):
        assert isinstance(receiver_for(CFG, "activesync"), EasReceiver)

    def test_sender_delivers_scenario_through_zpush(self):
        server = FakeZPush()
        sender = EasSender(CFG, transport=server.transport())
        meta = sender.send_plain_text("alici@gmail.test", "Konu ğüş", "Gövde ğüşıöç")
        (raw,) = server.sent
        msg = email.message_from_bytes(raw)
        assert " ".join(msg["Message-ID"].split()) == meta["msg_id"]
        assert msg["To"] == "alici@gmail.test" and b"\n" not in raw.replace(b"\r\n", b"")


# ═══════════════════════════════════════════════════════════════════
#  Görünüm kontrolleri: Z-Push dönüşümünde bozulan alanlar
# ═══════════════════════════════════════════════════════════════════

def _view(**over):
    base = {"subject": "Konu ğüş", "from": '"Gönderen" <gonderen@gmail.test>',
            "importance": "1", "attachments": [], "body_type": "2",
            "body": "<html><body><p>Merhaba ğüşıöç</p><p>[Run ID: abcd1234]</p></body></html>"}
    return {**base, **over}


def _failed(checks):
    return [c["name"] for c in checks if not c["passed"]]


class TestViewChecks:

    def test_intact_view_passes(self):
        checks = eas_view_checks(_view(), make_raw("<a@x>"), None)
        assert _failed(checks) == []
        assert {c["name"] for c in checks} >= {
            "[Kesin] ACTIVESYNC görünümü: konu", "[Kesin] ACTIVESYNC görünümü: gönderen",
            "[Kesin] ACTIVESYNC görünümü: gövde", "[Bilgi] ACTIVESYNC görünümü: öncelik"}

    def test_mangled_subject(self):
        checks = eas_view_checks(_view(subject="Konu g?s"), make_raw("<a@x>"), None)
        assert _failed(checks) == ["[Kesin] ACTIVESYNC görünümü: konu"]

    def test_priority_lost_in_conversion(self):
        raw = make_raw("<a@x>", importance="high")
        checks = eas_view_checks(_view(importance="1"), raw, {"priority_level": "high"})
        assert _failed(checks) == ["[Kesin] ACTIVESYNC görünümü: öncelik"]
        assert _failed(eas_view_checks(_view(importance="2"), raw, {"priority_level": "high"})) == []

    def test_attachment_missing_or_renamed(self):
        raw = make_raw("<a@x>", attachment="Şubat raporu.txt")
        ok = _view(attachments=[{"name": "Şubat raporu.txt", "inline": False}])
        assert _failed(eas_view_checks(ok, raw, None)) == []
        bad = _view(attachments=[{"name": "?ubat raporu.txt", "inline": False}])
        assert _failed(eas_view_checks(bad, raw, None)) == ["[Kesin] ACTIVESYNC görünümü: ek listesi"]

    def test_truncated_or_garbled_body(self):
        raw = make_raw("<a@x>")
        cut = eas_view_checks(_view(body="<p>Merhaba ğüşıöç</p>", truncated=True), raw, None)
        assert "[Kesin] ACTIVESYNC görünümü: gövde" in _failed(cut)
        assert any(c["name"] == "[Bilgi] ACTIVESYNC görünümü: kırpma" for c in cut)
        garbled = eas_view_checks(_view(body="Merhaba g?s?o? [Run ID: abcd1234]"), raw, None)
        assert "Türkçe karakterler bozulmuş" in garbled[-1]["detail"]


# ═══════════════════════════════════════════════════════════════════
#  Orkestratör: Z-Push üçüncü kanal olarak
# ═══════════════════════════════════════════════════════════════════

class TestOrchestratorChannel:

    def _run(self, tmp_path, monkeypatch, scenario, view_mutator=None):
        """SMTP → (loopback) → IMAP birincil kanal; aynı mesaj sahte Z-Push'tan da okunur."""
        from types import SimpleNamespace
        monkeypatch.chdir(tmp_path)
        main_mod.prepare_test_files()
        loop = Loopback()
        server = FakeZPush(view_mutator=view_mutator)
        sender, receiver = MailSender(_server_cfg("gmail")), MailReceiver(CFG)
        receiver.extra_channels = [("activesync", EasReceiver(CFG, transport=server.transport()))]

        def imap_wait(**kw):
            got = loop.wait_for_message(receiver, kw["expected_msg_id"])
            if got:                                   # Z-Push aynı kutuyu görür
                server.mailbox[str(len(server.mailbox) + 1)] = got["raw_bytes"]
            return got
        combo = SimpleNamespace(label="Gmail → EMS", receiver_server="ems", receiver_client="iPad",
                                sender_server="gmail", sender_client="SMTP", scenarios={})
        from unittest.mock import patch
        with patch.object(MailSender, "_connect", side_effect=lambda: loop.smtp()), \
             patch.object(MailReceiver, "wait_for_message", side_effect=imap_wait), \
             patch.object(main_mod.time, "sleep"):
            return main_mod.run_scenario(
                scenario, combo, 0, sender, receiver, NullAnalyzer(),
                {"wait_seconds": 0, "max_retries": 1, "retry_interval": 0})

    def test_clean_zpush_passes_with_view_checks(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch, "priority_high")
        names = [c["name"] for c in result["analysis"]["checks"]]
        assert result["analysis"]["passed"] is True, _failed(result["analysis"]["checks"])
        assert "[Kesin] ACTIVESYNC kanalı: içerik IMAP ile aynı" in names
        assert "[Kesin] ACTIVESYNC görünümü: öncelik" in names

    def test_zpush_dropping_priority_fails_only_there(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch, "priority_high",
                           view_mutator=lambda f: {**f, "importance": "1"})
        assert result["analysis"]["passed"] is False
        assert _failed(result["analysis"]["checks"]) == ["[Kesin] ACTIVESYNC görünümü: öncelik"]
        assert "Kanal hatası" in result["analysis"]["summary"]

    def test_zpush_garbling_turkish_subject(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch, "plain_text", view_mutator=lambda f: {
            **f, "subject": f["subject"].encode("ascii", "replace").decode()})
        assert "[Kesin] ACTIVESYNC görünümü: konu" in _failed(result["analysis"]["checks"])

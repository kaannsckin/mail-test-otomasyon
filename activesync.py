"""
activesync.py — Exchange ActiveSync (EAS) istemcisi: Z-Push kanalı.

iOS/iPadOS Mail kurum sunucusuna IMAP ile değil, Z-Push üzerinden ActiveSync
ile bağlanır. Z-Push mesajı IMAP'ten alır ve EAS için yeniden biçimlendirir:
konu, gönderen, öncelik ve ek listesi ayrı alanlara çıkarılır, gövde HTML/düz
metne çevrilir. Telefonun ekranında görünen şey bu alanlardır — IMAP'te sağlam
duran bir mesaj burada bozulmuş olabilir.

Bu modül o yolu otomatik sınar:

  * ``EasClient``    — protokol: OPTIONS, Provision, FolderSync, Sync,
                       ItemOperations (Fetch), SendMail. Gövde WBXML'dir.
  * ``EasReceiver``  — mesajı Z-Push'tan okur: hem özgün MIME (IMAP ile
                       karşılaştırmak için) hem "telefonun gördüğü" alanlar.
  * ``EasSender``    — mesajı Z-Push üzerinden gönderir (iPad'in gönderim yolu).

Sunucu bölümü anahtarları (config.yaml):

  eas_url: "https://posta.kurum.gov.tr/Microsoft-Server-ActiveSync"
  eas_username: ""        # varsayılan: username
  eas_device_id: ""       # varsayılan: kullanıcı adından türetilir (kalıcı)
  eas_device_type: ""     # varsayılan: MailTestOtomasyon
  eas_version: ""         # varsayılan: sunucunun bildirdiklerinden 14.0 > 12.1 > 14.1

Kaynak: MS-ASWBXML (kod sayfaları), MS-ASCMD (komutlar), MS-ASPROV.
"""
from __future__ import annotations

import base64
import email
import hashlib
import logging
import time
import uuid
from typing import Optional
from urllib.parse import urlencode

import httpx

from mail_transport import ssl_context_for
from receiver import MailReceiver
from sender import SMTP_POLICY, MailSender

logger = logging.getLogger(__name__)

WBXML_TYPE = "application/vnd.ms-sync.wbxml"
PREFERRED_VERSIONS = ("14.0", "12.1", "14.1")
# Sunucu "önce cihaz ilkesini kabul et" diyor (MS-ASCMD ortak durum kodları).
PROVISION_STATUSES = {"141", "142", "143", "144"}
INBOX_TYPE = "2"


# ────────────────────────────────────────────────────────────────────
#  WBXML (MS-ASWBXML) — yalnızca kullandığımız kod sayfaları adlandırılır;
#  bilinmeyen etiketler "P<sayfa>:0x<kod>" adıyla sorunsuz çözülür.
# ────────────────────────────────────────────────────────────────────
_PAGES = {
    0: ("AirSync", {
        0x05: "Sync", 0x06: "Responses", 0x07: "Add", 0x08: "Change", 0x09: "Delete",
        0x0A: "Fetch", 0x0B: "SyncKey", 0x0C: "ClientId", 0x0D: "ServerId", 0x0E: "Status",
        0x0F: "Collection", 0x10: "Class", 0x12: "CollectionId", 0x13: "GetChanges",
        0x14: "MoreAvailable", 0x15: "WindowSize", 0x16: "Commands", 0x17: "Options",
        0x18: "FilterType", 0x19: "Truncation", 0x1B: "Conflict", 0x1C: "Collections",
        0x1D: "ApplicationData", 0x1E: "DeletesAsMoves", 0x20: "Supported",
        0x21: "SoftDelete", 0x22: "MIMESupport", 0x23: "MIMETruncation"}),
    2: ("Email", {
        0x05: "Attachment", 0x06: "Attachments", 0x07: "AttName", 0x08: "AttSize",
        0x0C: "Body", 0x0D: "BodySize", 0x0E: "BodyTruncated", 0x0F: "DateReceived",
        0x10: "DisplayName", 0x11: "DisplayTo", 0x12: "Importance", 0x13: "MessageClass",
        0x14: "Subject", 0x15: "Read", 0x16: "To", 0x17: "Cc", 0x18: "From",
        0x19: "ReplyTo", 0x22: "MeetingRequest", 0x32: "Sensitivity", 0x35: "ThreadTopic", 0x36: "MIMEData",
        0x37: "MIMETruncated", 0x38: "MIMESize", 0x39: "InternetCPID", 0x3A: "Flag",
        0x3C: "ContentClass"}),
    7: ("FolderHierarchy", {
        0x05: "Folders", 0x06: "Folder", 0x07: "DisplayName", 0x08: "ServerId",
        0x09: "ParentId", 0x0A: "Type", 0x0B: "Response", 0x0C: "Status",
        0x0E: "Changes", 0x0F: "Add", 0x10: "Delete", 0x11: "Update", 0x12: "SyncKey",
        0x16: "FolderSync", 0x17: "Count"}),
    14: ("Provision", {
        0x05: "Provision", 0x06: "Policies", 0x07: "Policy", 0x08: "PolicyType",
        0x09: "PolicyKey", 0x0A: "Data", 0x0B: "Status", 0x0C: "RemoteWipe",
        0x0D: "EASProvisionDoc"}),
    17: ("AirSyncBase", {
        0x05: "BodyPreference", 0x06: "Type", 0x07: "TruncationSize", 0x08: "AllOrNone",
        0x0A: "Body", 0x0B: "Data", 0x0C: "EstimatedDataSize", 0x0D: "Truncated",
        0x0E: "Attachments", 0x0F: "Attachment", 0x10: "DisplayName",
        0x11: "FileReference", 0x12: "Method", 0x13: "ContentId", 0x14: "ContentLocation",
        0x15: "IsInline", 0x16: "NativeBodyType", 0x17: "ContentType", 0x18: "Preview"}),
    20: ("ItemOperations", {
        0x05: "ItemOperations", 0x06: "Fetch", 0x07: "Store", 0x08: "Options",
        0x09: "Range", 0x0A: "Total", 0x0B: "Properties", 0x0C: "Data", 0x0D: "Status",
        0x0E: "Response", 0x0F: "Version", 0x10: "Schema", 0x11: "Part"}),
    21: ("ComposeMail", {
        0x05: "SendMail", 0x06: "SmartForward", 0x07: "SmartReply",
        0x08: "SaveInSentItems", 0x09: "ReplaceMime", 0x0B: "Source", 0x0C: "FolderId",
        0x0D: "ItemId", 0x10: "Mime", 0x11: "ClientId", 0x12: "Status", 0x13: "AccountId"}),
}
_TOKENS = {f"{ns}:{name}": (page, token)
           for page, (ns, names) in _PAGES.items() for token, name in names.items()}

_SWITCH_PAGE, _END, _STR_I, _OPAQUE = 0x00, 0x01, 0x03, 0xC3


class WbxmlError(ValueError):
    """Sunucu yanıtı WBXML olarak çözülemedi."""


class Node:
    """WBXML öğesi: ad + (alt öğeler | metin/bayt | boş)."""
    __slots__ = ("name", "children", "data")

    def __init__(self, name: str, *children, data=None):
        self.name = name
        self.children = [c for c in children if c is not None]
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.data: Optional[bytes] = data

    @property
    def text(self) -> str:
        return (self.data or b"").decode("utf-8", errors="replace")

    def find(self, path: str) -> Optional["Node"]:
        node = self
        for name in path.split("/"):
            node = next((c for c in node.children if c.name == name), None)
            if node is None:
                return None
        return node

    def findall(self, name: str) -> list:
        return [c for c in self.children if c.name == name]

    def value(self, path: str, default: str = "") -> str:
        node = self.find(path)
        return node.text if node is not None else default

    def dump(self, indent: int = 0) -> str:
        """Teşhis için okunabilir ağaç (uzun/ikili veri kısaltılır)."""
        pad = "  " * indent
        if self.children:
            return f"{pad}<{self.name}>\n" + "\n".join(
                c.dump(indent + 1) for c in self.children)
        if self.data is None:
            return f"{pad}<{self.name}/>"
        shown = self.text if len(self.data) <= 120 else f"({len(self.data)} bayt) " + self.text[:80] + "…"
        return f"{pad}<{self.name}> {shown!r}"


def _mb_uint(value: int) -> bytes:
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def wbxml_encode(root: Node) -> bytes:
    out = bytearray(b"\x03\x01\x6a\x00")       # WBXML 1.3, bilinmeyen DTD, UTF-8, boş dizgi tablosu
    page = [0]

    def emit(node: Node) -> None:
        try:
            node_page, token = _TOKENS[node.name]
        except KeyError:
            raise WbxmlError(f"Bilinmeyen WBXML etiketi: {node.name}") from None
        if node_page != page[0]:
            out.extend((_SWITCH_PAGE, node_page))
            page[0] = node_page
        if not node.children and node.data is None:
            out.append(token)
            return
        out.append(token | 0x40)
        if node.children:
            for child in node.children:
                emit(child)
        elif b"\x00" in node.data or node.name == "ComposeMail:Mime":
            out.append(_OPAQUE)
            out.extend(_mb_uint(len(node.data)))
            out.extend(node.data)
        else:
            out.append(_STR_I)
            out.extend(node.data)
            out.append(0)
        out.append(_END)

    emit(root)
    return bytes(out)


def wbxml_decode(data: bytes) -> Optional[Node]:
    """WBXML → Node ağacı. Boş gövde → None (EAS'te "değişiklik yok")."""
    if not data:
        return None
    pos = [0]

    def byte() -> int:
        if pos[0] >= len(data):
            raise WbxmlError("WBXML beklenmedik biçimde bitti")
        pos[0] += 1
        return data[pos[0] - 1]

    def mb_uint() -> int:
        value = 0
        while True:
            b = byte()
            value = (value << 7) | (b & 0x7F)
            if not b & 0x80:
                return value

    byte()                      # sürüm
    mb_uint()                   # public id
    mb_uint()                   # karakter seti
    table_length = mb_uint()    # dizgi tablosu (EAS kullanmaz; varsa atlanır)
    pos[0] += table_length
    page = 0
    root: Optional[Node] = None
    stack: list = []
    while pos[0] < len(data):
        token = byte()
        if token == _SWITCH_PAGE:
            page = byte()
        elif token == _END:
            if not stack:
                raise WbxmlError("WBXML: fazladan END")
            stack.pop()
        elif token == _STR_I:
            end = data.find(b"\x00", pos[0])
            if end < 0 or not stack:
                raise WbxmlError("WBXML: sonlanmamış dizgi")
            stack[-1].data = (stack[-1].data or b"") + data[pos[0]:end]
            pos[0] = end + 1
        elif token == _OPAQUE:
            length = mb_uint()
            if not stack or pos[0] + length > len(data):
                raise WbxmlError("WBXML: geçersiz opaque uzunluğu")
            stack[-1].data = (stack[-1].data or b"") + data[pos[0]:pos[0] + length]
            pos[0] += length
        else:
            if token & 0x80:
                raise WbxmlError("WBXML: öznitelikli etiket desteklenmiyor")
            ns, names = _PAGES.get(page, (f"P{page}", {}))
            code = token & 0x3F
            node = Node(f"{ns}:{names.get(code, f'0x{code:02X}')}")
            if stack:
                stack[-1].children.append(node)
            elif root is None:
                root = node
            if token & 0x40:
                stack.append(node)
    if root is None:
        raise WbxmlError("WBXML: kök öğe yok")
    return root


# ────────────────────────────────────────────────────────────────────
#  İstemci
# ────────────────────────────────────────────────────────────────────
class EasError(Exception):
    """ActiveSync isteği başarısız (HTTP ya da protokol durumu)."""


class EasAuthError(EasError):
    """Kimlik doğrulama reddedildi (HTTP 401/403)."""


def default_device_id(username: str) -> str:
    """Kullanıcıya göre KALICI cihaz kimliği: Z-Push eşitleme durumunu cihaz
    başına tutar; her koşuda yeni kimlik sunucuda cihaz kaydı biriktirir."""
    return "MTO" + hashlib.sha1(username.lower().encode("utf-8")).hexdigest()[:24].upper()


class EasClient:
    def __init__(self, server_config: dict, transport=None, timeout: float = 60.0):
        url = str(server_config.get("eas_url") or "").strip()
        if not url:
            raise ValueError("ActiveSync için sunucu bölümünde eas_url tanımlı değil "
                             "(ör. https://posta.kurum.gov.tr/Microsoft-Server-ActiveSync)")
        self.url = url
        self.username = str(server_config.get("eas_username")
                            or server_config.get("username") or "")
        self.password = str(server_config.get("password") or "")
        self.device_id = str(server_config.get("eas_device_id")
                             or default_device_id(self.username))
        self.device_type = str(server_config.get("eas_device_type") or "MailTestOtomasyon")
        self.version = str(server_config.get("eas_version") or "")
        self.policy_key = "0"
        self.inbox_id: Optional[str] = None
        self.sync_key = "0"
        self.items: dict = {}                 # ServerId → Sync ile gelen alanlar
        kwargs = {"timeout": timeout, "transport": transport} if transport is not None else {
            "timeout": timeout, "verify": ssl_context_for(server_config)}
        self.http = httpx.Client(**kwargs)

    def close(self) -> None:
        self.http.close()

    # ── HTTP ─────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode()
        return {"Authorization": f"Basic {token}", "User-Agent": "MailTestOtomasyon/1.0",
                "MS-ASProtocolVersion": self.version or "14.0",
                "X-MS-PolicyKey": self.policy_key}

    def _check_http(self, resp) -> None:
        if resp.status_code in (401, 403):
            raise EasAuthError(f"ActiveSync kimlik doğrulaması reddedildi (HTTP {resp.status_code})")
        if resp.status_code == 451:
            raise EasError("Sunucu başka adrese yönlendiriyor (HTTP 451): "
                           + resp.headers.get("X-MS-Location", "?") + " — eas_url'i güncelleyin")
        if resp.status_code != 200:
            raise EasError(f"ActiveSync HTTP {resp.status_code}: {resp.text[:200]}")

    def options(self) -> dict:
        """Sunucunun desteklediği sürümler ve komutlar; sürüm buradan seçilir."""
        resp = self.http.request("OPTIONS", self.url, headers=self._headers())
        self._check_http(resp)
        versions = [v.strip() for v in resp.headers.get("MS-ASProtocolVersions", "").split(",")
                    if v.strip()]
        if not self.version:
            self.version = next((v for v in PREFERRED_VERSIONS if v in versions), "14.0")
        return {"versions": versions, "version": self.version,
                "commands": [c.strip() for c in
                             resp.headers.get("MS-ASProtocolCommands", "").split(",") if c.strip()],
                "server": resp.headers.get("X-Z-Push-Version") or resp.headers.get("Server", "")}

    def _post(self, cmd: str, body: bytes, content_type: str = WBXML_TYPE,
              extra: Optional[dict] = None, _retry: bool = True) -> Optional[Node]:
        if not self.version:
            self.options()
        params = {"Cmd": cmd, "User": self.username, "DeviceId": self.device_id,
                  "DeviceType": self.device_type, **(extra or {})}
        resp = self.http.post(f"{self.url}?{urlencode(params)}", content=body,
                              headers={**self._headers(), "Content-Type": content_type})
        if resp.status_code == 449 and _retry and cmd != "Provision":
            self.provision()
            return self._post(cmd, body, content_type, extra, _retry=False)
        self._check_http(resp)
        root = wbxml_decode(resp.content)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"EAS {cmd} yanıtı:\n{root.dump() if root else '(boş)'}")
        # Bazı sunucular 449 yerine gövdede "ilke gerekli" durumu döndürür.
        if root is not None and _retry and cmd != "Provision":
            status = next((c.text for c in root.children if c.name.endswith(":Status")), "")
            if status in PROVISION_STATUSES:
                self.provision()
                return self._post(cmd, body, content_type, extra, _retry=False)
        return root

    # ── Provision (MS-ASPROV): cihaz ilkesini al ve kabul et ─────────
    def provision(self) -> str:
        ptype = Node("Provision:PolicyType", data="MS-EAS-Provisioning-WBXML")
        first = self._post("Provision", wbxml_encode(Node(
            "Provision:Provision", Node("Provision:Policies", Node("Provision:Policy", ptype)))))
        temp = first.value("Provision:Policies/Provision:Policy/Provision:PolicyKey") if first else ""
        if not temp:
            raise EasError("Provision: sunucu geçici ilke anahtarı vermedi"
                           + (f"\n{first.dump()}" if first else ""))
        self.policy_key = temp
        second = self._post("Provision", wbxml_encode(Node(
            "Provision:Provision", Node("Provision:Policies", Node(
                "Provision:Policy", ptype, Node("Provision:PolicyKey", data=temp),
                Node("Provision:Status", data="1"))))))
        final = second.value("Provision:Policies/Provision:Policy/Provision:PolicyKey") if second else ""
        if not final:
            raise EasError("Provision: ilke kabulü onaylanmadı"
                           + (f"\n{second.dump()}" if second else ""))
        self.policy_key = final
        logger.info("ActiveSync cihaz ilkesi kabul edildi (provisioning).")
        return final

    # ── Klasörler ────────────────────────────────────────────────────
    def folder_sync(self) -> list:
        root = self._post("FolderSync", wbxml_encode(Node(
            "FolderHierarchy:FolderSync", Node("FolderHierarchy:SyncKey", data="0"))))
        if root is None or root.value("FolderHierarchy:Status") != "1":
            raise EasError("FolderSync başarısız: durum="
                           + (root.value("FolderHierarchy:Status", "?") if root else "boş yanıt"))
        folders = [{"id": f.value("FolderHierarchy:ServerId"),
                    "name": f.value("FolderHierarchy:DisplayName"),
                    "type": f.value("FolderHierarchy:Type")}
                   for f in (root.find("FolderHierarchy:Changes") or Node("x")).findall(
                       "FolderHierarchy:Add")]
        inbox = next((f for f in folders if f["type"] == INBOX_TYPE), None)
        if inbox is None:
            raise EasError("FolderSync: gelen kutusu (Type=2) bulunamadı")
        self.inbox_id = inbox["id"]
        return folders

    # ── Sync: gelen kutusundaki son mesajların ALANLARI ──────────────
    def _sync_request(self, with_options: bool, filter_type: str) -> bytes:
        collection = [Node("AirSync:SyncKey", data=self.sync_key),
                      Node("AirSync:CollectionId", data=self.inbox_id)]
        if with_options:
            collection += [
                Node("AirSync:DeletesAsMoves"), Node("AirSync:GetChanges"),
                Node("AirSync:WindowSize", data="100"),
                Node("AirSync:Options",
                     Node("AirSync:FilterType", data=filter_type),
                     # Düz metin önizleme yeterli: tam içerik ItemOperations ile çekilir.
                     Node("AirSyncBase:BodyPreference", Node("AirSyncBase:Type", data="1"),
                          Node("AirSyncBase:TruncationSize", data="256")))]
        return wbxml_encode(Node("AirSync:Sync", Node(
            "AirSync:Collections", Node("AirSync:Collection", *collection))))

    def sync_inbox(self, filter_type: str = "2", max_rounds: int = 50) -> int:
        """Gelen kutusunu eşitler (filter_type 2 = son 3 gün); yeni öğe sayısını
        döndürür. Eşitleme anahtarı nesnede tutulur → sonraki çağrı yalnızca
        yeni gelenleri getirir."""
        if self.inbox_id is None:
            self.folder_sync()
        added = 0
        for _ in range(max_rounds):
            initial = self.sync_key == "0"
            root = self._post("Sync", self._sync_request(not initial, filter_type))
            if root is None:
                return added                       # boş yanıt = değişiklik yok
            col = root.find("AirSync:Collections/AirSync:Collection")
            status = (col.value("AirSync:Status") if col is not None
                      else root.value("AirSync:Status")) or "1"
            if status == "3":                      # eşitleme anahtarı geçersiz → baştan
                logger.warning("ActiveSync eşitleme anahtarı geçersiz; baştan eşitleniyor.")
                self.sync_key, self.items = "0", {}
                continue
            if status == "12":                     # klasör yapısı değişti
                self.folder_sync()
                self.sync_key = "0"
                continue
            if status != "1" or col is None:
                raise EasError(f"Sync başarısız: durum={status}")
            self.sync_key = col.value("AirSync:SyncKey") or self.sync_key
            for add in (col.find("AirSync:Commands") or Node("x")).findall("AirSync:Add"):
                data = add.find("AirSync:ApplicationData") or Node("x")
                self.items[add.value("AirSync:ServerId")] = self._item_fields(data)
                added += 1
            for gone in (col.find("AirSync:Commands") or Node("x")).findall("AirSync:Delete"):
                self.items.pop(gone.value("AirSync:ServerId"), None)
            if not initial and col.find("AirSync:MoreAvailable") is None:
                return added
        return added

    @staticmethod
    def _item_fields(data: Node) -> dict:
        """Telefonun mesaj listesinde/başlığında gösterdiği alanlar."""
        attachments = [{"name": a.value("AirSyncBase:DisplayName"),
                        "size": a.value("AirSyncBase:EstimatedDataSize"),
                        "inline": a.value("AirSyncBase:IsInline") == "1",
                        "content_id": a.value("AirSyncBase:ContentId")}
                       for a in (data.find("AirSyncBase:Attachments") or Node("x")).findall(
                           "AirSyncBase:Attachment")]
        return {"subject": data.value("Email:Subject"), "from": data.value("Email:From"),
                "to": data.value("Email:To"), "cc": data.value("Email:Cc"),
                "reply_to": data.value("Email:ReplyTo"),
                "importance": data.value("Email:Importance"),
                "date_received": data.value("Email:DateReceived"),
                "message_class": data.value("Email:MessageClass"),
                "has_meeting_request": data.find("Email:MeetingRequest") is not None,
                "attachments": attachments}

    # ── ItemOperations: tek mesajın içeriği ──────────────────────────
    def _fetch(self, server_id: str, options: list) -> Node:
        root = self._post("ItemOperations", wbxml_encode(Node(
            "ItemOperations:ItemOperations", Node(
                "ItemOperations:Fetch", Node("ItemOperations:Store", data="Mailbox"),
                Node("AirSync:CollectionId", data=self.inbox_id),
                Node("AirSync:ServerId", data=server_id),
                Node("ItemOperations:Options", *options)))))
        fetch = root.find("ItemOperations:Response/ItemOperations:Fetch") if root else None
        status = fetch.value("ItemOperations:Status") if fetch is not None else (
            root.value("ItemOperations:Status", "?") if root else "boş yanıt")
        if fetch is None or status != "1":
            raise EasError(f"ItemOperations Fetch başarısız: durum={status}")
        return fetch.find("ItemOperations:Properties") or Node("x")

    def fetch_mime(self, server_id: str) -> bytes:
        """Mesajın özgün MIME hâli (IMAP'tekiyle karşılaştırmak için)."""
        props = self._fetch(server_id, [
            Node("AirSync:MIMESupport", data="2"),
            Node("AirSyncBase:BodyPreference", Node("AirSyncBase:Type", data="4"))])
        body = props.find("AirSyncBase:Body/AirSyncBase:Data")
        if body is None or not body.data:
            raise EasError("Sunucu MIME gövdesi döndürmedi (BodyPreference Type=4)")
        return body.data

    def fetch_view(self, server_id: str) -> dict:
        """Telefonun gösterdiği gövde: HTML (yoksa sunucu düz metin döndürür)."""
        props = self._fetch(server_id, [
            Node("AirSyncBase:BodyPreference", Node("AirSyncBase:Type", data="2"),
                 Node("AirSyncBase:TruncationSize", data="1048576"))])
        return {"body_type": props.value("AirSyncBase:Body/AirSyncBase:Type"),
                "body": props.value("AirSyncBase:Body/AirSyncBase:Data"),
                "truncated": props.value("AirSyncBase:Body/AirSyncBase:Truncated") == "1"}

    # ── SendMail: Z-Push üzerinden gönderim ──────────────────────────
    def send_mail(self, raw: bytes) -> None:
        if not self.version:
            self.options()
        if self.version.startswith(("2.", "12.")):
            root = self._post("SendMail", raw, content_type="message/rfc822",
                              extra={"SaveInSent": "T"})
        else:
            root = self._post("SendMail", wbxml_encode(Node(
                "ComposeMail:SendMail", Node("ComposeMail:ClientId", data=uuid.uuid4().hex),
                Node("ComposeMail:SaveInSentItems"), Node("ComposeMail:Mime", data=raw))))
        if root is not None:                       # başarıda gövde boştur
            raise EasError("SendMail reddedildi: durum="
                           + root.value("ComposeMail:Status", "?"))


# ────────────────────────────────────────────────────────────────────
#  Alıcı / gönderici (orkestratör arayüzü)
# ────────────────────────────────────────────────────────────────────
class EasReceiver(MailReceiver):
    """Aynı posta kutusunu Z-Push (ActiveSync) üzerinden okur.

    Dönen sözlük IMAP alıcısıyla aynı biçimdedir (``raw_bytes`` = Z-Push'ın
    verdiği MIME) ve ek olarak ``eas_view`` taşır: telefonun gösterdiği
    konu/gönderen/öncelik/ek listesi ve gövde.
    """
    accepts_hint = True

    def __init__(self, server_config: dict, transport=None):
        self.config = server_config
        self.host = str(server_config.get("eas_url") or "")
        self.client = EasClient(server_config, transport=transport)
        self.last_error: Optional[BaseException] = None
        self._misses: set = set()              # Message-ID'si uymayan ServerId'ler

    @staticmethod
    def is_auth_error(exc: Optional[BaseException]) -> bool:
        return isinstance(exc, EasAuthError)

    def _connect(self):
        """Bağlantı testi (dry-run): sürüm keşfi + klasör listesi."""
        self.client.options()
        self.client.folder_sync()
        return self.client

    def _candidates(self, hint: Optional[str]) -> list:
        ids = [sid for sid in self.client.items if sid not in self._misses]
        if hint:
            ids = [sid for sid in ids if hint in self.client.items[sid].get("subject", "")]
        return list(reversed(ids))             # en yeni önce

    def wait_for_message(self, expected_msg_id: str, subject_prefix: str = "",
                         wait_seconds: int = 20, max_retries: int = 5,
                         retry_interval: int = 5, subject_hint: Optional[str] = None
                         ) -> Optional[dict]:
        """``subject_hint`` (konudaki Run ID) verilirse yalnızca konusu uyan
        mesajların MIME'ı indirilir; verilmezse son günlerin tüm mesajları."""
        expected = self._unfold(expected_msg_id)
        self.last_error = None
        time.sleep(wait_seconds)
        for attempt in range(1, max_retries + 1):
            try:
                self.client.sync_inbox()
                for server_id in self._candidates(subject_hint):
                    raw = self.client.fetch_mime(server_id)
                    parsed = email.message_from_bytes(raw)
                    if self._unfold(parsed.get("Message-ID", "")) != expected:
                        self._misses.add(server_id)
                        continue
                    logger.info(f"✅ Mesaj ActiveSync'te bulundu (deneme {attempt}/{max_retries})")
                    details = self._extract_details(parsed, raw, f"eas:{server_id}")
                    details["eas_view"] = {**self.client.items[server_id],
                                           **self.client.fetch_view(server_id)}
                    return details
                self.last_error = None
            except Exception as e:  # noqa: BLE001 — her deneme bağımsız
                self.last_error = e
                logger.error(f"ActiveSync hatası (deneme {attempt}): {e}")
                if self.is_auth_error(e):
                    return None
            if attempt < max_retries:
                time.sleep(retry_interval)
        return None


class EasSender(MailSender):
    """Mesajı SMTP yerine Z-Push üzerinden (SendMail) gönderir — iPad'in
    gönderim yolu. Mesajın kurulması MailSender ile aynıdır; yalnızca taşıma
    değişir, böylece tüm senaryolar bu yoldan da koşabilir."""

    def __init__(self, server_config: dict, transport=None):
        self.config = server_config
        self.host = str(server_config.get("eas_url") or "")
        self.port = 443
        self.username = str(server_config.get("username") or "")
        self.password = str(server_config.get("password") or "")
        self.from_address = server_config["test_address"]
        self.auth_method = server_config.get("auth_method", "password")
        self.totp_secret = ""
        self.client = EasClient(server_config, transport=transport)

    def _connect(self):
        self.client.options()
        self.client.folder_sync()
        return self.client

    def _send(self, msg, to_address: str) -> float:
        self.client.send_mail(msg.as_bytes(policy=SMTP_POLICY))
        return time.time()

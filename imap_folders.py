"""
imap_folders.py — IMAP klasör adları, LIST ayrıştırma ve özel klasör bulma.

Projenin başka modüllerine bağımlı değildir (receiver.py da kullanır).

* Klasör adları kablo üzerinde "değiştirilmiş UTF-7" (RFC 3501 §5.1.3) ile
  kodlanır: "Çöp Kutusu" → "&AMcA9g-p Kutusu". Türkçe adlı klasörler bunsuz
  seçilemez.
* Çöp, Taslaklar, Gönderilmiş, Gereksiz klasörleri önce SPECIAL-USE
  (RFC 6154) bayraklarıyla, yoksa yaygın adlarla (Türkçe dahil) bulunur.
"""
from __future__ import annotations

import base64
import re
from typing import Optional

SPECIAL_USES = ("\\Trash", "\\Drafts", "\\Sent", "\\Junk", "\\Archive")
# SPECIAL-USE bildirmeyen sunucular için yaygın adlar (küçük harfe çevrilmiş,
# hiyerarşi önekinden arındırılmış hâliyle karşılaştırılır).
FALLBACK_NAMES = {
    "\\Trash": ("trash", "deleted items", "deleted messages", "deleted", "çöp", "çöp kutusu",
                "silinmiş öğeler", "silinmiş", "silinenler", "bin"),
    "\\Drafts": ("drafts", "draft", "taslaklar", "taslak"),
    "\\Sent": ("sent", "sent items", "sent messages", "sent mail", "gönderilmiş öğeler",
               "gönderilmiş", "gönderilenler", "giden kutusu"),
    "\\Junk": ("junk", "spam", "junk e-mail", "junk email", "bulk mail", "gereksiz",
               "gereksiz e-posta", "önemsiz", "önemsiz e-posta", "istenmeyen",
               "istenmeyen e-posta"),
    "\\Archive": ("archive", "arşiv", "archives"),
}


# ────────────────────────────────────────────────────────────────────
#  Değiştirilmiş UTF-7
# ────────────────────────────────────────────────────────────────────
def utf7_encode(name: str) -> str:
    out, buffer = [], []

    def flush():
        if buffer:
            raw = "".join(buffer).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            buffer.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buffer.append(ch)
    flush()
    return "".join(out)


def utf7_decode(name: str) -> str:
    def repl(match: re.Match) -> str:
        body = match.group(1)
        if not body:
            return "&"
        data = body.replace(",", "/")
        data += "=" * (-len(data) % 4)
        try:
            return base64.b64decode(data).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return match.group(0)
    return re.sub(r"&([A-Za-z0-9+,]*)-", repl, name)


def quote(name: str) -> str:
    """Kablo adı (UTF-7) → tırnaklı IMAP dizgisi. imaplib sürümleri arasında
    tırnaklama davranışı farklı olduğundan her zaman kendimiz tırnaklarız."""
    wire = utf7_encode(name)
    return '"' + wire.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ────────────────────────────────────────────────────────────────────
#  LIST
# ────────────────────────────────────────────────────────────────────
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$',
                      re.I)


def _unquote(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = re.sub(r'\\(.)', r"\1", text[1:-1])
    return text


def parse_list(data) -> list:
    """imaplib.list() verisini [{name, flags, delimiter}] listesine çevirir.

    Literal ile dönen adlar (``(b'... {12}', b'ad')`` demetleri) da desteklenir.
    """
    folders = []
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            head, literal = item[0], item[1]
            match = _LIST_RE.match(re.sub(rb"\{\d+\}$", b'""', head.strip()))
            raw_name = literal
        elif isinstance(item, (bytes, bytearray)):
            match = _LIST_RE.match(bytes(item).strip())
            raw_name = match.group("name") if match else b""
        else:
            continue
        if not match:
            continue
        delimiter = _unquote(match.group("delim")) if match.group("delim").upper() != b"NIL" else ""
        folders.append({"name": utf7_decode(_unquote(raw_name)),
                        "flags": match.group("flags").decode("ascii", "replace").split(),
                        "delimiter": delimiter})
    return folders


def _leaf(folder: dict) -> str:
    name = folder["name"]
    delim = folder.get("delimiter") or ""
    if delim and delim in name:
        name = name.rsplit(delim, 1)[-1]
    return name.strip().lower()


def find_special(folders: list, use: str) -> Optional[str]:
    """Özel klasörün adını döndürür: önce SPECIAL-USE bayrağı, sonra yaygın ad."""
    use_lower = use.lower()
    for folder in folders:
        if any(f.lower() == use_lower for f in folder["flags"]) and \
                "\\noselect" not in (f.lower() for f in folder["flags"]):
            return folder["name"]
    names = FALLBACK_NAMES.get(use, ())
    for folder in folders:
        if _leaf(folder) in names and "\\noselect" not in (f.lower() for f in folder["flags"]):
            return folder["name"]
    return None


def list_folders(imap) -> list:
    typ, data = imap.list('""', "*")
    if typ != "OK":
        return []
    return parse_list(data)


def uid_by_message_id(imap, folder: str, message_id: str) -> Optional[bytes]:
    """Klasörü seçip Message-ID'si eşleşen en yeni mesajın UID'sini döndürür."""
    typ, _ = imap.select(quote(folder))
    if typ != "OK":
        return None
    core = " ".join(str(message_id).split()).strip("<>")
    typ, data = imap.uid("SEARCH", None, "HEADER", "Message-ID", f'"{core}"')
    if typ != "OK" or not data or not data[0]:
        return None
    return data[0].split()[-1]


def find_in_junk(imap, message_id: str) -> Optional[str]:
    """Mesaj gereksiz (Junk/Spam) klasöründe mi? Klasör adını döndürür.

    Çağrıdan sonra seçili klasör değişmiş olabilir; çağıran yeniden INBOX seçmelidir.
    """
    folders = list_folders(imap)
    candidates = []
    special = find_special(folders, "\\Junk")
    if special:
        candidates.append(special)
    for folder in folders:
        if _leaf(folder) in FALLBACK_NAMES["\\Junk"] and folder["name"] not in candidates:
            candidates.append(folder["name"])
    for name in candidates:
        if uid_by_message_id(imap, name, message_id):
            return name
    return None

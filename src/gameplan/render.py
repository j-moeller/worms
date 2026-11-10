"""Making a file from its content spec (gameplan/content.py), on the node, when it is written.

    render(spec, values) -> bytes

`values` are the run's secrets ({secret id: {field: value}}, scripts/round_agent/handlers.py); every
marker in the spec's strings is replaced by its value first, so a document's text, a zip's
password and a vault's master password can be secrets of the run. The formats are written here
from their specifications, with the standard library, and `cryptography` for KeePass (a
dependency of Ansible, so on the node anyway; the round agent's role installs it).

What is made is a real file of its kind, not a look-alike: `file` names it, and the usual tools
open it (unzip and 7z; Word, LibreOffice, antiword and catdoc; KeePassXC and pykeepass).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import struct
import time
import uuid
import zlib
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from gameplan.content import totp_uri
from gameplan.definition import SECRET_MARKER

MAX_SIZE = 4 * 1024 * 1024  # a rendered file


PASSWORD_ALPHABETS = {
    "alnum": "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789",
    "digits": "0123456789",
    "hex": "0123456789abcdef",
}


def new_secret(secret):
    """{field: value} for a secret of the spec, new for this run (an SSH key pair is made by
    ssh-keygen: scripts/round_agent/handlers.py)."""
    import secrets

    if secret["kind"] == "totp":
        return {"secret": base64.b32encode(secrets.token_bytes(20)).decode()}
    if secret.get("charset"):
        alphabet = PASSWORD_ALPHABETS[secret["charset"]]
        return {"password": "".join(secrets.choice(alphabet) for _ in range(int(secret["length"])))}
    return {"password": secrets.token_urlsafe(15)}


def fill(spec, values):
    """The spec with the run's secrets in place of their markers, in every string."""
    if isinstance(spec, str):
        return SECRET_MARKER.sub(lambda m: values[m.group(1)][m.group(2)], spec)
    if isinstance(spec, dict):
        return {key: fill(value, values) for key, value in spec.items()}
    if isinstance(spec, list):
        return [fill(value, values) for value in spec]
    return spec


def render(spec, values, now=None) -> bytes:
    """The file's bytes. A string is UTF-8 text."""
    now = now or datetime.now(timezone.utc)
    data = _render(fill(spec, values), now)
    if len(data) > MAX_SIZE:
        raise ValueError(f"the file would be {len(data)} bytes, more than {MAX_SIZE}")
    return data


def _render(spec, now):
    if isinstance(spec, str):
        return spec.encode()
    return RENDERERS[spec["kind"]](spec, now)


# Characters XML 1.0 cannot hold at all (documents carry text, not control codes).
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _xml(text):
    return escape(_NOT_XML.sub("", text), {'"': "&quot;"})


# ---------------------------------------------------------------------------
# Zip, with ZipCrypto (traditional PKWARE encryption, APPNOTE 6.1)
# ---------------------------------------------------------------------------


def _crc_table():
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC = _crc_table()


class _ZipCrypto:
    def __init__(self, password: bytes):
        self.keys = [0x12345678, 0x23456789, 0x34567890]
        for byte in password:
            self._update(byte)

    def _update(self, byte):
        k = self.keys
        k[0] = (k[0] >> 8) ^ _CRC[(k[0] ^ byte) & 0xFF]
        k[1] = ((k[1] + (k[0] & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        k[2] = (k[2] >> 8) ^ _CRC[(k[2] ^ (k[1] >> 24)) & 0xFF]

    def encrypt(self, data: bytes) -> bytes:
        out = bytearray(len(data))
        for i, byte in enumerate(data):
            temp = (self.keys[2] | 2) & 0xFFFF
            out[i] = byte ^ (((temp * (temp ^ 1)) >> 8) & 0xFF)
            self._update(byte)
        return bytes(out)


def _dos_time(now):
    local = now.astimezone(timezone.utc)
    return ((local.hour << 11) | (local.minute << 5) | (local.second // 2),
            ((max(local.year, 1980) - 1980) << 9) | (local.month << 5) | local.day)  # fmt: skip


def zip_bytes(files, now, password=None, stored=()):
    """A zip of [(name, bytes)]. `stored`: names kept uncompressed (an ODF mimetype)."""
    out, central = io.BytesIO(), []
    mtime, mdate = _dos_time(now)
    for name, data in files:
        raw_name = name.encode()
        crc = zlib.crc32(data) & 0xFFFFFFFF
        if name in stored:
            method, body = 0, data
        else:
            compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
            method, body = 8, compressor.compress(data) + compressor.flush()
        flags = 0x0800 if not raw_name.isascii() else 0
        if password is not None:
            flags |= 0x0001
            # 11 random bytes, then the CRC's high byte: what unzip checks the password against.
            body = _ZipCrypto(password.encode()).encrypt(os.urandom(11) + bytes([crc >> 24]) + body)
        offset = out.tell()
        out.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, method, mtime, mdate, crc, len(body), len(data),
                              len(raw_name), 0) + raw_name + body)  # fmt: skip
        central.append(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 0x031E, 20, flags, method, mtime, mdate, crc,
                                   len(body), len(data), len(raw_name), 0, 0, 0, 0, 0o100644 << 16, offset) + raw_name)  # fmt: skip
    start = out.tell()
    for record in central:
        out.write(record)
    size = out.tell() - start
    out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central), size, start, 0))
    return out.getvalue()


def _zip(spec, now):
    files = [(entry["name"], _render(entry["content"], now)) for entry in spec["files"]]
    return zip_bytes(files, now, password=spec.get("password"))


# ---------------------------------------------------------------------------
# Office Open XML (.docx) and OpenDocument (.ods)
# ---------------------------------------------------------------------------


def _docx(spec, now):
    body = "".join(
        '<w:p><w:r><w:t xml:space="preserve">' + _xml(p).replace("\t", '</w:t><w:tab/><w:t xml:space="preserve">') + "</w:t></w:r></w:p>"
        for p in spec["paragraphs"]
    )
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    parts = [
        ("[Content_Types].xml", head + (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            "</Types>")),
        ("_rels/.rels", head + (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
            'Target="docProps/core.xml"/>'
            "</Relationships>")),
        ("docProps/core.xml", head + (
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{_xml(spec['title'])}</dc:title>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
            "</cp:coreProperties>")),
        ("word/document.xml", head + (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}<w:sectPr/></w:body></w:document>")),
    ]  # fmt: skip
    return zip_bytes([(name, text.encode()) for name, text in parts], now)


ODS_MIME = "application/vnd.oasis.opendocument.spreadsheet"


def _cell(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (f'<table:table-cell office:value-type="float" office:value="{value!r}">'
                f"<text:p>{value!r}</text:p></table:table-cell>")  # fmt: skip
    return f'<table:table-cell office:value-type="string"><text:p>{_xml(str(value))}</text:p></table:table-cell>'


def _ods(spec, now):
    tables = "".join(
        f'<table:table table:name="{_xml(sheet["name"])}">'
        + "".join("<table:table-row>" + "".join(_cell(c) for c in row) + "</table:table-row>" for row in sheet["rows"])
        + "</table:table>"
        for sheet in spec["sheets"]
    )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">'
        f"<office:body><office:spreadsheet>{tables}</office:spreadsheet></office:body></office:document-content>"
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
        f'<manifest:file-entry manifest:full-path="/" manifest:version="1.2" manifest:media-type="{ODS_MIME}"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        "</manifest:manifest>"
    )
    # The mimetype first and uncompressed: how `file` and every ODF reader recognise the package.
    parts = [("mimetype", ODS_MIME.encode()), ("META-INF/manifest.xml", manifest.encode()), ("content.xml", content.encode())]
    return zip_bytes(parts, now, stored=("mimetype",))


# ---------------------------------------------------------------------------
# Word 97-2003 (.doc): the WordDocument and 1Table streams of [MS-DOC] in an OLE compound file
# ([MS-CFB]). The least Word itself writes: the text in one piece, one character run, a
# paragraph run per paragraph, the Normal style, one font, one section, default settings.
# ---------------------------------------------------------------------------

_FREE, _END, _FAT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
_SECTOR = 512
_MINI_CUTOFF = 4096
WORD_CLSID = uuid.UUID("00020906-0000-0000-c000-000000000046").bytes_le


def _cfb(streams, clsid):
    """An OLE compound file (version 3) holding `streams` ({name: bytes}) under its root.
    Every stream is at least 4096 bytes, so none goes into the mini stream."""
    streams = {name: data.ljust(_MINI_CUTOFF, b"\0") for name, data in streams.items()}
    names = sorted(streams, key=lambda n: (len(n), n.upper()))  # the compound file's own order
    if len(names) > 3:
        raise ValueError("at most 3 streams")
    dir_sectors = -(-(len(names) + 1) * 128 // _SECTOR)
    data_sectors = [-(-len(streams[n]) // _SECTOR) for n in names]
    fat_sectors = 1
    while fat_sectors * 128 < fat_sectors + dir_sectors + sum(data_sectors):
        fat_sectors += 1
    if fat_sectors > 109:
        raise ValueError("the document is too large")
    fat = [_FAT] * fat_sectors
    first_dir = len(fat)
    fat += list(range(first_dir + 1, first_dir + dir_sectors)) + [_END]
    starts = []
    for count in data_sectors:
        starts.append(len(fat))
        fat += list(range(len(fat) + 1, len(fat) + count)) + [_END]
    fat += [_FREE] * (fat_sectors * 128 - len(fat))

    def entry(name, kind, color=1, left=_FREE, right=_FREE, child=_FREE, cls=b"", start=0, size=0):
        encoded = name.encode("utf-16-le") + b"\0\0" if name else b""
        return (encoded.ljust(64, b"\0") + struct.pack("<HBBIII", len(encoded), kind, color, left, right, child)
                + cls.ljust(16, b"\0") + b"\0" * 20 + struct.pack("<IQ", start, size))  # fmt: skip

    # The root's children as a binary tree: the middle one is the root (black), the others
    # hang off it to the left and right (red), which keeps it a valid red-black tree for up to 3.
    middle = len(names) // 2
    entries = [entry("Root Entry", 5, child=1 + middle, cls=clsid, start=_END)]
    for i, name in enumerate(names):
        left = 1 + i - 1 if i == middle and i > 0 else _FREE
        right = 1 + i + 1 if i == middle and i + 1 < len(names) else _FREE
        entries.append(entry(name, 2, color=1 if i == middle else 0, left=left, right=right, start=starts[i], size=len(streams[name])))
    directory = b"".join(entries).ljust(dir_sectors * _SECTOR, b"\0")
    # Unused entries: no siblings and no child.
    directory = bytearray(directory)
    for index in range(len(entries), dir_sectors * 4):
        struct.pack_into("<III", directory, index * 128 + 68, _FREE, _FREE, _FREE)

    difat = [*range(fat_sectors)] + [_FREE] * (109 - fat_sectors)
    header = (bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 16
              + struct.pack("<HHHHH6sIIIIIIIII", 0x003E, 0x0003, 0xFFFE, 9, 6, b"", 0, fat_sectors, first_dir, 0,
                            _MINI_CUTOFF, _END, 0, _END, 0)
              + struct.pack("<109I", *difat))  # fmt: skip
    body = struct.pack(f"<{len(fat)}I", *fat) + bytes(directory) + b"".join(streams[n].ljust(c * _SECTOR, b"\0") for n, c in zip(names, data_sectors))
    return header + body


def _summary_information(title, now):
    """The \\x05SummaryInformation property set ([MS-OLEPS]): code page, title, application."""
    filetime = int((now.timestamp() + 11644473600) * 10_000_000)

    def lpstr(text):
        raw = text.encode("cp1252", "replace") + b"\0"
        return struct.pack("<HHI", 0x001E, 0, len(raw)) + raw.ljust(-(-len(raw) // 4) * 4, b"\0")

    props = [(1, struct.pack("<HHhH", 0x0002, 0, 1252, 0)), (2, lpstr(title)),
             (12, struct.pack("<HHQ", 0x0040, 0, filetime)), (13, struct.pack("<HHQ", 0x0040, 0, filetime)),
             (18, lpstr("Microsoft Office Word"))]  # fmt: skip
    offset, table, values = 8 + 8 * len(props), b"", b""
    for pid, value in props:
        table += struct.pack("<II", pid, offset + len(values))
        values += value
    section = struct.pack("<II", 8 + len(table) + len(values), len(props)) + table + values
    fmtid = uuid.UUID("f29f85e0-4ff9-1068-ab91-08002b27b3d9").bytes_le
    return struct.pack("<HHI16sI16sI", 0xFFFE, 0, 0x00020006, b"", 1, fmtid, 48) + section


_DOC_TEXT_START = 0x0600


def _doc_fkp_papx(fcs):
    """One PAPX page: paragraphs [fcs[i], fcs[i+1]), all in the Normal style."""
    page = bytearray(_SECTOR)
    count = len(fcs) - 1
    struct.pack_into(f"<{len(fcs)}I", page, 0, *fcs)
    papx = 506  # PapxInFkp: cb=0, cb'=1, then istd 0 (Normal); at an even offset
    page[papx:papx + 4] = b"\x00\x01\x00\x00"
    for i in range(count):
        page[4 * (count + 1) + 13 * i] = papx // 2  # BxPap: the PAPX's word offset, then a zero PHE
    page[511] = count
    return bytes(page)


def _doc_stylesheet():
    name = "Normal".encode("utf-16-le")
    std = (struct.pack("<HHHHH", 0x0000, (0x0FFF << 4) | 1, 2, 0, 0)  # sti 0, paragraph style, no base, 2 UPXs
           + struct.pack("<H", 6) + name + b"\0\0"
           + struct.pack("<HH", 2, 0)  # UpxPapx: istd 0
           + struct.pack("<H", 0))  # UpxChpx: empty  # fmt: skip
    std = std[:6] + struct.pack("<H", len(std)) + std[8:]  # bchUpe
    stshi = struct.pack("<HHHHHH3HH", 15, 0x000A, 0x0001, 0x005B, 0x000F, 0, 0, 0, 0, 0)
    return (struct.pack("<H", len(stshi)) + stshi + struct.pack("<H", len(std)) + std
            + struct.pack("<H", 0) * 14)  # fmt: skip


def _doc_fonts():
    name = "Times New Roman".encode("utf-16-le") + b"\0\0"
    ffn = struct.pack("<BHBB", 0x16, 400, 0, 0) + b"\0" * 10 + b"\0" * 24 + name
    return struct.pack("<HH", 1, 0) + bytes([len(ffn)]) + ffn


def _doc(spec, now):
    paragraphs = [_NOT_XML.sub("", p).replace("\r", "") for p in spec["paragraphs"]] or [""]
    text = "".join(p + "\r" for p in paragraphs)  # every paragraph ends with its mark
    try:
        raw, compressed = text.encode("cp1252"), True
    except UnicodeEncodeError:
        raw, compressed = text.encode("utf-16-le"), False
    width = 1 if compressed else 2
    start = _DOC_TEXT_START
    end = start + len(raw)

    word = bytearray(start) + raw
    word += b"\0" * (-len(word) % _SECTOR)
    # One character run, default formatting.
    chpx_page = len(word) // _SECTOR
    chpx = bytearray(_SECTOR)
    struct.pack_into("<II", chpx, 0, start, end)
    chpx[511] = 1
    word += chpx
    # The paragraph runs, 29 to a page.
    bounds, fc = [], start
    for p in paragraphs:
        bounds.append(fc)
        fc += (len(p) + 1) * width
    bounds.append(end)
    papx_pages, papx_first = [], []
    for i in range(0, len(paragraphs), 29):
        papx_pages.append(len(word) // _SECTOR)
        papx_first.append(bounds[i])
        word += _doc_fkp_papx(bounds[i:i + 30])
    cb_mac = len(word)

    table = bytearray()

    def put(blob):
        offset = len(table)
        table.extend(blob)
        return offset, len(blob)

    ccp = len(text)
    stsh = put(_doc_stylesheet())
    sed = put(struct.pack("<II", 0, ccp) + struct.pack("<HIHI", 0, _FREE, 0, _FREE))
    bte_chpx = put(struct.pack("<III", start, end, chpx_page))
    bte_papx = put(struct.pack(f"<{len(papx_first) + 1}I", *papx_first, end) + struct.pack(f"<{len(papx_pages)}I", *papx_pages))
    fonts = put(_doc_fonts())
    dop = bytearray(500)
    struct.pack_into("<H", dop, 0x0A, 720)  # default tab stop: half an inch
    dop_at = put(bytes(dop))
    piece = (start * 2) | 0x40000000 if compressed else start
    plc_pcd = struct.pack("<II", 0, ccp) + struct.pack("<HIH", 0, piece, 0)
    clx = put(b"\x02" + struct.pack("<I", len(plc_pcd)) + plc_pcd)

    fc_lcb = [(0, 0)] * 93
    for index, value in ((0, stsh), (1, stsh), (6, sed), (12, bte_chpx), (13, bte_papx), (15, fonts), (31, dop_at), (33, clx)):
        fc_lcb[index] = value
    fib_base = struct.pack("<HHHHHHHIBBHHII", 0xA5EC, 0x00C1, 0, 0x0409, 0, 0x1200, 0x00BF, 0, 0, 0, 0, 0, start, end)
    lw = [0] * 22
    lw[0], lw[3] = cb_mac, ccp
    fib = (fib_base + struct.pack("<H", 14) + b"\0" * 28 + struct.pack("<H", 22) + struct.pack("<22I", *lw)
           + struct.pack("<H", 93) + b"".join(struct.pack("<II", f, n) for f, n in fc_lcb) + struct.pack("<H", 0))  # fmt: skip
    word[: len(fib)] = fib
    streams = {"WordDocument": bytes(word), "1Table": bytes(table)}
    streams["\x05SummaryInformation"] = _summary_information(spec["title"], now)
    return _cfb(streams, WORD_CLSID)


# ---------------------------------------------------------------------------
# KeePass (KDBX 4.0): AES-256-CBC, AES-KDF, ChaCha20 for the protected values
# ---------------------------------------------------------------------------

KDBX_AES256 = uuid.UUID("31c1f2e6-bf71-4350-be58-05216afc5aff").bytes
KDBX_AES_KDF = uuid.UUID("c9d9f39a-628a-4460-bf74-0d08c18a4fea").bytes
KDBX_ROUNDS = 60_000
_EPOCH_0001 = 62135596800  # seconds from 0001-01-01 to 1970-01-01


def _variant_dict(items):
    out = struct.pack("<H", 0x0100)
    for kind, key, value in items:
        raw_key = key.encode()
        out += struct.pack("<Bi", kind, len(raw_key)) + raw_key + struct.pack("<i", len(value)) + value
    return out + b"\0"


def _kdbx_time(now):
    return base64.b64encode(struct.pack("<q", int(now.timestamp()) + _EPOCH_0001)).decode()


def _kdbx_xml(spec, now, protect):
    stamp = _kdbx_time(now)
    times = (f"<Times><CreationTime>{stamp}</CreationTime><LastModificationTime>{stamp}</LastModificationTime>"
             f"<LastAccessTime>{stamp}</LastAccessTime><ExpiryTime>{stamp}</ExpiryTime><Expires>False</Expires>"
             f"<UsageCount>0</UsageCount><LocationChanged>{stamp}</LocationChanged></Times>")  # fmt: skip

    def uid():
        return base64.b64encode(os.urandom(16)).decode()

    def field(key, value, protected=False):
        if protected:
            return f'<String><Key>{key}</Key><Value Protected="True">{protect(value)}</Value></String>'
        return f"<String><Key>{key}</Key><Value>{_xml(value)}</Value></String>"

    def entry(e):
        fields = [field("Title", e["title"]), field("UserName", e["username"]), field("Password", e["password"], True),
                  field("URL", e["url"]), field("Notes", e["notes"])]  # fmt: skip
        if e["otp"]:
            fields.append(field("otp", e["otp"], True))
        return f"<Entry><UUID>{uid()}</UUID>{times}{''.join(fields)}</Entry>"

    groups = "".join(
        f"<Group><UUID>{uid()}</UUID><Name>{_xml(g['name'])}</Name>{times}<IsExpanded>True</IsExpanded>"
        + "".join(entry(e) for e in g["entries"]) + "</Group>"
        for g in spec["groups"]
    )
    if len(spec["groups"]) == 1 and spec["groups"][0]["name"] == spec["name"]:
        root = groups  # one group: it is the root
    else:
        root = f"<Group><UUID>{uid()}</UUID><Name>{_xml(spec['name'])}</Name>{times}<IsExpanded>True</IsExpanded>{groups}</Group>"
    return ('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<KeePassFile><Meta>'
            f"<Generator>KeePassXC</Generator><DatabaseName>{_xml(spec['name'])}</DatabaseName>"
            f"<DatabaseNameChanged>{stamp}</DatabaseNameChanged>"
            "<MemoryProtection><ProtectTitle>False</ProtectTitle><ProtectUserName>False</ProtectUserName>"
            "<ProtectPassword>True</ProtectPassword><ProtectURL>False</ProtectURL><ProtectNotes>False</ProtectNotes>"
            f"</MemoryProtection></Meta><Root>{root}</Root></KeePassFile>")  # fmt: skip


def kdbx(spec, now, rounds=KDBX_ROUNDS):
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    master_seed, iv, kdf_seed, inner_key = os.urandom(32), os.urandom(16), os.urandom(32), os.urandom(64)

    def header_field(fid, data):
        return struct.pack("<BI", fid, len(data)) + data

    kdf = _variant_dict([(0x42, "$UUID", KDBX_AES_KDF), (0x05, "R", struct.pack("<Q", rounds)), (0x42, "S", kdf_seed)])
    header = (struct.pack("<III", 0x9AA2D903, 0xB54BFB67, 0x00040000)
              + header_field(2, KDBX_AES256) + header_field(3, struct.pack("<I", 1)) + header_field(4, master_seed)
              + header_field(7, iv) + header_field(11, kdf) + header_field(0, b"\r\n\r\n"))  # fmt: skip

    # The key: SHA-256 of the password's SHA-256, run through AES-KDF.
    key = hashlib.sha256(hashlib.sha256(spec["password"].encode()).digest()).digest()
    encryptor = Cipher(algorithms.AES(kdf_seed), modes.ECB()).encryptor()
    for _ in range(rounds):
        key = encryptor.update(key)
    transformed = hashlib.sha256(key).digest()
    cipher_key = hashlib.sha256(master_seed + transformed).digest()
    hmac_base = hashlib.sha512(master_seed + transformed + b"\x01").digest()

    def block_key(index):
        return hashlib.sha512(struct.pack("<Q", index) + hmac_base).digest()

    inner = hashlib.sha512(inner_key).digest()
    stream = Cipher(algorithms.ChaCha20(inner[:32], b"\0" * 4 + inner[32:44]), mode=None).encryptor()

    def protect(value):
        return base64.b64encode(stream.update(value.encode())).decode()

    xml = _kdbx_xml(spec, now, protect).encode()
    inner_header = header_field(1, struct.pack("<I", 3)) + header_field(2, inner_key) + header_field(0, b"")
    payload = gzip.compress(inner_header + xml, mtime=0)
    padder = padding.PKCS7(128).padder()
    aes = Cipher(algorithms.AES(cipher_key), modes.CBC(iv)).encryptor()
    encrypted = aes.update(padder.update(payload) + padder.finalize()) + aes.finalize()

    out = header + hashlib.sha256(header).digest() + hmac.new(block_key(0xFFFFFFFFFFFFFFFF), header, hashlib.sha256).digest()
    blocks = [encrypted[i:i + 1024 * 1024] for i in range(0, len(encrypted), 1024 * 1024)] + [b""]
    for index, block in enumerate(blocks):
        size = struct.pack("<i", len(block))
        mac = hmac.new(block_key(index), struct.pack("<Q", index) + size + block, hashlib.sha256).digest()
        out += mac + size + block
    return out


# ---------------------------------------------------------------------------
# TOTP: an authenticator's export, and the codes themselves
# ---------------------------------------------------------------------------


def _totp_export(spec, now):
    if spec["format"] == "uri":
        return "".join(totp_uri(t) + "\n" for t in spec["totps"]).encode()
    entries = [
        {"type": "totp", "uuid": str(uuid.uuid4()), "name": t["account"], "issuer": t["issuer"], "note": "",
         "favorite": False, "icon": None,
         "info": {"secret": t["secret"], "algo": t["algorithm"], "digits": t["digits"], "period": t["period"]},
         "groups": []}
        for t in spec["totps"]
    ]  # fmt: skip
    export = {"version": 1, "header": {"slots": None, "params": None}, "db": {"version": 3, "entries": entries, "groups": []}}
    return (json.dumps(export, indent=4) + "\n").encode()


def totp_code(secret, at=None, digits=6, period=30, algorithm="SHA1"):
    """The code an authenticator shows for `secret` (base32) at `at` (RFC 6238)."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, algorithm.lower())).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


RENDERERS = {
    "zip": _zip,
    "doc": _doc,
    "docx": _docx,
    "ods": _ods,
    "kdbx": kdbx,
    "totp_export": _totp_export,
}


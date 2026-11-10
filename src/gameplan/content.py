"""What a file holds besides plain text: documents, archives, vaults and TOTP exports.

A gameplan describes them in `__init__` and writes them like text:

    vault = self.generate_password()
    self.vm0.write_file("~/notes.doc", Doc(["Remember:", f"vault: {vault}"]))
    self.vm0.write_file("~/passwords.kdbx", KeePass(vault, [Entry("db", login.user, login.pwd)]))
    self.vm1.write_file("~/backup.zip", Zip({"id_ops": key.private}, password=login.pwd))

They are descriptions only, plain data in the spec. The node makes the file when it writes it
(gameplan/render.py), with the run's secrets in place: a document's text, a zip's password and a
vault's master password can be any of them (`login.pwd`, `key.private`, `totp.secret`, a
`generate_password()`, ...). Nothing of the file passes the database.

The kinds nest where a format holds files: a Zip holds text and any of the others.
"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import quote

from gameplan.errors import GameplanError

# The text a description holds, all strings together (the rendered file is larger: a document's
# container, a vault's encryption).
MAX_TEXT = 64 * 1024
MAX_ITEMS = 1000  # paragraphs, rows, cells, entries, files: each
MAX_DEPTH = 4  # a zip in a zip in a zip ...
ZIP_NAME = re.compile(r"^[A-Za-z0-9._+@ -]+(/[A-Za-z0-9._+@ -]+)*$")
SHEET_NAME = re.compile(r"^[^\[\]*?:/\\']{1,31}$")
TOTP_ALGORITHMS = ("SHA1", "SHA256", "SHA512")
TOTP_FORMATS = ("aegis", "uri")


def totp_uri(totp):
    """The otpauth:// URI of a TOTP spec (definition.TOTP.spec()): what a QR code and most
    authenticator exports hold."""
    label = quote(f"{totp['issuer']}:{totp['account']}", safe=":@")
    return (f"otpauth://totp/{label}?secret={totp['secret']}&issuer={quote(totp['issuer'])}"
            f"&algorithm={totp['algorithm']}&digits={totp['digits']}&period={totp['period']}")  # fmt: skip


# A description that cannot be made into a file.
ContentError = GameplanError


def _text(value, what):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ContentError(f"{what} is text (or a number)")
    return str(value)


def _texts(value, what):
    """A list of strings; a single string is split into its lines."""
    if isinstance(value, str):
        return value.split("\n")
    if not isinstance(value, (list, tuple)):
        raise ContentError(f"{what} is a string or a list of strings")
    return [_text(item, what) for item in value]


class Content:
    """A file the node makes when it writes it (see the module)."""

    kind = ""

    def spec(self) -> dict:
        raise NotImplementedError

    def __repr__(self):
        return f"{type(self).__name__}()"


def spec_of(value, what="content"):
    """The spec of a file's content: a string as it is, a Content as its description."""
    if isinstance(value, Content):
        return value.spec()
    if isinstance(value, str):
        return str(value)
    raise ContentError(f"{what} is text or one of Doc, Docx, Ods, Zip, KeePass, TotpExport")


def strings(spec):
    """Every string in a content spec: what may hold the run's secrets."""
    if isinstance(spec, str):
        yield spec
    elif isinstance(spec, dict):
        for value in spec.values():
            yield from strings(value)
    elif isinstance(spec, (list, tuple)):
        for value in spec:
            yield from strings(value)


def check(spec, depth=0):
    """Every problem of a content spec, for validation (the node renders only what passes)."""
    if isinstance(spec, str):
        return []
    if depth >= MAX_DEPTH:
        return [f"content is nested more than {MAX_DEPTH} deep"]
    problems = []
    size = sum(len(s.encode()) for s in strings(spec))
    if size > MAX_TEXT:
        problems.append(f"a {spec.get('kind')} holds more than {MAX_TEXT} bytes of text")
    if spec.get("kind") == "zip":
        for entry in spec["files"]:
            problems += check(entry["content"], depth + 1)
    return problems


# -- documents ----------------------------------------------------------------------------


class Doc(Content):
    """A Word document in the old binary format (.doc, Word 97-2003): one paragraph per item,
    or per line of a string. Word, LibreOffice, antiword and catdoc read it."""

    kind = "doc"

    def __init__(self, paragraphs, title=""):
        self.paragraphs = _texts(paragraphs, f"{type(self).__name__} paragraphs")
        self.title = _text(title, f"{type(self).__name__} title")
        if len(self.paragraphs) > MAX_ITEMS:
            raise ContentError(f"{type(self).__name__}: at most {MAX_ITEMS} paragraphs")

    def spec(self):
        return {"kind": self.kind, "paragraphs": list(self.paragraphs), "title": self.title}


class Docx(Doc):
    """A Word document in the current format (.docx, Office Open XML): as Doc."""

    kind = "docx"


class Ods(Content):
    """A spreadsheet (.ods, OpenDocument): {sheet name: rows}, or rows for one sheet called
    "Sheet1". A row is a list of cells; a cell is text or a number.

        Ods({"Accounts": [["host", "user", "password"], ["sandbox-vm2", login.user, login.pwd]]})
    """

    kind = "ods"

    def __init__(self, sheets):
        if isinstance(sheets, (list, tuple)):
            sheets = {"Sheet1": sheets}
        if not isinstance(sheets, dict) or not sheets:
            raise ContentError("Ods: give {sheet name: rows} or a list of rows")
        self.sheets = {}
        cells = 0
        for name, rows in sheets.items():
            if not isinstance(name, str) or not SHEET_NAME.fullmatch(name):
                raise ContentError(f"Ods: {name!r} is not a sheet name (1-31 characters, none of []*?:/\\')")
            if not isinstance(rows, (list, tuple)):
                raise ContentError(f"Ods: the rows of {name} are a list")
            out = []
            for row in rows:
                if not isinstance(row, (list, tuple)):
                    raise ContentError(f"Ods: a row of {name} is a list of cells")
                for cell in row:
                    if isinstance(cell, bool) or not isinstance(cell, (str, int, float)):
                        raise ContentError(f"Ods: a cell of {name} is text or a number")
                out.append(list(row))
                cells += len(row)
            self.sheets[name] = out
        if cells > MAX_ITEMS * 10:
            raise ContentError(f"Ods: at most {MAX_ITEMS * 10} cells")

    def spec(self):
        return {"kind": self.kind, "sheets": [{"name": name, "rows": rows} for name, rows in self.sheets.items()]}


# -- archives -----------------------------------------------------------------------------


class Zip(Content):
    """A zip archive: {name in the archive: content}, each text or another kind (a Doc, a
    KeePass, a Zip, ...). With `password`, every file in it is encrypted (ZipCrypto, which unzip,
    7z, Python and every archiver open; it is weak, as everywhere it is used).

        Zip({"backup/id_ops": key.private, "readme.txt": "restore with ..."}, password=pw)
    """

    kind = "zip"

    def __init__(self, files, password=None):
        if not isinstance(files, dict) or not files:
            raise ContentError("Zip: give {name: content}")
        if len(files) > MAX_ITEMS:
            raise ContentError(f"Zip: at most {MAX_ITEMS} files")
        self.files = {}
        for name, content in files.items():
            if (not isinstance(name, str) or len(name) > 200 or not ZIP_NAME.fullmatch(name)
                    or posixpath.normpath(name) != name or ".." in name.split("/")):  # fmt: skip
                raise ContentError(f"Zip: {name!r} is not a name in an archive (relative, letters, digits, ._+@-, /)")
            self.files[name] = content
        self.password = None if password is None else _text(password, "Zip password")
        if self.password == "":
            raise ContentError("Zip: an empty password; leave it out for no encryption")

    def spec(self):
        return {
            "kind": self.kind,
            "files": [{"name": name, "content": spec_of(content, f"Zip {name}")} for name, content in self.files.items()],
            "password": self.password,
        }


# -- vaults and authenticators --------------------------------------------------------------


class Entry:
    """A KeePass entry. `otp`: a TOTP (generate_totp), which KeePassXC shows as the entry's
    one-time code."""

    def __init__(self, title, username="", password="", url="", notes="", otp=None):
        self.title = _text(title, "Entry title")
        self.username = _text(username, "Entry username")
        self.password = _text(password, "Entry password")
        self.url = _text(url, "Entry url")
        self.notes = _text(notes, "Entry notes")
        if otp is not None and not hasattr(otp, "uri"):
            raise ContentError("Entry otp: give a TOTP from self.generate_totp()")
        self.otp = otp

    def spec(self):
        return {
            "title": self.title, "username": self.username, "password": self.password, "url": self.url,
            "notes": self.notes, "otp": self.otp.uri if self.otp is not None else "",
        }  # fmt: skip


class KeePass(Content):
    """A KeePass database (.kdbx, KDBX 4) locked with `password`: its entries in one group, or
    {group name: entries}. KeePass, KeePassXC, keepassxc-cli and pykeepass open it.

        KeePass(vault_pw, [Entry("db", login.user, login.pwd, url="ssh://sandbox-vm2")])
    """

    kind = "kdbx"

    def __init__(self, password, entries, name="Passwords"):
        self.password = _text(password, "KeePass password")
        if not self.password:
            raise ContentError("KeePass: the database needs a password")
        self.name = _text(name, "KeePass name")
        groups = entries if isinstance(entries, dict) else {self.name: entries}
        self.groups = {}
        count = 0
        for group, items in groups.items():
            group = _text(group, "KeePass group")
            if not isinstance(items, (list, tuple)) or not all(isinstance(e, Entry) for e in items):
                raise ContentError("KeePass: entries are a list of Entry")
            self.groups[group] = list(items)
            count += len(items)
        if count > MAX_ITEMS:
            raise ContentError(f"KeePass: at most {MAX_ITEMS} entries")

    def spec(self):
        return {
            "kind": self.kind, "password": self.password, "name": self.name,
            "groups": [{"name": group, "entries": [e.spec() for e in entries]} for group, entries in self.groups.items()],
        }  # fmt: skip


class TotpExport(Content):
    """What an authenticator app exports, unencrypted: the TOTP secrets in it (generate_totp).

      format="aegis"  Aegis's plain JSON export (aegis-export-plain-*.json)
      format="uri"    one otpauth:// URI per line, as many apps export and import
    """

    kind = "totp_export"

    def __init__(self, totps, format="aegis"):
        totps = list(totps) if isinstance(totps, (list, tuple)) else [totps]
        if not totps or not all(hasattr(t, "uri") for t in totps):
            raise ContentError("TotpExport: give TOTPs from self.generate_totp()")
        if format not in TOTP_FORMATS:
            raise ContentError(f"TotpExport: the format is one of {', '.join(TOTP_FORMATS)}")
        self.totps, self.format = totps, format

    def spec(self):
        return {"kind": self.kind, "format": self.format, "totps": [t.spec() for t in self.totps]}

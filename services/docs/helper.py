"""heypdf-docs inspection helper: every parse of an untrusted file happens here, in a short-lived child process
that the worker starts under prlimit (address space, CPU, file size) with a timeout. Never imported by the
worker; run as ``python3 -I helper.py <command> <args...>``. A password, if any, arrives on stdin.
Prints exactly one JSON object on stdout: the result, or ``{"error": code, "detail": text}``.

Commands
  office <src> <ext> <dst> <zip_max_bytes> <zip_max_ratio> <zip_max_entries> <text_max_bytes>
      classify an Office upload by content (OOXML / ODF / OLE / RTF / text), enforce the zip-bomb limits by
      actually inflating every entry, detect encryption; UTF-16 text is transcoded to UTF-8 into <dst>, and a
      DOCX / XLSX / ODF / RTF document whose file-name fields (Excel: the &Z header code) would print the server's
      job path is rewritten into <dst> with those fields frozen ("normalized": true in both cases).
  office-decrypt <src> <dst>         decrypt a password-protected Office file (OOXML or 97-2003)
  office-encrypt <src> <dst>         (tests only) encrypt an OOXML file
  pdf <src> <dst>                    page count, encryption, forms, signatures, PDF/A claim; writes a decrypted /
                                     header-normalised copy to <dst> when needed ("normalized": true)
  repair <src> <dst>                 qpdf (pikepdf) reconstruction + content-stream check; "clean": the written file
                                     reopens without a single qpdf warning
"""
from __future__ import annotations

import csv
import html
import json
import re
import shutil
import sys
import zipfile
from typing import Any

MiB = 1024 * 1024
RATIO_MIN_BYTES = 1 * MiB          # the ratio rule applies to entries that inflate beyond this
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
OOXML_TYPES = (  # main content types -> (doc_type, canonical extension)
    (b"wordprocessingml.document.main", "text", "docx"),
    (b"wordprocessingml.template.main", "text", "docx"),
    (b"ms-word.document.macroEnabled.main", "text", "docx"),
    (b"ms-word.template.macroEnabledTemplate.main", "text", "docx"),
    (b"spreadsheetml.sheet.main", "spreadsheet", "xlsx"),
    (b"spreadsheetml.template.main", "spreadsheet", "xlsx"),
    (b"ms-excel.sheet.macroEnabled.main", "spreadsheet", "xlsx"),
    (b"ms-excel.sheet.binary.macroEnabled.main", "spreadsheet", "xlsx"),
    (b"presentationml.presentation.main", "presentation", "pptx"),
    (b"presentationml.slideshow.main", "presentation", "pptx"),
    (b"presentationml.template.main", "presentation", "pptx"),
    (b"ms-powerpoint.presentation.macroEnabled.main", "presentation", "pptx"),
)
ODF_TYPES = {"text": ("text", "odt"), "spreadsheet": ("spreadsheet", "ods"), "presentation": ("presentation", "odp")}
LEGACY_EXT = {"doc": "text", "xls": "spreadsheet", "ppt": "presentation"}


class Refused(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")


# --------------------------------------------------------------------------------------------- zip / OOXML / ODF
def check_zip(path: str, max_bytes: int, max_ratio: int, max_entries: int) -> tuple[zipfile.ZipFile, dict]:
    """Open a zip and inflate every entry with hard caps (headers can lie; the bytes cannot)."""
    try:
        z = zipfile.ZipFile(path)
        infos = z.infolist()
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError) as e:
        raise Refused("unreadable", "The document is damaged (its zip structure could not be read).") from e
    if len(infos) > max_entries:
        raise Refused("suspicious_archive", f"The document has too many internal parts ({len(infos)}).")
    declared = sum(i.file_size for i in infos)
    if declared > max_bytes:
        raise Refused("too_large", f"The document unpacks to more than {max_bytes // MiB} MB.")
    total = 0
    for i in infos:
        if i.flag_bits & 0x1:
            raise Refused("encrypted_unsupported", "Zip-level encryption is not supported.")
        if i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise Refused("unreadable", "The document uses an unsupported compression method.")
        if i.file_size > RATIO_MIN_BYTES and i.file_size > max_ratio * max(i.compress_size, 1):
            raise Refused("suspicious_archive", "The document is compressed suspiciously well (possible zip bomb).")
        n = 0
        try:
            with z.open(i) as f:
                while chunk := f.read(MiB):
                    n += len(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise Refused("too_large", f"The document unpacks to more than {max_bytes // MiB} MB.")
                    if n > RATIO_MIN_BYTES and n > max_ratio * max(i.compress_size, 1):
                        raise Refused("suspicious_archive",
                                      "The document is compressed suspiciously well (possible zip bomb).")
        except Refused:
            raise
        except (zipfile.BadZipFile, OSError, EOFError, ValueError, RuntimeError, NotImplementedError) as e:
            # CRC mismatch here also catches entries whose header under-states their real size.
            raise Refused("unreadable", "The document is damaged (a part failed its integrity check).") from e
    return z, {"entries": len(infos), "unpacked": total}


def read_member(z: zipfile.ZipFile, name: str, cap: int) -> bytes | None:
    try:
        with z.open(name) as f:
            return f.read(cap)
    except KeyError:
        return None


def classify_zip(z: zipfile.ZipFile) -> dict:
    names = set(z.namelist())
    if "[Content_Types].xml" in names:
        ct = read_member(z, "[Content_Types].xml", 4 * MiB) or b""
        for marker, doc_type, ext in OOXML_TYPES:
            if marker in ct:
                return {"family": "ooxml", "doc_type": doc_type, "ext": ext, "encrypted": False}
        raise Refused("unsupported_type", "This is not a Word, Excel or PowerPoint document.")
    mimetype = read_member(z, "mimetype", 256)
    if mimetype is not None:
        mt = mimetype.decode("ascii", "replace").strip()
        kind = mt.removeprefix("application/vnd.oasis.opendocument.").split("-")[0]
        if not mt.startswith("application/vnd.oasis.opendocument.") or kind not in ODF_TYPES:
            raise Refused("unsupported_type", "Only OpenDocument text, spreadsheet and presentation files are accepted.")
        manifest = read_member(z, "META-INF/manifest.xml", 16 * MiB) or b""
        doc_type, ext = ODF_TYPES[kind]
        return {"family": "odf", "doc_type": doc_type, "ext": ext, "encrypted": b"encryption-data" in manifest}
    raise Refused("unsupported_type", "This zip file is not an Office document.")


# ----------------------------------------------------------------------------------------------- OLE (97-2003)
def classify_ole(path: str, ext: str) -> dict:
    import msoffcrypto
    from msoffcrypto import exceptions as mexc

    with open(path, "rb") as fh:
        try:
            office = msoffcrypto.OfficeFile(fh)
        except (mexc.FileFormatError, OSError, ValueError, AssertionError, KeyError, IndexError) as e:
            # An OLE file msoffcrypto does not know: let LibreOffice decide (it reads many legacy formats).
            if ext in LEGACY_EXT:
                return {"family": "ole", "doc_type": LEGACY_EXT[ext], "ext": ext, "encrypted": False}
            raise Refused("unsupported_type", "This is not a Word, Excel or PowerPoint document.") from e
        name = type(office).__name__
        doc_type, canon = {"Doc97File": ("text", "doc"), "Xls97File": ("spreadsheet", "xls"),
                           "Ppt97File": ("presentation", "ppt")}.get(name, (None, None))
        if name == "OOXMLFile":  # an OOXML package inside an OLE container is always encrypted (or IRM-protected)
            return {"family": "ole", "doc_type": None, "ext": ext, "encrypted": True}
        try:
            encrypted = bool(office.is_encrypted())
        except Exception:  # damaged stream: LibreOffice is more forgiving than msoffcrypto
            encrypted = False
        return {"family": "ole", "doc_type": doc_type or LEGACY_EXT.get(ext), "ext": canon or ext,
                "encrypted": encrypted}


# --------------------------------------------------------------------------------------------------------- text
def classify_text(path: str, ext: str, dst: str, max_bytes: int) -> dict:
    with open(path, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise Refused("too_large", f"Text files are limited to {max_bytes // MiB} MB.")
    charset, normalized = "utf-8", False
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError as e:
            raise Refused("unreadable", "The text file is not valid UTF-16.") from e
        with open(dst, "wb") as fh:
            fh.write(text.encode("utf-8"))
        data, normalized = text.encode("utf-8"), True
    head = data[: 64 * 1024]
    if b"\x00" in head:
        raise Refused("unsupported_type", "This does not look like a text file.")
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.start < len(head) - 4:  # not just a multi-byte character cut by the 64 KB window
            charset = "cp1252"
    sample = head.decode("utf-8" if charset == "utf-8" else "cp1252", "replace")
    controls = sum(1 for c in sample if ord(c) < 32 and c not in "\r\n\t\f")
    if sample and controls / len(sample) > 0.01:
        raise Refused("unsupported_type", "This does not look like a text file.")
    res = {"family": "text", "doc_type": "spreadsheet" if ext == "csv" else "text", "ext": ext,
           "encrypted": False, "charset": charset, "normalized": normalized}
    if ext == "csv":
        try:
            res["separator"] = csv.Sniffer().sniff(sample[:16384], delimiters=",;\t|").delimiter
        except csv.Error:
            res["separator"] = ","
    return res


# ------------------------------------------------------------------------------------------ file-name fields
# LibreOffice evaluates document fields on load. A file-name field (ODF text:file-name, Word/RTF FILENAME, Excel's
# &Z header code) would print the server-side job path (/tmp/jobs/…) instead of what the author saw, so the fields
# are frozen: ODF gets text:fixed="true" plus a display without the folder, Word fields are unwrapped to their saved
# result runs (LibreOffice ignores w:fldLock), RTF instructions get an unknown keyword (LibreOffice then shows the
# saved result), &Z is dropped. Plain byte rewriting, namespace-prefix aware; no XML parser touches the untrusted
# part. Legacy binary .doc/.xls are not rewritten (they still show the job path for a path field); the file name
# itself is always the user's own (the worker names the input after the upload's sanitised stem).
ODF_TEXT_NS = b"urn:oasis:names:tc:opendocument:xmlns:text:1.0"
WML_NS = (b"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
          b"http://purl.oclc.org/ooxml/wordprocessingml/main")
_ATTRS = rb"(?P<attrs>(?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(?P<close>/?)>"
_QNAME = rb"(?:(?P<p>[A-Za-z_][\w.-]*):)?"


def _prefixes(xml: bytes, uris: tuple[bytes, ...]) -> tuple[set[bytes], bool]:
    """Prefixes bound to any of ``uris`` anywhere in the part, and whether one is the default namespace."""
    alt = b"|".join(re.escape(u) for u in uris)
    pre = set(re.findall(rb"xmlns:([A-Za-z_][\w.-]*)\s*=\s*[\"'](?:" + alt + rb")[\"']", xml))
    default = re.search(rb"xmlns\s*=\s*[\"'](?:" + alt + rb")[\"']", xml) is not None
    return pre, default


def _attr(attrs: bytes, prefixes: set[bytes], local: bytes) -> bytes | None:
    for m in re.finditer(rb"\s+(?:([A-Za-z_][\w.-]*):)?" + re.escape(local) + rb"\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
                         attrs):
        if m.group(1) in prefixes:
            return m.group(2) if m.group(2) is not None else m.group(3)
    return None


def freeze_odf_fields(xml: bytes) -> tuple[bytes, int]:
    """Freeze every text:file-name / text:template-name field: text:fixed="true" (Writer and Impress then keep the
    saved text) and a display without the folder ("full" → "name-and-extension", "path" → "name"), because Calc's
    header/footer fields ignore "fixed" and would print the job path; they print the user's own file name instead."""
    prefixes, default = _prefixes(xml, (ODF_TEXT_NS,))
    if not prefixes and not default:
        return xml, 0
    alt = b"|".join(re.escape(x) for x in sorted(prefixes))
    n = 0

    def fix(m: re.Match) -> bytes:
        nonlocal n
        p = m.group("p")
        if not (p in prefixes or (p is None and default)):
            return m.group(0)
        n += 1
        attrs = m.group("attrs")
        if not prefixes:  # only a default namespace: an attribute needs a prefix of its own
            return (b"<" + m.group("qn") + attrs + b' xmlns:heypdftext="' + ODF_TEXT_NS + b'" heypdftext:fixed="true"'
                    + (b"/>" if m.group("close") else b">"))
        attrs = re.sub(rb"\s+(?:" + alt + rb"):fixed\s*=\s*(?:\"[^\"]*\"|'[^']*')", b"", attrs)
        attrs = re.sub(rb"(\s(?:" + alt + rb"):display\s*=\s*)(?:\"(full|path)\"|'(full|path)')",
                       lambda d: d.group(1) + (b'"name-and-extension"' if b"full" in (d.group(2) or d.group(3))
                                               else b'"name"'), attrs)
        return (b"<" + m.group("qn") + attrs + b" " + min(prefixes) + b':fixed="true"'
                + (b"/>" if m.group("close") else b">"))

    out = re.sub(rb"<(?P<qn>" + _QNAME + rb"(?:file-name|template-name))(?=[\s/>])" + _ATTRS, fix, xml)
    return out, n


# Excel header/footer codes: "&Z" prints the file's folder (here the job path); "&&" is a literal ampersand.
_XL_HF = re.compile(rb"(<(?:[A-Za-z_][\w.-]*:)?(?:odd|even|first)(?:Header|Footer)(?:\s[^>]*)?>)([^<]*)(</)")


def drop_excel_path_codes(xml: bytes) -> tuple[bytes, int]:
    """Remove the folder code (&Z) from every header/footer of an OOXML worksheet; &F (the file name) stays."""
    n = 0

    def fix(m: re.Match) -> bytes:
        nonlocal n
        text = html.unescape(m.group(2).decode("utf-8", "replace"))
        out, i = [], 0
        while i < len(text):
            if text[i] == "&" and i + 1 < len(text):
                if text[i + 1] == "Z":
                    n += 1
                else:
                    out.append(text[i:i + 2])
                i += 2
                continue
            out.append(text[i])
            i += 1
        new = "".join(out)
        if new == text:
            return m.group(0)
        return m.group(1) + html.escape(new, quote=False).encode() + m.group(3)

    return _XL_HF.sub(fix, xml), n


# RTF: a field whose instruction starts with an unknown keyword keeps its saved result in LibreOffice.
_RTF_TOKEN = re.compile(rb"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}]+)", re.S)


def _rtf_field_instruction(data: bytes, pos: int, limit: int = 64) -> str:
    """The first ``limit`` characters of the plain text of the group that ``pos`` (just after \\fldinst) is in."""
    depth, text, skip, uc = 1, "", 0, 1
    for t in _RTF_TOKEN.finditer(data, pos):
        word, num, hexd, sym, brace, plain = t.groups()
        chunk = ""
        if brace is not None:
            depth += 1 if brace == b"{" else -1
            if depth == 0:
                break
        elif word is not None:
            if word == b"uc" and num:
                uc = max(0, int(num))
            elif word == b"u" and num:  # \uN, then \ucN fallback characters to skip
                text += chr(int(num) % 65536)
                skip = uc
                continue
            elif word in (b"par", b"line", b"tab"):
                chunk = " "
        elif hexd is not None:
            chunk = chr(int(hexd, 16))
        elif sym is not None:
            chunk = sym.decode("latin-1") if sym in b"\\{}" else ""
        else:
            chunk = plain.decode("latin-1").replace("\r", "").replace("\n", "")
        if skip and chunk:
            cut = min(skip, len(chunk))
            chunk, skip = chunk[cut:], skip - cut
        text += chunk
        if len(text) > limit:
            break
    return text


def freeze_rtf_filename_fields(data: bytes) -> tuple[bytes, int]:
    """Prefix every FILENAME field's instruction with an unknown keyword (LibreOffice then shows the saved result)."""
    at = [m.end() for m in re.finditer(rb"\\fldinst(?![a-zA-Z])", data)
          if (_rtf_field_instruction(data, m.end()).split() or [""])[0].upper() == "FILENAME"]
    if not at:
        return data, 0
    out, last = [], 0
    for pos in at:
        out += [data[last:pos], b" HEYPFROZEN "]
        last = pos
    out.append(data[last:])
    return b"".join(out), len(at)


def _field_name(instr: str) -> str:
    return (html.unescape(instr).split() or [""])[0].upper()


def unwrap_word_filename_fields(xml: bytes) -> tuple[bytes, int]:
    """Replace Word FILENAME fields (simple and complex) by their saved result runs."""
    prefixes, default = _prefixes(xml, WML_NS)
    if not prefixes and not default:
        return xml, 0
    ok = prefixes | ({None} if default else set())
    tok = re.compile(rb"<(?P<end>/)?(?P<qn>" + _QNAME + rb"(?P<local>fldSimple|fldChar|instrText))(?=[\s/>])"
                     + _ATTRS)
    drop: list[tuple[int, int]] = []
    simple: list[tuple[tuple[int, int], bool]] = []  # open <fldSimple>: (start-tag span, is FILENAME)
    fields: list[dict] = []                         # open complex fields (begin … separate … end)
    pending: tuple[str, int, int, str] | None = None  # an open <instrText> / non-empty <fldChar>
    found = 0

    def fld_char(kind: str, span: tuple[int, int]) -> None:
        nonlocal found
        if kind == "begin":
            fields.append({"begin": span, "instr": [], "spans": [], "sep": None})
        elif kind == "separate" and fields:
            fields[-1]["sep"] = span
        elif kind == "end" and fields:
            f = fields.pop()
            if _field_name("".join(f["instr"])) == "FILENAME":
                found += 1
                drop.extend([f["begin"], *f["spans"], span] + ([f["sep"]] if f["sep"] else []))

    for m in tok.finditer(xml):
        if m.group("p") not in ok:
            continue
        local, start, end = m.group("local").decode(), m.start(), m.end()
        if m.group("end"):
            if local == "fldSimple" and simple:
                span, is_fn = simple.pop()
                if is_fn:
                    found += 1
                    drop.extend([span, (start, end)])
            elif pending is not None and pending[0] == local:
                kind, s, e, fld_type = pending
                pending = None
                if kind == "instrText":
                    if fields and fields[-1]["sep"] is None:
                        fields[-1]["instr"].append(html.unescape(xml[e:start].decode("utf-8", "replace")))
                        fields[-1]["spans"].append((s, end))
                else:
                    fld_char(fld_type, (s, end))
            continue
        if pending is not None:  # inside an instrText / fldChar: nothing else can start there
            continue
        attrs, empty = m.group("attrs"), bool(m.group("close"))
        if local == "fldSimple":
            instr = (_attr(attrs, prefixes | {None}, b"instr") or b"").decode("utf-8", "replace")
            is_fn = _field_name(instr) == "FILENAME"
            if not empty:
                simple.append(((start, end), is_fn))
            elif is_fn:
                found += 1
                drop.append((start, end))
        elif local == "instrText":
            if not empty:
                pending = ("instrText", start, end, "")
        else:
            fld_type = (_attr(attrs, prefixes | {None}, b"fldCharType") or b"").decode("ascii", "replace")
            if empty:
                fld_char(fld_type, (start, end))
            else:
                pending = ("fldChar", start, end, fld_type)
    if not drop:
        return xml, 0
    out, last = [], 0
    for s, e in sorted(set(drop)):
        if s < last:  # overlapping spans cannot happen with well-formed XML; never cut twice
            continue
        out.append(xml[last:s])
        last = e
    out.append(xml[last:])
    return b"".join(out), found


def _field_parts(z: zipfile.ZipFile, family: str, doc_type: str) -> tuple[list[zipfile.ZipInfo], bytes, Any]:
    """The parts that can hold file-name fields, a cheap marker to look for first, and the fixer."""
    if family == "odf":  # body + master pages (headers/footers), also of embedded objects ("Object 1/content.xml")
        return ([i for i in z.infolist() if i.filename.rsplit("/", 1)[-1] in ("content.xml", "styles.xml")],
                rb"file-name|template-name", freeze_odf_fields)
    if family == "ooxml" and doc_type == "text":  # document, headers, footers, foot-/endnotes, comments
        return ([i for i in z.infolist() if re.fullmatch(r"word/[^/]+\.xml", i.filename)], rb"(?i)filename",
                unwrap_word_filename_fields)
    if family == "ooxml" and doc_type == "spreadsheet":  # worksheet / chartsheet page headers and footers
        return ([i for i in z.infolist() if re.fullmatch(r"xl/(?:work|chart)sheets/[^/]+\.xml", i.filename)],
                rb"&amp;Z|&#0*38;Z|&#x0*26;Z", drop_excel_path_codes)
    return [], b"", None


def freeze_file_name_fields(z: zipfile.ZipFile, res: dict, dst: str) -> int:
    """Rewrite the package into ``dst`` with every file-name field frozen; returns how many (0: untouched)."""
    parts, marker, fix = _field_parts(z, res["family"], res["doc_type"])
    changed: dict[str, bytes] = {}
    count = 0
    for info in parts:
        with z.open(info) as f:
            data = f.read()
        if not re.search(marker, data):
            continue
        new, n = fix(data)
        if n:
            changed[info.filename], count = new, count + n
    if not changed:
        return 0
    with zipfile.ZipFile(dst, "w") as out:  # same entries, same order ("mimetype" first, stored), same methods
        for info in z.infolist():
            zi = zipfile.ZipInfo(info.filename, info.date_time)
            zi.compress_type, zi.external_attr = info.compress_type, info.external_attr
            if info.filename in changed:
                out.writestr(zi, changed[info.filename])
            else:
                zi.file_size = info.file_size
                with z.open(info) as fin, out.open(zi, "w") as fout:
                    shutil.copyfileobj(fin, fout, MiB)
    return count


def cmd_office(src: str, ext: str, dst: str, zip_max: str, ratio: str, entries: str, text_max: str) -> dict:
    with open(src, "rb") as fh:
        head = fh.read(8)
    if head.startswith(b"PK\x03\x04"):
        z, stats = check_zip(src, int(zip_max), int(ratio), int(entries))
        with z:
            res = classify_zip(z)
            if not res["encrypted"]:
                frozen = freeze_file_name_fields(z, res, dst)
                if frozen:
                    res |= {"normalized": True, "frozen_fields": frozen}
        return res | stats
    if head.startswith(OLE_MAGIC):
        return classify_ole(src, ext)
    if head.startswith(b"{\\rtf"):
        res = {"family": "rtf", "doc_type": "text", "ext": "rtf", "encrypted": False}
        with open(src, "rb") as fh:
            data = fh.read()
        if b"\\fldinst" in data:
            data, frozen = freeze_rtf_filename_fields(data)
            if frozen:
                with open(dst, "wb") as fh:
                    fh.write(data)
                res |= {"normalized": True, "frozen_fields": frozen}
        return res
    if ext in ("txt", "csv"):
        return classify_text(src, ext, dst, int(text_max))
    raise Refused("unsupported_type", "This file is not a supported Office document.")


def cmd_office_decrypt(src: str, dst: str, password: str) -> dict:
    import msoffcrypto
    from msoffcrypto import exceptions as mexc

    if not password:
        return {"error": "needs_password", "detail": "This document is password-protected."}
    with open(src, "rb") as fin:
        try:
            office = msoffcrypto.OfficeFile(fin)
            office.load_key(password=password, verify_password=True)
            with open(dst, "wb") as fout:
                office.decrypt(fout)
        except mexc.InvalidKeyError:
            return {"error": "wrong_password", "detail": "The password is incorrect."}
        except (mexc.DecryptionError, mexc.FileFormatError, mexc.ParseError, NotImplementedError) as e:
            msg = str(e).lower()
            if "password" in msg or "key" in msg:
                return {"error": "wrong_password", "detail": "The password is incorrect."}
            return {"error": "encrypted_unsupported",
                    "detail": "This document uses a protection scheme that can't be opened here (e.g. rights "
                              "management). Save an unprotected copy and try again."}
        except (ValueError, KeyError, IndexError, AssertionError, OSError, EOFError):
            # Damaged ciphertext (e.g. a truncated stream: "not a multiple of the block length").
            return {"error": "unreadable", "detail": "The protected document is damaged and could not be decrypted."}
    return {"ok": True}


def cmd_office_encrypt(src: str, dst: str, password: str) -> dict:  # used by the test-suite only
    from msoffcrypto.format.ooxml import OOXMLFile

    with open(src, "rb") as fin, open(dst, "wb") as fout:
        OOXMLFile(fin).encrypt(password, fout)
    return {"ok": True}


# ---------------------------------------------------------------------------------------------------------- PDF
def _pdf_header_offset(path: str) -> int:
    with open(path, "rb") as fh:
        return fh.read(1024).find(b"%PDF-")


def _open_pdf(path: str, password: str):
    import pikepdf

    try:
        return pikepdf.open(path, password=password or "")
    except pikepdf.PasswordError as e:
        raise Refused("wrong_password" if password else "needs_password",
                      "The password is incorrect." if password else "This PDF is password-protected.") from e
    except (pikepdf.PdfError, OSError, ValueError, RuntimeError) as e:
        raise Refused("unreadable", "The PDF could not be read (it may be damaged; try Repair PDF).") from e


def _pdf_facts(pdf) -> dict:
    import pikepdf

    facts = {"pages": len(pdf.pages), "encrypted": bool(pdf.is_encrypted), "acroform": False, "signed": False,
             "pdfa": None}
    acro = pdf.Root.get("/AcroForm")
    if isinstance(acro, pikepdf.Dictionary):
        fields = acro.get("/Fields")
        facts["acroform"] = isinstance(fields, pikepdf.Array) and len(fields) > 0
        try:
            facts["signed"] = bool(int(acro.get("/SigFlags", 0)) & 1)
        except (TypeError, ValueError):
            pass
    try:
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
            part, conf = meta.get("pdfaid:part"), meta.get("pdfaid:conformance")
            if part:
                facts["pdfa"] = f"{part}{(conf or '').lower()}"
    except Exception:
        pass
    return facts


def cmd_pdf(src: str, dst: str, password: str) -> dict:
    off = _pdf_header_offset(src)
    if off < 0:
        return {"error": "not_pdf", "detail": "This file is not a PDF."}
    with _open_pdf(src, password) as pdf:
        facts = _pdf_facts(pdf)
        facts["header_offset"] = off
        # Engines downstream (Ghostscript above all) only ever see a file that starts with "%PDF-" and is not
        # encrypted: anything else is rewritten by qpdf here first.
        facts["normalized"] = bool(off > 0 or facts["encrypted"])
        if facts["normalized"]:
            pdf.save(dst)  # pikepdf drops the encryption unless asked to keep it
    return facts


def _content_streams(page):
    import pikepdf

    contents = page.obj.get("/Contents")
    if isinstance(contents, pikepdf.Array):
        return [c for c in contents if isinstance(c, pikepdf.Stream)]
    return [contents] if isinstance(contents, pikepdf.Stream) else []


def cmd_repair(src: str, dst: str, password: str) -> dict:
    off = _pdf_header_offset(src)
    if off < 0:
        return {"error": "not_pdf", "detail": "This file is not a PDF."}
    import pikepdf

    try:
        pdf = pikepdf.open(src, password=password or "")
    except pikepdf.PasswordError:
        return {"error": "wrong_password" if password else "needs_password",
                "detail": "The password is incorrect." if password else "This PDF is password-protected."}
    except Exception as e:  # qpdf could not even reconstruct a trailer: let Ghostscript try
        return {"ok": False, "reason": type(e).__name__}
    with pdf:
        issues = len(pdf.get_warnings())
        streams_ok, decoded = True, 0
        for page in pdf.pages:
            try:
                for s in _content_streams(page):
                    decoded += len(s.read_bytes())
            except Exception:
                streams_ok = False
                break
            if decoded > 512 * MiB:  # enough evidence; don't spend the whole CPU budget
                break
        pages = len(pdf.pages)
        if pages == 0:
            return {"ok": False, "reason": "no-pages"}
        if "/Size" not in pdf.trailer:
            # qpdf's trailer rebuilt from a truncated file has no /Size, and qpdf writes /Size only when the trailer
            # has one (with the value it computes): without this the "repaired" file is still damaged for readers.
            pdf.trailer.Size = len(pdf.objects) + 1
        pdf.save(dst)
    return {"ok": True, "engine": "qpdf", "pages": pages, "issues": issues, "streams_ok": streams_ok,
            "clean": _reopens_clean(dst)}


def _reopens_clean(path: str) -> bool:
    """True when qpdf reads ``path`` back without a single warning (every object resolved)."""
    import pikepdf

    try:
        with pikepdf.open(path) as pdf:
            for _ in pdf.objects:
                pass
            for page in pdf.pages:
                _content_streams(page)
            return not pdf.get_warnings()
    except Exception:
        return False


COMMANDS = {"office": (cmd_office, 7), "office-decrypt": (cmd_office_decrypt, 2), "office-encrypt": (cmd_office_encrypt, 2),
            "pdf": (cmd_pdf, 2), "repair": (cmd_repair, 2)}
NEEDS_PASSWORD = {"office-decrypt", "office-encrypt", "pdf", "repair"}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in COMMANDS or len(argv) - 2 != COMMANDS[argv[1]][1]:
        out({"error": "bad_request", "detail": "usage"})
        return 2
    cmd, args = argv[1], argv[2:]
    if cmd in NEEDS_PASSWORD:
        args.append(sys.stdin.buffer.read(4096).decode("utf-8", "replace"))
    try:
        out(COMMANDS[cmd][0](*args))
    except Refused as e:
        out({"error": e.code, "detail": e.detail})
    except MemoryError:
        out({"error": "too_large", "detail": "The file needs more memory to process than allowed."})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

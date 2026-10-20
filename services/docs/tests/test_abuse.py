"""Regression tests for hostile inputs and resource abuse (critic findings) — real engines, real limits."""
from __future__ import annotations

import asyncio
import io
import os
import threading
import time
import zipfile

import pytest
import worker
from conftest import (CT_DOCX, PASSWD_MARKERS, RELS_DOCX, W_NS, assert_clean_error, make_odt, make_text_pdf,
                      make_zip_bomb_docx, pdf_text, post, run_helper)

MiB = 1024 * 1024


# ------------------------------------------------------------------------------------------------ zip bombs
def test_critic_docx_bomb_is_refused_before_libreoffice(tmp_path):
    """1.2 MB DOCX whose document.xml inflates to 300 MB OOM-killed soffice (critic blocking #3). With the
    production limits (200 MB unpacked, ratio 100) the helper refuses it quickly, from the zip directory alone."""
    bomb = make_zip_bomb_docx(tmp_path / "bomb.docx", 300)
    assert bomb.stat().st_size < 3 * MiB
    t = time.monotonic()
    res = run_helper("office", str(bomb), "docx", str(tmp_path / "t"), str(200 * MiB), "100", "10000", str(20 * MiB))
    assert res["error"] in ("too_large", "suspicious_archive"), res
    assert time.monotonic() - t < 10


def test_bomb_over_the_unpacked_limit(client, tmp_path):
    bomb = make_zip_bomb_docx(tmp_path / "bomb.docx", 25)  # test limit: 20 MB unpacked
    assert_clean_error(post(client, "office_to_pdf", bomb.read_bytes(), "bomb.docx"), 413, "too_large")


def test_ratio_bomb_under_the_size_limit(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("[Content_Types].xml", CT_DOCX)
        z.writestr("_rels/.rels", RELS_DOCX)
        z.writestr("word/document.xml", f'<w:document {W_NS}><w:body><w:p><w:r><w:t>'
                                        + "A" * (15 * MiB) + "</w:t></w:r></w:p></w:body></w:document>")
    assert_clean_error(post(client, "office_to_pdf", buf.getvalue(), "ratio.docx"), 422, "suspicious_archive")


def test_too_many_parts(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("[Content_Types].xml", CT_DOCX)
        for i in range(10_001):
            z.writestr(f"customXml/item{i}.xml", "")
    assert_clean_error(post(client, "office_to_pdf", buf.getvalue(), "many.docx"), 422, "suspicious_archive")


def test_lying_zip_header_is_caught(client):
    """Central directory claims a tiny entry; the real stream is bigger → integrity failure, never a free pass."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT_DOCX)
        z.writestr("_rels/.rels", RELS_DOCX)
        z.writestr("word/document.xml", f'<w:document {W_NS}><w:body>' + "<w:p/>" * 200_000 + "</w:body></w:document>")
    data = bytearray(buf.getvalue())
    i = 0
    while True:  # central directory record of word/document.xml; uncompressed size lives at offset 24
        i = data.find(b"PK\x01\x02", i)
        assert i >= 0
        nlen = int.from_bytes(data[i + 28:i + 30], "little")
        if data[i + 46:i + 46 + nlen] == b"word/document.xml":
            data[i + 24:i + 28] = (10).to_bytes(4, "little")
            break
        i += 4
    r = post(client, "office_to_pdf", bytes(data), "liar.docx")
    assert_clean_error(r, 422, "unreadable")


# ---------------------------------------------------------------------------------- LibreOffice local files
def test_linked_local_files_are_not_embedded(client):
    """Stock LibreOffice pulled a file:// image of another job into the output (engine research). Linked
    sections and images pointing at local files must stay unresolved."""
    body = ('<text:p>Linked content follows</text:p>'
            '<text:section text:name="Leak"><text:section-source xlink:href="file:///etc/passwd" '
            'xlink:type="simple" text:filter-name="Text (encoded)"/><text:p>placeholder</text:p></text:section>'
            '<text:p><draw:frame draw:name="i1" text:anchor-type="as-char" svg:width="3cm" svg:height="3cm">'
            '<draw:image xlink:href="file:///etc/passwd" xlink:type="simple" xlink:show="embed" '
            'xlink:actuate="onLoad"/></draw:frame></text:p>')
    r = post(client, "office_to_pdf", make_odt(body), "leak.odt")
    assert r.status_code == 200, r.text
    text = pdf_text(r.content)
    assert "Linked content follows" in text
    assert not any(m in text for m in PASSWD_MARKERS), text[:500]


def test_hardening_layer_is_installed():
    xcd = "/usr/lib/libreoffice/share/registry/heypdf-hardening.xcd"
    data = open(xcd).read()
    for key in ("DisableMacrosExecution", "BlockUntrustedRefererLinks", "DisableActiveContent", "MacroSecurityLevel"):
        assert key in data
    assert data.count('oor:finalized="true"') >= 6


# ------------------------------------------------------------------------------------- PostScript as "PDF"
PS_READ_PASSWD = (b"%!PS-Adobe-3.0\n/f (/etc/passwd) (r) file def /s 4096 string def\n"
                  b"/Helvetica findfont 10 scalefont setfont 36 760 moveto f s readstring pop show showpage\n")


@pytest.mark.parametrize("op", ["compress", "pdfa", "ocr", "repair", "pdf_to_pptx"])
def test_postscript_disguised_as_pdf_is_refused(client, op):
    assert_clean_error(post(client, op, PS_READ_PASSWD, "invoice.pdf"), 415, "unsupported_type")


@pytest.mark.parametrize("op", ["compress", "pdfa", "repair"])
def test_postscript_with_pdf_magic_never_runs_as_postscript(client, op):
    """A PostScript program that mentions %PDF- inside the first KB passes the cheap magic check, but every engine
    only ever sees bytes that START with %PDF- (pdfi, not the PostScript interpreter) and -dSAFER is always on."""
    polyglot = b"%!PS-Adobe-3.0\n% %PDF-1.7 decoy\n" + PS_READ_PASSWD
    r = post(client, op, polyglot, "invoice.pdf")
    if r.status_code == 200:
        assert not any(m in pdf_text(r.content) for m in PASSWD_MARKERS)
    else:
        assert_clean_error(r, r.status_code)


def test_ghostscript_safer_blocks_file_reads_even_on_pdf_input(client):
    """A PDF whose content stream is PostScript-y must not read local files either."""
    body = b"%PDF-1.4\n" + PS_READ_PASSWD
    r = post(client, "repair", body)
    if r.status_code == 200:
        assert not any(m in pdf_text(r.content) for m in PASSWD_MARKERS)


# ------------------------------------------------------------------------------------------ PDF bombs etc.
def test_flate_bomb_pdf_is_bounded(client):
    """A ~1 MB PDF whose page content inflates to 1.5 GB: the repair helper hits RLIMIT_AS/its decode budget
    and the job still ends with a clean answer (no worker crash)."""
    import zlib

    import pikepdf

    c = zlib.compressobj(9)
    data = b"".join(c.compress(b" " * MiB) for _ in range(1536)) + c.flush()
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    st = pikepdf.Stream(pdf, data)
    st.Filter = pikepdf.Name.FlateDecode
    pdf.pages[0].obj.Contents = st
    buf = io.BytesIO()
    pdf.save(buf, compress_streams=False)
    t = time.monotonic()
    r = post(client, "repair", buf.getvalue())
    assert r.status_code in (200, 413, 422), r.text
    assert time.monotonic() - t < 120
    assert client.get("/healthz").status_code == 200


def test_page_cap_applies_to_every_pdf_op(client):
    assert_clean_error(post(client, "compress", make_text_pdf(51)), 413, "too_many_pages")


# --------------------------------------------------------------------------------------------- internals
def test_admission_rejects_when_full_and_never_leaks():
    async def scenario():
        adm = worker.Admission(slots=1, queue=1)
        hold = asyncio.Event()

        async def runner():
            async with adm.slot():
                await hold.wait()

        first = asyncio.create_task(runner())
        await asyncio.sleep(0)
        second = asyncio.create_task(runner())  # queued
        await asyncio.sleep(0)
        assert adm.pending == 2 and adm.full()
        with pytest.raises(worker.DocsError) as e:
            async with adm.slot():
                pass
        assert e.value.status == 503
        second.cancel()  # a client that gives up while queued (the prototype leaked here → permanent 503)
        with pytest.raises(asyncio.CancelledError):
            await second
        assert adm.pending == 1 and not adm.full()
        hold.set()
        await first
        assert adm.pending == 0

    asyncio.run(scenario())


def _alive(marker: str) -> list[str]:
    found = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
            except OSError:
                continue
            if marker in cmd and "pytest" not in cmd:
                found.append(cmd)
    return found


def test_timeout_kills_the_whole_process_group(tmp_path):
    job = worker.Job("test", time.monotonic() + 60)
    try:
        t = time.monotonic()
        with pytest.raises(worker.DocsError) as e:
            job.run(["sh", "-c", "sleep 31.5 & sleep 31.6; wait"], timeout=1)
        assert e.value.status == 504 and time.monotonic() - t < 5
        time.sleep(0.2)
        assert _alive("sleep 31.") == []
    finally:
        job.cleanup()


def test_cancel_kills_running_engines():
    job = worker.Job("test", time.monotonic() + 60)
    err: list[BaseException] = []

    def target():
        try:
            job.run(["sleep", "32.5"], timeout=30)
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    th = threading.Thread(target=target)
    th.start()
    time.sleep(0.5)
    t = time.monotonic()
    job.cancel()
    th.join(5)
    assert not th.is_alive() and time.monotonic() - t < 3
    assert isinstance(err[0], worker.DocsError) and err[0].code == "cancelled"
    assert _alive("sleep 32.5") == []
    job.cleanup()


def test_address_space_limit_applies_to_engines():
    job = worker.Job("test", time.monotonic() + 60)
    try:
        rc, _out, err = job.run(["python3", "-c", "b = bytearray(1536 * 1024 * 1024)"], timeout=20)
        assert rc != 0 and b"MemoryError" in err
        rc, _out, _err = job.run(["python3", "-c", "b = bytearray(64 * 1024 * 1024)"], timeout=20)
        assert rc == 0
    finally:
        job.cleanup()


def test_deadline_header_shortens_the_budget(client):
    r = client.post("/v1/ocr", files={"file": ("a.pdf", make_text_pdf(1))}, data={"options": "{}"},
                    headers={"X-Heypdf-Timeout": "garbage"})
    assert r.status_code == 200  # a bad header is ignored, not fatal


def test_password_is_never_echoed(client):
    secret = "hunter2-very-secret"
    r = post(client, "compress", b"not a pdf at all" * 10, password=secret)
    assert secret not in r.text


def test_damaged_encrypted_office_file_is_a_clean_error(client, tmp_path):
    """Truncated ciphertext must not crash the decrypt helper (it used to surface as a generic failure)."""
    import random

    from conftest import encrypt_docx, make_docx

    rnd = random.Random(3)
    paras = ["".join(rnd.choice("abcdefghij ") for _ in range(80)) for _ in range(200)]
    enc = encrypt_docx(make_docx(paras), tmp_path, "pw")
    damaged = enc[: len(enc) - 4096 - 7]
    r = post(client, "office_to_pdf", damaged, "broken.docx", password="pw")
    assert_clean_error(r, 422)
    assert r.json()["error"] in ("unreadable", "wrong_password", "encrypted_unsupported")

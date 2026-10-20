"""Organize engines: merge, split, remove / extract pages, organize, rotate (PyMuPDF).

Each function runs inside the sandbox child: ``fn(srcs, opts, out_dir) -> Result``.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

from app.pdf import options as O
from app.pdf.errors import PdfError
from app.pdf.util import Result, Src, has_signatures, open_pdf, out_name, parse_pages, parse_ranges, save_pdf, stem

MAX_PAGES = 5000          # one input
MAX_TOTAL_PAGES = 10_000  # merged / organized output
MAX_OUTPUT_FILES = 2000
A4 = (595.276, 841.89)
LETTER = (612.0, 792.0)


def _sig_warning(doc: pymupdf.Document, name: str, res: Result) -> None:
    if has_signatures(doc):
        res.warn(f"“{name}” is digitally signed; the changed document no longer carries a valid signature")


def _clean_toc(toc: list[list], page_count: int) -> list[list]:
    """Make a TOC acceptable to set_toc: starts at level 1, never jumps more than one level."""
    out: list[list] = []
    prev = 0
    for entry in toc:
        level, title, page = int(entry[0]), str(entry[1])[:500], int(entry[2])
        level = max(1, min(level, prev + 1))
        if page < 1 or page > page_count:
            page = -1
        out.append([level, title, page])
        prev = level
    return out


def _sub_toc(doc: pymupdf.Document, first: int, last: int, offset: int) -> list[list]:
    """Bookmarks of pages first..last (1-based) re-based so ``first`` lands on ``offset + 1``."""
    out = []
    try:
        toc = doc.get_toc(simple=True)
    except Exception:
        return out
    for level, title, page in toc:
        if first <= page <= last:
            out.append([level, title, page - first + 1 + offset])
    return out


def merge(srcs: list[Src], opts: O.MergeOptions, out_dir: str) -> Result:
    res = Result()
    if len(srcs) < 2:
        raise PdfError(422, "Choose at least two PDFs to merge")
    out = pymupdf.open()
    toc: list[list] = []
    for src in srcs:
        doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
        if out.page_count + doc.page_count > MAX_TOTAL_PAGES:
            raise PdfError(413, f"The merged document would exceed {MAX_TOTAL_PAGES} pages")
        _sig_warning(doc, src.name, res)
        start = out.page_count
        out.insert_pdf(doc)
        if opts.bookmarks == "per_file":
            toc.append([1, stem(src.name), start + 1])
            toc += [[lvl + 1, t, p] for lvl, t, p in _sub_toc(doc, 1, doc.page_count, start)]
        elif opts.bookmarks == "keep":
            toc += _sub_toc(doc, 1, doc.page_count, start)
        doc.close()
    if toc:
        out.set_toc(_clean_toc(toc, out.page_count))
    path = Path(out_dir) / "merged.pdf"
    save_pdf(out, path)
    res.add(path, out_name(srcs[0].name, "merged", "pdf"))
    res.data.update(pages=out.page_count, files=len(srcs))
    return res


def _size_groups(doc: pymupdf.Document, budget: int, res: Result) -> list[tuple[int, int]]:
    """Greedy page groups (1-based) whose saved size stays under ``budget`` bytes."""
    n = doc.page_count

    def size(a: int, b: int) -> int:
        part = pymupdf.open()
        part.insert_pdf(doc, from_page=a - 1, to_page=b - 1)
        return len(part.tobytes(garbage=3, deflate=True, use_objstms=1))

    single = [size(p, p) for p in range(1, n + 1)]
    groups: list[tuple[int, int]] = []
    start = 1
    checks = 0
    while start <= n:
        end, est = start, single[start - 1]
        while end < n and est + single[end] <= budget:
            est += single[end]
            end += 1
        # single-page sizes double-count shared fonts, so try to grow the group with real measurements
        while end < n and checks < 3 * n + 50 and size(start, end + 1) <= budget:
            end += 1
            checks += 1
        if single[start - 1] > budget and end == start:
            res.warn(f"Page {start} alone is larger than the size limit; it is in a file of its own")
        groups.append((start, end))
        start = end + 1
    return groups


def split(srcs: list[Src], opts: O.SplitOptions, out_dir: str) -> Result:
    res = Result()
    src = srcs[0]
    doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
    n = doc.page_count
    if opts.mode == "ranges":
        groups = parse_ranges(opts.ranges, n)
    elif opts.mode == "fixed":
        groups = [(a, min(a + opts.every - 1, n)) for a in range(1, n + 1, opts.every)]
    elif opts.mode == "all":
        groups = [(p, p) for p in range(1, n + 1)]
    else:
        if n > 1000:
            raise PdfError(413, "Splitting by size works on documents of up to 1000 pages")
        groups = _size_groups(doc, int(opts.max_mb * 1024 * 1024), res)
    if not groups:
        raise PdfError(422, "No pages selected")
    if len(groups) > MAX_OUTPUT_FILES:
        raise PdfError(413, f"That would create {len(groups)} files; the limit is {MAX_OUTPUT_FILES}")
    _sig_warning(doc, src.name, res)
    if opts.mode == "ranges" and opts.merge:
        out = pymupdf.open()
        for a, b in groups:
            out.insert_pdf(doc, from_page=a - 1, to_page=b - 1)
        path = Path(out_dir) / "split.pdf"
        save_pdf(out, path)
        res.add(path, out_name(src.name, "split", "pdf"))
        res.data.update(files=1, pages=out.page_count)
        return res
    names = []
    for i, (a, b) in enumerate(groups):
        part = pymupdf.open()
        part.insert_pdf(doc, from_page=a - 1, to_page=b - 1)
        sub = _sub_toc(doc, a, b, 0)
        if sub:
            part.set_toc(_clean_toc(sub, part.page_count))
        path = Path(out_dir) / f"part-{i:05d}.pdf"
        save_pdf(part, path)
        label = f"{a}" if a == b else f"{a}-{b}"
        names.append(label)
        res.add(path, out_name(src.name, label, "pdf"))
    res.data.update(files=len(groups), ranges=[[a, b] for a, b in groups])
    return res


def remove_pages(srcs: list[Src], opts: O.RemovePagesOptions, out_dir: str) -> Result:
    res = Result()
    src = srcs[0]
    doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
    pages = parse_pages(opts.pages, doc.page_count, empty_means_all=False)
    if len(pages) >= doc.page_count:
        raise PdfError(422, "You can't remove every page; leave at least one")
    _sig_warning(doc, src.name, res)
    doc.delete_pages([p - 1 for p in pages])
    path = Path(out_dir) / "removed.pdf"
    save_pdf(doc, path)
    res.add(path, out_name(src.name, "removed", "pdf"))
    res.data.update(removed=len(pages), pages=doc.page_count)
    return res


def extract_pages(srcs: list[Src], opts: O.ExtractPagesOptions, out_dir: str) -> Result:
    res = Result()
    src = srcs[0]
    doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
    pages = parse_pages(opts.pages, doc.page_count)
    if len(pages) > MAX_OUTPUT_FILES and not opts.merge:
        raise PdfError(413, f"That would create {len(pages)} files; the limit is {MAX_OUTPUT_FILES}")
    _sig_warning(doc, src.name, res)
    if opts.merge or len(pages) == 1:
        doc.select([p - 1 for p in pages])
        path = Path(out_dir) / "extracted.pdf"
        save_pdf(doc, path)
        res.add(path, out_name(src.name, "extracted" if len(pages) > 1 else f"page-{pages[0]}", "pdf"))
    else:
        for p in pages:
            part = pymupdf.open()
            part.insert_pdf(doc, from_page=p - 1, to_page=p - 1)
            path = Path(out_dir) / f"page-{p:05d}.pdf"
            save_pdf(part, path)
            res.add(path, out_name(src.name, f"page-{p}", "pdf"))
    res.data.update(pages=pages if len(pages) <= 2000 else len(pages))
    return res


def organize(srcs: list[Src], opts: O.OrganizeOptions, out_dir: str) -> Result:
    res = Result()
    docs = []
    for src in srcs:
        d = open_pdf(src, max_pages=MAX_PAGES, result=res)
        _sig_warning(d, src.name, res)
        docs.append(d)
    plan = opts.pages or [O.OrganizePage(file=i, page=p) for i, d in enumerate(docs)
                          for p in range(1, d.page_count + 1)]
    if len(plan) > MAX_TOTAL_PAGES:
        raise PdfError(413, f"The new document would exceed {MAX_TOTAL_PAGES} pages")
    for k, item in enumerate(plan):
        if item.blank:
            continue
        if item.file >= len(docs):
            raise PdfError(422, f"Entry {k + 1} refers to file {item.file + 1}, but only {len(docs)} were given")
        if item.page is None or item.page > docs[item.file].page_count:
            raise PdfError(422, f"Entry {k + 1}: page {item.page} does not exist in "
                                f"“{srcs[item.file].name}” ({docs[item.file].page_count} pages)")
    if all(item.blank for item in plan):
        raise PdfError(422, "The new document has no pages from the input")
    last_use = {item.file: k for k, item in enumerate(plan) if not item.blank}
    out = pymupdf.open()
    for k, item in enumerate(plan):
        if item.blank:
            if opts.blank_size == "previous" and out.page_count:
                prev = out[-1]
                w, h = prev.rect.width, prev.rect.height
            elif opts.blank_size == "letter":
                w, h = LETTER
            else:
                w, h = A4
            out.new_page(width=w, height=h)
            continue
        src_doc = docs[item.file]
        out.insert_pdf(src_doc, from_page=item.page - 1, to_page=item.page - 1, final=last_use[item.file] == k)
        if item.rotate:
            page = out[-1]
            page.set_rotation((page.rotation + item.rotate) % 360)
    path = Path(out_dir) / "organized.pdf"
    save_pdf(out, path)
    res.add(path, out_name(srcs[0].name, "organized", "pdf"))
    res.data.update(pages=out.page_count)
    return res


def rotate(srcs: list[Src], opts: O.RotateOptions, out_dir: str) -> Result:
    res = Result()
    rotated_total = 0
    for i, src in enumerate(srcs):
        doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
        _sig_warning(doc, src.name, res)
        count = 0
        for p in parse_pages(opts.pages, doc.page_count):
            page = doc[p - 1]
            w, h = page.rect.width, page.rect.height
            if (opts.only == "portrait" and w > h) or (opts.only == "landscape" and h >= w):
                continue
            page.set_rotation((page.rotation + opts.angle) % 360)
            count += 1
        if not count:
            res.warn(f"No pages of “{src.name}” matched the selection; it is unchanged")
        rotated_total += count
        path = Path(out_dir) / f"rotated-{i:03d}.pdf"
        save_pdf(doc, path)
        res.add(path, out_name(src.name, "rotated", "pdf"))
    res.data.update(rotated_pages=rotated_total)
    return res

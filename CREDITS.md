# Credits and third-party licences

The fonts and programs heypdf is built on, and their licences.

heypdf itself is free software under the
[GNU Affero General Public License v3.0](https://github.com/uroy80/heypdf/blob/main/LICENSE) (AGPL-3.0-or-later). It
stands on the work of many other projects; each component below keeps its own licence. This page is also served by
every running copy at `/credits`.

Exact versions are pinned in `requirements.lock` (Python) and the `Dockerfile` / `services/*/Dockerfile` (system
packages, base images). Every published image also carries a signed SBOM listing every package and licence it
contains:

```bash
gh attestation verify oci://ghcr.io/uroy80/heypdf:<tag> -R uroy80/heypdf --predicate-type https://spdx.dev/Document/v2.3
```

## Fonts

All fonts are bundled — the site loads nothing from font CDNs.

| Font | Used for | Licence | Source |
|---|---|---|---|
| Fraunces | headlines and the nameplate (UI); text added to PDFs | SIL Open Font License 1.1 | [github.com/undercasetype/Fraunces](https://github.com/undercasetype/Fraunces) |
| Libre Franklin | interface text; text added to PDFs | SIL Open Font License 1.1 | [github.com/impallari/Libre-Franklin](https://github.com/impallari/Libre-Franklin) |
| IBM Plex Mono | figures and file sizes (UI); text added to PDFs | SIL Open Font License 1.1 | [github.com/IBM/plex](https://github.com/IBM/plex) |
| Anton | text added to PDFs (watermarks, headings) | SIL Open Font License 1.1 | [github.com/googlefonts/AntonFont](https://github.com/googlefonts/AntonFont) |
| DejaVu Sans, Serif and Sans Mono | text added to PDFs, fallback text, SVG rendering | Bitstream Vera licence + public-domain changes | [dejavu-fonts.github.io](https://dejavu-fonts.github.io/) |
| Caveat, Dancing Script, Great Vibes | typed signatures, handwriting | SIL Open Font License 1.1 | [Google Fonts](https://github.com/google/fonts/tree/main/ofl) |
| Homemade Apple | typed signatures | Apache License 2.0 | [Google Fonts](https://github.com/google/fonts/tree/main/apache/homemadeapple) |
| Noto Sans (Bengali, Devanagari, Gujarati, Gurmukhi, Hebrew, Kannada, Malayalam, Oriya, Tamil, Telugu, Thai), Noto Naskh Arabic, Noto Sans CJK SC (subset) | text in other scripts added to PDFs | SIL Open Font License 1.1 | [github.com/notofonts](https://github.com/notofonts), [github.com/notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) |

The licence text of each font ships next to it (`static/fonts/LICENSE-*.txt` for the interface fonts,
`app/assets/fonts/licenses/` for the fonts used in PDFs).

## AI

The optional AI tools (AI Summarizer, Translate PDF) use **Claude** by Anthropic through its API when the operator
enables them; no AI model is included in this software.

## Engines in the `heypdf` image

| Component | Used for | Licence |
|---|---|---|
| [PyMuPDF / MuPDF](https://github.com/pymupdf/PyMuPDF) (Artifex) | PDF reading, rendering, editing, redaction, forms, comparison, Markdown | AGPL-3.0 (or Artifex commercial) |
| [pikepdf](https://github.com/pikepdf/pikepdf) with [qpdf](https://github.com/qpdf/qpdf) | protect, unlock | MPL-2.0; qpdf Apache-2.0 |
| [img2pdf](https://gitlab.mister-muffin.de/josch/img2pdf) | lossless images → PDF | LGPL-3.0 |
| [pyHanko](https://github.com/MatthiasValvekens/pyHanko) | PAdES digital signatures | MIT |
| [pdf2docx](https://github.com/ArtifexSoftware/pdf2docx), [python-docx](https://github.com/python-openxml/python-docx), [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) | PDF → Word / Excel | MIT |
| [Pillow](https://github.com/python-pillow/Pillow), [pillow-heif](https://github.com/bigcat88/pillow_heif) (libheif LGPL-3.0) | image decoding | MIT-CMU; BSD-3-Clause |
| [OpenCV](https://github.com/opencv/opencv) (headless) | Scan to PDF (page detection, perspective correction), Compare PDF (visual differences) | Apache-2.0 |
| [NumPy](https://numpy.org/) | image arrays | BSD-3-Clause |
| [resvg](https://github.com/linebender/resvg) via resvg-py | SVG → image | Apache-2.0 OR MIT |
| [rawpy](https://github.com/letmaik/rawpy) with [LibRaw](https://www.libraw.org/) | camera RAW files | MIT; LibRaw LGPL-2.1 / CDDL-1.0 |
| [psd-tools](https://github.com/psd-tools/psd-tools) | PSD / PSB files | MIT |
| [FastAPI](https://github.com/fastapi/fastapi), [Starlette](https://github.com/encode/starlette), [Uvicorn](https://github.com/encode/uvicorn), [Pydantic](https://github.com/pydantic/pydantic) | the web server | MIT / BSD-3-Clause |
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | the optional AI tools | MIT |
| [Debian](https://www.debian.org/) 13 "trixie" and [Python](https://www.python.org/) 3.12 (`python:3.12-slim-trixie`) | base system | various free licences; PSF-2.0 |

## Engines in the `heypdf-docs` image (Office conversions, OCR, compression, repair, PDF/A, PDF → PowerPoint)

| Component | Licence |
|---|---|
| [LibreOffice](https://www.libreoffice.org/about-us/source-code/) (headless, one-shot) | MPL-2.0 (parts LGPL-3.0-or-later) |
| [Ghostscript](https://ghostscript.com/) (always with `-dSAFER`) | AGPL-3.0 (or Artifex commercial) |
| [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) | MPL-2.0 |
| [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) and its language data | Apache-2.0 |
| [qpdf](https://github.com/qpdf/qpdf) | Apache-2.0 |
| Liberation, Carlito, Noto fonts; Caladea; DejaVu | OFL-1.1; Apache-2.0; Bitstream Vera |

## Engines in the `heypdf-renderer` and `heypdf-egress` images (HTML → PDF)

| Component | Licence |
|---|---|
| [Playwright](https://github.com/microsoft/playwright-python) | Apache-2.0 |
| [Chromium](https://www.chromium.org/) headless shell (bundled `LICENSE.headless_shell` and third-party notices) | BSD-3-Clause and others |
| Noto fonts (`fonts-noto-core`) | OFL-1.1 |
| The egress filter (`services/egress/`) | AGPL-3.0 (part of heypdf) |

The renderer's seccomp profile (`deploy/seccomp/chromium-userns.json`) is Docker's default profile from
[moby/profiles](https://github.com/moby/profiles) (Apache-2.0) with `clone`, `unshare` and `chroot` added.

## Source code for GPL and AGPL components

The images published at `ghcr.io/uroy80/heypdf*` contain programs licensed under the GPL and AGPL (Ghostscript,
MuPDF, and the GPL/LGPL libraries of the Debian base system). We distribute them unmodified, as built by Debian or by
their authors:

- **Debian packages** (Ghostscript, LibreOffice, Tesseract, qpdf, fonts, …): the exact source of every version is kept
  permanently at [snapshot.debian.org](https://snapshot.debian.org/) and browsable at
  [sources.debian.org](https://sources.debian.org/). Inside an image, `dpkg-query -W` lists the exact versions and
  `/usr/share/doc/<package>/copyright` holds each licence.
- **Python packages** (PyMuPDF and the rest): the exact versions are in `requirements.lock`; their sources are on
  [PyPI](https://pypi.org/) (sdists) and in the project repositories linked above.
- **heypdf itself**: this repository; every image is labelled with the commit it was built from
  (`org.opencontainers.image.revision`), and the site's footer links to that commit.

If you can't get any of these sources from the places above, write to the maintainer through
[github.com/uroy80/heypdf](https://github.com/uroy80/heypdf/issues) and we'll send you the complete corresponding
source for any image we published, for at least three years after we last distributed it.

## Thanks

Product inspiration: the many "do one thing to a file" web tools — heypdf was written from scratch, with its own
name, design and words.

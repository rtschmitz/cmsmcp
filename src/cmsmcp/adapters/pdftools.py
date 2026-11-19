"""Local PDF tooling: reading (text or page images), probing, cropping, rendering.

Two reading routes, cheapest first:

- :func:`get_text` — the text of every page under ``# Page N — <title>``
  headings, pages with figures flagged. Long documents reach the agent as a
  skim whose outline lists the pages, and are searched/read through
  ``cmsmcp_read_more``.
- :func:`view_pages` — selected pages as images inline in the tool result
  (full context: plots, tables, layout), sized to what the model uses.

Unlike every other adapter this module performs no network I/O: it processes
local PDF files with PyMuPDF. Inputs may be any readable local path (typically
a file produced by one of the ``*_download_*`` tools); outputs are constrained
to the cmsmcp download directory, the same policy the HTTP downloads enforce.

The vector-crop recipe mirrors the one validated in the PhD thesis tooling
(notes/pdf_mining.md): normalize the crop box to the mediabox, clamp the clip
rectangle, replay the source page into a new same-size page via
``show_pdf_page`` so vector drawings and text survive un-rasterized, and save
with ``garbage=4, deflate=True``.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any, Literal

import pymupdf
from mcp.types import ImageContent

from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, PolicyViolationError, RequestTooLargeError

_SOURCE = "local"
_BLOCK_TEXT_LIMIT = 300
_MAX_DPI = 600
# Largest raster cmsmcp builds in memory (RGBA bytes); independent of the
# download cap, which only bounds files on disk.
_MAX_BITMAP_BYTES = 64 * 1024 * 1024

# Inline images: Claude downsizes anything with a long edge over 1568 px or
# above ~1.15 megapixels, so larger renders only cost bytes. Tokens are about
# width * height / 750: ~1.5k per page at "standard", ~0.5k at "low".
_INLINE_MAX_EDGE = 1568
_INLINE_PIXELS = {"standard": 1_150_000, "low": 400_000}
MAX_VIEW_PAGES = 8
# A page with an embedded image or at least this many vector paths likely
# carries a plot or diagram (slide frames and rules stay below it).
_FIGURE_DRAWINGS = 20
_TITLE_CHARS = 80
Detail = Literal["standard", "low"]

BBox = tuple[float, float, float, float]


def _resolve_input(pdf_path: str) -> Path:
    """Resolve the input PDF path and prove it exists."""
    candidate = Path(pdf_path).expanduser()
    if not candidate.is_file():
        raise InvalidInputError(
            f"Not a readable file: {candidate.name}",
            instructions=(
                "Pass the path of a local PDF, for example one returned by a *_download_* tool."
            ),
        )
    return candidate


def _open_pdf(pdf_path: str) -> tuple[Path, pymupdf.Document]:
    """Open a local PDF, mapping failures to a safe error."""
    path = _resolve_input(pdf_path)
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise InvalidInputError(
            f"The file is not a valid PDF: {path.name}",
            instructions="Pass a PDF file; other document formats are not supported.",
        ) from exc
    if not doc.is_pdf:
        doc.close()
        raise InvalidInputError(
            f"The file is not a PDF: {path.name}",
            instructions="Convert the document to PDF before processing it.",
        )
    if doc.is_encrypted:
        doc.close()
        raise InvalidInputError(
            f"The PDF is encrypted: {path.name}",
            instructions="Decrypt the PDF locally before processing it.",
        )
    return path, doc


def _get_page(doc: pymupdf.Document, page: int) -> pymupdf.Page:
    """Return a 1-based page after validation."""
    if page < 1 or page > doc.page_count:
        raise InvalidInputError(
            f"Page {page} is out of range; the document has {doc.page_count} page(s)",
            instructions="Pages are numbered from 1, like pdftotext.",
        )
    normalized = doc[page - 1]
    normalized.set_cropbox(normalized.mediabox)
    return normalized


def _clip(page: pymupdf.Page, bbox: BBox | None) -> pymupdf.Rect:
    """Validate a clip rectangle against the normalized page."""
    full = pymupdf.Rect(page.mediabox)
    if bbox is None:
        return full
    rect = pymupdf.Rect(*bbox)
    if rect.is_empty or rect.is_infinite:
        raise InvalidInputError(
            f"The bounding box {list(bbox)} is empty or inverted",
            instructions="Pass [x0, y0, x1, y1] in PDF points with x0 < x1 and y0 < y1.",
        )
    clipped = rect & full
    if clipped.is_empty:
        raise InvalidInputError(
            "The bounding box does not intersect the page",
            instructions=(
                f"Page size is [{full.x0:.0f}, {full.y0:.0f}, {full.x1:.0f}, {full.y1:.0f}] "
                "(x0, y0, x1, y1) in PDF points."
            ),
        )
    return clipped


def _output_path(settings: Settings, output_name: str | None, default: str) -> Path:
    """Constrain outputs to the cmsmcp download directory."""
    settings.ensure_directories()
    name = output_name if output_name is not None else default
    candidate = Path(name)
    if candidate.is_absolute() or candidate.name != name:
        raise PolicyViolationError(
            "Output names must be plain file names without directories",
            instructions="Files are always written inside the cmsmcp download directory.",
        )
    return (settings.download_dir / candidate.name).resolve()


def _check_size(settings: Settings, path: Path, description: str) -> int:
    """Enforce the bounded-output policy on a freshly written file."""
    size = path.stat().st_size
    if size > settings.max_download_bytes:
        path.unlink()
        raise RequestTooLargeError(
            f"The {description} exceeds the {settings.max_download_bytes}-byte limit",
            instructions="Lower the DPI or crop a smaller region.",
        )
    return size


def _union(rects: list[pymupdf.Rect]) -> list[float] | None:
    """Union of rectangles as a JSON-safe list, or None when empty."""
    if not rects:
        return None
    result = rects[0]
    for rect in rects[1:]:
        result |= rect
    return [round(result.x0, 2), round(result.y0, 2), round(result.x1, 2), round(result.y1, 2)]


async def probe_page(pdf_path: str, page: int) -> dict[str, Any]:
    """Return the geometry of one PDF page for deriving a crop box.

    Coordinates are PDF points in the same space used by ``crop_figure``:
    text blocks come from ``get_text('blocks')``, images from
    ``get_image_info``, vector drawings from ``get_drawings`` (dashed frames
    and plot axes extend below the last text block, so their union is key).
    """
    input_path, doc = _open_pdf(pdf_path)
    try:
        target = _get_page(doc, page)
        text_blocks = [
            {
                "bbox": [round(b[0], 2), round(b[1], 2), round(b[2], 2), round(b[3], 2)],
                "text": " ".join(b[4].split())[:_BLOCK_TEXT_LIMIT],
            }
            for b in target.get_text("blocks")
            if b[6] == 0
        ]
        images = [
            {
                "bbox": [round(v, 2) for v in info["bbox"]],
                "width_px": info["width"],
                "height_px": info["height"],
            }
            for info in target.get_image_info()
        ]
        drawings = [item["rect"] for item in target.get_drawings()]
        text_rects = [pymupdf.Rect(b["bbox"]) for b in text_blocks]
        image_rects = [pymupdf.Rect(i["bbox"]) for i in images]
        return {
            "source": _SOURCE,
            "pdf_path": str(input_path),
            "page": page,
            "page_count": doc.page_count,
            "page_size": [round(target.mediabox.width, 2), round(target.mediabox.height, 2)],
            "rotation": target.rotation,
            "text_blocks": text_blocks,
            "images": images,
            "drawing_count": len(drawings),
            "clip_hints": {
                "text_union": _union(text_rects),
                "images_union": _union(image_rects),
                "drawings_union": _union(drawings),
                "all_union": _union(text_rects + image_rects + drawings),
            },
        }
    finally:
        doc.close()


async def crop_figure(
    settings: Settings,
    pdf_path: str,
    page: int,
    bbox: BBox,
    output_name: str | None = None,
) -> dict[str, Any]:
    """Crop a page region into a new single-page PDF, preserving vectors.

    The source content stream is replayed into a new page sized to the clip,
    so drawings stay vector and text stays selectable; nothing is rasterized.
    """
    input_path, doc = _open_pdf(pdf_path)
    try:
        target = _get_page(doc, page)
        clip = _clip(target, bbox)
        default = (
            f"{input_path.stem}-p{page}-{clip.x0:.0f}-{clip.y0:.0f}-{clip.x1:.0f}-{clip.y1:.0f}.pdf"
        )
        destination = _output_path(settings, output_name, default)
        out = pymupdf.open()
        try:
            new_page = out.new_page(width=clip.width, height=clip.height)
            new_page.show_pdf_page(new_page.rect, doc, page - 1, clip=clip)
            out.save(destination, garbage=4, deflate=True)
        finally:
            out.close()
        size = _check_size(settings, destination, "cropped PDF")
        return {
            "source": _SOURCE,
            "pdf_path": str(input_path),
            "page": page,
            "clip": [round(clip.x0, 2), round(clip.y0, 2), round(clip.x1, 2), round(clip.y1, 2)],
            "local_path": str(destination),
            "size_bytes": size,
        }
    finally:
        doc.close()


async def render_page(
    settings: Settings,
    pdf_path: str,
    page: int,
    bbox: BBox | None = None,
    dpi: int = 150,
) -> tuple[dict[str, Any], ImageContent]:
    """Rasterize a page (or a clipped region) to PNG for visual verification.

    The PNG keeps the requested DPI on disk; the inline copy is fitted to the
    model's image size.
    """
    if dpi < 1 or dpi > _MAX_DPI:
        raise InvalidInputError(
            f"DPI {dpi} is outside the supported range 1-{_MAX_DPI}",
            instructions="Render at ~110 DPI for a quick check, ~220 DPI for pale figures.",
        )
    input_path, doc = _open_pdf(pdf_path)
    try:
        target = _get_page(doc, page)
        clip = _clip(target, bbox)
        pixels = int(clip.width * dpi / 72) * int(clip.height * dpi / 72) * 4
        if pixels > _MAX_BITMAP_BYTES:
            raise RequestTooLargeError(
                "The rendered bitmap exceeds the configured size limit",
                instructions="Lower the DPI or render a smaller region.",
            )
        pixmap = target.get_pixmap(dpi=dpi, clip=clip)
        default = f"{input_path.stem}-p{page}-{dpi}dpi.png"
        destination = _output_path(settings, None, default)
        pixmap.save(destination)
        size = _check_size(settings, destination, "rendered PNG")
        info = {
            "source": _SOURCE,
            "pdf_path": str(input_path),
            "page": page,
            "clip": [round(clip.x0, 2), round(clip.y0, 2), round(clip.x1, 2), round(clip.y1, 2)],
            "dpi": dpi,
            "width_px": pixmap.width,
            "height_px": pixmap.height,
            "local_path": str(destination),
            "size_bytes": size,
        }
        return info, _image_content(_inline_pixmap(target, clip, "standard"))
    finally:
        doc.close()


# ---------------------------------------------------------------- reading


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    """``"3"``, ``"1-5,8"`` or None (all) -> sorted 1-based page numbers."""
    if spec is None or not spec.strip():
        return list(range(1, page_count + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not match:
            raise InvalidInputError(
                f"Bad page selection {spec!r}",
                instructions="Use page numbers and ranges, e.g. '3' or '1-5,8'.",
            )
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if first < 1 or last < first or last > page_count:
            raise InvalidInputError(
                f"Pages {part.strip()} are out of range; the document has {page_count} page(s)",
                instructions="Pages are numbered from 1.",
            )
        pages.update(range(first, last + 1))
    return sorted(pages)


def _has_figure(page: pymupdf.Page) -> bool:
    return bool(page.get_images()) or len(page.get_drawings()) >= _FIGURE_DRAWINGS


def _page_text(page: pymupdf.Page) -> str:
    """Reading-order text; blank runs collapsed, '#' line starts escaped so
    PDF content cannot pose as a page heading in the outline."""
    lines = [line.rstrip() for line in page.get_text("text", sort=True).splitlines()]
    out: list[str] = []
    for line in lines:
        if not line.strip():
            if out and out[-1]:
                out.append("")
            continue
        out.append("\\" + line if line.lstrip().startswith("#") else line)
    return "\n".join(out).strip()


def _title(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first = first.lstrip("\\")
    return first if len(first) <= _TITLE_CHARS else first[: _TITLE_CHARS - 1].rstrip() + "…"


async def get_text(pdf_path: str, pages: str | None = None) -> dict[str, Any]:
    """Text of the selected pages, each under a ``# Page N — <title>`` heading."""
    input_path, doc = _open_pdf(pdf_path)
    try:
        selected = parse_pages(pages, doc.page_count)
        sections: list[str] = []
        figures: list[int] = []
        empty: list[int] = []
        for number in selected:
            page = _get_page(doc, number)
            text = _page_text(page)
            figure = _has_figure(page)
            if figure:
                figures.append(number)
            if not text:
                empty.append(number)
            title = _title(text)
            heading = f"# Page {number}" + (f" — {title}" if title else "")
            heading += " [figure]" if figure else ""
            body = text or "(no text layer: scanned or image-only; see it with pdf_view_pages)"
            sections.append(f"{heading}\n{body}")
        return {
            "source": _SOURCE,
            "pdf_path": str(input_path),
            "page_count": doc.page_count,
            "pages": _ranges(selected),
            "figure_pages": _ranges(figures) or None,
            "pages_without_text": _ranges(empty) or None,
            "note": (
                "Pages marked [figure] carry plots/diagrams: when they matter, look at them "
                "with pdf_view_pages."
                if figures
                else None
            ),
            "text": "\n\n".join(sections),
        }
    finally:
        doc.close()


async def view_pages(
    pdf_path: str,
    pages: str,
    *,
    detail: Detail = "standard",
    include_text: bool = True,
) -> tuple[dict[str, Any], list[ImageContent]]:
    """Selected pages as inline images (plus their text), for full context."""
    input_path, doc = _open_pdf(pdf_path)
    try:
        selected = parse_pages(pages, doc.page_count)
        if len(selected) > MAX_VIEW_PAGES:
            raise InvalidInputError(
                f"{len(selected)} pages requested; at most {MAX_VIEW_PAGES} per call",
                instructions="Split the pages over several calls, or read the text with "
                "pdf_get_text first and view only the pages that matter.",
            )
        items: list[dict[str, Any]] = []
        images: list[ImageContent] = []
        for number in selected:
            page = _get_page(doc, number)
            pixmap = _inline_pixmap(page, pymupdf.Rect(page.mediabox), detail)
            images.append(_image_content(pixmap))
            item: dict[str, Any] = {
                "page": number,
                "image": f"{pixmap.width}x{pixmap.height} px",
            }
            if include_text:
                item["text"] = _page_text(page) or None
            items.append(item)
        return (
            {
                "source": _SOURCE,
                "pdf_path": str(input_path),
                "page_count": doc.page_count,
                "detail": detail,
                "note": "Page images follow in page order.",
                "pages": items,
            },
            images,
        )
    finally:
        doc.close()


def _ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 7] -> "1-3,7"."""
    parts: list[list[int]] = []
    for number in numbers:
        if parts and number == parts[-1][1] + 1:
            parts[-1][1] = number
        else:
            parts.append([number, number])
    return ",".join(f"{a}" if a == b else f"{a}-{b}" for a, b in parts)


def _inline_pixmap(page: pymupdf.Page, clip: pymupdf.Rect, detail: Detail) -> pymupdf.Pixmap:
    """Render ``clip`` at the largest size the model uses for ``detail``."""
    area = max(clip.width * clip.height, 1.0)
    zoom = min(
        _INLINE_MAX_EDGE / max(clip.width, clip.height, 1.0),
        (_INLINE_PIXELS[detail] / area) ** 0.5,
        _MAX_DPI / 72,
    )
    # Pixel dimensions round up; stay just inside the caps.
    zoom *= 0.995
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)


def _image_content(pixmap: pymupdf.Pixmap) -> ImageContent:
    """PNG or JPEG, whichever is smaller (line plots favour PNG, photos JPEG)."""
    png = pixmap.tobytes("png")
    jpeg = pixmap.tobytes("jpeg", jpg_quality=85)
    data, mime = (png, "image/png") if len(png) <= len(jpeg) else (jpeg, "image/jpeg")
    return ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type=mime)

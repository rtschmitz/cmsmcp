"""Contract tests for the local PDF figure tools (no network)."""

from __future__ import annotations

import base64
from pathlib import Path

import pymupdf
import pytest
from mcp.types import ImageContent

from cmsmcp.adapters import pdftools
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.errors import InvalidInputError, PolicyViolationError, RequestTooLargeError

PAGE_W, PAGE_H = 400.0, 600.0
FIG_RECT = (100.0, 200.0, 300.0, 400.0)
CAPTION_Y = 415.0


@pytest.fixture
def figure_pdf(tmp_path: Path) -> Path:
    """One page with body text, a vector rectangle, and a caption under it."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((50, 50), "Section text above the figure.")
    page.draw_rect(pymupdf.Rect(*FIG_RECT), color=(1, 0, 0), width=2)
    page.insert_text((FIG_RECT[0], FIG_RECT[1] + 20), "axis label")
    page.insert_text((50, CAPTION_Y), "Figure 1.1: caption below the frame.")
    path = tmp_path / "figure.pdf"
    doc.save(path)
    doc.close()
    return path


async def test_probe_page_reports_geometry(figure_pdf: Path) -> None:
    probe = await pdftools.probe_page(str(figure_pdf), 1)
    assert probe["page_count"] == 1
    assert probe["page_size"] == [PAGE_W, PAGE_H]
    texts = [block["text"] for block in probe["text_blocks"]]
    assert any("caption" in text for text in texts)
    assert any("axis label" in text for text in texts)
    # The vector frame extends below the last text inside the figure; its union
    # must reach the frame bottom, further down than the text union.
    drawings_union = probe["clip_hints"]["drawings_union"]
    assert drawings_union == pytest.approx(list(FIG_RECT), abs=1.5)
    assert probe["images"] == []
    assert probe["drawing_count"] >= 1


async def test_crop_figure_preserves_vectors(figure_pdf: Path, settings: Settings) -> None:
    result = await pdftools.crop_figure(
        settings, str(figure_pdf), 1, (90.0, 190.0, 320.0, FIG_RECT[3] + 2)
    )
    out_path = Path(result["local_path"])
    assert out_path.is_file()
    assert out_path.parent == (settings.download_dir).resolve()
    cropped = pymupdf.open(out_path)
    try:
        page = cropped[0]
        assert page.rect.width == pytest.approx(230.0, abs=0.01)
        assert page.rect.height == pytest.approx(212.0, abs=0.01)
        assert page.get_drawings(), "vector frame lost during crop"
        assert "axis label" in page.get_text()
        assert "caption" not in page.get_text()
        assert "Section text" not in page.get_text()
    finally:
        cropped.close()


async def test_crop_figure_clamps_clip_to_page(figure_pdf: Path, settings: Settings) -> None:
    result = await pdftools.crop_figure(settings, str(figure_pdf), 1, (100.0, 200.0, 9999.0, 400.0))
    assert result["clip"] == [100.0, 200.0, PAGE_W, 400.0]


async def test_crop_figure_rejects_non_intersecting_bbox(
    figure_pdf: Path, settings: Settings
) -> None:
    with pytest.raises(InvalidInputError):
        await pdftools.crop_figure(settings, str(figure_pdf), 1, (1000.0, 1000.0, 1200.0, 1400.0))


async def test_crop_figure_rejects_inverted_bbox(figure_pdf: Path, settings: Settings) -> None:
    with pytest.raises(InvalidInputError):
        await pdftools.crop_figure(settings, str(figure_pdf), 1, (300.0, 400.0, 100.0, 200.0))


async def test_output_name_cannot_escape_download_dir(figure_pdf: Path, settings: Settings) -> None:
    with pytest.raises(PolicyViolationError):
        await pdftools.crop_figure(
            settings, str(figure_pdf), 1, FIG_RECT, output_name="../escaped.pdf"
        )


async def test_crop_figure_accepts_output_name(figure_pdf: Path, settings: Settings) -> None:
    result = await pdftools.crop_figure(
        settings, str(figure_pdf), 1, FIG_RECT, output_name="figure_1_1.pdf"
    )
    assert Path(result["local_path"]).name == "figure_1_1.pdf"


async def test_render_page_writes_png(figure_pdf: Path, settings: Settings) -> None:
    result, image = await pdftools.render_page(settings, str(figure_pdf), 1, bbox=FIG_RECT, dpi=110)
    out_path = Path(result["local_path"])
    assert out_path.suffix == ".png"
    assert out_path.parent == (settings.download_dir).resolve()
    assert result["width_px"] == pytest.approx((FIG_RECT[2] - FIG_RECT[0]) * 110 / 72, abs=2)
    assert out_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    # The inline copy is fitted to the model's image size, not the file's DPI.
    shown = _pixmap(image)
    assert shown.width * shown.height <= 1_150_000
    assert max(shown.width, shown.height) <= 1568


async def test_render_page_enforces_pixel_budget(
    figure_pdf: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdftools, "_MAX_BITMAP_BYTES", 4000)
    with pytest.raises(RequestTooLargeError):
        await pdftools.render_page(settings, str(figure_pdf), 1, dpi=600)


async def test_probe_rejects_out_of_range_page(figure_pdf: Path) -> None:
    with pytest.raises(InvalidInputError):
        await pdftools.probe_page(str(figure_pdf), 2)


async def test_probe_rejects_non_pdf(tmp_path: Path) -> None:
    not_pdf = tmp_path / "not-a-pdf.txt"
    not_pdf.write_text("plain text")
    with pytest.raises(InvalidInputError):
        await pdftools.probe_page(str(not_pdf), 1)


async def test_probe_rejects_missing_file() -> None:
    with pytest.raises(InvalidInputError):
        await pdftools.probe_page("/nonexistent/missing.pdf", 1)


def _pixmap(image: ImageContent) -> pymupdf.Pixmap:
    assert image.mime_type in {"image/png", "image/jpeg"}
    return pymupdf.Pixmap(base64.b64decode(image.data))


@pytest.fixture
def deck_pdf(tmp_path: Path) -> Path:
    """A 12-slide deck: titles on every slide, a raster plot on slide 4, a
    vector plot on slide 7, an image-only slide 9, a '#'-led line on slide 2."""
    doc = pymupdf.open()
    for number in range(1, 13):
        page = doc.new_page(width=720, height=540)
        if number != 9:
            page.insert_text((40, 60), f"Slide {number} title: trigger efficiency", fontsize=20)
            page.insert_text((40, 120), f"body text of slide {number}")
        if number == 2:
            page.insert_text((40, 160), "# not a heading")
        if number in (4, 9):
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), 0)
            pix.set_rect(pix.irect, (200, 30, 30))
            page.insert_image(pymupdf.Rect(200, 180, 520, 420), pixmap=pix)
        if number == 7:
            for i in range(30):
                page.draw_line((100, 400 - i * 8), (600, 400 - i * 7), color=(0, 0, 1))
    path = tmp_path / "deck.pdf"
    doc.save(path)
    doc.close()
    return path


async def test_get_text_pages_titles_and_figure_marks(deck_pdf: Path) -> None:
    result = await pdftools.get_text(str(deck_pdf))
    text = result["text"]
    assert result["page_count"] == 12 and result["pages"] == "1-12"
    assert result["figure_pages"] == "4,7,9"
    assert result["pages_without_text"] == "9"
    assert "# Page 1 — Slide 1 title: trigger efficiency\nSlide 1 title" in text
    assert "# Page 4 — Slide 4 title: trigger efficiency [figure]" in text
    assert "# Page 7 — Slide 7 title: trigger efficiency [figure]" in text
    assert "# Page 9 [figure]\n(no text layer" in text
    # PDF content cannot pose as a page heading.
    assert "\\# not a heading" in text
    assert [line for line in text.splitlines() if line.startswith("# ")] == [
        line for line in text.splitlines() if line.startswith("# Page ")
    ]
    assert "pdf_view_pages" in result["note"]


async def test_get_text_page_selection(deck_pdf: Path) -> None:
    result = await pdftools.get_text(str(deck_pdf), "2-3,11")
    assert result["pages"] == "2-3,11"
    headings = [line for line in result["text"].splitlines() if line.startswith("# ")]
    assert [line.split(" —")[0] for line in headings] == ["# Page 2", "# Page 3", "# Page 11"]
    for bad in ("0", "5-3", "13", "x"):
        with pytest.raises(InvalidInputError):
            await pdftools.get_text(str(deck_pdf), bad)


async def test_view_pages_returns_fitted_images_with_text(deck_pdf: Path) -> None:
    data, images = await pdftools.view_pages(str(deck_pdf), "4,7")
    assert [item["page"] for item in data["pages"]] == [4, 7]
    assert "body text of slide 4" in data["pages"][0]["text"]
    assert len(images) == 2
    for image in images:
        pix = _pixmap(image)
        assert max(pix.width, pix.height) <= 1568
        assert 900_000 < pix.width * pix.height <= 1_150_000  # ~1.5k tokens
    low, low_images = await pdftools.view_pages(
        str(deck_pdf), "4", detail="low", include_text=False
    )
    assert "text" not in low["pages"][0]
    assert _pixmap(low_images[0]).width * _pixmap(low_images[0]).height <= 400_000


async def test_view_pages_caps_pages_per_call(deck_pdf: Path) -> None:
    with pytest.raises(InvalidInputError, match="at most 8"):
        await pdftools.view_pages(str(deck_pdf), "1-9")

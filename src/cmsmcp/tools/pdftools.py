"""MCP tool registration for local PDF reading and figure extraction."""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from cmsmcp.adapters import pdftools
from cmsmcp.auth.service import AuthService
from cmsmcp.config.settings import Settings
from cmsmcp.contracts.results import ToolEnvelope, WithImages
from cmsmcp.net.client import SafeHttpClient
from cmsmcp.tools.common import guarded, guarded_with_images

PdfPath = Annotated[
    str,
    Field(description="Path of a local PDF, e.g. one returned by a *_download_* tool."),
]

BBoxAnnotation = Annotated[
    tuple[float, float, float, float],
    Field(
        description=(
            "Clip rectangle [x0, y0, x1, y1] in PDF points (origin top-left, "
            "y grows downward), in the coordinate space returned by "
            "pdf_probe_page."
        )
    ),
]


def register_pdftools_tools(
    server: MCPServer,
    settings: Settings,
    auth: AuthService,
    http: SafeHttpClient,
) -> None:
    """Register the local PDF reading and figure-processing tools.

    Local processing only: no HTTP client is needed, but the signature keeps
    the shared registration shape.
    """
    del http

    @server.tool(
        name="pdf_get_text",
        description=(
            "Read a downloaded PDF (slides, note, paper, thesis) as text: every page under a "
            "'# Page N — <title>' heading, pages carrying plots marked [figure]. The cheap "
            "route — start here. A long PDF arrives as a skim whose outline lists the pages; "
            "search it with cmsmcp_read_more(find=...) or read on. When a figure, table "
            "layout or equation matters, look at those pages with pdf_view_pages."
        ),
    )
    async def pdf_get_text(
        pdf_path: PdfPath,
        pages: Annotated[
            str | None, Field(description="Pages, e.g. '3' or '1-5,8'; default all.")
        ] = None,
    ) -> ToolEnvelope:
        """Page-by-page text of a local PDF."""
        return await guarded(auth, lambda: pdftools.get_text(pdf_path, pages))

    @server.tool(
        name="pdf_view_pages",
        description=(
            "Look at PDF pages as images (full context: plots, tables, diagrams, layout), "
            f"returned inline with each page's text; at most {pdftools.MAX_VIEW_PAGES} pages "
            "per call. Costs ~1.5k tokens per page (detail='low': ~0.5k, enough to read a "
            "slide's layout but not small plot labels). Use it for the pages that matter, "
            "found with pdf_get_text ([figure] marks) — not to read a whole document."
        ),
    )
    async def pdf_view_pages(
        pdf_path: PdfPath,
        pages: Annotated[str, Field(description="Pages, e.g. '7' or '3-5,9'.")],
        detail: Literal["standard", "low"] = "standard",
        include_text: Annotated[
            bool, Field(description="Also return each page's text (cheap; helps quoting).")
        ] = True,
    ) -> WithImages:
        """Inline page images of a local PDF."""
        return await guarded_with_images(
            auth,
            lambda: pdftools.view_pages(pdf_path, pages, detail=detail, include_text=include_text),
        )

    @server.tool(
        name="pdf_probe_page",
        description=(
            "Geometry of one page of a local PDF: text blocks, embedded "
            "images, and union bounds of vector drawings, all in PDF points. "
            "Use it to derive the clip box for pdf_crop_figure: figure labels "
            "live between the surrounding text blocks, and dashed frames/plot "
            "axes can extend below the last text block, so always check the "
            "drawings_union clip hint before cropping. Pages are numbered "
            "from 1."
        ),
    )
    async def pdf_probe_page(
        *,
        pdf_path: Annotated[
            str,
            Field(description="Path of a local PDF, e.g. one returned by a *_download_* tool."),
        ],
        page: Annotated[int, Field(ge=1, description="1-based page number.")],
    ) -> ToolEnvelope:
        """Text/image/vector drawing geometry of one PDF page."""
        return await guarded(auth, lambda: pdftools.probe_page(pdf_path, page))

    @server.tool(
        name="pdf_crop_figure",
        description=(
            "Crop one region of a local PDF page into a new single-page PDF, "
            "preserving vector drawings and selectable text (no rasterization) "
            "— extract a figure out of a paper or note. The output is written "
            "inside cmsmcp download storage. Derive the bbox with "
            "pdf_probe_page and verify the result with pdf_render_page (pale "
            "vector figures can look empty at low DPI; re-render higher "
            "before concluding they are broken)."
        ),
    )
    async def pdf_crop_figure(
        *,
        pdf_path: Annotated[
            str,
            Field(description="Path of a local PDF, e.g. one returned by a *_download_* tool."),
        ],
        page: Annotated[int, Field(ge=1, description="1-based page number.")],
        bbox: BBoxAnnotation,
        output_name: Annotated[
            str | None,
            Field(description="Optional plain output file name (no directories)."),
        ] = None,
    ) -> ToolEnvelope:
        """Vector-preserving crop of one PDF page region."""
        return await guarded(
            auth,
            lambda: pdftools.crop_figure(settings, pdf_path, page, bbox, output_name=output_name),
        )

    @server.tool(
        name="pdf_render_page",
        description=(
            "Rasterize one local PDF page (or a clipped region of it, e.g. one plot) to PNG "
            "inside cmsmcp download storage at the given DPI, and show it inline (fitted to "
            "~1.15 MP) — a region renders sharper than its whole page in pdf_view_pages. "
            "~110 DPI is enough for a quick check; use ~220 DPI for light/pale vector figures."
        ),
    )
    async def pdf_render_page(
        *,
        pdf_path: Annotated[
            str,
            Field(description="Path of a local PDF, e.g. one returned by a *_download_* tool."),
        ],
        page: Annotated[int, Field(ge=1, description="1-based page number.")],
        bbox: BBoxAnnotation | None = None,
        dpi: Annotated[int, Field(ge=1, le=600, description="Render resolution, 1-600.")] = 150,
    ) -> WithImages:
        """PNG render of a PDF page or region for visual checks."""

        async def render() -> tuple[dict, list]:
            info, image = await pdftools.render_page(settings, pdf_path, page, bbox=bbox, dpi=dpi)
            return info, [image]

        return await guarded_with_images(auth, render)

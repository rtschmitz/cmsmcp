"""Skim-then-read helpers for long tool results (see ``cmsmcp_read_more``).

A result longer than the first pass is cut; the agent gets the head plus an
outline of the headings that follow (Markdown-style ``#`` lines, which
:func:`cmsmcp.adapters.common.html_to_text` emits for HTML headings), and can
then search the full text (:func:`find_passages`) or read on from any offset.
"""

from __future__ import annotations

import re
from bisect import bisect_right

from cmsmcp.output import cut_point

# Multi-line body fields render two spaces in; four or more extra spaces is a
# code block (html_to_text indents <pre>), whose "# comments" are not headings.
_HEADING_RE = re.compile(r"^ {0,3}(#{1,4}) +(\S.{0,118})$", re.MULTILINE)
OUTLINE_MAX_CHARS = 2_500
_FIND_CONTEXT = 600


def outline(text: str, *, after: int, max_chars: int = OUTLINE_MAX_CHARS) -> str:
    """``@offset ## heading`` lines for headings at or after ``after``.

    Deeper levels are dropped first when the outline would exceed ``max_chars``;
    any remainder is counted rather than listed. Empty when there are none.
    """
    headings = [
        (m.start(), len(m.group(1)), m.group(2).strip()) for m in _HEADING_RE.finditer(text, after)
    ]
    for depth in (4, 3, 2, 1):
        lines = [
            f"  @{pos} {'#' * level} {title}" for pos, level, title in headings if level <= depth
        ]
        if sum(len(line) + 1 for line in lines) <= max_chars:
            return "\n".join(lines)
    shown: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            break
        shown.append(line)
        used += len(line) + 1
    return "\n".join([*shown, f"  …+{len(lines) - len(shown)} more headings (use find)"])


def _pattern(query: str) -> re.Pattern[str]:
    terms = [re.escape(term.strip()) for term in query.split("|") if term.strip()]
    return re.compile("|".join(terms) or re.escape(query), re.IGNORECASE)


def _window(text: str, start: int, end: int) -> tuple[int, int]:
    """About ``_FIND_CONTEXT`` chars either side of a match, in whole lines."""
    lo = max(0, start - _FIND_CONTEXT)
    hi = min(len(text), end + _FIND_CONTEXT)
    if lo > 0:
        newline = text.find("\n", lo, start)
        lo = newline + 1 if newline != -1 else lo
    if hi < len(text):
        newline = text.rfind("\n", end, hi)
        hi = newline if newline != -1 else hi
    return lo, hi


def find_passages(text: str, query: str, *, start: int, max_chars: int) -> tuple[str, int | None]:
    """Passages of ``text`` (from ``start``) around case-insensitive matches of
    ``query`` (``|`` separates alternatives), within ``max_chars``.

    Returns the rendered passages and the offset to resume the search from, or
    None when every match was shown.
    """
    matches = list(_pattern(query).finditer(text, start))
    if not matches:
        return f'find "{query}": no matches after offset {start} of {len(text)} chars', None
    # Each passage is labelled with the heading it falls under (page, section).
    headings = [(m.start(), m.group(0).strip()) for m in _HEADING_RE.finditer(text)]
    heading_starts = [pos for pos, _ in headings]
    # [lo, hi, match starts]; overlapping windows merge.
    windows: list[tuple[int, int, list[int]]] = []
    for match in matches:
        lo, hi = _window(text, match.start(), match.end())
        if windows and lo <= windows[-1][1]:
            prev_lo, prev_hi, starts = windows[-1]
            windows[-1] = (prev_lo, max(prev_hi, hi), [*starts, match.start()])
        else:
            windows.append((lo, hi, [match.start()]))
    parts: list[str] = []
    used = shown = 0
    resume: int | None = None
    for lo, hi, starts in windows:
        if parts and used + (hi - lo) > max_chars:
            resume = starts[0]
            break
        end = min(hi, cut_point(text, lo, max_chars - used))
        under = bisect_right(heading_starts, lo) - 1
        where = f" · under {headings[under][1]}" if under >= 0 else ""
        parts.append(f"── @{lo}{where} ──\n{text[lo:end].strip()}")
        used += end - lo
        shown += sum(1 for pos in starts if pos < end)
        if end < hi:  # budget ran out inside this window
            resume = next((pos for pos in starts if pos >= end), end)
            break
    header = (
        f'find "{query}": {len(matches)} matches after offset {start} of {len(text)} chars'
        f"{f', showing {shown}' if resume is not None else ''}"
    )
    return "\n".join([header, *parts]), resume

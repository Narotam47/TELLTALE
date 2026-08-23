from __future__ import annotations

import html
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Heading anchors: an explicit "here begins the job" marker.
# ---------------------------------------------------------------------------
_ANCHOR_PATTERNS = [
    r"key\s+responsibilities",
    r"roles?\s*(?:&|and)\s*responsibilities",
    r"responsibilities",
    r"what\s+you(?:'|’)?ll\s+(?:do|be\s+doing|bring)",
    r"what\s+you\s+will\s+(?:do|be\s+doing)",
    r"about\s+the\s+role",
    r"the\s+role",
    r"your\s+role",
    r"role\s+overview",
    r"job\s+description",
    r"job\s+purpose",
    r"position\s+overview",
    r"what\s+we(?:'|’)?re\s+looking\s+for",
    r"who\s+we(?:'|’)?re\s+looking\s+for",
    r"requirements",
    r"qualifications",
    r"skills\s*(?:&|and)\s*experience",
    # Sentence-style leads seen in the corpus, e.g. "As a leader, you will be:"
    r"as\s+an?\s+[\w /-]{2,40},?\s+you\s+will(?:\s+be)?",
    r"in\s+this\s+role,?\s+you\s+will",
    r"you\s+will\s+be\s+responsible\s+for",
]
_ANCHOR_RE = re.compile(
    r"^[\s\W]{0,4}(?:" + "|".join(_ANCHOR_PATTERNS) + r")\s*[:\-–—]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_ANCHOR_INLINE_RE = re.compile(
    r"(?:^|\n)[\s\W]{0,4}(?:" + "|".join(_ANCHOR_PATTERNS) + r")\s*[:\-–—]",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Independent content scorer.
#
# Deliberately NOT derived from the stripper's own boundary: measuring the
# window against the same function that chose it would report ~100% by
# construction and prove nothing.
# ---------------------------------------------------------------------------
_ROLE_MARKERS = re.compile(
    r"you(?:'|’)?ll\b|you\s+will\b|responsib|require|qualificat|experience\s+(?:in|with)"
    r"|years?\s+of\b|ability\s+to\b|proficien|familiar\s+with|degree\s+in\b|expertise"
    r"|hands[- ]on|you\s+should|we\s+expect|day[- ]to[- ]day|reporting\s+to"
    r"|\b(?:design|build|develop|implement|maintain|own|drive|manage|lead|ensure|"
    r"collaborate|partner|deliver|execute|analyse|analyze|monitor|coordinate)\b",
    re.IGNORECASE,
)
_COMPANY_MARKERS = re.compile(
    r"headquarter|founded|our\s+mission|flagship|registered\s+users|crore|"
    r"million\s+users|we\s+are\s+a\b|our\s+story|great\s+place\s+to\s+work|"
    r"equal\s+opportunit|portfolio\s+of\s+businesses|about\s+us|culture:|"
    r"our\s+journey|celebrate\s+diversity|inclusive\s+environment|valuation|"
    r"backed\s+by|investors|transactions\s+daily|launched\s+in\s+\w+\s+\d{4}",
    re.IGNORECASE,
)

_BOILERPLATE_HEADING_RE = re.compile(
    r"^[\s\W]{0,4}(?:"
    r"about\s+(?:us|the\s+company|our\s+company|the\s+team|[A-Z][\w.& ]{1,40}?)"
    r"|who\s+we\s+are|our\s+story|our\s+mission|company\s+overview"
    r"|build\s+the\s+future|why\s+join\s+us|culture|life\s+at\s+[\w.& ]{1,30}"
    r"|benefits|perks|equal\s+opportunity"
    r")\s*[:\-–—]?\s*$",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    """Decode stray entities and collapse whitespace.

    Some sources double-encode, so a literal "&nbsp;" survives the HTML parse and
    burns ~3 tokens every time it appears.
    """
    out = html.unescape(html.unescape(text))
    out = out.replace("\xa0", " ")
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


@dataclass
class Window:
    text: str
    start: int
    anchor: str | None
    stripped: bool


def _line_is_role(line: str) -> bool | None:
    """True if role-ish, False if company-ish, None if neutral/blank."""
    stripped = line.strip()
    if len(stripped) < 15:
        return None
    role = len(_ROLE_MARKERS.findall(stripped))
    company = len(_COMPANY_MARKERS.findall(stripped))
    if role > company:
        return True
    if company > role:
        return False
    return None


def role_content_share(text: str, window_start: int, chars: int) -> float:
    """Share of the window's characters on lines that read as role content.

    Uses the independent line scorer, not the stripper's boundary.
    """
    if not text:
        return 0.0
    window = text[window_start : window_start + chars]
    if not window:
        return 0.0
    role_chars = 0
    scored_chars = 0
    for line in window.splitlines():
        kind = _line_is_role(line)
        if kind is None:
            continue
        scored_chars += len(line)
        if kind:
            role_chars += len(line)
    if scored_chars == 0:
        return 0.0
    return role_chars / scored_chars


def find_role_anchor(text: str) -> tuple[int, str] | None:
    if not text:
        return None
    match = _ANCHOR_RE.search(text)
    if match:
        return match.start(), match.group(0).strip()
    match = _ANCHOR_INLINE_RE.search(text)
    if match:
        return match.start(), match.group(0).strip().lstrip("\n").strip()
    return None


def _first_role_block(text: str) -> int:
    """Index of the first block that reads as role content, else 0."""
    pos = 0
    for block in re.split(r"(\n\s*\n)", text):
        if not block.strip():
            pos += len(block)
            continue
        first_line = block.strip().splitlines()[0] if block.strip() else ""
        is_boiler_heading = bool(_BOILERPLATE_HEADING_RE.match(first_line))
        role_lines = sum(1 for ln in block.splitlines() if _line_is_role(ln) is True)
        company_lines = sum(1 for ln in block.splitlines() if _line_is_role(ln) is False)
        if not is_boiler_heading and role_lines > company_lines and role_lines > 0:
            return pos
        pos += len(block)
    return 0


def role_content_start(text: str) -> tuple[int, str | None]:
    anchor = find_role_anchor(text)
    if anchor:
        return anchor[0], anchor[1]
    return _first_role_block(text), None


def build_window(text: str | None, chars: int, strip: bool = True) -> Window:
    """The slice of the description handed to the model."""
    if not text:
        return Window(text="", start=0, anchor=None, stripped=False)
    text = normalize(text)
    if not strip:
        return Window(text=text[:chars], start=0, anchor=None, stripped=False)

    start, anchor = role_content_start(text)
    if start <= 0 or start >= len(text):
        return Window(text=text[:chars], start=0, anchor=None, stripped=False)
    return Window(
        text=text[start : start + chars], start=start, anchor=anchor, stripped=True
    )

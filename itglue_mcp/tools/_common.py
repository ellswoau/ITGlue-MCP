"""Shared helpers for the IT Glue tool implementations.

IT Glue is a JSON:API service: every resource is
``{"id": "..", "type": "..", "attributes": {...}, "relationships": {...}}``.
The helpers here flatten that envelope and parse document HTML (text + the
images embedded in it).
"""
from __future__ import annotations

import html as _html
import re
import urllib.parse
from typing import Any, Dict, List, Optional

# IT Glue returns resource ids as strings.
DocId = Any  # int or str


def attrs(item: Any) -> Dict[str, Any]:
    """Return a resource's ``attributes`` dict (or the item itself if flat)."""
    if isinstance(item, dict):
        a = item.get("attributes")
        if isinstance(a, dict):
            return a
        return item
    return {}


def rid(item: Any) -> Optional[str]:
    """Return a resource's id as a string."""
    if isinstance(item, dict) and item.get("id") is not None:
        return str(item["id"])
    return None


def _first(a: Dict[str, Any], *names, default=None):
    for n in names:
        if n in a and a[n] is not None:
            return a[n]
    return default


def summarize_organization(item: Any) -> Dict[str, Any]:
    a = attrs(item)
    return {
        "id": rid(item),
        "name": _first(a, "name"),
        "short_name": _first(a, "short-name"),
        "description": _first(a, "description"),
        "organization_type": _first(a, "organization-type-name"),
        "organization_status": _first(a, "organization-status-name"),
        "psa_integration": _first(a, "psa-integration"),
        "created_at": _first(a, "created-at"),
        "updated_at": _first(a, "updated-at"),
    }


def summarize_folder(item: Any) -> Dict[str, Any]:
    a = attrs(item)
    return {
        "id": rid(item),
        "name": _first(a, "name"),
        "organization_id": _first(a, "organization-id"),
        "parent_folder_id": _first(a, "document-folder-id", "parent-id"),
        "created_at": _first(a, "created-at"),
        "updated_at": _first(a, "updated-at"),
    }


def document_name(item: Any) -> str:
    return str(_first(attrs(item), "name", default="") or "")


def summarize_document(item: Any, config=None) -> Dict[str, Any]:
    """Compact document summary for search results / listings."""
    a = attrs(item)
    org_id = _first(a, "organization-id")
    doc_id = rid(item)
    out = {
        "id": doc_id,
        "name": document_name(item),
        "organization_id": org_id,
        "document_folder_id": _first(a, "document-folder-id"),
        "created_at": _first(a, "created-at"),
        "updated_at": _first(a, "updated-at"),
        "resource_url": _first(a, "resource-url"),
    }
    if config is not None:
        out["url"] = config.document_url(doc_id, org_id)
    return out


# ------------------------------------------------------------------ HTML parse
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_SRC_RE = re.compile(r"\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE | re.DOTALL)
_ALT_RE = re.compile(r"\balt\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE | re.DOTALL)
_WIDTH_RE = re.compile(r"\bwidth\s*=\s*[\"']?(\d+)", re.IGNORECASE)
_HEIGHT_RE = re.compile(r"\bheight\s*=\s*[\"']?(\d+)", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(
    r"<\s*(br|/p|/div|/tr|/li|/h[1-6]|hr)\s*/?\s*>", re.IGNORECASE)
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_MULTI_NL_RE = re.compile(r"\n{3,}")
# IT Glue inline-image references look like
# ``/6006071/docs/16101974/images/23512329`` (optionally ``.../developer/images/<id>``);
# the trailing integer is the document-image id used by GET /document_images/:id.
_IMG_ID_RE = re.compile(r"/images/(\d+)/?$")


def _absolutize(src: str, base_url: str) -> str:
    src = _html.unescape((src or "").strip())
    if not src:
        return ""
    if src.startswith("data:"):
        return src
    if src.startswith("//"):
        return "https:" + src
    if src.startswith(("http://", "https://")):
        return src
    if base_url:
        return urllib.parse.urljoin(base_url.rstrip("/") + "/", src.lstrip("/"))
    return src


def image_id_from_src(src: str):
    """Return the IT Glue document-image id embedded in an image ``src`` path.

    Handles both the relative inline path
    (``/6006071/docs/16101974/images/23512329``) and the
    ``.../developer/images/<id>`` form; returns None for ``data:`` URIs or
    anything without a trailing ``/images/<int>``."""
    if not src or src.startswith("data:"):
        return None
    m = _IMG_ID_RE.search(src.split("?", 1)[0])
    return int(m.group(1)) if m else None


def extract_images(content_html: Optional[str], base_url: str = "") -> List[Dict[str, Any]]:
    """Parse ``<img>`` tags out of a document's HTML content.

    Returns a list of ``{index, src, alt, width, height}`` with each ``src``
    resolved to an absolute URL (or left as a ``data:`` URI). Order is the
    document order.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, tag in enumerate(_IMG_TAG_RE.findall(content_html or "")):
        m = _SRC_RE.search(tag)
        if not m:
            continue
        src = _absolutize(m.group(1), base_url)
        if not src:
            continue
        if src in seen:
            continue
        seen.add(src)
        alt = _ALT_RE.search(tag)
        width = _WIDTH_RE.search(tag)
        height = _HEIGHT_RE.search(tag)
        out.append({
            "index": i,
            "src": src,
            "image_id": image_id_from_src(src),
            "alt": _html.unescape(alt.group(1)) if alt else None,
            "width": int(width.group(1)) if width else None,
            "height": int(height.group(1)) if height else None,
        })
    return out


def html_to_text(content_html: Optional[str], *, max_chars: Optional[int] = None) -> str:
    """Convert a document's HTML content to readable plain text.

    Preserves block/line structure for headings, paragraphs, list items and
    table rows so the text stays legible for the model.
    """
    if not content_html:
        return ""
    text = _SCRIPT_STYLE_RE.sub("", content_html)
    # Images -> a labelled placeholder so the model knows one exists.
    text = _IMG_TAG_RE.sub("[image]", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    # Normalise whitespace per line, drop leading/trailing blank lines.
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(lines).strip()
    text = _MULTI_NL_RE.sub("\n\n", text)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + f"\n... [truncated to {max_chars} chars]"
    return text


def document_content(item: Any) -> str:
    """Return a document resource's raw ``content`` field verbatim.

    IT Glue returns this as a *list of content blocks* (``Document::Text``,
    ``Document::Heading``, ``Document::Step``, ...), each carrying its own
    ``resource.content`` -- not a plain HTML string. Use :func:`document_html`
    to get renderable HTML."""
    a = attrs(item)
    content = _first(a, "content", "body", default="") or ""
    return content if isinstance(content, str) else str(content)


_HEADING_TAG = {1: "h1", 2: "h2", 3: "h3", 4: "h4", 5: "h5", 6: "h6"}


def _block_html(block: Any) -> str:
    """Render a single IT Glue document block to HTML.

    Text/Step blocks carry HTML in ``resource.content``; Heading blocks carry
    plain text plus a ``level``; Image blocks carry a URL."""
    if not isinstance(block, dict):
        return ""
    rt = str(block.get("resource_type") or "")
    res = block.get("resource")
    if not isinstance(res, dict):
        return ""
    content = res.get("content")
    if rt == "Document::Heading":
        try:
            level = int(res.get("level") or 2)
        except (TypeError, ValueError):
            level = 2
        tag = _HEADING_TAG.get(min(max(level, 1), 6), "h2")
        text = _html.escape(str(content).strip()) if content is not None else ""
        return f"<{tag}>{text}</{tag}>" if text else ""
    if "Image" in rt:
        url = res.get("url") or res.get("image_url") or res.get("image-url")
        if not url and isinstance(content, str) and ("/" in content or content.startswith("http")):
            url = content
        return f'<img src="{_html.escape(str(url))}">' if url else ""
    if isinstance(content, str) and content.strip():
        return content
    return ""


def document_html(item: Any) -> str:
    """Render a document resource to a single HTML string for text/image parsing.

    Handles the three shapes IT Glue returns:
      * ``content`` as a list of ``Document::*`` blocks (the usual case) -- blocks
        are ordered by ``sort`` and their HTML concatenated;
      * ``sections`` as a JSON:API list of ``document-sections`` resources (a
        fallback when ``content`` is empty);
      * ``content`` as a plain HTML string (older/simple documents).
    """
    a = attrs(item)
    content = a.get("content")
    blocks = None
    if isinstance(content, list) and content:
        blocks = content
    elif isinstance(a.get("sections"), list) and a["sections"]:
        blocks = a["sections"]
    elif isinstance(content, list):
        blocks = content  # empty list -> renders to ""
    if blocks is not None:
        ordered = sorted(
            (b for b in blocks if isinstance(b, dict)),
            key=lambda b: b.get("sort", 0) if isinstance(b.get("sort"), int) else 0,
        )
        parts = []
        for b in ordered:
            # ``sections`` entries nest their fields under ``attributes``.
            if "resource" not in b and isinstance(b.get("attributes"), dict):
                att = b["attributes"]
                b = {"resource_type": att.get("resource-type", ""), "resource": att}
            parts.append(_block_html(b))
        return "\n".join(p for p in parts if p)
    if isinstance(content, str):
        return content
    return ""


def normalize_org_id(value, default: int = 0) -> int:
    """Validate/coerce an organization id; fall back to the configured default."""
    if value in (None, "", 0, "0"):
        if default:
            return int(default)
        raise ValueError("organization_id is required (or set ITGLUE_DEFAULT_ORGANIZATION_ID).")
    try:
        oid = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("organization_id must be an integer, e.g. 6006071.")
    if oid <= 0:
        raise ValueError("organization_id must be a positive integer.")
    return oid


def normalize_document_id(value) -> int:
    """Validate a document id (accepts a bare number or an IT Glue doc URL)."""
    if value in (None, ""):
        raise ValueError("document_id is required.")
    s = str(value).strip()
    m = re.search(r"(\d+)/?$", s) if "/" in s else None
    raw = m.group(1) if m else s
    if not str(raw).isdigit():
        raise ValueError(
            "document_id must be a positive integer (or a document URL ending in one)."
        )
    did = int(raw)
    if did <= 0:
        raise ValueError("document_id must be a positive integer.")
    return did

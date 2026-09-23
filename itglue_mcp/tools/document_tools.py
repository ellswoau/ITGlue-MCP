"""Document operations for IT Glue: list folders/documents, search documents by
name (and optionally by content), read a document's full content, and load the
images embedded in a document so the agent can inspect them.

Key facts about the IT Glue documents API:
  * Listing is ``GET /organizations/{org_id}/relationships/documents`` and is
    paginated (``page[size]`` max 1000, ``page[number]``).
  * There is no reliable server-side document *name* filter, so a name search
    pages the whole organization listing once, caches it, and searches the JSON
    locally (see ``ITGlueClient.list_documents``).
  * A single document is ``GET /documents/{doc_id}``; its ``attributes.content``
    is an HTML string. Images embedded in that HTML are fetched separately (the
    document endpoint returns only the markup).
"""
from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING, List, Optional
from urllib.parse import urljoin

from mcp.types import ImageContent, TextContent

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import ITGlueConfig

from ..client import get_client
from ._common import (
    attrs,
    document_html,
    extract_images,
    html_to_text,
    normalize_document_id,
    normalize_org_id,
    rid,
    summarize_document,
    summarize_folder,
)

# Guardrails so a single call cannot pull unbounded bytes / images into context.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES_PER_CALL = 8
DEFAULT_DOC_TEXT_CHARS = 40000


def _doc_summary(item, config) -> dict:
    return summarize_document(item, config)


def _match(name: str, needle: str) -> bool:
    return needle in (name or "").lower()


def _decode_data_uri(src: str) -> tuple:
    """Decode a ``data:image/...;base64,....`` URI -> (bytes, mime)."""
    m = re.match(r"data:([^;,]+)?(;base64)?,(.*)$", src, re.IGNORECASE | re.DOTALL)
    if not m:
        raise ValueError("not a data: URI")
    mime = m.group(1) or "application/octet-stream"
    payload = m.group(3)
    if m.group(2):
        raw = base64.b64decode(payload)
    else:
        raw = base64.b64decode(payload)  # urllib-style; base64 is the norm here
    return raw, mime


def _load_image_bytes(client, img: dict) -> tuple:
    """Return (bytes, content_type, name) for one embedded image.

    Prefers the IT Glue document-image endpoint (``/document_images/{id}``),
    which yields a short-lived pre-signed S3 URL holding the real bytes -- the
    inline ``/org/docs/doc/images/<id>`` path in the content is *not* directly
    downloadable. Falls back to fetching the raw ``src`` (e.g. a ``data:`` URI
    or a direct URL)."""
    iid = img.get("image_id")
    if iid is not None:
        raw, ctype, name, _meta = client.download_document_image(iid)
        return raw, ctype, name
    src = img.get("src") or ""
    if src.startswith("data:"):
        raw, ctype = _decode_data_uri(src)
        return raw, ctype, "inline"
    raw, ctype, name = client.download_binary(src)
    return raw, ctype, name


def register(mcp: "FastMCP", config: "ITGlueConfig") -> None:
    @mcp.tool()
    def list_document_folders(organization_id: Optional[int] = None) -> dict:
        """List the document folders in an organization.

        Returns each folder's id/name/parent so a folder id can be passed to
        ``list_documents`` to scope a listing. Defaults to the configured
        organization when ``organization_id`` is omitted."""
        client = get_client(config)
        oid = normalize_org_id(organization_id, default=config.default_organization_id)
        folders, _meta = client.get_list(
            f"/organizations/{oid}/relationships/document_folders",
            page_size=1000,
            page=1,
        )
        return {
            "organization_id": oid,
            "returned": len(folders),
            "folders": [summarize_folder(f) for f in folders],
        }

    @mcp.tool()
    def list_documents(organization_id: Optional[int] = None,
                       folder_id: Optional[int] = None,
                       page_size: int = 1000,
                       refresh: bool = False) -> dict:
        """List documents in an organization (the whole organization by
        default).

        IT Glue returns the whole organization only when the folder filter is
        set to null, so this sends ``filter[document_folder_id]=null`` by
        default and pages up to ``page[size]=1000``. Returns each document's id,
        name, folder id and a clickable ``url``. The result is cached per
        organization for ``ITGLUE_CACHE_TTL`` seconds so repeated calls (and
        ``search_documents``) are cheap; pass ``refresh=True`` to force a fresh
        fetch.

        Pass an integer ``folder_id`` (from ``list_document_folders``) to scope
        the listing to one folder."""
        client = get_client(config)
        oid = normalize_org_id(organization_id, default=config.default_organization_id)
        docs, cached = client.list_documents(oid, folder_id=folder_id, refresh=refresh)
        return {
            "organization_id": oid,
            "returned": len(docs),
            "cached": cached,
            "documents": [_doc_summary(d, config) for d in docs[:page_size]],
        }

    @mcp.tool()
    def search_documents(query: str, organization_id: Optional[int] = None,
                         folder_id: Optional[int] = None,
                         limit: int = 25, refresh: bool = False) -> dict:
        """Search documents by name (case-insensitive substring) in one
        organization.

        Because the IT Glue API has no reliable document-name filter, this pages
        the whole organization listing once (``filter[document_folder_id]=null``
        so every folder is included), caches it, and matches locally. Returns
        the matching documents with their ids and clickable ``url``s -- use
        ``get_document`` on an id to read the full content.

        Defaults to the configured organization when ``organization_id`` is
        omitted. ``query`` matches the document name only; use
        ``search_document_contents`` to search inside the text."""
        client = get_client(config)
        oid = normalize_org_id(organization_id, default=config.default_organization_id)
        needle = (query or "").strip().lower()
        if not needle:
            raise ValueError("query must not be empty.")
        docs, cached = client.list_documents(oid, folder_id=folder_id, refresh=refresh)
        matches = [d for d in docs if _match(summarize_document(d).get("name"), needle)]
        matches.sort(key=lambda d: summarize_document(d).get("name") or "")
        return {
            "organization_id": oid,
            "query": query,
            "matched": len(matches),
            "scanned": len(docs),
            "cached": cached,
            "documents": [_doc_summary(d, config) for d in matches[:max(1, int(limit))]],
        }

    @mcp.tool()
    def search_document_contents(query: str, organization_id: Optional[int] = None,
                                 name_filter: Optional[str] = None,
                                 max_documents: int = 0,
                                 snippet_chars: int = 240) -> dict:
        """Search *inside* document text (case-insensitive) and return matching
        snippets with the document id and link.

        The whole-organization listing already carries each document's content,
        so this scans the cached listing locally -- no per-document fetches.
        ``max_documents`` caps how many are scanned (0 or negative = all). When
        ``name_filter`` is given only documents whose name contains it are
        scanned; otherwise the most-recently-updated are scanned first. Use
        ``search_documents`` (name search) to narrow down first."""
        client = get_client(config)
        oid = normalize_org_id(organization_id, default=config.default_organization_id)
        needle = (query or "").strip().lower()
        if not needle:
            raise ValueError("query must not be empty.")
        docs, _cached = client.list_documents(oid)
        if name_filter:
            nf = name_filter.strip().lower()
            candidates = [d for d in docs
                          if nf in (summarize_document(d).get("name") or "").lower()]
        else:
            candidates = sorted(
                docs,
                key=lambda d: (attrs(d).get("updated-at") or ""),
                reverse=True,
            )
        limit = int(max_documents)
        if limit and limit > 0:
            candidates = candidates[:limit]

        results = []
        for d in candidates:
            text = html_to_text(document_html(d))
            low = text.lower()
            idx = low.find(needle)
            if idx < 0:
                continue
            start = max(0, idx - snippet_chars // 3)
            snippet = text[start:start + snippet_chars].replace("\n", " ").strip()
            results.append({
                **_doc_summary(d, config),
                "match_snippet": snippet,
            })
        return {
            "organization_id": oid,
            "query": query,
            "scanned_documents": len(candidates),
            "matched": len(results),
            "documents": results,
        }

    @mcp.tool()
    def get_document(document_id, include_text: bool = True,
                     max_chars: int = DEFAULT_DOC_TEXT_CHARS,
                     refresh: bool = False) -> dict:
        """Read a single IT Glue document by id (e.g. 9623259, or a document URL).

        Returns the document's metadata, a clickable ``url`` for use in internal
        notes, the full raw HTML ``content``, a plain-text rendering
        (``content_text``) for decision-making, and the list of images embedded
        in the document (``images`` with their resolved URLs). Use
        ``get_document_images`` to actually pull those images into context.

        ``include_text=False`` returns metadata only (cheaper). ``max_chars``
        bounds the plain-text rendering."""
        client = get_client(config)
        did = normalize_document_id(document_id)
        item = client.get_document(did, refresh=refresh)
        if not item:
            return {"document_id": did, "found": False}
        content = document_html(item)
        org_id = attrs(item).get("organization-id")
        images = extract_images(content, base_url=config.resolved_base_url())
        out = {
            "found": True,
            **_doc_summary(item, config),
            "url": config.document_url(did, org_id),
            "content_html_length": len(content or ""),
            "image_count": len(images),
            "images": [{"index": i["index"], "image_id": i.get("image_id"),
                        "src": i["src"], "alt": i.get("alt"),
                        "width": i.get("width"), "height": i.get("height")}
                       for i in images],
        }
        if include_text:
            out["content_html"] = content
            out["content_text"] = html_to_text(content, max_chars=max_chars)
        return out

    @mcp.tool()
    def list_document_images(document_id, include_metadata: bool = True) -> dict:
        """List the images embedded in a document, with their ids and URLs.

        Each image gets an ``image_id`` (the IT Glue document-image id) and, when
        ``include_metadata=True``, its filename and size resolved via
        ``GET /document_images/{id}``. Image URLs are deliberately **not**
        returned -- their signatures are redacted and cannot be fetched. Use
        ``get_document_images`` to load the images themselves into context."""
        client = get_client(config)
        did = normalize_document_id(document_id)
        item = client.get_document(did)
        if not item:
            return {"document_id": did, "found": False}
        images = extract_images(document_html(item), base_url=config.resolved_base_url())
        out = []
        for i in images:
            entry = {
                "index": i["index"],
                "image_id": i.get("image_id"),
                "src": i["src"],
                "alt": i.get("alt"),
                "width": i.get("width"),
                "height": i.get("height"),
            }
            if include_metadata and i.get("image_id") is not None:
                try:
                    a = (client.get_document_image(i["image_id"]) or {}).get("attributes") or {}
                    entry["name"] = a.get("name")
                    entry["size"] = a.get("size")
                    # Deliberately do NOT echo the pre-signed S3 URLs
                    # (original/slim/thumbnail). Their credential and signature
                    # parts are redacted by the MCP transport, so a caller that
                    # tries to fetch one gets S3 ``400 InvalidToken``. The only
                    # route to the bytes is ``get_document_images``.
                except Exception as exc:  # noqa: BLE001 - report, keep listing
                    entry["error"] = str(exc)[:200]
            out.append(entry)
        return {
            "found": True,
            "document_id": str(did),
            "name": summarize_document(item).get("name"),
            "count": len(out),
            "images": out,
            "hint": (
                "Call get_document_images(document_id) to load the image bytes. "
                "Image URLs are intentionally not returned: their signatures are "
                "redacted and cannot be fetched directly."
            ),
        }

    @mcp.tool()
    def get_document_images(document_id, max_images: int = 4,
                            min_width: int = 0, min_height: int = 0):
        """Download the images embedded in a document and return them *as
        images* so a vision-capable model can inspect them (screenshots, network
        diagrams, photos of equipment, error dialogs).

        Discover the images with ``get_document`` or ``list_document_images``
        first. ``max_images`` caps how many are returned (never more than 8);
        each image is capped at 8 MB. Use ``min_width``/``min_height`` to skip
        small icons/logos."""
        client = get_client(config)
        did = normalize_document_id(document_id)
        item = client.get_document(did)
        if not item:
            return [TextContent(type="text", text=f"Document {did} was not found.")]
        images = extract_images(document_html(item), base_url=config.resolved_base_url())
        cap = max(1, min(int(max_images), MAX_IMAGES_PER_CALL))

        out: List[object] = []
        loaded = 0
        for img in images:
            if loaded >= cap:
                break
            try:
                raw, ctype, name = _load_image_bytes(client, img)
            except Exception as exc:  # noqa: BLE001 - report, keep going
                out.append(TextContent(type="text", text=(
                    f"image {img['index']} (id={img.get('image_id')}, {img['src']}) "
                    f"could not be downloaded: {exc}")))
                continue
            ctype = (ctype or "application/octet-stream").lower()
            if not ctype.startswith("image/"):
                out.append(TextContent(type="text", text=(
                    f"image {img['index']} is {ctype} ({len(raw)} bytes), not an "
                    "image -- not rendered.")))
                continue
            if len(raw) > MAX_IMAGE_BYTES:
                out.append(TextContent(type="text", text=(
                    f"image {img['index']} is {len(raw)} bytes (> {MAX_IMAGE_BYTES}); "
                    "too large to inline.")))
                continue
            out.append(TextContent(type="text", text=(
                f"Image {img['index']} from document {did}"
                f"{(' (' + str(name) + ')') if name else ''}"
                f"{(' (alt: ' + str(img['alt']) + ')') if img.get('alt') else ''}"
                f" [{ctype}, {len(raw)} bytes] (image id {img.get('image_id')}):")))
            out.append(ImageContent(
                type="image",
                data=base64.b64encode(raw).decode("ascii"),
                mime_type=ctype,
            ))
            loaded += 1
        if not out:
            return [TextContent(type="text", text=(
                f"No images found in document {did} (considered {len(images)})."))]
        return out

    @mcp.tool()
    def get_related_items(document_id, page_size: int = 100) -> dict:
        """List the items related to a document -- related configurations,
        passwords, contacts, other documents, and so on.

        Documents in IT Glue are linked to other resources; this surfaces those
        links so the agent can pull in the related record (e.g. the server or
        credential a runbook refers to). Uses
            "GET /documents/{id}/relationships/related_items``. Returns each
        related item's id, name and resource type."""
        client = get_client(config)
        did = normalize_document_id(document_id)
        items, meta = client.get_list(
            f"/documents/{did}/relationships/related_items",
            page_size=page_size,
            page=1,
        )
        related = []
        for it in items:
            if not isinstance(it, dict):
                continue
            a = attrs(it)
            related.append({
                "id": rid(it),
                "type": it.get("type"),
                "resource_type": a.get("resource-type") or a.get("resource_type"),
                "name": a.get("name"),
                "organization_id": a.get("organization-id") or a.get("organization_id"),
            })
        return {
            "document_id": did,
            "returned": len(related),
            "total_count": meta.get("total-count"),
            "related_items": related,
        }

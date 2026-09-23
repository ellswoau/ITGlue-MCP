"""Thin REST client for the IT Glue API (JSON:API).

Conforms to the JSON:API spec (https://jsonapi.org):
  * Request headers: ``x-api-key: <key>`` and
    ``content-type: application/vnd.api+json`` (the content-type is only
    required when there is a payload; we send ``accept`` as well).
  * List responses wrap items in ``{"data": [ ... ], "meta": {...},
    "links": {...}}``; single-resource responses in ``{"data": {...}}``.
  * Each resource is ``{"id": "..", "type": "..", "attributes": {...},
    "relationships": {...}}`` -- the useful fields live under ``attributes``.
  * Pagination uses ``page[size]`` (default 50, max 1000) and
    ``page[number]``; ``meta`` carries ``total-pages`` / ``total-count``.
  * Throttling: 3000 requests / 5 minutes -> HTTP 429 on excess.

The API key is never logged. A per-organization in-memory cache holds the
document listing so name searches can be done locally (the API has no reliable
document-name filter).

Base URL: https://api.itglue.com (EU: https://api.eu.itglue.com,
Australia: https://api.au.itglue.com).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import requests

from .config import ITGlueConfig

# JSON:API media type.
JSONAPI_CONTENT_TYPE = "application/vnd.api+json"


class ITGlueError(Exception):
    """Raised for IT Glue API errors, carrying status + parsed detail."""

    def __init__(self, status: Optional[int], message: str, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - helper
        return f"ITGlueError(status={self.status}, message={self.message!r})"


class _DocCache:
    """TTL cache for an organization's document listing, plus its folders."""

    def __init__(self, ttl: int = 900):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._docs: Dict[int, tuple] = {}      # org_id -> (fetched_at, [docs])
        self._folders: Dict[int, tuple] = {}   # org_id -> (fetched_at, [folders])
        self._detail: Dict[int, tuple] = {}    # doc_id -> (fetched_at, doc)

    def get_docs(self, org_id, *, refresh: bool = False):
        with self._lock:
            entry = self._docs.get(org_id)
            if entry and not refresh and (time.time() - entry[0]) < self.ttl:
                return entry[1], False
        return None, True  # (value, needs_fetch)

    def put_docs(self, org_id, docs):
        with self._lock:
            self._docs[org_id] = (time.time(), docs)

    def get_folders(self, org_id, *, refresh: bool = False):
        with self._lock:
            entry = self._folders.get(org_id)
            if entry and not refresh and (time.time() - entry[0]) < self.ttl:
                return entry[1], False
        return None, True

    def put_folders(self, org_id, folders):
        with self._lock:
            self._folders[org_id] = (time.time(), folders)

    def get_detail(self, doc_id, *, refresh: bool = False):
        with self._lock:
            entry = self._detail.get(doc_id)
            if entry and not refresh and (time.time() - entry[0]) < self.ttl:
                return entry[1], False
        return None, True

    def put_detail(self, doc_id, doc):
        with self._lock:
            self._detail[doc_id] = (time.time(), doc)

    def invalidate(self, org_id: Optional[int] = None) -> None:
        with self._lock:
            if org_id is None:
                self._docs.clear()
                self._folders.clear()
                self._detail.clear()
            else:
                self._docs.pop(org_id, None)
                self._folders.pop(org_id, None)


class ITGlueClient:
    """Stateful client bound to one IT Glue account config."""

    DEFAULT_PAGE_SIZE = 1000
    MAX_PAGE_SIZE = 1000       # API maximum page size / total results.
    MAX_RETRIES = 3
    RATE_LIMIT_MIN_BACKOFF = 1.0
    RATE_LIMIT_MAX_BACKOFF = 20.0

    def __init__(self, config: ITGlueConfig):
        self.config = config
        self.base_url = config.resolved_base_url()
        self.verify_ssl = config.verify_ssl
        self.timeout = config.timeout
        self.cache = _DocCache(ttl=config.cache_ttl)
        self._session = requests.Session()
        self._session.headers.update({
            "x-api-key": config.api_key,
            "content-type": JSONAPI_CONTENT_TYPE,
            "accept": JSONAPI_CONTENT_TYPE,
            "cache-control": "no-cache",
        })

    # ------------------------------------------------------------ request core
    @staticmethod
    def _error_detail(resp: requests.Response) -> Any:
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError):
            return resp.text[:500]

    def _raise_for(self, resp: requests.Response, url: str) -> None:
        if resp.status_code < 200 or resp.status_code >= 300:
            detail = self._error_detail(resp)
            msg = (
                f"IT Glue API {resp.status_code} for {url}: "
                f"{detail if not isinstance(detail, dict) else json.dumps(detail)}"
            )
            raise ITGlueError(resp.status_code, msg, detail)

    def _request(self, method: str, url: str, *, params=None, json_body=None) -> requests.Response:
        resp = None
        attempt = 0
        while True:
            attempt += 1
            # ``params`` may contain bracket keys (page[size]); requests encodes
            # those correctly. Pass a list of tuples via ``_encode_params`` when
            # callers want deterministic ordering.
            resp = self._session.request(
                method, url, params=params, json=json_body,
                timeout=self.timeout, verify=self.verify_ssl,
            )
            if resp.status_code != 429:
                break
            if attempt >= self.MAX_RETRIES:
                break
            wait = self.RATE_LIMIT_MIN_BACKOFF * (2 ** (attempt - 1))
            if "Retry-After" in resp.headers:
                try:
                    wait = max(wait, float(resp.headers["Retry-After"]))
                except ValueError:
                    pass
            wait = min(wait, self.RATE_LIMIT_MAX_BACKOFF)
            time.sleep(wait)
        if resp.status_code == 429:
            raise ITGlueError(
                429,
                f"IT Glue rate limit exceeded for {url} (retried {self.MAX_RETRIES} "
                "times). The limit is 3000 requests / 5 minutes; wait a moment and "
                "retry, or lower the page size.",
            )
        self._raise_for(resp, url)
        return resp

    def get_json(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self._url(path)
        resp = self._request("GET", url, params=params)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    # ------------------------------------------------------- JSON:API envelope
    @staticmethod
    def unwrap_list(data: Any) -> List[dict]:
        """Return the ``data`` list from a JSON:API collection response."""
        if isinstance(data, dict):
            d = data.get("data")
            if isinstance(d, list):
                return d
            if isinstance(d, dict):
                return [d]
            return []
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def unwrap_one(data: Any) -> dict:
        """Return the single resource object from ``{"data": {...}}``."""
        if isinstance(data, dict):
            d = data.get("data")
            if isinstance(d, dict):
                return d
            if isinstance(d, list) and d:
                return d[0]
            if "attributes" in data or "id" in data:
                return data
        if isinstance(data, list) and data:
            return data[0]
        return {}

    @staticmethod
    def meta(data: Any) -> dict:
        if isinstance(data, dict) and isinstance(data.get("meta"), dict):
            return data["meta"]
        return {}

    # ------------------------------------------------------------- pagination
    @staticmethod
    def _page_params(page_size: int, page: int) -> Dict[str, Any]:
        return {f"page[{k}]": v for k, v in {"size": page_size, "number": page}.items()}

    def get_list(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        page_size: int = 50,
        page: int = 1,
        max_pages: Optional[int] = None,
    ) -> tuple:
        """Fetch items from a paginated JSON:API collection endpoint.

        Returns ``(items, meta)`` where ``meta`` is the last page's meta block
        (carrying ``total-pages`` / ``total-count`` when present).

        By default this returns *one* page (``max_pages=None``). Pass a
        ``max_pages`` value > 1 to walk forward. Because a helpdesk account can
        hold thousands of documents, the caller decides how far to walk.
        """
        page_size = max(1, min(int(page_size), self.MAX_PAGE_SIZE))
        collected: List[dict] = []
        cur = max(1, int(page))
        last_meta: dict = {}
        while True:
            p = dict(params or {})
            p.update(self._page_params(page_size, cur))
            data = self.get_json(path, params=p)
            items = self.unwrap_list(data)
            last_meta = self.meta(data)
            if not items:
                break
            collected.extend(items)
            if max_pages is None:
                break
            if cur >= (int(page) + max_pages - 1):
                break
            if len(items) < page_size:
                break
            cur += 1
        return collected, last_meta

    def get_all(self, path: str, *, params=None, page_size: int = 1000,
                max_pages: int = 50) -> List[dict]:
        """Walk pages until exhausted (bounded by ``max_pages``) and return all
        items. Used to build the local document cache."""
        items, _ = self.get_list(
            path, params=params, page_size=page_size, page=1, max_pages=max_pages
        )
        return items

    def get_one_item(self, path: str, *, params=None) -> dict:
        data = self.get_json(path, params=params)
        return self.unwrap_one(data)

    # ----------------------------------------------------- document listing (cached)
    def list_documents(self, org_id, *, folder_id=None, refresh: bool = False):
        """Return (documents, cached) for an organization.

        IT Glue only returns the *whole* organization's documents when the
        folder filter is explicitly set to null
        (``filter[document_folder_id]=null``); with no filter it returns just a
        small root subset. So the whole-org listing always sends that filter and
        is cached per organization. Pass an integer ``folder_id`` to scope the
        listing to a single folder instead.
        """
        org_id = int(org_id)
        if folder_id is None:
            cached, needs = self.cache.get_docs(org_id, refresh=refresh)
            if not needs:
                return cached, True
            docs = self.get_all(
                f"/organizations/{org_id}/relationships/documents",
                params={"sort": "name", "filter[document_folder_id]": "null"},
            )
            docs = [d for d in docs if isinstance(d, dict)]
            self.cache.put_docs(org_id, docs)
            return docs, False
        # Folder-scoped request: not cached (small, caller-driven).
        docs = self.get_all(
            f"/organizations/{org_id}/relationships/documents",
            params={"sort": "name", "filter[document_folder_id]": str(folder_id)},
        )
        return [d for d in docs if isinstance(d, dict)], False

    def get_document(self, doc_id, *, refresh: bool = False) -> dict:
        doc_id = int(doc_id)
        cached, needs = self.cache.get_detail(doc_id, refresh=refresh)
        if not needs:
            return cached
        item = self.get_one_item(f"/documents/{doc_id}")
        if item:
            self.cache.put_detail(doc_id, item)
        return item

    # ---------------------------------------------------------- document images
    # IT Glue inline images are NOT downloadable from the document content path.
    # ``GET /document_images/:id`` returns their attributes, including
    # ``original-src`` / ``slim-src`` / ``thumbnail-src`` -- short-lived
    # pre-signed S3 URLs holding the real bytes.
    MIME_BY_EXT = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".svg": "image/svg+xml", ".tif": "image/tiff", ".tiff": "image/tiff",
    }

    def get_document_image(self, image_id) -> dict:
        """Return the attributes of one document image (metadata + S3 URLs)."""
        return self.get_one_item(f"/document_images/{int(image_id)}")

    def download_document_image(self, image_id, *, preference: str = "original") -> tuple:
        """Fetch the bytes of a document image by its image id.

        Returns ``(content_bytes, content_type, filename, attributes)``. The
        image's ``<preference>-src`` (default ``original``) is a pre-signed S3
        URL, fetched without the API key.
        """
        item = self.get_document_image(image_id)
        a = (item.get("attributes") if isinstance(item, dict) else None) or {}
        if not a:
            raise ITGlueError(None, f"document image {image_id} was not found")
        order = [f"{preference}-src", "original-src", "slim-src", "thumbnail-src"]
        url = next((a.get(k) for k in order if a.get(k)), None)
        if not url:
            raise ITGlueError(None, f"document image {image_id} has no download URL")
        raw, ctype, name = self.download_binary(url)
        name = a.get("name") or name
        ctype = (ctype or "").lower()
        if not ctype.startswith("image/"):
            guess = self.MIME_BY_EXT.get(os.path.splitext(str(name))[1].lower())
            if guess:
                ctype = guess
        return raw, ctype, name, a

    # ------------------------------------------------------------ attachments
    def download_binary(self, url: str, *, use_auth: bool = True) -> tuple:
        """Download binary content (typically an image embedded in a document).

        Sends the API key only for IT Glue hosts; pre-signed/CDN URLs are
        fetched without it so the signature is not disturbed. Returns
        ``(content_bytes, content_type, filename)``.
        """
        absolute = url if url.startswith(("http://", "https://")) else self._url(url)
        signed = ("token=" in absolute) or ("x-amz-signature" in absolute.lower()) \
            or ("signature=" in absolute.lower())
        # Send the API key only to IT Glue hosts and only for unsigned URLs, so a
        # pre-signed/CDN link's signature is not disturbed.
        if use_auth and self._is_itglue_host(absolute) and not signed:
            resp = self._session.get(absolute, timeout=self.timeout,
                                     verify=self.verify_ssl, allow_redirects=True)
        else:
            resp = requests.get(absolute, timeout=self.timeout,
                                verify=self.verify_ssl, allow_redirects=True)
        self._raise_for(resp, absolute)
        ctype = (resp.headers.get("Content-Type") or "application/octet-stream")
        ctype = ctype.split(";")[0].strip()
        return resp.content, ctype, self._filename_from(resp, absolute)

    @staticmethod
    def _is_itglue_host(url: str) -> bool:
        host = urllib.parse.urlparse(url).hostname or ""
        return host.endswith("itglue.com")

    @staticmethod
    def _filename_from(resp: requests.Response, url: str) -> str:
        cd = resp.headers.get("Content-Disposition") or ""
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
        if m:
            return m.group(1).strip()
        path = urllib.parse.urlparse(url).path
        return os.path.basename(path) or "image"

    def test_connection(self) -> Dict[str, Any]:
        """Authenticate and return basic connectivity info (no sensitive data).

        Genuinely probes the API: ``GET /organizations`` returns 200 only with a
        valid key (an invalid key yields HTTP 403 per the IT Glue docs).
        """
        try:
            data = self.get_json("/organizations", params={"page[size]": 1})
            orgs = self.unwrap_list(data)
            meta = self.meta(data)
        except ITGlueError as exc:
            return {"connected": False, "base_url": self.base_url, "error": exc.message}
        first = orgs[0] if orgs else {}
        attrs = first.get("attributes") if isinstance(first, dict) else {}
        return {
            "connected": True,
            "base_url": self.base_url,
            "organization_count": meta.get("total-count"),
            "sample_organization": (attrs or {}).get("name"),
            "default_organization_id": self.config.default_organization_id or None,
        }


# Registry so tools can share one client per config (keyed by base URL + key id).
_client_registry: Dict[str, ITGlueClient] = {}
_client_registry_lock = threading.Lock()


def _client_key(config: ITGlueConfig) -> str:
    # Secret-free key: base URL + a short hash of the key so a rotated key gets
    # a fresh client without exposing the key in the key string.
    import hashlib
    h = hashlib.sha256((config.api_key or "").encode()).hexdigest()[:8]
    return f"{config.resolved_base_url()}#{h}"


def get_client(config: ITGlueConfig) -> ITGlueClient:
    key = _client_key(config)
    with _client_registry_lock:
        client = _client_registry.get(key)
        if client is None or client.config != config:
            client = ITGlueClient(config)
            _client_registry[key] = client
        return client


def clear_client(config: Optional[ITGlueConfig] = None) -> None:
    """Drop a cached client (used on shutdown)."""
    with _client_registry_lock:
        if config is None:
            _client_registry.clear()
        else:
            _client_registry.pop(_client_key(config), None)

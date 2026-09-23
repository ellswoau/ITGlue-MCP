"""Configuration and secure credential handling for the IT Glue MCP server.

Credentials can be supplied from (in order of precedence):
  1. Explicit keyword arguments (e.g. when called programmatically)
  2. Environment variables (ITGLUE_*)
  3. A JSON config file (ITGLUE_CONFIG_FILE, or --config)

The API key is never logged and config files are written with 0600 permissions
when created via the ``itglue config init`` wizard.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

# Environment variable names
ENV_API_KEY = "ITGLUE_API_KEY"
ENV_BASE_URL = "ITGLUE_BASE_URL"
ENV_VERIFY_SSL = "ITGLUE_VERIFY_SSL"
ENV_TIMEOUT = "ITGLUE_TIMEOUT"
ENV_CONFIG_FILE = "ITGLUE_CONFIG_FILE"
# Optional API key that gates the network MCP endpoints when set. This is the
# key an MCP client presents (Authorization: Bearer <key>) to the daemon, kept
# separate from the IT Glue account API key above.
ENV_MCP_TOKEN = "ITGLUE_MCP_AUTH_TOKEN"
# Defaults that make the documentation tools usable without first looking up an
# organization id (e.g. the Wellers account, org id 6006071).
ENV_DEFAULT_ORG_ID = "ITGLUE_DEFAULT_ORGANIZATION_ID"
# Subdomain used to build human-clickable documentation links (internal notes).
ENV_SUBDOMAIN = "ITGLUE_SUBDOMAIN"
# Template for a document's UI link; placeholders {subdomain} {org_id} {doc_id}.
ENV_DOC_URL_TEMPLATE = "ITGLUE_DOC_URL_TEMPLATE"
# How long a fetched document/org listing is cached (seconds) for local search.
ENV_CACHE_TTL = "ITGLUE_CACHE_TTL"

DEFAULT_BASE_URL = "https://api.itglue.com"
DEFAULT_SUBDOMAIN = "weller"
DEFAULT_DOC_URL_TEMPLATE = "https://{subdomain}.itglue.com/{org_id}/documents/{doc_id}"
DEFAULT_CACHE_TTL = 900

_PASSWORD_TAG = "***REDACTED***"


@dataclass
class ITGlueConfig:
    """Resolved configuration for a single IT Glue account."""

    # IT Glue API key, sent in the ``x-api-key`` header.
    api_key: str = ""
    # API base URL (region-specific for EU/AU accounts).
    base_url: str = DEFAULT_BASE_URL
    verify_ssl: bool = True
    # Connection / request timeout in seconds.
    timeout: int = 30
    # Optional API key that gates the HTTP/SSE MCP transport.
    mcp_auth_token: str = ""
    # Organization used when a tool call omits one.
    default_organization_id: int = 0
    # Account subdomain + URL template for clickable document links.
    subdomain: str = DEFAULT_SUBDOMAIN
    doc_url_template: str = DEFAULT_DOC_URL_TEMPLATE
    # Cache lifetime for listings used by local document search.
    cache_ttl: int = DEFAULT_CACHE_TTL

    def resolved_base_url(self) -> str:
        return (self.base_url or DEFAULT_BASE_URL).rstrip("/")

    def document_url(self, document_id, organization_id=None) -> Optional[str]:
        """Build the human-clickable IT Glue UI link for a document.

        Falls back to a link without the org segment when the org id is unknown
        (IT Glue redirects it to the right organization).
        """
        if document_id in (None, ""):
            return None
        org = organization_id or self.default_organization_id or ""
        sub = (self.subdomain or DEFAULT_SUBDOMAIN).strip()
        tpl = (self.doc_url_template or DEFAULT_DOC_URL_TEMPLATE).strip()
        try:
            return tpl.format(subdomain=sub, org_id=org, doc_id=document_id)
        except (KeyError, IndexError):
            return f"https://{sub}.itglue.com/{org}/documents/{document_id}"

    def redacted(self) -> dict:
        """Return a dict safe for logging (api key/token redacted)."""
        d = asdict(self)
        d["api_key"] = _PASSWORD_TAG if d.get("api_key") else ""
        d["mcp_auth_token"] = _PASSWORD_TAG if d.get("mcp_auth_token") else ""
        d["base_url"] = self.resolved_base_url()
        return d

    def is_complete(self) -> bool:
        return bool(self.resolved_base_url() and self.api_key)


class ConfigError(Exception):
    """Raised when configuration/credentials are missing or invalid."""


def _as_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


_SCALAR_KEYS = (
    "api_key", "base_url", "verify_ssl", "timeout", "mcp_auth_token",
    "default_organization_id", "subdomain", "doc_url_template", "cache_ttl",
)


def load_config(
    config_file: Optional[str] = None,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    verify_ssl: Optional[bool] = None,
    timeout: Optional[int] = None,
    mcp_auth_token: Optional[str] = None,
    default_organization_id: Optional[int] = None,
    subdomain: Optional[str] = None,
    doc_url_template: Optional[str] = None,
    cache_ttl: Optional[int] = None,
) -> ITGlueConfig:
    """Load and merge configuration from kwargs, env and a config file.

    Raises :class:`ConfigError` if essential credentials are missing.
    """
    cfg = ITGlueConfig()

    # 1. Load from well-known config file (env or explicit path).
    config_file = config_file or os.environ.get(ENV_CONFIG_FILE)
    if config_file and Path(config_file).exists():
        data = json.loads(Path(config_file).read_text(encoding="utf-8"))
        for key in _SCALAR_KEYS:
            if key in data and data[key] is not None:
                setattr(cfg, key, data[key])

    # 2. Env variables override the file.
    if os.environ.get(ENV_API_KEY):
        cfg.api_key = os.environ[ENV_API_KEY].strip()
    if os.environ.get(ENV_BASE_URL):
        cfg.base_url = os.environ[ENV_BASE_URL].strip()
    if os.environ.get(ENV_VERIFY_SSL) is not None:
        cfg.verify_ssl = _as_bool(os.environ[ENV_VERIFY_SSL], True)
    if os.environ.get(ENV_TIMEOUT):
        try:
            cfg.timeout = int(os.environ[ENV_TIMEOUT])
        except ValueError:
            pass
    if os.environ.get(ENV_MCP_TOKEN):
        cfg.mcp_auth_token = os.environ[ENV_MCP_TOKEN].strip()
    if os.environ.get(ENV_DEFAULT_ORG_ID):
        try:
            cfg.default_organization_id = int(os.environ[ENV_DEFAULT_ORG_ID])
        except ValueError:
            pass
    if os.environ.get(ENV_SUBDOMAIN):
        cfg.subdomain = os.environ[ENV_SUBDOMAIN].strip()
    if os.environ.get(ENV_DOC_URL_TEMPLATE):
        cfg.doc_url_template = os.environ[ENV_DOC_URL_TEMPLATE].strip()
    if os.environ.get(ENV_CACHE_TTL):
        try:
            cfg.cache_ttl = int(os.environ[ENV_CACHE_TTL])
        except ValueError:
            pass

    # 3. Explicit arguments win.
    if api_key is not None:
        cfg.api_key = api_key.strip()
    if base_url is not None:
        cfg.base_url = base_url.strip()
    if verify_ssl is not None:
        cfg.verify_ssl = bool(verify_ssl)
    if timeout is not None:
        cfg.timeout = int(timeout)
    if mcp_auth_token is not None:
        cfg.mcp_auth_token = mcp_auth_token.strip()
    if default_organization_id is not None:
        cfg.default_organization_id = int(default_organization_id)
    if subdomain is not None:
        cfg.subdomain = subdomain.strip()
    if doc_url_template is not None:
        cfg.doc_url_template = doc_url_template.strip()
    if cache_ttl is not None:
        cfg.cache_ttl = int(cache_ttl)

    if not cfg.is_complete():
        missing = []
        if not cfg.resolved_base_url():
            missing.append("base_url")
        if not cfg.api_key:
            missing.append("api_key")
        raise ConfigError(
            "Incomplete IT Glue credentials. Missing: "
            + ", ".join(missing)
            + ". Set ITGLUE_* env vars or run `python -m itglue_mcp "
              "config init --config <file>`."
        )
    return cfg


def configure_interactive(config_file: str) -> str:
    """Prompt securely for credentials and write a 0600 config file.

    The API key is requested with ``getpass`` so it is never echoed to the
    terminal, and the resulting file is only readable by the owner.
    """
    import getpass

    data = {}
    p = Path(config_file).expanduser()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}

    print("IT Glue MCP\n-----------")
    data["api_key"] = getpass.getpass("IT Glue API key: ") or data.get("api_key", "")
    data["base_url"] = input(
        f"API base URL [{data.get('base_url', DEFAULT_BASE_URL)}]: "
    ).strip() or data.get("base_url", DEFAULT_BASE_URL)
    data["subdomain"] = input(
        f"IT Glue subdomain (for document links) [{data.get('subdomain', DEFAULT_SUBDOMAIN)}]: "
    ).strip() or data.get("subdomain", DEFAULT_SUBDOMAIN)
    data["verify_ssl"] = data.get("verify_ssl", True)

    if not data.get("api_key"):
        raise ConfigError("api_key is required.")

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(p, 0o600)
    return str(p)

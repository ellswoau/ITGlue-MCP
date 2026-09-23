"""Organization (and organization lookup) operations for IT Glue.

Every document lives under an organization, so these tools resolve the
organization id used by the document tools.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from ..config import ITGlueConfig

from ..client import get_client
from ._common import normalize_org_id, summarize_organization


def register(mcp: "FastMCP", config: "ITGlueConfig") -> None:
    @mcp.tool()
    def list_organizations(page_size: int = 1000, page: int = 1,
                           name: Optional[str] = None) -> dict:
        """List the IT Glue organizations (customers) in the account.

        Returns each organization's id, name, short name, type and status.
        ``page_size`` defaults to 1000 (the API maximum); the API's default is
        only 50, so this raises it to return the whole roster in one call when
        the account has <= 1000 organizations. Pass ``name`` to filter locally
        by a case-insensitive substring of the organization name.

        Tip: the Wellers organization is id 6006071.
        """
        client = get_client(config)
        items, meta = client.get_list(
            "/organizations",
            params={"sort": "-id"},
            page_size=page_size,
            page=page,
        )
        orgs = [summarize_organization(o) for o in items]
        needle = (name or "").strip().lower()
        if needle:
            orgs = [o for o in orgs if needle in (o.get("name") or "").lower()
                    or needle in (o.get("short_name") or "").lower()]
        return {
            "page": page,
            "returned": len(orgs),
            "total_count": meta.get("total-count"),
            "total_pages": meta.get("total-pages"),
            "organizations": orgs,
        }

    @mcp.tool()
    def get_organization(organization_id: int) -> dict:
        """Fetch a single IT Glue organization by id (e.g. 6006071 for Wellers)."""
        client = get_client(config)
        oid = normalize_org_id(organization_id, default=config.default_organization_id)
        item = client.get_one_item(f"/organizations/{oid}")
        if not item:
            return {"organization_id": oid, "found": False}
        return {"found": True, "organization": summarize_organization(item)}

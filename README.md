# itglue-mcp

An MCP server (FastMCP) that exposes **IT Glue internal documentation** to a
helpdesk agent: discover organizations, search documents by name (and inside
their text), read a document's full content, and pull the images embedded in a
document into the agent's context so screenshots, network diagrams and photos
can be inspected.

It follows the house pattern used by `freshservice-mcp`, `activedirectory-mcp`,
`teamdirectory-mcp`, `paloalto-branches-mcp` and `desktopcentral-mcp`:
env-var/config credentials, one tool per API operation, `/health` +
bearer-token gating for the network transport, Docker image, and offline sanity
tests. The tool surface is deliberately **documents-only** (no generic API
passthrough).

## Tools

| Tool | What it does |
| --- | --- |
| `list_organizations` | List/ filter IT Glue organizations (customers). |
| `get_organization` | Fetch one organization by id (e.g. `6006071`). |
| `list_document_folders` | List an organization's document folders. |
| `list_documents` | List documents in an organization (whole org or one folder), cached. |
| `search_documents` | Search documents **by name** (local, over the cached listing). |
| `search_document_contents` | Search **inside** document text; returns snippets (bounded). |
| `get_document` | Full document: metadata, clickable `url`, raw HTML, plain text, image list. |
| `list_document_images` | Metadata for the images embedded in a document (id, name, size). URLs are intentionally omitted. |
| `get_document_images` | Download embedded images and return them **as images**. |
| `get_related_items` | Items related to a document (configs, passwords, other docs). |
| `itglue_config` | Redacted view of the connected environment. |

### Why a local name search?

The IT Glue API has no reliable server-side filter on document *name*. So
`search_documents` pages the organization's whole document listing once
(`GET /organizations/{org_id}/relationships/documents?filter[document_folder_id]=null`,
up to `page[size]=1000`) and matches names locally.

**The `filter[document_folder_id]=null` filter is essential**: without it the
API returns only a small root subset (verified live: 13 documents vs **576** for
the same organization). The whole-org listing also carries each document's
`content`, so `search_document_contents` scans all documents locally with no
per-document fetches. The listing is cached in memory for `ITGLUE_CACHE_TTL`
seconds; pass `refresh=True` to bypass it.

### Document links vs. content vs. images

`get_document` returns both a human-clickable `url`
(`https://<subdomain>.itglue.com/<org_id>/documents/<doc_id>`, for internal
notes) and the full content (`content_html` + a plain-text `content_text` for
decision-making). Note the API's `content` field is a **list of `Document::*`
blocks** (`Text`/`Heading`/`Step`/`Gallery`), each with its own `resource.content`
HTML -- not a plain HTML string; the server flattens these to HTML.

Embedded images appear in that HTML as relative paths
(`/org_id/docs/doc_id/images/<image_id>`). Those paths are **not** directly
downloadable (they 404 on the API and redirect to SSO on the web host). The
reliable route is `GET /document_images/<image_id>`, which returns a short-lived
pre-signed S3 URL (`original-src` / `slim-src` / `thumbnail-src`) holding the
real bytes -- `get_document_images` uses it and returns the images as image
content for the agent to inspect. The API key is sent only to IT Glue hosts,
never to pre-signed S3 URLs.

**`list_document_images` deliberately does not return those S3 URLs.** Their
credential/signature fields are redacted in MCP output, so a caller that tries
to fetch one gets S3 `400 InvalidToken`; the tool returns image id/name/size
plus a hint instead, and `get_document_images` remains the only route to the
bytes.

## Configuration

Precedence: explicit args > `ITGLUE_*` env vars > JSON config file.

| Env var | Meaning |
| --- | --- |
| `ITGLUE_API_KEY` | API key (required). |
| `ITGLUE_BASE_URL` | `https://api.itglue.com` (`.eu` / `.au` for those regions). |
| `ITGLUE_SUBDOMAIN` | Subdomain for document links (default `weller`). |
| `ITGLUE_DEFAULT_ORGANIZATION_ID` | Org used when a call omits one (default unset). |
| `ITGLUE_VERIFY_SSL` | Default `true`. |
| `ITGLUE_TIMEOUT` | Request timeout seconds (default 30). |
| `ITGLUE_CACHE_TTL` | Listing cache seconds (default 900). |
| `ITGLUE_MCP_AUTH_TOKEN` | Bearer key gating the HTTP transport (empty = open). |
| `ITGLUE_CONFIG_FILE` | Optional JSON config file path. |

## Run

```bash
# stdio (embedded MCP client)
ITGLUE_API_KEY=... python -m itglue_mcp

# network daemon
python -m itglue_mcp --transport http --host 0.0.0.0 --port 8000

# connectivity check
python -m itglue_mcp ping

# masked credential wizard -> 0600 JSON config
python -m itglue_mcp config init --config ./itglue.json
```

### Docker

```bash
docker build -t itglue-mcp:latest .
docker run -d --name itglue-mcp --restart unless-stopped -p 8006:8000 \
  --env-file ~/.openclaw/itglue-mcp.env itglue-mcp:latest \
  --transport http --host 0.0.0.0 --port 8000
```

`docker compose up -d` uses the project `.env` file.

## Register with OpenClaw

```bash
openclaw mcp add itglue-mcp --url http://127.0.0.1:8006/mcp \
  --transport streamable-http \
  --header "Authorization=Bearer <ITGLUE_MCP_AUTH_TOKEN>" --timeout 60
openclaw mcp reload && openclaw mcp probe itglue-mcp
```

## Tests

```bash
python -m unittest -v tests_sanity
```

Offline only — they exercise the JSON:API envelope/pagination, HTML and image
parsing, id validation, link building, and the local-search / image tools
against a stub client.
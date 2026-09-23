"""Offline sanity tests for the IT Glue MCP server.

These do not touch the network. They exercise the JSON:API envelope handling,
pagination parameter building, document HTML parsing (text + embedded images),
id validation, document-link building, and the two document tools that build
their results locally (`search_documents`, `get_document_images`) against a stub
client.

Run with:

    python -m unittest -v tests_sanity
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from itglue_mcp.client import ITGlueClient
from itglue_mcp.config import ITGlueConfig, load_config
from itglue_mcp.tools import _common as common
from itglue_mcp.tools import document_tools as dt


# --------------------------------------------------------------------- stubs
class FakeMCP:
    """Captures @mcp.tool()-decorated functions for direct invocation."""

    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class StubClient:
    def __init__(self, docs=None, documents=None, images=None, doc_images=None):
        self._docs = docs or []
        self._documents = documents or {}
        self._images = images or {}
        # image_id (str) -> {"attributes": {...}} from GET /document_images/:id
        self._doc_images = doc_images or {}

    def list_documents(self, org_id, *, folder_id=None, refresh=False):
        return list(self._docs), False

    def get_document(self, doc_id, *, refresh=False):
        return self._documents.get(str(doc_id))

    def get_document_image(self, image_id):
        return self._doc_images.get(str(image_id))

    def download_document_image(self, image_id, *, preference="original"):
        a = (self._doc_images[str(image_id)] or {}).get("attributes") or {}
        url = a.get(f"{preference}-src") or a.get("original-src")
        raw, ctype, name = self._images[url]
        return raw, ctype, a.get("name") or name, a

    def download_binary(self, url, use_auth=True):
        return self._images[url]


def _doc(doc_id, name, content="", org=6006071, updated="2026-01-01T00:00:00.000Z"):
    return {
        "id": str(doc_id),
        "type": "documents",
        "attributes": {
            "name": name,
            "organization-id": org,
            "content": content,
            "updated-at": updated,
        },
    }


# --------------------------------------------------------------------- tests
class HtmlTests(unittest.TestCase):
    def test_html_to_text_preserves_structure(self):
        html = "<h1>VPN Setup</h1><p>Step 1</p><ul><li>Alpha</li><li>Beta</li></ul>"
        text = common.html_to_text(html)
        self.assertIn("VPN Setup", text)
        self.assertIn("Step 1", text)
        self.assertIn("Alpha", text)
        # headings/list items become separate lines
        self.assertGreaterEqual(len(text.splitlines()), 4)

    def test_html_to_text_marks_images(self):
        self.assertIn("[image]", common.html_to_text('<p>a</p><img src="x.png">'))

    def test_max_chars_truncates(self):
        text = common.html_to_text("<p>" + "a" * 100 + "</p>", max_chars=10)
        self.assertIn("truncated", text)


class ImageParseTests(unittest.TestCase):
    def test_resolves_absolute_relative_and_data_uri(self):
        html = (
            '<img src="https://cdn.example.com/a.png" alt="A">'
            '<img src="/attachments/1/b.png">'
            '<img src="//api.itglue.com/c.png">'
            '<img src="data:image/png;base64,AAAA">'
        )
        imgs = common.extract_images(html, base_url="https://api.itglue.com")
        srcs = [i["src"] for i in imgs]
        self.assertIn("https://cdn.example.com/a.png", srcs)
        self.assertIn("https://api.itglue.com/attachments/1/b.png", srcs)
        self.assertIn("https://api.itglue.com/c.png", srcs)
        self.assertTrue(any(s.startswith("data:image/png") for s in srcs))
        self.assertEqual(imgs[0]["alt"], "A")

    def test_extracts_document_image_id(self):
        html = '<img src="/6006071/docs/16101974/images/23512329">'
        imgs = common.extract_images(html, base_url="https://api.itglue.com")
        self.assertEqual(imgs[0]["image_id"], 23512329)
        # developer-path form and data URI (no id) are handled too
        self.assertEqual(common.image_id_from_src("/1/docs/2/developer/images/794078"), 794078)
        self.assertIsNone(common.image_id_from_src("data:image/png;base64,AAAA"))

    def test_dedupes_repeated_src(self):
        html = '<img src="a.png"><img src="a.png">'
        self.assertEqual(len(common.extract_images(html, "https://x")), 1)


class DocumentHtmlTests(unittest.TestCase):
    def test_renders_blocks_in_sort_order(self):
        item = {"attributes": {"content": [
            {"resource_type": "Document::Heading", "sort": 1,
             "resource": {"content": "Title", "level": 2}},
            {"resource_type": "Document::Text", "sort": 0,
             "resource": {"content": "<p>Body</p>"}},
        ]}}
        html = common.document_html(item)
        self.assertLess(html.index("Body"), html.index("Title"))
        self.assertIn("<h2>Title</h2>", html)

    def test_falls_back_to_sections(self):
        item = {"attributes": {"content": [], "sections": [
            {"type": "document-sections", "sort": 0,
             "attributes": {"resource-type": "Document::Text",
                            "content": "<p>From sections</p>"}},
        ]}}
        self.assertIn("From sections", common.document_html(item))

    def test_plain_string_content(self):
        item = {"attributes": {"content": "<p>plain</p>"}}
        self.assertEqual(common.document_html(item), "<p>plain</p>")

    def test_image_block_renders_img(self):
        item = {"attributes": {"content": [
            {"resource_type": "Document::Image", "sort": 0,
             "resource": {"url": "/6006071/docs/1/images/2"}},
        ]}}
        self.assertIn('<img src="/6006071/docs/1/images/2">', common.document_html(item))


class IdValidationTests(unittest.TestCase):
    def test_document_id_accepts_bare_and_url(self):
        self.assertEqual(common.normalize_document_id(9623259), 9623259)
        self.assertEqual(common.normalize_document_id("9623259"), 9623259)
        self.assertEqual(
            common.normalize_document_id("https://weller.itglue.com/6006071/documents/9623259"),
            9623259,
        )

    def test_document_id_rejects_garbage(self):
        with self.assertRaises(ValueError):
            common.normalize_document_id("not-a-doc")

    def test_org_id_default_and_validation(self):
        self.assertEqual(common.normalize_org_id(None, default=6006071), 6006071)
        with self.assertRaises(ValueError):
            common.normalize_org_id("abc")
        with self.assertRaises(ValueError):
            common.normalize_org_id(None, default=0)


class DocumentUrlTests(unittest.TestCase):
    def test_default_template(self):
        cfg = ITGlueConfig(subdomain="weller")
        self.assertEqual(
            cfg.document_url("9623259", 6006071),
            "https://weller.itglue.com/6006071/documents/9623259",
        )

    def test_missing_org_falls_back_to_subdomain(self):
        cfg = ITGlueConfig(subdomain="weller")
        url = cfg.document_url("9623259")
        self.assertIn("weller.itglue.com", url)
        self.assertIn("9623259", url)


class ConfigTests(unittest.TestCase):
    def test_redacted_hides_secrets(self):
        cfg = ITGlueConfig(api_key="secret-key", mcp_auth_token="secret-token")
        red = cfg.redacted()
        self.assertEqual(red["api_key"], "***REDACTED***")
        self.assertEqual(red["mcp_auth_token"], "***REDACTED***")
        self.assertNotIn("secret", str(red))

    def test_load_config_from_env_and_default_org(self):
        env = {
            "ITGLUE_API_KEY": "k",
            "ITGLUE_DEFAULT_ORGANIZATION_ID": "6006071",
            "ITGLUE_SUBDOMAIN": "weller",
        }
        for k in ("ITGLUE_BASE_URL", "ITGLUE_CONFIG_FILE", "ITGLUE_MCP_AUTH_TOKEN"):
            env.pop(k, None)
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = load_config()
        self.assertEqual(cfg.api_key, "k")
        self.assertEqual(cfg.default_organization_id, 6006071)
        self.assertTrue(cfg.is_complete())

    def test_missing_key_raises(self):
        from itglue_mcp.config import ConfigError
        with mock.patch.dict(os.environ, {"ITGLUE_API_KEY": ""}, clear=False):
            os.environ.pop("ITGLUE_API_KEY", None)
            os.environ.pop("ITGLUE_CONFIG_FILE", None)
            with self.assertRaises(ConfigError):
                load_config()


class ClientEnvelopeTests(unittest.TestCase):
    def test_page_params(self):
        self.assertEqual(
            ITGlueClient._page_params(1000, 2),
            {"page[size]": 1000, "page[number]": 2},
        )

    def test_unwrap_list_and_one(self):
        self.assertEqual(ITGlueClient.unwrap_list({"data": [{"id": "1"}]}), [{"id": "1"}])
        self.assertEqual(ITGlueClient.unwrap_list({"data": {"id": "1"}}), [{"id": "1"}])
        self.assertEqual(ITGlueClient.unwrap_one({"data": {"id": "1"}}), {"id": "1"})

    def test_meta(self):
        self.assertEqual(
            ITGlueClient.meta({"data": [], "meta": {"total-count": 121}}),
            {"total-count": 121},
        )


class DocumentListingParamsTests(unittest.TestCase):
    """The whole-org listing must send ``filter[document_folder_id]=null`` --
    without it IT Glue returns only a small root subset (verified live: 13 vs
    576 documents)."""

    def _client(self, captured):
        class C(ITGlueClient):
            def get_all(self, path, *, params=None, page_size=1000, max_pages=50):
                captured["path"] = path
                captured["params"] = params
                return []
        return C(ITGlueConfig(api_key="k", base_url="https://api.itglue.com"))

    def test_whole_org_listing_sends_null_folder_filter(self):
        captured = {}
        self._client(captured).list_documents(6006071)
        self.assertEqual(captured["params"]["filter[document_folder_id]"], "null")
        self.assertEqual(
            captured["path"], "/organizations/6006071/relationships/documents"
        )

    def test_folder_scoped_listing_sends_folder_id(self):
        captured = {}
        self._client(captured).list_documents(6006071, folder_id=3833777)
        self.assertEqual(captured["params"]["filter[document_folder_id]"], "3833777")


class ToolRegistrationTests(unittest.TestCase):
    def test_expected_tool_count(self):
        mcp = FakeMCP()
        config = ITGlueConfig(api_key="k", default_organization_id=6006071)
        from itglue_mcp.tools import register_all
        register_all(mcp, config)
        expected = {
            "list_organizations", "get_organization",
            "list_document_folders", "list_documents", "search_documents",
            "search_document_contents", "get_document", "list_document_images",
            "get_document_images", "get_related_items",
        }
        self.assertEqual(set(mcp.tools), expected)


class SearchDocumentsTests(unittest.TestCase):
    def _tools(self, stub):
        mcp = FakeMCP()
        config = ITGlueConfig(api_key="k", default_organization_id=6006071)
        from itglue_mcp.tools import register_all
        register_all(mcp, config)
        return mcp.tools, config

    def test_local_name_search(self):
        docs = [
            _doc(1, "VPN Setup Guide"),
            _doc(2, "Printer Troubleshooting"),
            _doc(3, "VPN Client Install"),
        ]
        stub = StubClient(docs=docs)
        with mock.patch.object(dt, "get_client", return_value=stub):
            tools, _ = self._tools(stub)
            result = tools["search_documents"](query="vpn")
        names = [d["name"] for d in result["documents"]]
        self.assertEqual(result["matched"], 2)
        self.assertIn("VPN Setup Guide", names)
        self.assertIn("VPN Client Install", names)
        self.assertNotIn("Printer Troubleshooting", names)

    def test_empty_query_raises(self):
        stub = StubClient(docs=[])
        with mock.patch.object(dt, "get_client", return_value=stub):
            tools, _ = self._tools(stub)
            with self.assertRaises(ValueError):
                tools["search_documents"](query="  ")


class SearchDocumentContentsTests(unittest.TestCase):
    def test_scans_listing_content_locally(self):
        docs = [
            _doc(1, "VPN Setup", content="<p>Configure the VPN client.</p>"),
            _doc(2, "Printer", content="<p>Load the toner cartridge.</p>"),
        ]
        stub = StubClient(docs=docs)
        mcp = FakeMCP()
        config = ITGlueConfig(api_key="k", default_organization_id=6006071)
        from itglue_mcp.tools import register_all
        register_all(mcp, config)
        with mock.patch.object(dt, "get_client", return_value=stub):
            res = mcp.tools["search_document_contents"](query="toner")
        self.assertEqual(res["matched"], 1)
        self.assertEqual(res["documents"][0]["name"], "Printer")
        self.assertIn("toner", res["documents"][0]["match_snippet"].lower())


class GetDocumentImagesTests(unittest.TestCase):
    def _tools(self, stub):
        mcp = FakeMCP()
        config = ITGlueConfig(api_key="k", default_organization_id=6006071,
                              base_url="https://api.itglue.com")
        from itglue_mcp.tools import register_all
        register_all(mcp, config)
        return mcp.tools

    def test_returns_image_content(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        s3 = "https://itg-prod-paperclip.s3.us-west-2.amazonaws.com/document/images/x.png?sig=1"
        doc = _doc(9623259, "Handbook",
                   content='<p>hi</p><img src="/6006071/docs/9623259/images/555" alt="diagram">')
        stub = StubClient(
            documents={"9623259": doc},
            doc_images={"555": {"attributes": {"name": "diagram.png", "size": len(png),
                                                "original-src": s3}}},
            images={s3: (png, "image/png", "diagram.png")},
        )
        with mock.patch.object(dt, "get_client", return_value=stub):
            tools = self._tools(stub)
            out = tools["get_document_images"](document_id="9623259", max_images=2)
        kinds = [getattr(o, "type", None) for o in out]
        self.assertIn("image", kinds)

    def test_non_image_reports_text(self):
        # An image whose resolved content type is not image/* falls back to text.
        doc = _doc(5, "Doc", content='<img src="/6006071/docs/5/images/9">')
        stub = StubClient(
            documents={"5": doc},
            doc_images={"9": {"attributes": {"name": "a.pdf", "original-src": "https://s3/a.pdf"}}},
            images={"https://s3/a.pdf": (b"%PDF-1.4", "application/pdf", "a.pdf")},
        )
        with mock.patch.object(dt, "get_client", return_value=stub):
            tools = self._tools(stub)
            out = tools["get_document_images"](document_id=5)
        self.assertTrue(all(getattr(o, "type", None) == "text" for o in out))
        self.assertIn("not an image", "".join(o.text for o in out))


class ListDocumentImagesTests(unittest.TestCase):
    def test_metadata_present_but_urls_omitted(self):
        """list_document_images must NOT echo the pre-signed S3 URLs (their
        signatures are redacted, so fetching them yields S3 400 InvalidToken)."""
        doc = _doc(7, "Doc", content='<img src="/6006071/docs/7/images/42">')
        stub = StubClient(
            documents={"7": doc},
            doc_images={"42": {"attributes": {
                "name": "pic.png", "size": 1234,
                "original-src": "https://s3/pic.png?sig=x",
                "slim-src": "https://s3/slim/pic.png?sig=x",
                "thumbnail-src": "https://s3/thumb/pic.png?sig=x",
            }}},
        )
        mcp = FakeMCP()
        config = ITGlueConfig(api_key="***", default_organization_id=6006071)
        from itglue_mcp.tools import register_all
        register_all(mcp, config)
        with mock.patch.object(dt, "get_client", return_value=stub):
            res = mcp.tools["list_document_images"](document_id=7)
        img = res["images"][0]
        self.assertEqual(img["image_id"], 42)
        self.assertEqual(img["name"], "pic.png")
        self.assertEqual(img["size"], 1234)
        self.assertNotIn("urls", img)
        self.assertNotIn("s3", str(img))
        self.assertIn("hint", res)


if __name__ == "__main__":
    unittest.main()

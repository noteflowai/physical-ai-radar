import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile
import zlib

from pairadar.wechat import export_pack, inspect_pack


def png(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    pixels = (b"\0" + b"\x10\x28\x39" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class WechatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for folder in ("assets", "articles", "sources"):
            (self.source / folder).mkdir()
        self.body = ('<section><p>来源 https://www.oneai.host/fixture-article/</p>'
                     '<img src="../assets/fixture-diagram.png" alt="原创说明图">'
                     '<p>AI 辅助生成。理想算例，未验证动力学。</p></section>')
        self.full = '<!doctype html><html lang="zh-CN"><head><title>测试</title></head><body>' + self.body + '</body></html>'
        self.md = '# 测试文章\n\nAI 辅助生成。来源：https://www.oneai.host/fixture-article/\n'
        self.row = {
            "slug": "fixture", "title": "测试文章", "author_proposed": "物理AI Lab",
            "digest": "离线测试夹具", "source_url": "https://www.oneai.host/fixture-article/",
            "source_post_ids": [7], "status": "local-draft", "published": False,
            "wechat_draft_id": None, "wechat_article_url": None,
            "ai_assisted": True, "originality_declaration_verified": False,
            "source_scope": "Public, unauthenticated WordPress content; no member material copied",
            "cover": "assets/fixture-cover.png", "square_cover": "assets/fixture-square.png",
            "diagram": "assets/fixture-diagram.png",
        }
        self.provenance = {"sources": [{
            "id": 7, "title": "公开来源测试夹具", "url": self.row["source_url"],
            "http_status": 200, "access_scope": "Unauthenticated public page only",
            "public_html_sha256": "a" * 64, "public_text_sha256": "b" * 64,
            "modified": "2026-10-05T00:00:00",
            "links": [{"url": "https://www.oneai.host/wp-login.php?test=ignored"}],
        }]}
        for key, size in [("cover", (900, 383)), ("square_cover", (200, 200)), ("diagram", (800, 500))]:
            (self.source / self.row[key]).write_bytes(png(*size))
        self.save()

    def save(self):
        for suffix, text in [(".md", self.md), (".html", self.full), (".body.html", self.body)]:
            (self.source / ("articles/fixture" + suffix)).write_text(text)
        self.row.update(markdown_sha256=hashlib.sha256(self.md.encode()).hexdigest(),
                        html_sha256=hashlib.sha256(self.full.encode()).hexdigest())
        self.manifest = {"published": False, "articles": [self.row], "provenance_manifest": "sources/manifest.json"}
        (self.source / "content-manifest.json").write_text(json.dumps(self.manifest))
        (self.source / "sources/manifest.json").write_text(json.dumps(self.provenance))

    def test_actual_files_are_bound_and_export_is_repeatable(self):
        normalized, files = inspect_pack(self.source)
        self.assertFalse(normalized["published"])
        self.assertFalse(normalized["wechat_uploaded"])
        self.assertEqual(normalized["articles"][0]["body_html_sha256"], hashlib.sha256(self.body.encode()).hexdigest())
        for name, digest in normalized["files"].items():
            self.assertEqual(hashlib.sha256(files[name]).hexdigest(), digest)
        one = export_pack(self.source, self.root / "one")
        two = export_pack(self.source, self.root / "two")
        self.assertEqual(one["archive_sha256"], two["archive_sha256"])
        self.assertFalse(one["wechat_live_validated"])

    def test_unselected_config_credentials_and_scraped_login_links_are_excluded(self):
        (self.source / "browser-request.json").write_text('{"private": "excluded fixture"}')
        (self.source / "account-config.proposed.json").write_text('{"private": "excluded fixture"}')
        export_pack(self.source, self.root / "export")
        with zipfile.ZipFile(self.root / "export/editorial-pack.zip") as archive:
            self.assertEqual(len(archive.namelist()), 7)
            text = archive.read("manifest.json").decode()
            self.assertNotIn("wp-login", text)
            self.assertNotIn("account-config", " ".join(archive.namelist()))

    def test_changed_md_full_html_body_or_png_is_rejected(self):
        for name in ["articles/fixture.md", "articles/fixture.html", "articles/fixture.body.html",
                     self.row["cover"], self.row["square_cover"], self.row["diagram"]]:
            with self.subTest(name=name):
                path = self.source / name
                before = path.read_bytes()
                path.write_bytes(before + b"changed")
                with self.assertRaises(ValueError):
                    inspect_pack(self.source)
                path.write_bytes(before)

    def test_active_html_inside_or_outside_body_is_rejected_even_with_new_hash(self):
        for injected in ['<script>bad()</script>', '<img src="https://example.org/p.png">',
                         '<p onclick="bad()">bad</p>', '<p style="background:url(https://example.org)">bad</p>',
                         '<!-- private archival comment -->', '<form></form>']:
            with self.subTest(injected=injected):
                body, full = self.body, self.full
                self.body = self.body.replace("</section>", injected + "</section>")
                self.full = full.replace(body, self.body)
                self.save()
                with self.assertRaises(ValueError):
                    inspect_pack(self.source)
                self.body, self.full = body, full
                self.save()
        self.full = self.full.replace("</head>", "<script>bad()</script></head>")
        self.save()
        with self.assertRaises(ValueError):
            inspect_pack(self.source)

    def test_source_identity_ai_disclosure_and_platform_claims_are_checked(self):
        for key, value in [("source_post_ids", [8]), ("source_url", "https://www.oneai.host/wp-login.php"),
                           ("published", True), ("wechat_draft_id", "fixture-id"),
                           ("ai_assisted", False), ("originality_declaration_verified", True),
                           ("title", "长" * 33)]:
            with self.subTest(key=key):
                row = copy.deepcopy(self.row)
                self.row[key] = value
                self.save()
                with self.assertRaises(ValueError):
                    inspect_pack(self.source)
                self.row = row
                self.save()
        self.body = self.body.replace("AI 辅助生成", "辅助")
        self.full = '<!doctype html><html><body>' + self.body + '</body></html>'
        self.save()
        with self.assertRaises(ValueError):
            inspect_pack(self.source)

    def test_wrong_dimensions_and_invalid_png_are_rejected(self):
        path = self.source / self.row["cover"]
        for data in [png(899, 383), b"\x89PNG\r\n\x1a\n", b"not a png"]:
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                inspect_pack(self.source)

    def test_symlink_and_relative_path_escape_are_rejected(self):
        path = self.source / self.row["diagram"]
        external = self.root / "outside.png"
        external.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(external)
        with self.assertRaises(ValueError):
            inspect_pack(self.source)
        path.unlink()
        path.write_bytes(external.read_bytes())
        for name in ["../outside.png", "assets/../../outside.png", "assets/%2e%2e/outside.png"]:
            self.row["diagram"] = name
            self.save()
            with self.assertRaises(ValueError):
                inspect_pack(self.source)

    def test_invalid_input_creates_no_output_and_interrupted_output_is_not_reused(self):
        (self.source / "articles/fixture.md").write_text("changed")
        output = self.root / "failed"
        with self.assertRaises(ValueError):
            export_pack(self.source, output)
        self.assertFalse(output.exists())
        self.save()
        output.mkdir()
        marker = output / "partial"
        marker.write_text("preserve")
        with self.assertRaises(FileExistsError):
            export_pack(self.source, output)
        self.assertEqual(marker.read_text(), "preserve")
        for path in [self.source / "nested", self.root, Path("relative-output")]:
            with self.assertRaises(ValueError):
                export_pack(self.source, path)


if __name__ == "__main__":
    unittest.main()

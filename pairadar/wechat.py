"""Verify and export an existing local WeChat editorial pack, without networking.

This domain adapter preserves authored content and source limitations. It does
not grant account access, upload media, create drafts or certify publication.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import sys
from urllib.parse import urlsplit
import zipfile
import zlib

MAX_FILE = 2_000_000
MAX_TOTAL = 20_000_000
SHA = re.compile(r"[a-f0-9]{64}")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
SOURCE_SCOPE = "Public, unauthenticated WordPress content; no member material copied"


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def local_path(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or "%" in value or "?" in value or "#" in value
            or PurePosixPath(value).is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("plain relative content path required")
    return PurePosixPath(value)


def read_local(root, relative):
    parts = local_path(relative).parts
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlinked editorial input rejected")
    if not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError("bounded regular editorial input required")
    raw = path.read_bytes()
    if len(raw) > MAX_FILE:
        raise ValueError("editorial input exceeds limit")
    return raw


def public_url(value):
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("bounded public source URL required")
    parsed = urlsplit(value)
    # This adapter only imports the already reviewed public WordPress corpus.
    if (parsed.scheme != "https" or parsed.netloc != "www.oneai.host"
            or parsed.query or parsed.fragment or not parsed.path.startswith("/")
            or parsed.path.startswith(("/wp-admin", "/wp-login", "/wp-json"))):
        raise ValueError("public WordPress article URL required")
    return value


class Body(HTMLParser):
    TAGS = {"section", "p", "h2", "h3", "h4", "div", "span", "strong", "em",
            "b", "i", "ul", "ol", "li", "pre", "code", "blockquote", "figure",
            "figcaption", "img", "br", "hr", "a", "table", "thead", "tbody",
            "tr", "td", "th", "sup", "sub"}
    VOID = {"img", "br", "hr"}

    def __init__(self, expected_image):
        super().__init__(convert_charrefs=True)
        self.expected_image = "../" + expected_image
        self.images = []
        self.text = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.TAGS or len(dict(attrs)) != len(attrs):
            raise ValueError("active or unsupported article HTML")
        for name, value in attrs:
            if name not in {"style", "alt", "title", "src", "href", "class"} or value is None:
                raise ValueError("unsupported article attribute")
            if name == "class" and (tag != "code" or not re.fullmatch(r"language-[a-zA-Z0-9_-]{1,30}", value)):
                raise ValueError("only bounded code language classes are accepted")
            if name == "style" and re.search(
                    r"url\s*\(|@import|expression\s*\(|behavior\s*:|[\\<>]", value, re.I):
                raise ValueError("active article style")
            if name == "src" and (tag != "img" or value != self.expected_image):
                raise ValueError("body image must match the local diagram")
            if name == "href":
                parsed = urlsplit(value)
                if (tag != "a" or parsed.scheme != "https" or not parsed.hostname
                        or parsed.username or parsed.password or parsed.query
                        or parsed.netloc != parsed.hostname):
                    raise ValueError("plain public HTTPS article link required")
        if tag == "img":
            if dict(attrs).get("src") != self.expected_image:
                raise ValueError("missing body image")
            self.images.append(self.expected_image)
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag not in self.VOID:
            raise ValueError("non-void self-closing article tag")
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag not in self.VOID:
            if not self.stack or self.stack.pop() != tag:
                raise ValueError("unbalanced article HTML")

    def handle_data(self, value):
        self.text.append(value)

    def handle_comment(self, value):
        raise ValueError("article comments must not enter archival output")

    def handle_decl(self, value):
        raise ValueError("body HTML cannot include declarations")


class Full(Body):
    """The offline wrapper is inspected too, including content outside body."""
    TAGS = Body.TAGS | {"html", "head", "meta", "title", "body"}
    VOID = Body.VOID | {"meta"}

    def handle_starttag(self, tag, attrs):
        if tag in {"html", "meta"}:
            allowed = {"lang"} if tag == "html" else {"charset", "name", "content"}
            if (len(dict(attrs)) != len(attrs)
                    or any(k not in allowed or v is None for k, v in attrs)):
                raise ValueError("unsupported offline wrapper attributes")
            if tag == "html":
                self.stack.append(tag)
            return
        super().handle_starttag(tag, attrs)

    def handle_decl(self, value):
        if value.lower() != "doctype html":
            raise ValueError("unsupported offline declaration")


def png_size(raw):
    """Check actual PNG chunks, CRCs and bounded decoded pixels, using stdlib."""
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG required")
    offset, chunks, pixels = 8, [], b""
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise ValueError("truncated PNG")
        size = int.from_bytes(raw[offset:offset + 4], "big")
        end = offset + size + 12
        if end > len(raw):
            raise ValueError("truncated PNG chunk")
        kind = raw[offset + 4:offset + 8]
        data = raw[offset + 8:offset + 8 + size]
        crc = int.from_bytes(raw[end - 4:end], "big")
        if zlib.crc32(kind + data) & 0xffffffff != crc:
            raise ValueError("PNG CRC mismatch")
        if kind not in {b"IHDR", b"IDAT", b"IEND"}:
            raise ValueError("PNG metadata or unsupported encoding rejected")
        chunks.append(kind)
        if kind == b"IHDR":
            if len(chunks) != 1 or size != 13:
                raise ValueError("one PNG header required")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            if (not 1 <= width <= 4000 or not 1 <= height <= 4000
                    or width * height > 4_000_000 or depth != 8 or color not in {2, 6}
                    or compression or filtering or interlace):
                raise ValueError("bounded 8-bit RGB/RGBA PNG required")
        elif kind == b"IDAT":
            pixels += data
        elif kind == b"IEND" and (size or end != len(raw)):
            raise ValueError("invalid PNG end")
        offset = end
    if (not chunks or chunks[0] != b"IHDR" or chunks[-1] != b"IEND"
            or chunks.count(b"IHDR") != 1 or chunks.count(b"IEND") != 1
            or not pixels or any(c != b"IDAT" for c in chunks[1:-1])):
        raise ValueError("complete contiguous PNG image required")
    row_bytes = 1 + width * (3 if color == 2 else 4)
    expected = height * row_bytes
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(pixels, expected + 1)
    if (len(decoded) != expected or not decoder.eof or decoder.unused_data
            or decoder.unconsumed_tail
            or any(decoded[i] > 4 for i in range(0, expected, row_bytes))):
        raise ValueError("invalid PNG pixels")
    return [width, height]


def inspect_pack(source: Path):
    """Read exact input bytes; embedded 'passed' fields are never evidence."""
    source = source.expanduser().absolute()
    if source.is_symlink() or not source.is_dir():
        raise ValueError("editorial source directory required")
    source = source.resolve()
    raw_manifest = read_local(source, "content-manifest.json")
    manifest = json.loads(raw_manifest)
    if not isinstance(manifest, dict) or manifest.get("published") is not False:
        raise ValueError("local unpublished pack required")
    rows = manifest.get("articles")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("one to twenty local articles required")
    provenance_raw = read_local(source, manifest.get("provenance_manifest"))
    provenance = json.loads(provenance_raw)
    sources = provenance.get("sources") if isinstance(provenance, dict) else None
    if not isinstance(sources, list) or not 1 <= len(sources) <= 100:
        raise ValueError("bounded source provenance required")
    by_id = {}
    for row in sources:
        if (not isinstance(row, dict) or type(row.get("id")) is not int
                or row["id"] in by_id or row.get("http_status") != 200
                or row.get("access_scope") != "Unauthenticated public page only"
                or not SHA.fullmatch(str(row.get("public_html_sha256")))
                or not SHA.fullmatch(str(row.get("public_text_sha256")))
                or not isinstance(row.get("title"), str)
                or not isinstance(row.get("modified"), str)):
            raise ValueError("public source provenance required")
        public_url(row.get("url"))
        # No scraped login links, account configuration or raw page bodies.
        by_id[row["id"]] = {k: row[k] for k in (
            "id", "title", "url", "modified", "public_html_sha256", "public_text_sha256")}
    files, articles, used, slugs = {}, [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("article object required")
        slug = row.get("slug")
        if not isinstance(slug, str) or not SLUG.fullmatch(slug) or slug in slugs:
            raise ValueError("unique plain article slug required")
        slugs.add(slug)
        for key, limit in (("title", 32), ("author_proposed", 16), ("digest", 120)):
            if not isinstance(row.get(key), str) or not 1 <= len(row[key]) <= limit:
                raise ValueError("conservative local editorial length exceeded")
        if (row.get("status") != "local-draft" or row.get("published") is not False
                or row.get("wechat_draft_id") is not None or row.get("wechat_article_url") is not None
                or row.get("ai_assisted") is not True
                or row.get("originality_declaration_verified") is not False
                or row.get("source_scope") != SOURCE_SCOPE):
            raise ValueError("unpublished AI-assisted public-source article required")
        ids = row.get("source_post_ids")
        if (not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids)
                or len(ids) != len(set(ids)) or any(i not in by_id for i in ids)
                or public_url(row.get("source_url")) not in {by_id[i]["url"] for i in ids}):
            raise ValueError("article must bind its source provenance")
        used.update(ids)
        names = {suffix: f"articles/{slug}{suffix}" for suffix in (".md", ".html", ".body.html")}
        content = {suffix: read_local(source, name) for suffix, name in names.items()}
        for suffix, key in ((".md", "markdown_sha256"), (".html", "html_sha256")):
            if digest(content[suffix]) != row.get(key):
                raise ValueError("authored article hash mismatch")
        texts = {suffix: data.decode("utf-8") for suffix, data in content.items()}
        if not texts[".body.html"] or texts[".html"].count(texts[".body.html"]) != 1:
            raise ValueError("body must be bound to the hashed full HTML")
        parser = Body(row.get("diagram"))
        parser.feed(texts[".body.html"])
        parser.close()
        visible = "".join(parser.text)
        if parser.stack or len(parser.images) != 1 or "AI 辅助生成" not in visible:
            raise ValueError("balanced illustrated AI-disclosed body required")
        full_parser = Full(row.get("diagram"))
        full_parser.feed(texts[".html"])
        full_parser.close()
        if full_parser.stack or full_parser.images != parser.images:
            raise ValueError("balanced offline wrapper required")
        if (not texts[".md"].startswith("# " + row["title"] + "\n")
                or "AI 辅助生成" not in texts[".md"]
                or any(by_id[i]["url"] not in visible for i in ids)):
            raise ValueError("title, AI disclosure and visible source links required")
        for suffix, name in names.items():
            files[name] = content[suffix]
        asset_hashes = {}
        for key, expected in (("cover", [900, 383]), ("square_cover", [200, 200]), ("diagram", None)):
            name = row.get(key)
            if (not isinstance(name, str) or not name.startswith(f"assets/{slug}-")
                    or not name.endswith(".png") or len(local_path(name).parts) != 2):
                raise ValueError("article-specific PNG asset required")
            data = read_local(source, name)
            size = png_size(data)
            if expected and size != expected:
                raise ValueError("cover dimensions mismatch")
            files[name] = data
            asset_hashes[key] = {"path": name, "sha256": digest(data), "dimensions": size}
        articles.append({
            "slug": slug, "title": row["title"], "author": row["author_proposed"],
            "digest": row["digest"], "source_url": row["source_url"], "source_post_ids": ids,
            "markdown": names[".md"], "full_html": names[".html"], "body_html": names[".body.html"],
            "markdown_sha256": digest(content[".md"]), "full_html_sha256": digest(content[".html"]),
            "body_html_sha256": digest(content[".body.html"]), "assets": asset_hashes,
            "ai_assisted": True, "platform_ai_declaration_pending": True,
            "originality_declaration_verified": False, "inline_images_require_platform_upload": True,
            "status": "local-draft", "wechat_draft_id": None, "wechat_article_url": None,
            "published": False,
        })
    total = sum(map(len, files.values()))
    if total > MAX_TOTAL:
        raise ValueError("editorial pack budget exceeded")
    normalized = {
        "schema_version": 1, "kind": "wechat-editorial-handoff", "state": "local-verified",
        "input_manifest_sha256": digest(raw_manifest), "input_provenance_sha256": digest(provenance_raw),
        "articles": articles, "sources": [by_id[i] for i in sorted(used)],
        "files": {name: digest(raw) for name, raw in sorted(files.items())},
        "wechat_editor_preview_verified": False, "wechat_uploaded": False, "published": False,
        "account_association_verified": False, "publication_approved": False,
        "scope": "Actual local bytes and editorial structure; source records are saved provenance, "
                 "not fresh remote readback or independent reproduction of source claims.",
    }
    return normalized, files


def export_pack(source: Path, output: Path):
    """One new attempt directory; never overwrite an existing output or input."""
    if not output.is_absolute():
        raise ValueError("absolute output path required")
    output = output.absolute()
    resolved = output.resolve()
    source_resolved = source.expanduser().resolve()
    if resolved.is_relative_to(source_resolved) or source_resolved.is_relative_to(resolved):
        raise ValueError("output must not overlap source")
    manifest, files = inspect_pack(source)  # Fail before creating any output.
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    files = {**files, "manifest.json": canonical(manifest)}
    archive = output / "editorial-pack.zip"
    with archive.open("xb") as stream:
        os.chmod(archive, 0o600)
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zip_file:
            for name, data in sorted(files.items()):
                entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o600 << 16
                zip_file.writestr(entry, data)
        stream.flush()
        os.fsync(stream.fileno())
    with zipfile.ZipFile(archive) as zip_file:
        if zip_file.testzip() is not None:
            raise ValueError("export archive verification failed")
    receipt = {
        "schema_version": 1, "kind": "wechat-editorial-handoff",
        "state": "local-verified", "archive_sha256": digest(archive.read_bytes()),
        "manifest_sha256": digest(files["manifest.json"]), "archive_files": len(files),
        "articles": len(manifest["articles"]), "model_calls": 0, "remote_effects": 0,
        "wechat_live_validated": False, "publication_approved": False,
    }
    with (output / "receipt.json").open("x", encoding="utf-8") as stream:
        os.chmod(output / "receipt.json", 0o600)
        stream.write(canonical(receipt).decode())
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="new absolute directory; omit to check only")
    args = parser.parse_args(argv)
    try:
        if args.out:
            result = export_pack(args.source, args.out)
        else:
            manifest, _ = inspect_pack(args.source)
            result = {"state": manifest["state"], "articles": len(manifest["articles"]),
                      "files": len(manifest["files"]), "wechat_live_validated": False,
                      "publication_approved": False, "remote_effects": 0, "model_calls": 0}
    except (OSError, ValueError, KeyError, TypeError, zlib.error, struct.error) as error:
        # Do not echo malformed content, credentials, URLs or private input paths.
        print(json.dumps({"state": "invalid-local-pack", "error": type(error).__name__,
                          "publication_approved": False}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

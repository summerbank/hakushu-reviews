#!/usr/bin/env python3
"""Archive posts from the review category of the DCInside Hakushu gallery."""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import mimetypes
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from lxml import etree
from lxml import html as lxml_html


GALLERY_ID = "hakushu"
REVIEW_HEAD_ID = "80"
LIST_URL = "https://gall.dcinside.com/mgallery/board/lists/"
VIEW_URL = "https://gall.dcinside.com/mgallery/board/view/"
COMMENT_URL = "https://gall.dcinside.com/board/comment/"
SCHEMA_VERSION = 2
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

CLASS_XPATH = "contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


class ArchiveError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, delay: float, timeout: float, retries: int, cookie: str = ""):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.cookie = cookie.strip()
        self.last_request_at = 0.0
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(
        self,
        url: str,
        *,
        referer: str = LIST_URL,
        form: dict[str, str] | None = None,
    ) -> tuple[bytes, str]:
        retryable = {429, 500, 502, 503, 504}
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            elapsed = time.monotonic() - self.last_request_at
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)

            headers = {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": (
                    "application/json, text/javascript, */*; q=0.01"
                    if form is not None
                    else "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
                "Referer": referer,
            }
            payload = None
            if form is not None:
                payload = urlencode(form).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                headers["Origin"] = "https://gall.dcinside.com"
                headers["X-Requested-With"] = "XMLHttpRequest"
            if self.cookie:
                headers["Cookie"] = self.cookie

            try:
                request = Request(url, data=payload, headers=headers, method="POST" if payload else "GET")
                with self.opener.open(request, timeout=self.timeout) as response:
                    body = response.read()
                    content_type = response.headers.get_content_type() or "application/octet-stream"
                self.last_request_at = time.monotonic()
                return body, content_type
            except HTTPError as exc:
                self.last_request_at = time.monotonic()
                last_error = exc
                if exc.code not in retryable or attempt == self.retries:
                    break
                retry_after = exc.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                time.sleep(min(wait, 60) + random.uniform(0.1, 0.5))
            except (URLError, TimeoutError, OSError) as exc:
                self.last_request_at = time.monotonic()
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep((2 ** attempt) + random.uniform(0.1, 0.5))

        raise ArchiveError(f"요청 실패: {url} ({last_error})")

    def get(self, url: str, *, referer: str = LIST_URL) -> tuple[bytes, str]:
        return self.request(url, referer=referer)

    def post_form(
        self, url: str, form: dict[str, str], *, referer: str
    ) -> tuple[bytes, str]:
        return self.request(url, referer=referer, form=form)

    def get_html(self, url: str, *, referer: str = LIST_URL) -> str:
        body, _ = self.get(url, referer=referer)
        return body.decode("utf-8", errors="replace")


def has_class(name: str) -> str:
    return CLASS_XPATH.format(name=name)


def first_text(node: etree._Element, xpath: str, default: str = "") -> str:
    found = node.xpath(xpath)
    if not found:
        return default
    value = found[0]
    if isinstance(value, etree._Element):
        value = value.text_content()
    return " ".join(str(value).split())


def parse_list_page(source: str, page_url: str) -> list[dict[str, str]]:
    try:
        document = lxml_html.fromstring(source)
    except (etree.ParserError, ValueError) as exc:
        raise ArchiveError(f"목록 HTML 파싱 실패: {exc}") from exc

    posts: list[dict[str, str]] = []
    rows = document.xpath(f"//tr[{has_class('us-post')}]")
    for row in rows:
        number = (row.get("data-no") or "").strip()
        if not number.isdigit():
            continue

        subject = first_text(row, f".//td[{has_class('gall_subject')}]")
        if "리뷰" not in subject:
            continue

        links = row.xpath(
            f".//td[{has_class('gall_tit')}]//a[not({has_class('reply_numbox')})][@href]"
        )
        if not links:
            continue
        link = links[0]

        writer_nodes = row.xpath(f".//td[{has_class('gall_writer')}]")
        writer = writer_nodes[0] if writer_nodes else None
        date_nodes = row.xpath(f".//td[{has_class('gall_date')}]")
        date_node = date_nodes[0] if date_nodes else None

        posts.append(
            {
                "number": number,
                "title": " ".join(link.text_content().split()),
                "url": urljoin(page_url, link.get("href")),
                "author": (writer.get("data-nick") or "").strip() if writer is not None else "",
                "date": (
                    (date_node.get("title") or date_node.text_content()).strip()
                    if date_node is not None
                    else ""
                ),
            }
        )
    return posts


def parse_post_page(source: str, source_url: str) -> dict[str, Any]:
    try:
        document = lxml_html.fromstring(source)
    except (etree.ParserError, ValueError) as exc:
        raise ArchiveError(f"본문 HTML 파싱 실패: {exc}") from exc

    body_nodes = document.xpath(f"//div[{has_class('write_div')}]")
    if not body_nodes:
        raise ArchiveError("본문 영역(.write_div)을 찾지 못했습니다")

    head = document.xpath(f"//div[{has_class('gallview_head')}]")
    head_node = head[0] if head else document
    writer_nodes = head_node.xpath(f".//div[{has_class('gall_writer')}]")
    writer_node = writer_nodes[0] if writer_nodes else None

    title = first_text(head_node, f".//span[{has_class('title_subject')}]")
    category = first_text(head_node, f".//span[{has_class('title_headtext')}]")
    if "리뷰" not in category:
        raise ArchiveError(f"리뷰 말머리 글이 아닙니다: {category or '말머리 없음'}")

    date_nodes = head_node.xpath(f".//span[{has_class('gall_date')}]")
    date_node = date_nodes[0] if date_nodes else None
    date = (
        (date_node.get("title") or date_node.text_content()).strip()
        if date_node is not None
        else ""
    )

    body = copy.deepcopy(body_nodes[0])

    def hidden_value(element_id: str) -> str:
        values = document.xpath(f'//*[@id="{element_id}"]/@value')
        return str(values[0]) if values else ""

    return {
        "title": title,
        "category": category.strip("[]"),
        "author": (writer_node.get("data-nick") or "").strip() if writer_node is not None else "",
        "author_uid": (writer_node.get("data-uid") or "").strip() if writer_node is not None else "",
        "author_ip": (writer_node.get("data-ip") or "").strip() if writer_node is not None else "",
        "date": date,
        "source_url": source_url,
        "body": body,
        "e_s_n_o": hidden_value("e_s_n_o"),
        "gall_type": hidden_value("_GALLTYPE_") or "M",
        "secret_article_key": hidden_value("secret_article_key"),
        "reported_comment_count": int(hidden_value("comment_cnt") or 0),
    }


def fetch_comments(
    client: HttpClient,
    post: dict[str, Any],
    number: str,
) -> tuple[list[dict[str, Any]], int]:
    if post["reported_comment_count"] <= 0:
        return [], 0
    if not post["e_s_n_o"]:
        raise ArchiveError("댓글 조회용 토큰(e_s_n_o)을 찾지 못했습니다")

    comments: list[dict[str, Any]] = []
    seen_comment_numbers: set[str] = set()
    seen_pages: set[tuple[str, ...]] = set()
    total_reported = post["reported_comment_count"]

    for page in range(1, 101):
        form = {
            "id": GALLERY_ID,
            "no": number,
            "cmt_id": GALLERY_ID,
            "cmt_no": number,
            "focus_cno": "",
            "focus_pno": "",
            "e_s_n_o": post["e_s_n_o"],
            "comment_page": str(page),
            "sort": "D",
            "prevCnt": "",
            "board_type": "",
            "_GALLTYPE_": post["gall_type"],
            "secret_article_key": post["secret_article_key"],
            "clean": "",
            "nptest": "",
        }
        raw, _ = client.post_form(COMMENT_URL, form, referer=post["source_url"])
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            message = raw.decode("utf-8", errors="replace").strip()[:120]
            raise ArchiveError(f"댓글 응답 파싱 실패: {message or exc}") from exc

        total_reported = int(data.get("total_cnt") or total_reported)
        page_comments = data.get("comments") or []
        fingerprint = tuple(str(item.get("no") or "") for item in page_comments)
        if not page_comments or fingerprint in seen_pages:
            break
        seen_pages.add(fingerprint)

        for item in page_comments:
            if str(item.get("nicktype") or "") == "COMMENT_BOY":
                continue
            comment_number = str(item.get("no") or "")
            if not comment_number or comment_number in seen_comment_numbers:
                continue
            seen_comment_numbers.add(comment_number)
            comments.append(
                {
                    "number": comment_number,
                    "parent": str(item.get("parent") or number),
                    "reply_to": str(item.get("c_no") or ""),
                    "depth": int(item.get("depth") or 0),
                    "author": str(item.get("name") or ""),
                    "author_uid": str(item.get("user_id") or ""),
                    "author_ip": str(item.get("ip") or ""),
                    "date": str(item.get("reg_date") or ""),
                    "memo": str(item.get("memo") or ""),
                    "deleted": str(item.get("del_yn") or "N") == "Y"
                    or str(item.get("is_delete") or "0") == "1",
                }
            )

        if len(comments) >= total_reported:
            break

    return comments, total_reported


def safe_slug(text: str, max_length: int = 80) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", text)
    text = re.sub(r"\s+", "_", text).strip("._ ")
    return (text[:max_length].rstrip("._ ") or "untitled")


def extension_for(url: str, content_type: str, payload: bytes) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    content_type = content_type.split(";", 1)[0].lower()
    known = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/svg+xml": ".svg",
    }
    if content_type in known:
        return known[content_type]

    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    if payload[4:12] in {b"ftypavif", b"ftypavis"}:
        return ".avif"
    if payload.lstrip().startswith(b"<svg"):
        return ".svg"
    guessed = mimetypes.guess_extension(content_type)
    if guessed in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg"}:
        return ".jpg" if guessed == ".jpeg" else guessed
    raise ArchiveError(f"이미지 형식이 아닌 응답입니다: {content_type}")


def sanitize_and_download_images(
    content: etree._Element,
    post_dir: Path,
    client: HttpClient,
    source_url: str,
    download_images: bool,
    *,
    asset_subdir: str,
    filename_prefix: str = "",
) -> tuple[int, list[str]]:
    warnings: list[str] = []
    downloaded = 0
    url_to_local: dict[str, str] = {}

    for bad in content.xpath(".//script|.//style|.//form|.//iframe|.//object|.//embed"):
        bad.drop_tree()

    for element in content.iter():
        for attribute in list(element.attrib):
            if attribute.lower().startswith("on") or attribute.lower() in {"srcset", "integrity"}:
                del element.attrib[attribute]

        if element.tag == "a" and element.get("href"):
            href = urljoin(source_url, element.get("href"))
            if urlparse(href).scheme.lower() not in {"http", "https"}:
                href = "#"
            element.set("href", href)
            element.set("rel", "noopener noreferrer")

    if not download_images:
        for image in content.xpath(".//img"):
            remote = image.get("src") or image.get("data-original") or image.get("data-src")
            if remote:
                image.set("src", urljoin(source_url, remote))
        return 0, warnings

    asset_dir = post_dir / "assets" / asset_subdir
    for image in content.xpath(".//img"):
        remote = image.get("src") or image.get("data-original") or image.get("data-src")
        if not remote or remote.startswith("data:"):
            continue
        remote = urljoin(source_url, remote)

        if remote in url_to_local:
            image.set("src", url_to_local[remote])
            continue

        try:
            payload, content_type = client.get(remote, referer=source_url)
            if not payload:
                raise ArchiveError("빈 응답")
            asset_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(remote.encode("utf-8")).hexdigest()[:10]
            prefix = f"{safe_slug(filename_prefix, 30)}_" if filename_prefix else ""
            filename = (
                f"{prefix}{downloaded + 1:03d}_{digest}"
                f"{extension_for(remote, content_type, payload)}"
            )
            target = asset_dir / filename
            target.write_bytes(payload)
            local_url = f"./assets/{asset_subdir}/{filename}"
            url_to_local[remote] = local_url
            image.set("src", local_url)
            downloaded += 1
        except ArchiveError as exc:
            warnings.append(f"이미지 저장 실패: {remote} ({exc})")
            image.set("src", remote)

        for lazy_attribute in ("data-original", "data-src"):
            image.attrib.pop(lazy_attribute, None)

    return downloaded, warnings


def normalized_body_text(body: etree._Element) -> str:
    raw = body.text_content().replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
    output: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line
        if blank and previous_blank:
            continue
        output.append(line)
        previous_blank = blank
    return "\n".join(output).strip() + "\n"


def inner_html(node: etree._Element) -> str:
    parts = [html.escape(node.text or "")]
    parts.extend(
        etree.tostring(child, encoding="unicode", method="html", with_tail=True)
        for child in node
    )
    return "".join(parts)


def prepare_comments(
    comments: list[dict[str, Any]],
    post_dir: Path,
    client: HttpClient,
    source_url: str,
    download_images: bool,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    prepared: list[dict[str, Any]] = []
    image_count = 0
    warnings: list[str] = []

    for comment in comments:
        memo = "(삭제된 댓글입니다.)" if comment["deleted"] else comment["memo"]
        try:
            fragment = lxml_html.fragment_fromstring(memo or "", create_parent="div")
        except (etree.ParserError, ValueError):
            fragment = etree.Element("div")
            fragment.text = memo

        count, comment_warnings = sanitize_and_download_images(
            fragment,
            post_dir,
            client,
            source_url,
            download_images,
            asset_subdir="comments",
            filename_prefix=comment["number"],
        )
        item = dict(comment)
        item.pop("memo", None)
        item["memo_html"] = inner_html(fragment)
        item["memo_text"] = " ".join(fragment.text_content().replace("\u00a0", " ").split())
        item["image_count"] = count
        prepared.append(item)
        image_count += count
        warnings.extend(comment_warnings)

    return prepared, image_count, warnings


def comments_text(comments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for comment in comments:
        indent = "  " * min(int(comment.get("depth", 0)), 8)
        author = comment.get("author") or comment.get("author_ip") or "알 수 없음"
        lines.append(
            f"{indent}[{comment.get('date', '')}] {author}: {comment.get('memo_text', '')}"
        )
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def render_comments(comments: list[dict[str, Any]]) -> str:
    if not comments:
        return '<p class="empty-comments">저장된 댓글이 없습니다.</p>'

    rendered: list[str] = []
    for comment in comments:
        author = html.escape(comment.get("author") or comment.get("author_ip") or "알 수 없음")
        date = html.escape(comment.get("date", ""))
        memo = comment.get("memo_html", "")
        depth = min(max(int(comment.get("depth", 0)), 0), 8)
        reply = '<span class="reply-mark">답글</span>' if depth else ""
        rendered.append(
            f'<article class="comment depth-{depth}">'
            f'<div class="comment-meta">{reply}<strong>{author}</strong><time>{date}</time></div>'
            f'<div class="comment-body">{memo}</div></article>'
        )
    return "".join(rendered)


def post_html(
    post: dict[str, Any],
    body_html: str,
    comments: list[dict[str, Any]],
    newer: dict[str, Any] | None = None,
    older: dict[str, Any] | None = None,
) -> str:
    title = html.escape(post["title"])
    author = html.escape(post["author"])
    date = html.escape(post["date"])
    category = html.escape(post.get("category", ""))
    source_url = html.escape(post["source_url"], quote=True)

    def adjacent_link(item: dict[str, Any] | None, label: str) -> str:
        if item is None:
            return '<span class="disabled">없음</span>'
        folder = Path(item["path"]).name
        href = html.escape(f"../{folder}/index.html", quote=True)
        adjacent_title = html.escape(item["title"])
        return f'<a href="{href}">{label}<span>{adjacent_title}</span></a>'

    newer_link = adjacent_link(newer, "← 최신 방향")
    older_link = adjacent_link(older, "과거 방향 →")
    rendered_comments = render_comments(comments)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ max-width: 960px; margin: 0 auto; padding: 28px 20px 60px; color: #202124; background: #fff; font: 16px/1.7 system-ui, sans-serif; }}
    a {{ color: #1a5fb4; overflow-wrap: anywhere; }}
    .top-nav {{ display: flex; justify-content: space-between; gap: 16px; margin-bottom: 28px; }}
    header {{ border-bottom: 1px solid #dfe3e8; margin-bottom: 32px; padding-bottom: 22px; }}
    h1 {{ margin: 12px 0; line-height: 1.35; }}
    .category {{ display: inline-block; padding: 3px 9px; border-radius: 999px; background: #eef4ff; color: #174ea6; font-weight: 700; }}
    .meta {{ color: #667085; }}
    main img, .comments img {{ display: block; max-width: 100%; height: auto; margin: 12px auto; object-fit: contain; }}
    main {{ min-height: 160px; overflow-wrap: anywhere; }}
    .comments {{ border-top: 1px solid #dfe3e8; margin-top: 48px; padding-top: 24px; }}
    .comments h2 {{ margin-bottom: 18px; }}
    .comment {{ border-top: 1px solid #eceff3; padding: 14px 4px; }}
    .comment.depth-1, .comment.depth-2, .comment.depth-3, .comment.depth-4,
    .comment.depth-5, .comment.depth-6, .comment.depth-7, .comment.depth-8 {{ margin-left: 32px; padding-left: 14px; border-left: 3px solid #d8e5f5; }}
    .comment-meta {{ display: flex; align-items: center; gap: 10px; color: #667085; font-size: 14px; }}
    .comment-meta strong {{ color: #202124; }}
    .reply-mark {{ color: #1a5fb4; font-weight: 700; }}
    .comment-body {{ margin-top: 6px; overflow-wrap: anywhere; }}
    .comment-body p {{ margin: 4px 0; }}
    .empty-comments, .disabled {{ color: #98a2b3; }}
    .post-nav {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; border-top: 1px solid #dfe3e8; margin-top: 36px; padding-top: 20px; }}
    .post-nav > * {{ min-width: 0; }}
    .post-nav a {{ display: flex; flex-direction: column; text-decoration: none; }}
    .post-nav a:last-child {{ text-align: right; }}
    .post-nav a span {{ color: #667085; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    @media (max-width: 640px) {{
      body {{ padding: 18px 14px 40px; }}
      .post-nav {{ grid-template-columns: 1fr; }}
      .post-nav a:last-child {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <nav class="top-nav"><a href="../../index.html">← 전체 리뷰 목록</a><a href="{source_url}">원문 보기</a></nav>
  <header>
    <span class="category">{category}</span>
    <h1>{title}</h1>
    <div class="meta">{author} · {date}</div>
  </header>
  <main>{body_html}</main>
  <section class="comments">
    <h2>댓글 {len(comments)}개</h2>
    {rendered_comments}
  </section>
  <nav class="post-nav"><div>{newer_link}</div><div>{older_link}</div></nav>
</body>
</html>
"""


def save_post(
    listed: dict[str, str],
    archive_root: Path,
    client: HttpClient,
    download_images: bool,
    include_comments: bool,
) -> tuple[dict[str, Any], list[str]]:
    source = client.get_html(listed["url"], referer=LIST_URL)
    parsed = parse_post_page(source, listed["url"])
    number = listed["number"]
    title = parsed["title"] or listed["title"]
    post_dir = archive_root / "posts" / f"{int(number):010d}_{safe_slug(title)}"
    post_dir.mkdir(parents=True, exist_ok=True)

    body_image_count, warnings = sanitize_and_download_images(
        parsed["body"],
        post_dir,
        client,
        listed["url"],
        download_images,
        asset_subdir="body",
    )
    body_html = inner_html(parsed["body"])
    text_content = normalized_body_text(parsed["body"])

    raw_comments: list[dict[str, Any]] = []
    reported_comment_count = parsed["reported_comment_count"]
    if include_comments:
        raw_comments, reported_comment_count = fetch_comments(client, parsed, number)
    comments, comment_image_count, comment_warnings = prepare_comments(
        raw_comments, post_dir, client, listed["url"], download_images
    )
    warnings.extend(comment_warnings)

    archived_at = datetime.now(timezone.utc).isoformat()
    relative_path = post_dir.relative_to(archive_root).as_posix()

    metadata = {
        "number": int(number),
        "category": parsed["category"],
        "title": title,
        "author": parsed["author"] or listed["author"],
        "author_uid": parsed["author_uid"],
        "author_ip": parsed["author_ip"],
        "date": parsed["date"] or listed["date"],
        "source_url": listed["url"],
        "archived_at": archived_at,
        "path": relative_path,
        "body_image_count": body_image_count,
        "comment_image_count": comment_image_count,
        "image_count": body_image_count + comment_image_count,
        "comment_count": len(comments),
        "reported_comment_count": reported_comment_count,
    }

    (post_dir / "content.txt").write_text(text_content, encoding="utf-8")
    (post_dir / "content.html").write_text(body_html, encoding="utf-8")
    (post_dir / "comments.txt").write_text(comments_text(comments), encoding="utf-8")
    (post_dir / "comments.json").write_text(
        json.dumps(comments, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (post_dir / "post.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (post_dir / "index.html").write_text(
        post_html(metadata, body_html, comments), encoding="utf-8"
    )
    return metadata, warnings


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "gallery_id": GALLERY_ID,
            "review_head_id": REVIEW_HEAD_ID,
            "posts": {},
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ArchiveError(f"기존 manifest.json을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(data.get("posts"), dict):
        raise ArchiveError("manifest.json의 posts 형식이 올바르지 않습니다")
    return data


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def write_archive_index(archive_root: Path, manifest: dict[str, Any]) -> None:
    posts = sorted(manifest["posts"].values(), key=lambda item: item["number"], reverse=True)
    rows = []
    for post in posts:
        local = html.escape(f"./{post['path']}/index.html", quote=True)
        title = html.escape(post["title"])
        category = html.escape(post.get("category", ""))
        author = html.escape(post.get("author", ""))
        date = html.escape(post.get("date", ""))
        comment_count = int(post.get("comment_count", 0))
        rows.append(
            f'<tr><td>{post["number"]}</td><td><span class="category">{category}</span></td>'
            f'<td><a href="{local}">{title}</a></td>'
            f"<td>{author}</td><td>{date}</td><td>{comment_count}</td></tr>"
        )
    generated = html.escape(manifest.get("updated_at", ""))
    page = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>하쿠슈 갤러리 리뷰 백업</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ max-width: 1180px; margin: 40px auto; padding: 0 20px; color: #202124; background: #fff; font: 15px/1.6 system-ui, sans-serif; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #dfe3e8; padding: 10px 8px; text-align: left; }}
    td:first-child {{ width: 7em; }}
    a {{ color: #1a5fb4; }}
    .meta {{ color: #667085; }}
    .category {{ display: inline-block; padding: 2px 8px; border-radius: 999px; background: #eef4ff; color: #174ea6; font-size: 13px; white-space: nowrap; }}
    @media (max-width: 760px) {{
      body {{ margin: 20px auto; padding: 0 12px; }}
      th:nth-child(1), td:nth-child(1), th:nth-child(4), td:nth-child(4) {{ display: none; }}
      th, td {{ padding: 9px 5px; }}
    }}
  </style>
</head>
<body>
  <h1>하쿠슈 갤러리 리뷰 백업</h1>
  <p class="meta">게시글 {len(posts)}개 · 갱신 {generated}</p>
  <table>
    <thead><tr><th>번호</th><th>말머리</th><th>제목</th><th>작성자</th><th>작성일</th><th>댓글</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body>
</html>
"""
    (archive_root / "index.html").write_text(page, encoding="utf-8")
    (archive_root / ".nojekyll").touch()


def write_all_post_pages(archive_root: Path, manifest: dict[str, Any]) -> None:
    posts = sorted(manifest["posts"].values(), key=lambda item: item["number"], reverse=True)
    for index, post in enumerate(posts):
        post_dir = archive_root / post["path"]
        content_path = post_dir / "content.html"
        comments_path = post_dir / "comments.json"
        if not content_path.exists() or not comments_path.exists():
            continue
        body_html = content_path.read_text(encoding="utf-8")
        comments = json.loads(comments_path.read_text(encoding="utf-8"))
        newer = posts[index - 1] if index > 0 else None
        older = posts[index + 1] if index + 1 < len(posts) else None
        (post_dir / "index.html").write_text(
            post_html(post, body_html, comments, newer=newer, older=older),
            encoding="utf-8",
        )


def read_cookie(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    except OSError as exc:
        raise ArchiveError(f"쿠키 파일을 읽을 수 없습니다: {exc}") from exc
    return "; ".join(line for line in lines if line and not line.startswith("#"))


def list_page_url(page: int) -> str:
    return LIST_URL + "?" + urlencode(
        {"id": GALLERY_ID, "search_head": REVIEW_HEAD_ID, "page": page, "list_num": 100}
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="디시인사이드 하쿠슈 갤러리의 리뷰 말머리 게시글만 백업합니다."
    )
    parser.add_argument("--output", type=Path, default=Path("docs"), help="백업 폴더")
    parser.add_argument("--start-page", type=int, default=1, help="시작 목록 페이지")
    parser.add_argument("--max-pages", type=int, default=0, help="처리할 페이지 수(0: 끝까지)")
    parser.add_argument("--limit-posts", type=int, default=0, help="최대 게시글 수(0: 제한 없음)")
    parser.add_argument("--delay", type=float, default=1.5, help="HTTP 요청 간 최소 지연(초)")
    parser.add_argument("--timeout", type=float, default=30.0, help="요청 제한 시간(초)")
    parser.add_argument("--retries", type=int, default=3, help="실패 시 재시도 횟수")
    parser.add_argument("--refresh", action="store_true", help="이미 저장된 글도 다시 받기")
    parser.add_argument("--no-images", action="store_true", help="본문 이미지를 저장하지 않기")
    parser.add_argument("--no-comments", action="store_true", help="댓글을 저장하지 않기")
    parser.add_argument("--cookie-file", type=Path, help="선택: Cookie 헤더 내용이 든 파일")
    return parser


def run(args: argparse.Namespace) -> int:
    if args.start_page < 1 or args.max_pages < 0 or args.limit_posts < 0:
        raise ArchiveError("페이지와 게시글 수 옵션은 0 이상의 정수여야 합니다")
    if args.delay < 0.5:
        raise ArchiveError("사이트 부하 방지를 위해 --delay는 0.5초 이상이어야 합니다")
    if args.timeout <= 0 or args.retries < 0:
        raise ArchiveError("--timeout은 양수, --retries는 0 이상이어야 합니다")

    archive_root = args.output.expanduser().resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    (archive_root / "posts").mkdir(exist_ok=True)
    manifest_path = archive_root / "manifest.json"
    manifest = load_manifest(manifest_path)
    refresh_existing = args.refresh or manifest.get("schema_version") != SCHEMA_VERSION
    client = HttpClient(args.delay, args.timeout, args.retries, read_cookie(args.cookie_file))

    added = 0
    skipped = 0
    failed = 0
    seen_pages: set[tuple[str, ...]] = set()
    page = args.start_page
    pages_read = 0
    stop = False

    while not stop and (args.max_pages == 0 or pages_read < args.max_pages):
        url = list_page_url(page)
        print(f"[목록] {page}페이지")
        listed_posts = parse_list_page(client.get_html(url), url)
        fingerprint = tuple(post["number"] for post in listed_posts)
        if not listed_posts or fingerprint in seen_pages:
            break
        seen_pages.add(fingerprint)
        pages_read += 1

        for listed in listed_posts:
            number = listed["number"]
            if not refresh_existing and number in manifest["posts"]:
                skipped += 1
                continue
            if args.limit_posts and added >= args.limit_posts:
                stop = True
                break

            print(f"  [저장] {number} {listed['title']}")
            try:
                metadata, warnings = save_post(
                    listed,
                    archive_root,
                    client,
                    download_images=not args.no_images,
                    include_comments=not args.no_comments,
                )
                manifest["posts"][number] = metadata
                write_manifest(manifest_path, manifest)
                added += 1
                for warning in warnings:
                    print(f"    [경고] {warning}", file=sys.stderr)
            except ArchiveError as exc:
                failed += 1
                print(f"    [실패] {exc}", file=sys.stderr)

        page += 1

    write_manifest(manifest_path, manifest)
    write_all_post_pages(archive_root, manifest)
    write_archive_index(archive_root, manifest)
    print(f"완료: 저장/갱신 {added}개, 기존 건너뜀 {skipped}개, 실패 {failed}개")
    print(f"목록: {archive_root / 'index.html'}")
    return 1 if failed else 0


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (ArchiveError, KeyboardInterrupt) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

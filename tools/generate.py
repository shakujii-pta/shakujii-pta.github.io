#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PTAホームページ ニュース自動生成スクリプト

Notion の DB_ニュース を読み取り、tools/template.html の
/*__NEWS_DATA__*/ プレースホルダに記事データ（NEWS 配列）を差し込んで
リポジトリ直下の index.html を生成する。

実行には環境変数 NOTION_TOKEN（Notion インテグレーションのトークン）が必要。
依存は Python 標準ライブラリ + Pillow のみ。

API 接続について（2026-11-26 実接続で検証済み）:
  - 本命: POST /v1/data_sources/{DATA_SOURCE_ID}/query
          Notion-Version: 2025-09-03、page_size 100 で全17件取得できた。
          プロパティは 名前(title) / タグ(multi_select) /
          作成日時(created_time。ページの created_time として返る) /
          サムネイル(files) が想定通り。
  - フォールバック: 上記が失敗した場合のみ
          POST /v1/databases/{DATABASE_ID}/query
          Notion-Version: 2022-06-28 で再試行する。
  - 本文: GET /v1/blocks/{page_id}/children（2025-09-03 で 200 を確認）。
          ページネーションと子ブロックの再帰取得を行う。
  - 認証: Authorization: Bearer ${NOTION_TOKEN}
"""

import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

# ---------------------------------------------------------------- 定数

DATA_SOURCE_ID = "22373cab-04f3-8170-92e8-000b6c581b38"
DATABASE_ID = "22373cab-04f3-8134-b25e-cc1a4fa9b720"  # フォールバック用
API_BASE = "https://api.notion.com/v1"
VERSION_PRIMARY = "2025-09-03"
VERSION_FALLBACK = "2022-06-28"

TITLE_PROP = "名前"
TAG_PROP = "タグ"
CREATED_PROP = "作成日時"
THUMBNAIL_PROP = "サムネイル"

TAG_CLASS = {"イベント": "sun", "レポート": "sky"}  # それ以外は ""
DEFAULT_TAG = "その他"

TZ_TOKYO = ZoneInfo("Asia/Tokyo")

IMAGE_MAX_WIDTH = 1280   # 現行データの本文画像は最大幅1280px（これを超えるものはない）
WEBP_QUALITY = 82        # 現行取り込み時の変換設定
EXCERPT_MAX = 88         # excerpt は88文字 + 「…」（絵文字も1文字として数える）

PLACEHOLDER = "/*__NEWS_DATA__*/"
REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "tools" / "template.html"
OUTPUT_PATH = REPO_ROOT / "index.html"

# ファイルリンク用アイコン（現行 index.html の実例そのまま）
FILE_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/>'
    '<path d="M14 3v6h6"/>'
    '<path d="M12 11v6M9 14.5 12 17.5l3-3"/></svg>'
)

USER_AGENT = "pta-site-sync/1.0 (+https://shakujii-pta.github.io/)"


# ---------------------------------------------------------------- HTTP

def http_json(url, token, version, method="GET", payload=None, retries=3):
    """Notion API を呼び、JSON を返す。429/5xx は簡易リトライ。"""
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": version,
        "User-Agent": USER_AGENT,
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(wait, 30))
                continue
            raise
    raise last_err


def download_bytes(url, retries=3):
    """ファイルをダウンロードして bytes を返す（テスト時に差し替え可能）。"""
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001 - リトライ後に再送出
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise last_err


class NotionClient:
    def __init__(self, token):
        self.token = token
        self.version = VERSION_PRIMARY

    def query_all_pages(self):
        """DB_ニュースの全ページを取得する（data_sources 本命、失敗時は databases）。"""
        try:
            pages = self._query(
                f"{API_BASE}/data_sources/{DATA_SOURCE_ID}/query", VERSION_PRIMARY
            )
            self.version = VERSION_PRIMARY
            return pages
        except urllib.error.HTTPError:
            pages = self._query(
                f"{API_BASE}/databases/{DATABASE_ID}/query", VERSION_FALLBACK
            )
            self.version = VERSION_FALLBACK
            return pages

    def _query(self, url, version):
        results = []
        cursor = None
        while True:
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            resp = http_json(url, self.token, version, method="POST", payload=payload)
            results.extend(resp.get("results", []))
            if resp.get("has_more"):
                cursor = resp.get("next_cursor")
            else:
                return results

    def fetch_blocks(self, block_id):
        """ブロックの子要素を全件取得し、has_children のものは再帰で _children に格納。"""
        blocks = []
        cursor = None
        while True:
            qs = {"page_size": 100}
            if cursor:
                qs["start_cursor"] = cursor
            url = f"{API_BASE}/blocks/{block_id}/children?" + urllib.parse.urlencode(qs)
            resp = http_json(url, self.token, self.version)
            blocks.extend(resp.get("results", []))
            if resp.get("has_more"):
                cursor = resp.get("next_cursor")
            else:
                break
        for block in blocks:
            if block.get("has_children"):
                block["_children"] = self.fetch_blocks(block["id"])
        return blocks


# ---------------------------------------------------------------- 画像

def image_to_webp_uri(data):
    """画像バイト列を WebP（quality 82、幅1280px上限）化して data URI にする。"""
    im = Image.open(io.BytesIO(data))
    if getattr(im, "is_animated", False):
        im.seek(0)
    if im.mode == "P":
        im = im.convert("RGBA" if "transparency" in im.info else "RGB")
    elif im.mode in ("LA", "RGBa"):
        im = im.convert("RGBA")
    elif im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    if im.width > IMAGE_MAX_WIDTH:
        new_h = round(im.height * IMAGE_MAX_WIDTH / im.width)
        im = im.resize((IMAGE_MAX_WIDTH, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=WEBP_QUALITY)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def bytes_to_octet_uri(data):
    return "data:application/octet-stream;base64," + base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------- テキスト変換

def escape_html(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(s):
    return escape_html(s).replace('"', "&quot;")


def trim_rich_text(rts):
    """rich_text 配列の前後にある改行を取り除く。

    実例: paragraph の rich_text 先頭に "\\n" が含まれるケースがあるため、
    変換前に先頭・末尾の改行（\\n / \\r）を除去する。
    """
    items = [dict(rt) for rt in rts]
    while items:
        t = items[0].get("plain_text", "")
        stripped = t.lstrip("\r\n")
        if stripped == t:
            break
        if stripped:
            items[0]["plain_text"] = stripped
            break
        items.pop(0)
    while items:
        t = items[-1].get("plain_text", "")
        stripped = t.rstrip("\r\n")
        if stripped == t:
            break
        if stripped:
            items[-1]["plain_text"] = stripped
            break
        items.pop()
    return items


def rich_text_to_html(rts):
    """Notion rich_text を現行 body 形式のインライン HTML に変換する。

    入れ子の順序は現行データの実例に合わせる:
      リンクが最外、次に <b>、<u>、<i>（<b><u>…</u></b>、<a …><b>…</b></a> を確認済み）。
      strikethrough / code は現行データに実例がないため標準的な位置に入れている。
    テキスト中の改行は <br> になる。
    """
    out = []
    for rt in rts:
        text = escape_html(rt.get("plain_text", "")).replace("\n", "<br>")
        ann = rt.get("annotations", {}) or {}
        if ann.get("code"):
            text = f"<code>{text}</code>"
        if ann.get("strikethrough"):
            text = f"<s>{text}</s>"
        if ann.get("italic"):
            text = f"<i>{text}</i>"
        if ann.get("underline"):
            text = f"<u>{text}</u>"
        if ann.get("bold"):
            text = f"<b>{text}</b>"
        href = rt.get("href")
        if href:
            text = f'<a href="{escape_attr(href)}" target="_blank" rel="noopener">{text}</a>'
        out.append(text)
    return "".join(out)


def rich_text_plain(rts):
    return "".join(rt.get("plain_text", "") for rt in rts)


# ---------------------------------------------------------------- ブロック変換

class ArticleBuilder:
    """1記事分のブロック列を body HTML と excerpt 素材に変換する。"""

    def __init__(self, title):
        self.title = title
        self.image_count = 0
        self.first_image_uri = None
        self.text_segments = []  # excerpt 用のプレーンテキスト（ブロック単位）

    # -- 画像・ファイル・動画 -------------------------------------------

    def _media_url(self, media):
        """file / external のメディアオブジェクトから URL を取る。"""
        mtype = media.get("type")
        if mtype == "file":
            return media.get("file", {}).get("url")
        if mtype == "external":
            return media.get("external", {}).get("url")
        return None

    def _file_name(self, block, media):
        btype = block["type"]
        for cand in (block.get(btype, {}), media):
            if isinstance(cand, dict) and cand.get("name"):
                return cand["name"]
        url = self._media_url(media) or ""
        name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
        return name or "ファイル"

    def convert_image(self, block):
        media = block.get("image", {})
        url = self._media_url(media)
        if not url:
            return None
        uri = image_to_webp_uri(download_bytes(url))
        self.image_count += 1
        if self.first_image_uri is None:
            self.first_image_uri = uri
        alt = escape_attr(f"{self.title}の写真{self.image_count}")
        return f'<img src="{uri}" alt="{alt}" loading="lazy">'

    def convert_file(self, block):
        media = block.get(block["type"], {})
        url = self._media_url(media)
        if not url:
            return None
        uri = bytes_to_octet_uri(download_bytes(url))
        name = escape_html(self._file_name(block, media))
        return (
            f'<a class="file-link" href="{uri}" download>{FILE_ICON_SVG}'
            f"<span>{name}</span></a>"
        )

    def convert_video(self, block):
        media = block.get("video", {})
        if media.get("type") == "file":
            url = self._media_url(media)
            if not url:
                return None
            uri = bytes_to_octet_uri(download_bytes(url))
            return (
                '<video class="article-video" controls playsinline '
                f'preload="metadata" src="{uri}"></video>'
            )
        # 外部動画（YouTube等）は現行データに実例がないためリンク段落にフォールバック
        url = self._media_url(media)
        if url:
            return (
                f'<p><a href="{escape_attr(url)}" target="_blank" '
                f'rel="noopener">{escape_html(url)}</a></p>'
            )
        return None

    # -- テキスト系ブロック ----------------------------------------------

    def _text_block(self, block, tag):
        rts = trim_rich_text(block.get(block["type"], {}).get("rich_text", []))
        plain = rich_text_plain(rts)
        if plain:
            self.text_segments.append(plain)
        if not plain:
            return None
        return f"<{tag}>{rich_text_to_html(rts)}</{tag}>"

    # -- メイン変換 --------------------------------------------------------

    def convert_blocks(self, blocks):
        """ブロック列を HTML ブロック文字列のリストに変換する。"""
        out = []
        i = 0
        while i < len(blocks):
            block = blocks[i]
            btype = block.get("type")

            if btype == "image":
                # 連続画像は gallery にまとめる（現行実例は3枚以上、2枚も gallery とする）
                run = []
                while i < len(blocks) and blocks[i].get("type") == "image":
                    img = self.convert_image(blocks[i])
                    if img:
                        run.append(f"<figure>{img}</figure>")
                    i += 1
                if len(run) >= 2:
                    out.append('<div class="gallery">' + "".join(run) + "</div>")
                elif run:
                    single = run[0].replace("<figure>", '<figure class="article-photo">', 1)
                    out.append(single)
                continue

            if btype in ("bulleted_list_item", "numbered_list_item"):
                html, i = self._convert_list(blocks, i, btype)
                if html:
                    out.append(html)
                continue

            html = self.convert_block(block)
            if html:
                out.append(html)
            i += 1
        return out

    def _convert_list(self, blocks, start, list_type):
        tag = "ul" if list_type == "bulleted_list_item" else "ol"
        items = []
        i = start
        num = 0
        while i < len(blocks) and blocks[i].get("type") == list_type:
            block = blocks[i]
            num += 1
            rts = trim_rich_text(block.get(list_type, {}).get("rich_text", []))
            plain = rich_text_plain(rts)
            if plain:
                seg = f"{num}. {plain}" if list_type == "numbered_list_item" else plain
                self.text_segments.append(seg)
            inner = rich_text_to_html(rts)
            # 子ブロック（入れ子リスト等）は現行データに実例がないため、
            # 通常のブロック変換をそのまま <li> 内に埋め込む
            children = block.get("_children", [])
            if children:
                inner += "".join(self.convert_blocks(children))
            items.append(f"<li>{inner}</li>")
            i += 1
        if items:
            return f"<{tag}>" + "".join(items) + f"</{tag}>", i
        return None, i

    def convert_block(self, block):
        btype = block.get("type")

        if btype == "paragraph":
            return self._text_block(block, "p")
        # 見出し対応は実例で確認済み: heading_2→<h2>、heading_3→<h3>。
        # heading_1 は現行未確認のため <h2> に寄せる。
        if btype in ("heading_1", "heading_2"):
            return self._text_block(block, "h2")
        if btype == "heading_3":
            return self._text_block(block, "h3")
        if btype == "divider":
            return "<hr>"
        if btype in ("file", "pdf", "audio"):
            return self.convert_file(block)
        if btype == "video":
            return self.convert_video(block)

        # ---- 以下は現行17件に実例がないブロック種別のフォールバック ----
        if btype in ("quote", "callout", "toggle"):
            html = self._text_block(block, "p")
            parts = [html] if html else []
            parts.extend(self.convert_blocks(block.get("_children", [])))
            return "\n".join(parts) if parts else None
        if btype in ("column_list", "column", "synced_block"):
            parts = self.convert_blocks(block.get("_children", []))
            return "\n".join(parts) if parts else None
        if btype == "table":
            rows = []
            for row in block.get("_children", []):
                if row.get("type") != "table_row":
                    continue
                cells = row.get("table_row", {}).get("cells", [])
                cell_html = " ".join(rich_text_to_html(c) for c in cells)
                cell_plain = " ".join(rich_text_plain(c) for c in cells)
                if cell_plain:
                    self.text_segments.append(cell_plain)
                rows.append(f"<p>{cell_html}</p>")
            return "\n".join(rows) if rows else None
        if btype in ("bookmark", "embed"):
            url = block.get(btype, {}).get("url", "")
            if url:
                return (
                    f'<p><a href="{escape_attr(url)}" target="_blank" '
                    f'rel="noopener">{escape_html(url)}</a></p>'
                )
            return None
        if btype == "code":
            raw = rich_text_plain(block.get("code", {}).get("rich_text", []))
            if raw:
                self.text_segments.append(raw)
                return "<p>" + escape_html(raw).replace("\n", "<br>") + "</p>"
            return None
        if btype == "equation":
            expr = block.get("equation", {}).get("expression", "")
            if expr:
                self.text_segments.append(expr)
                return f"<p>{escape_html(expr)}</p>"
            return None
        # child_page / link_to_page / breadcrumb 等は出力しない
        return None


# ---------------------------------------------------------------- ページ→記事

def page_title(page):
    props = page.get("properties", {})
    prop = props.get(TITLE_PROP)
    if prop and prop.get("type") == "title":
        return rich_text_plain(prop.get("title", []))
    for prop in props.values():
        if prop.get("type") == "title":
            return rich_text_plain(prop.get("title", []))
    return ""


def page_tag(page):
    prop = page.get("properties", {}).get(TAG_PROP, {})
    if prop.get("type") == "multi_select":
        opts = prop.get("multi_select", [])
        if opts:
            return opts[0].get("name", "") or DEFAULT_TAG
    elif prop.get("type") == "select":
        sel = prop.get("select")
        if sel:
            return sel.get("name", "") or DEFAULT_TAG
    return DEFAULT_TAG


def page_created(page):
    """作成日時を aware datetime で返す。

    作成日時プロパティは created_time 型で、ページの created_time として返る。
    プロパティ側に値があればそれを優先し、なければページの created_time を使う。
    """
    prop = page.get("properties", {}).get(CREATED_PROP, {})
    raw = None
    if prop.get("type") == "created_time":
        raw = prop.get("created_time")
    elif prop.get("type") == "date" and prop.get("date"):
        raw = prop["date"].get("start")
    if not raw:
        raw = page.get("created_time")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_TOKYO)
    return dt


def page_thumbnail_uri(page):
    """サムネイルプロパティ（files）の先頭画像を WebP data URI にする。なければ None。"""
    prop = page.get("properties", {}).get(THUMBNAIL_PROP, {})
    files = prop.get("files", []) if prop.get("type") == "files" else []
    for f in files:
        url = None
        if f.get("type") == "file":
            url = f.get("file", {}).get("url")
        elif f.get("type") == "external":
            url = f.get("external", {}).get("url")
        if url:
            return image_to_webp_uri(download_bytes(url))
    return None


def make_excerpt(segments):
    text = re.sub(r"\s+", " ", " ".join(segments)).strip()
    if len(text) > EXCERPT_MAX:
        return text[:EXCERPT_MAX] + "…"
    return text


def page_to_article(client, page):
    notion_id = page["id"]
    title = page_title(page)
    tag = page_tag(page)
    created = page_created(page)
    created_jst = created.astimezone(TZ_TOKYO)

    blocks = client.fetch_blocks(notion_id)
    builder = ArticleBuilder(title)
    body = "\n".join(builder.convert_blocks(blocks))

    img = page_thumbnail_uri(page)
    if img is None:
        img = builder.first_image_uri

    return {
        "id": "n" + notion_id.replace("-", ""),
        "notionId": notion_id,
        "date": created_jst.strftime("%Y-%m-%d"),
        "dateLabel": f"{created_jst.year}年{created_jst.month}月{created_jst.day}日",
        "tag": tag,
        "tagClass": TAG_CLASS.get(tag, ""),
        "title": title,
        "excerpt": make_excerpt(builder.text_segments),
        "img": img,
        "imgAlt": f"{title}の写真" if img else "",
        "body": body,
        "_created": created,
    }


# ---------------------------------------------------------------- シリアライズ

FIELD_ORDER = [
    "id", "notionId", "date", "dateLabel", "tag", "tagClass",
    "title", "excerpt", "img", "imgAlt", "body",
]


def serialize_news(articles):
    """記事 dict のリストを現行 index.html と同じ形式の JS 配列文字列にする。"""
    lines = ["["]
    for a in articles:
        lines.append("  {")
        for idx, key in enumerate(FIELD_ORDER):
            value = a.get(key)
            rendered = "null" if value is None else json.dumps(value, ensure_ascii=False)
            comma = "," if idx < len(FIELD_ORDER) - 1 else ""
            lines.append(f"    {key}: {rendered}{comma}")
        lines.append("  },")
    lines.append("]")
    return "\n".join(lines)


def parse_news_literal(literal):
    """現行 index.html の NEWS 配列リテラルを Python オブジェクトに戻す（selfcheck 用）。"""
    text = re.sub(
        r"(?m)^(\s{4})(id|notionId|date|dateLabel|tag|tagClass|title|excerpt|img|imgAlt|body):",
        r'\1"\2":',
        literal,
    )
    text = re.sub(r",(\s*\])", r"\1", text)
    return json.loads(text)


def extract_news_literal(html_text):
    """index.html 文字列から NEWS 配列リテラルの (開始, 終了) 位置と文字列を返す。"""
    start = html_text.index("var NEWS = [")
    arr_start = html_text.index("[", start)
    end = html_text.index("\n];", start)
    arr_end = end + 2  # ']' の直後
    return arr_start, arr_end, html_text[arr_start:arr_end]


def build_site(template_text, articles):
    return template_text.replace(PLACEHOLDER, serialize_news(articles))


# ---------------------------------------------------------------- メイン

def main():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("エラー: 環境変数 NOTION_TOKEN が設定されていません", file=sys.stderr)
        return 1
    client = NotionClient(token)
    pages = client.query_all_pages()
    articles = [page_to_article(client, p) for p in pages]
    articles.sort(key=lambda a: a["_created"], reverse=True)
    for a in articles:
        a.pop("_created", None)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        print(f"エラー: テンプレートに {PLACEHOLDER} がありません", file=sys.stderr)
        return 1
    OUTPUT_PATH.write_text(build_site(template, articles), encoding="utf-8", newline="")
    print(f"記事 {len(articles)} 件で index.html を生成しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())

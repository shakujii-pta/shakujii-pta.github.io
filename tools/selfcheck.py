#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自己検証スクリプト

現行 index.html と tools/template.html / tools/generate.py の整合性を確認する。

チェック内容:
  1. template.html が、現行 index.html の NEWS 配列部分を
     /*__NEWS_DATA__*/ に置換したものとバイト単位で一致すること。
  2. テンプレート + 現行 NEWS 配列リテラル で index.html が再現できること。
  3. NEWS 配列リテラルをパースして generate.py のシリアライザで再生成すると、
     元のリテラルとバイト単位で一致すること（＝生成形式が現行と同一）。

使い方: python3 tools/selfcheck.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def first_diff(a, b):
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    if len(a) != len(b):
        return n
    return -1


def show_diff(name, expected, actual):
    i = first_diff(expected, actual)
    print(f"  [{name}] 不一致: 先頭からの位置 {i}")
    print(f"    期待: ...{expected[max(0, i - 60):i + 60]!r}")
    print(f"    実際: ...{actual[max(0, i - 60):i + 60]!r}")
    print(f"    長さ: 期待 {len(expected)} / 実際 {len(actual)}")


def main():
    index_bytes = (REPO_ROOT / "index.html").read_bytes()
    template_bytes = (REPO_ROOT / "tools" / "template.html").read_bytes()
    index_text = index_bytes.decode("utf-8")
    template_text = template_bytes.decode("utf-8")

    _, _, literal = generate.extract_news_literal(index_text)
    ok = True

    # 1. テンプレートが「index.html の配列部分をプレースホルダに置換したもの」か
    expected_template = index_text.replace(literal, generate.PLACEHOLDER, 1)
    if expected_template == template_text:
        print("  [template] OK: 配列部分をプレースホルダに置換したものと一致")
    else:
        ok = False
        show_diff("template", expected_template, template_text)

    # 2. テンプレート + 現行リテラル → index.html 再現
    rebuilt = template_text.replace(generate.PLACEHOLDER, literal, 1)
    if rebuilt.encode("utf-8") == index_bytes:
        print("  [rebuild] OK: テンプレートから現行 index.html をバイト単位で再現")
    else:
        ok = False
        show_diff("rebuild", index_text, rebuilt)

    # 3. パース → 再シリアライズ → 元リテラルと一致
    articles = generate.parse_news_literal(literal)
    reserialized = generate.serialize_news(articles)
    if reserialized == literal:
        print(f"  [serialize] OK: 全{len(articles)}記事の再シリアライズが元と一致")
    else:
        ok = False
        show_diff("serialize", literal, reserialized)

    print("SELFCHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

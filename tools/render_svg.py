#!/usr/bin/env python3
"""Claude が描いたイラスト（SVG）をチェックし、見た目を確かめるための画像（PNG）を書き出す。

書き出した PNG はサイトには使わない（サイトには SVG をそのまま載せる）。
ブラウザ（screenshot.py と同じ仕組み）で表示して撮るので、サイトで見えるのと同じ見た目になる。

使い方:
  python tools/render_svg.py images/news/cleanup.svg
  → /tmp/issue-images/check-cleanup.png に書き出す（出力先は第2引数で変更可）
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from build import check_svg  # noqa: E402

WIDTH = 1200  # 書き出す画像の横幅


def launch(playwright):
    errors = []
    for options in ({"channel": "chrome"}, {}):
        try:
            return playwright.chromium.launch(**options)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc).splitlines()[0])
    raise RuntimeError(" / ".join(errors))


def aspect_height(svg_path: Path) -> int:
    """viewBox の縦横の比から、書き出す画像の高さを決める。"""
    match = re.search(r'viewBox\s*=\s*["\']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', svg_path.read_text(encoding="utf-8", errors="replace"))
    if not match or float(match.group(1)) <= 0:
        return round(WIDTH * 9 / 16)
    return max(1, round(WIDTH * float(match.group(2)) / float(match.group(1))))


def main() -> int:
    if len(sys.argv) < 2:
        print("使い方: python tools/render_svg.py images/news/ファイル名.svg [書き出し先.png]")
        return 1
    src = Path(sys.argv[1])
    if not src.is_absolute():
        src = ROOT / src
    if not src.is_file() or src.suffix.lower() != ".svg":
        print(f"[エラー] SVGファイルが見つかりません: {sys.argv[1]}")
        return 1
    try:
        src.resolve().relative_to((ROOT / "images").resolve())
    except ValueError:
        print("[エラー] images/ の中のSVGだけを変換できます")
        return 1

    errors = check_svg(src)
    if errors:
        for message in errors:
            print(f"[エラー] {message}")
        print("問題を直してから、もう一度実行してください。")
        return 1

    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp/issue-images") / f"check-{src.stem}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[注意] 画像を書き出す部品（playwright）が入っていないため、書き出せませんでした。SVGのチェックは通っています。")
        return 0

    height = aspect_height(src)
    with sync_playwright() as p:
        try:
            browser = launch(p)
        except RuntimeError as exc:
            print(f"[注意] ブラウザを起動できなかったため、書き出せませんでした（{exc}）。SVGのチェックは通っています。")
            return 0
        tab = browser.new_page(viewport={"width": WIDTH, "height": height})
        # サイトと同じく <img> として表示する（SVGの中の仕組みは動かない）
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "check.html"
            page.write_text(
                "<!doctype html><html><body style='margin:0;background:#fff'>"
                f"<img src='{src.resolve().as_uri()}' style='display:block;width:{WIDTH}px;height:{height}px'>"
                "</body></html>",
                encoding="utf-8",
            )
            tab.goto(page.as_uri())
            tab.wait_for_load_state("load")
        loaded = tab.evaluate("document.images[0].complete && document.images[0].naturalWidth > 0")
        if not loaded:
            browser.close()
            print("[エラー] ブラウザでSVGを表示できませんでした。書き方を見直してください。")
            return 1
        tab.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": WIDTH, "height": height})
        browser.close()

    print(f"チェックOK。確認用の画像を書き出しました: {out}")
    print("この画像を Read で開いて、見た目を確かめてください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

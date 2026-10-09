#!/usr/bin/env python3
"""確認用のサイトを作り、パソコンとスマートフォンの画面の画像を撮る。

デザイン（CSS）を変えるときに、変える前と変えた後の見た目を確かめるために使う。
撮った画像は /tmp/issue-images/screens/ に保存する（Read で開いて見る）。

使い方:
  python tools/screenshot.py before          変える前に撮る
  python tools/screenshot.py after           変えた後に撮る
  python tools/screenshot.py after news/xxx.html jobs/yyy.html   撮るページを指定する
撮るページを指定しないときは、トップ・お知らせの記事1件・求人1件を撮る。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
OUT = Path("/tmp/issue-images/screens")
SIZES = {"pc": (1280, 900), "sp": (390, 844)}
MAX_HEIGHT = 3200  # 長いページは上から 3200px までを撮る
LABEL_RE = re.compile(r"^[a-z0-9-]+$")


def default_pages() -> list[str]:
    pages = ["index.html"]
    for folder in ("news", "jobs"):
        items = sorted(p for p in (DIST / folder).glob("*.html") if p.name != "index.html")
        if items:
            pages.append(f"{folder}/{items[-1].name}")
    return pages


def launch(playwright):
    errors = []
    for options in ({"channel": "chrome"}, {}):
        try:
            return playwright.chromium.launch(**options)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc).splitlines()[0])
    raise RuntimeError(" / ".join(errors))


def main() -> int:
    if len(sys.argv) < 2 or not LABEL_RE.match(sys.argv[1]):
        print("使い方: python tools/screenshot.py before（または after）[撮るページ…]")
        return 1
    label = sys.argv[1]

    try:
        return take(label)
    finally:
        shutil.rmtree(DIST, ignore_errors=True)


def take(label: str) -> int:
    result = subprocess.run([sys.executable, str(ROOT / "build.py"), "--preview"], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout + result.stderr)
        print("[エラー] サイトを作れなかったため、画面を撮れませんでした。上の内容を直してください。")
        return 1

    pages = sys.argv[2:] or default_pages()
    for page in pages:
        if not (DIST / page).is_file():
            print(f"[エラー] ページが見つかりません: {page}（例: index.html、news/ファイル名.html）")
            return 1

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[注意] 画面を撮る部品（playwright）が入っていないため、撮れませんでした。")
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    saved = []
    with sync_playwright() as p:
        try:
            browser = launch(p)
        except RuntimeError as exc:
            print(f"[注意] ブラウザを起動できなかったため、撮れませんでした（{exc}）。")
            return 0
        for page_path in pages:
            for size_name, (width, height) in SIZES.items():
                tab = browser.new_page(viewport={"width": width, "height": height})
                tab.goto((DIST / page_path).as_uri())
                tab.wait_for_load_state("load")
                full = tab.evaluate("document.documentElement.scrollHeight")
                overflow = tab.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                name = f"{label}-{page_path.replace('/', '_').removesuffix('.html')}-{size_name}.png"
                tab.screenshot(path=str(OUT / name), full_page=True, clip={"x": 0, "y": 0, "width": width, "height": min(full, MAX_HEIGHT)})
                note = "  ※横にはみ出しています（スマートフォンで横スクロールが出る）" if overflow else ""
                saved.append(f"{OUT / name}{note}")
                tab.close()
        browser.close()

    print("画面を撮りました。Read で開いて見た目を確かめてください。")
    for line in saved:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

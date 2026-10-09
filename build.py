#!/usr/bin/env python3
"""サイト生成スクリプト

content/ にあるお知らせ・求人のデータ（Markdown）と、templates/ のHTMLテンプレートから、
サーバーにアップロードするHTMLを dist/ フォルダに書き出します。

使い方:
  python build.py            本番用を生成する
  python build.py --preview  確認用を生成する（検索エンジンに載らない設定、確認用の帯、下書きや公開予定の記事も表示）
  python build.py --check    データのチェックだけ行う（HTMLは書き出さない）
  python build.py --weekday 2026-12-29 2027-01-04   日付の曜日を確かめる
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader, TemplateError, select_autoescape

ROOT = Path(__file__).resolve().parent
CONTENT_DIR = ROOT / "content"
TEMPLATE_DIR = ROOT / "templates"
DIST_DIR = ROOT / "dist"
STATIC_DIRS = ["assets", "images"]  # そのまま dist/ にコピーするフォルダ
JST = ZoneInfo("Asia/Tokyo")

STATUS_PUBLISHED = "公開"
STATUS_DRAFT = "下書き"
STATUS_HIDDEN = "非公開"
VALID_STATUSES = {STATUS_PUBLISHED, STATUS_DRAFT, STATUS_HIDDEN}

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BODY_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((images/[^)\s]+)")
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n(.*))?\Z", re.S)

NEWS_FIELDS = {"title", "date", "status", "category", "summary", "image", "image_alt", "end_date"}
NEWS_CATEGORIES = ["お知らせ", "休業案内", "イベント", "メディア掲載", "採用"]

# 求人の募集要項（表示順）。(データ上の名前, 表示名, 必須かどうか)
JOB_FIELDS = [
    ("employment_type", "雇用形態", True),
    ("location", "勤務地", True),
    ("location_change_scope", "勤務地の変更の範囲", True),
    ("duty_change_scope", "業務の変更の範囲", True),
    ("contract_period", "契約期間", True),
    ("probation", "試用期間", False),
    ("hours", "勤務時間", True),
    ("overtime", "時間外労働", False),
    ("holidays", "休日・休暇", True),
    ("salary", "給与", True),
    ("fixed_overtime", "固定残業代", False),
    ("allowances", "手当", False),
    ("insurance", "加入保険", True),
    ("smoking", "受動喫煙対策", True),
    ("requirements", "応募資格", True),
    ("benefits", "待遇・福利厚生", False),
    ("selection", "選考の流れ", False),
    ("apply", "応募方法", True),
]
JOB_BASE_FIELDS = {"title", "date", "status", "end_date", "summary", "image", "image_alt"}
JOB_ALL_FIELDS = JOB_BASE_FIELDS | {key for key, _, _ in JOB_FIELDS}


@dataclass
class Entry:
    kind: str  # "news" または "jobs"
    slug: str
    path: Path
    meta: dict
    body_md: str
    title: str = ""
    date: dt.date | None = None
    end_date: dt.date | None = None
    status: str = ""
    body_html: str = ""
    state: str = ""  # published / scheduled / expired / draft / hidden
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"{self.kind}/{self.slug}.html"

    @property
    def label(self) -> str:
        return str(self.path.relative_to(ROOT))


def today_jst() -> dt.date:
    return dt.datetime.now(JST).date()


def parse_date(value, field_name: str, errors: list[str]) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", str(value).strip())
    try:
        if not match:
            raise ValueError
        return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        errors.append(f"{field_name} の日付「{value}」が読み取れません。2026-10-07 の形で書いてください。")
        return None


def read_entry(kind: str, path: Path) -> Entry:
    raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = FRONT_MATTER_RE.match(raw)
    entry = Entry(kind=kind, slug=path.stem, path=path, meta={}, body_md="")
    if not match:
        entry.errors.append("ファイルの先頭が「---」で始まる項目欄になっていません。")
        return entry
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        entry.errors.append(f"項目欄の書き方に誤りがあります: {exc}")
        return entry
    if not isinstance(meta, dict):
        entry.errors.append("項目欄の書き方に誤りがあります。")
        return entry
    entry.meta = meta
    entry.body_md = (match.group(2) or "").strip()
    return entry


def validate(entry: Entry) -> None:
    meta, errors = entry.meta, entry.errors
    if not SLUG_RE.match(entry.slug):
        errors.append("ファイル名は半角の英小文字・数字・ハイフンだけにしてください（例: 2026-10-07-autumn-holiday.md）。")

    entry.title = str(meta.get("title") or "").strip()
    if not entry.title:
        errors.append("title（タイトル）がありません。")

    entry.status = str(meta.get("status") or "").strip()
    if entry.status not in VALID_STATUSES:
        errors.append(f"status は「公開」「下書き」「非公開」のどれかにしてください（今は「{entry.status}」）。")

    entry.date = parse_date(meta.get("date"), "date（公開日）", errors)
    if entry.date is None and not any("date（公開日）" in e for e in errors):
        errors.append("date（公開日）がありません。")
    entry.end_date = parse_date(meta.get("end_date"), "end_date（掲載終了日）", errors)
    if entry.date and entry.end_date and entry.end_date < entry.date:
        errors.append("end_date（掲載終了日）が date（公開日）より前になっています。")

    image = meta.get("image")
    if image:
        image_path = ROOT / str(image)
        if not str(image).startswith("images/"):
            errors.append("image は images/ から始まるパスで書いてください（例: images/news/photo.jpg）。")
        elif not image_path.is_file():
            errors.append(f"image に指定した画像「{image}」が見つかりません。")
        if not meta.get("image_alt"):
            entry.warnings.append("image_alt（画像の説明文）がありません。")

    for ref in sorted(set(BODY_IMAGE_RE.findall(entry.body_md))):
        if not (ROOT / ref).is_file():
            errors.append(f"本文中の画像「{ref}」が見つかりません。")

    allowed = NEWS_FIELDS if entry.kind == "news" else JOB_ALL_FIELDS
    for key in meta:
        if key not in allowed:
            entry.warnings.append(f"「{key}」という項目は使われません（書き間違いかもしれません）。")

    if entry.kind == "news":
        category = meta.get("category")
        if category and category not in NEWS_CATEGORIES:
            entry.warnings.append(f"category「{category}」は決められた種類（{'・'.join(NEWS_CATEGORIES)}）にありません。")
        if not entry.body_md:
            errors.append("本文がありません。")
    else:
        for key, label, required in JOB_FIELDS:
            if required and not str(meta.get(key) or "").strip():
                errors.append(f"{key}（{label}）がありません。")
        if not entry.body_md:
            errors.append("本文（仕事内容）がありません。")


def decide_state(entry: Entry, today: dt.date) -> None:
    if entry.status == STATUS_HIDDEN:
        entry.state = "hidden"
    elif entry.status == STATUS_DRAFT:
        entry.state = "draft"
    elif entry.date and entry.date > today:
        entry.state = "scheduled"
    elif entry.end_date and entry.end_date < today:
        entry.state = "expired"
    else:
        entry.state = "published"


def rewrite_relative_urls(body_html: str, root: str) -> str:
    """本文中のリンク・画像は「サイトのトップからのパス」で書く決まりなので、ページの階層に合わせて直す。"""

    def fix(match: re.Match) -> str:
        attr, quote, url = match.group(1), match.group(2), match.group(3)
        if re.match(r"^(?:[a-z]+:|//|/|#|\.\./|\./)", url):
            return match.group(0)
        return f"{attr}={quote}{root}{url}{quote}"

    return re.sub(r'\b(src|href)=(["\'])([^"\']*)\2', fix, body_html)


def render_markdown(text: str, root: str) -> str:
    md = markdown.Markdown(extensions=["extra", "nl2br", "sane_lists"])
    return rewrite_relative_urls(md.convert(text), root)


def nl2br(value) -> str:
    if value is None:
        return ""
    escaped = html.escape(str(value).strip())
    return escaped.replace("\n", "<br>\n")


def format_date(value: dt.date | None, style: str = "dot") -> str:
    if not value:
        return ""
    if style == "ja":
        return f"{value.year}年{value.month}月{value.day}日"
    return value.strftime("%Y.%m.%d")


def load_site() -> tuple[dict, list[str]]:
    errors: list[str] = []
    try:
        site = yaml.safe_load((ROOT / "site.yml").read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return {}, [f"書き方に誤りがあります: {exc}"]
    for key in ("name", "tagline", "contact", "recruit_contact", "company"):
        if not site.get(key):
            errors.append(f"{key} がありません。")
    image = site.get("hero_image")
    if image:
        if not str(image).startswith("images/"):
            errors.append("hero_image は images/ から始まるパスで書いてください（例: images/site/top.jpg）。")
        elif not (ROOT / str(image)).is_file():
            errors.append(f"hero_image に指定した画像「{image}」が見つかりません。")
    return site, errors


def collect(kind: str, today: dt.date) -> list[Entry]:
    folder = CONTENT_DIR / kind
    entries = []
    for path in sorted(folder.glob("*.md")):
        entry = read_entry(kind, path)
        if not entry.errors:
            validate(entry)
        if not entry.errors:
            decide_state(entry, today)
        entries.append(entry)
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description="サイトを生成します")
    parser.add_argument("--preview", action="store_true", help="確認用として生成する")
    parser.add_argument("--check", action="store_true", help="データのチェックだけ行う")
    parser.add_argument("--today", help="今日の日付を指定して試す（例: 2026-12-01）")
    parser.add_argument("--weekday", nargs="+", metavar="DATE", help="日付の曜日を表示する（例: 2026-12-29）")
    args = parser.parse_args()

    weekdays = "月火水木金土日"
    if args.weekday:
        for text in args.weekday:
            errors: list[str] = []
            day = parse_date(text, "日付", errors)
            print(f"{text}: {day.year}年{day.month}月{day.day}日（{weekdays[day.weekday()]}）" if day else errors[0])
        return 0

    today = dt.date.fromisoformat(args.today) if args.today else today_jst()
    print(f"今日の日付（日本時間）: {today.year}年{today.month}月{today.day}日（{weekdays[today.weekday()]}）")
    news = collect("news", today)
    jobs = collect("jobs", today)
    all_entries = news + jobs

    for kind, items in (("news", news), ("jobs", jobs)):
        seen: dict[str, Entry] = {}
        for item in items:
            if item.slug in seen:
                item.errors.append(f"{seen[item.slug].label} と同じファイル名です。")
            seen[item.slug] = item

    site, site_errors = load_site()
    has_error = False
    for message in site_errors:
        has_error = True
        print(f"[エラー] site.yml: {message}")
    for entry in all_entries:
        for message in entry.errors:
            has_error = True
            print(f"[エラー] {entry.label}: {message}")
        for message in entry.warnings:
            print(f"[注意]   {entry.label}: {message}")
    if has_error:
        print("\nエラーがあるため、ページを生成しませんでした。上の内容を直してください。")
        return 1

    state_names = {"published": "公開中", "scheduled": "公開予定", "expired": "掲載終了", "draft": "下書き", "hidden": "非公開"}
    for kind_label, items in (("お知らせ", news), ("求人", jobs)):
        counts = {}
        for item in items:
            counts[state_names[item.state]] = counts.get(state_names[item.state], 0) + 1
        summary = "、".join(f"{k} {v}件" for k, v in counts.items()) or "0件"
        print(f"{kind_label}: {summary}")

    # --check のときも、テンプレートに誤りがないか確かめるため、一時フォルダに実際に生成してみる
    preview = args.preview or args.check
    visible_states = {"published"}
    if preview:
        visible_states |= {"scheduled", "draft"}

    def visible(items: list[Entry]) -> list[Entry]:
        return [i for i in items if i.state in visible_states]

    news_visible = sorted(visible(news), key=lambda e: (e.date, e.slug), reverse=True)
    jobs_visible = sorted(visible(jobs), key=lambda e: (e.date, e.slug), reverse=True)

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["nl2br"] = nl2br
    env.filters["date"] = format_date

    out_dir = Path(tempfile.mkdtemp()) if args.check else DIST_DIR
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir()
    for name in STATIC_DIRS:
        src = ROOT / name
        if src.exists():
            shutil.copytree(src, out_dir / name, ignore=shutil.ignore_patterns(".gitkeep", ".DS_Store"))

    # CSSを変えたら、ブラウザが古いCSSを使い続けないよう、読み込むURLに中身から作った番号を付ける
    css_file = ROOT / "assets" / "css" / "style.css"
    css_version = hashlib.sha256(css_file.read_bytes()).hexdigest()[:10] if css_file.is_file() else "0"
    common = {"site": site, "preview": preview, "today": today, "job_fields": JOB_FIELDS, "css_version": css_version}

    def write(rel_path: str, template: str, **context) -> None:
        depth = rel_path.count("/")
        root = "../" * depth
        out = out_dir / rel_path
        out.parent.mkdir(parents=True, exist_ok=True)
        page_html = env.get_template(template).render(root=root, **common, **context)
        out.write_text(page_html, encoding="utf-8")

    try:
        write("index.html", "index.html", page_id="home", news=news_visible[:5], jobs=jobs_visible)
        write("news/index.html", "news_list.html", page_id="news", news=news_visible)
        write("jobs/index.html", "jobs_list.html", page_id="jobs", jobs=jobs_visible)
        for item in news_visible:
            item.body_html = render_markdown(item.body_md, "../")
            write(item.url, "news_detail.html", page_id="news", item=item)
        for item in jobs_visible:
            item.body_html = render_markdown(item.body_md, "../")
            write(item.url, "job_detail.html", page_id="jobs", item=item)
    except TemplateError as exc:
        where = getattr(exc, "filename", None) or getattr(exc, "name", None) or "テンプレート"
        try:
            where = str(Path(where).resolve().relative_to(ROOT))
        except ValueError:
            pass
        line = getattr(exc, "lineno", None)
        print(f"[エラー] {where}{f' の {line} 行目' if line else ''}: ページのひな形に誤りがあります（{exc}）")
        if args.check:
            shutil.rmtree(out_dir, ignore_errors=True)
        return 1

    if args.check:
        shutil.rmtree(out_dir, ignore_errors=True)
        print("チェックOK（エラーはありません）")
        return 0

    if preview:
        (out_dir / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")

    mode = "確認用" if args.preview else "本番用"
    print(f"{mode}のページを dist/ に生成しました（基準日: {today.isoformat()}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

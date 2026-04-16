"""
convert.py — Convert cleaned article JSON files to Obsidian Markdown.

Usage:
    python convert.py
    python convert.py --config path/to/config.yaml

Reads all JSON files from output_dir/cleaned/, converts each to a clean
Obsidian-compatible Markdown file, and writes them to output_dir/.

Features:
  - YAML frontmatter (title, author, date, source)
  - HTML → Markdown via markdownify
  - Relative image paths (embed_images: false) or Base64 inline (embed_images: true)
  - Filename sanitisation with duplicate-suffix handling
  - Per-article error isolation (one failure won't abort the batch)

Exit codes:
    0 — Always (errors are logged per-article, not fatal)
"""

import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify as md

from utils import load_config, sanitize_filename


# ── MIME type map for extension → data-URL prefix ─────────────────────────────
MIME_MAP = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".svg":  "image/svg+xml",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Convert cleaned WeChat article JSON to Obsidian Markdown"
    )
    parser.add_argument("--config", default=None,
                        help="Path to config.yaml (default: project root)")
    return parser.parse_args()


# ── Loading ────────────────────────────────────────────────────────────────────

def load_cleaned_files(config: dict) -> list:
    """
    Read all JSON files from output_dir/cleaned/.

    Returns:
        List of (filepath, article_dict) tuples.
        Prints [WARN] and returns [] if directory is missing or empty.
    """
    cleaned_dir = Path(config["output_dir"]) / "cleaned"
    if not cleaned_dir.exists():
        print(f"[WARN] No cleaned JSON files found (directory missing: {cleaned_dir})")
        return []

    json_files = sorted(cleaned_dir.glob("*.json"), key=lambda p: p.stem)
    if not json_files:
        print(f"[WARN] No cleaned JSON files found in {cleaned_dir}")
        return []

    articles = []
    for fp in json_files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            articles.append((fp, data))
        except Exception as e:
            print(f"[WARN] Skipping malformed file: {fp.name} ({e})")
    return articles


# ── Frontmatter ────────────────────────────────────────────────────────────────

def build_frontmatter(article: dict) -> str:
    """
    Build a YAML frontmatter block.
    Omits the `date` line when article['date'] is None.
    """
    title = article.get("title") or "Untitled"
    author = article.get("author") or ""
    date = article.get("date")
    source = article.get("source_url") or ""

    # Escape double-quotes in title/author for YAML safety
    safe_title  = title.replace('"', '\\"')
    safe_author = author.replace('"', '\\"')

    lines = [
        "---",
        f'title: "{safe_title}"',
        f'author: "{safe_author}"',
    ]
    if date:
        lines.append(f"date: {date}")
    lines.append(f'source: "{source}"')
    lines.append("---")
    return "\n".join(lines)


# ── HTML → Markdown conversion ─────────────────────────────────────────────────

def _prepare_html_for_markdownify(html: str) -> str:
    """
    Pre-process HTML before passing to markdownify:
    - Convert lone <section> wrappers to <div> so markdownify treats them
      as block elements rather than emitting them as raw tags.
    - Strip any remaining inline style/class noise.
    """
    soup = BeautifulSoup(html, "lxml")

    # Rename <section> → <div> so markdownify handles them as generic blocks
    for tag in soup.find_all("section"):
        tag.name = "div"

    # Strip class and style that survived fetch.py (extra safety)
    for tag in soup.find_all(True):
        tag.attrs.pop("class", None)
        tag.attrs.pop("style", None)

    return str(soup)


def html_to_markdown(html: str, images: list, embed_images: bool) -> str:
    """
    Convert preprocessed HTML to clean Markdown.

    Image replacement strategy (position-based, not src-based):
      Images are matched by their order of appearance in the document.
      WeChat lazy-loads images (src="" / data-src="..."), so src values
      cannot be used as lookup keys after fetch.py processes them.

      embed_images=True  → use images[i]['base64'] if present, else local path
      embed_images=False → use images[i]['local'] relative path
      Neither available  → insert <!-- 图片不可用 -->
    """
    # Build an ordered list of replacement strings (index = appearance order)
    replacements: list[str] = []
    for entry in images:
        local = entry.get("local") or ""
        if embed_images:
            b64 = entry.get("base64")
            if b64:
                replacements.append(b64)
            elif local:
                # Graceful fallback for old JSONs without base64 field
                replacements.append(local)
            else:
                replacements.append("")  # will become <!-- 图片不可用 -->
        else:
            replacements.append(local)  # empty string → 图片不可用

    # Prepare HTML
    clean_html = _prepare_html_for_markdownify(html)

    # Run markdownify
    result = md(
        clean_html,
        strip=["style", "script", "button", "form"],
        heading_style="ATX",           # use ## style headings
        bullets="-",
        # newline_style 不传，默认不在行尾插入 \ 字符
    )

    # Post-process: replace image tags by position order
    # markdownify renders <img src="…"> as ![alt](src)
    img_counter = [0]  # mutable container so nested function can increment it

    def replace_img(m):
        idx = img_counter[0]
        img_counter[0] += 1
        if idx < len(replacements) and replacements[idx]:
            return f"![]({replacements[idx]})"
        return "<!-- 图片不可用 -->"

    result = re.sub(r'!\[[^\]]*\]\([^)]*\)', replace_img, result)

    # Clean up excessive blank lines (max 2 consecutive)
    result = re.sub(r'\n{3,}', '\n\n', result)

    return result.strip()


# ── Duplicate-title removal ────────────────────────────────────────────────────

def remove_duplicate_title(body: str, title: str) -> str:
    """
    If the first non-empty line of body is the same as title (ignoring leading #),
    remove that line to avoid duplication with the frontmatter title field.
    """
    lines = body.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Remove leading # characters and whitespace
        heading_text = re.sub(r'^#+\s*', '', stripped)
        if heading_text == title.strip():
            lines.pop(i)
        break
    return "\n".join(lines)


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    config = load_config(args.config)

    articles = load_cleaned_files(config)
    if not articles:
        sys.exit(0)

    output_dir = config["output_dir"]
    embed_images = config.get("embed_images", False)

    success_count = 0
    fail_count = 0

    for fp, article in articles:
        try:
            title  = article.get("title") or "Untitled"
            date   = article.get("date")
            html   = article.get("html") or ""
            images = article.get("images") or []

            # Build filename — check for existing file but do NOT create -2 duplicates.
            # If the same-titled file already exists, skip it (it was already generated
            # in a prior run or from a duplicate fetch).
            base_filename = sanitize_filename(title, date=date)   # no output_dir → no dedup suffix
            out_path = Path(output_dir) / f"{base_filename}.md"
            if out_path.exists():
                print(f"[SKIP] Already exists, skipping: {out_path.name}")
                success_count += 1
                continue

            # Build frontmatter
            frontmatter = build_frontmatter(article)

            # Convert HTML → Markdown
            body = html_to_markdown(html, images, embed_images)

            # Remove duplicate title heading from body
            body = remove_duplicate_title(body, title)

            # Assemble final document
            content = f"{frontmatter}\n\n{body}\n"

            # Write output
            out_path.write_text(content, encoding="utf-8")
            print(f"[OK] {out_path.name}")
            success_count += 1

        except Exception as e:
            print(f"[WARN] Skipping malformed file: {fp.name} ({e})")
            fail_count += 1

    print(f"\n[DONE] Converted {success_count + fail_count} articles: "
          f"{success_count} success, {fail_count} failed")


if __name__ == "__main__":
    main()

"""
fetch.py — Visit article pages, download images, and preprocess HTML.

Usage:
    python fetch.py
    python fetch.py --config path/to/config.yaml

Reads urls.json from the output directory and processes each article:
1. Navigate to article URL via CDP
2. Intercept and download images
3. Extract and preprocess HTML with BeautifulSoup
4. Extract metadata (title, author, date)
5. Save preprocessed data as JSON to cleaned/

Exit codes:
    0 — Success (some articles may have failed individually)
    1 — CDP connection failure
    2 — urls.json not found or empty
"""

import argparse
import base64
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from utils import load_config, connect_cdp, ensure_output_dirs, make_slug

# Reuse a single requests session for all direct image downloads
_http = requests.Session()
_http.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://mp.weixin.qq.com/",
})


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Fetch and preprocess WeChat articles")
    parser.add_argument('--config', default=None,
                        help='Path to config.yaml (default: project root)')
    return parser.parse_args()


def load_urls(config: dict) -> list:
    """Load and validate urls.json."""
    urls_path = Path(config["output_dir"]) / "urls.json"
    if not urls_path.exists():
        print(f"[ERROR] urls.json not found at {urls_path}")
        print("[ERROR] Run search.py first to collect article URLs.")
        sys.exit(2)

    with open(urls_path, "r", encoding="utf-8") as f:
        urls = json.load(f)

    if not urls:
        print("[ERROR] urls.json is empty.")
        sys.exit(2)

    print(f"[INFO] Loaded {len(urls)} articles from urls.json")
    return urls


def get_image_extension(content_type: str, url: str) -> str:
    """Determine image file extension from content-type or URL."""
    if content_type:
        mime_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
        }
        for mime, ext in mime_map.items():
            if mime in content_type:
                return ext

    # Fallback: try to extract from URL
    parsed = urlparse(url)
    path = parsed.path.lower()
    for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]:
        if ext in path:
            return ext if ext != ".jpeg" else ".jpg"

    # Check wx_fmt parameter
    if "wx_fmt=" in url:
        fmt = re.search(r'wx_fmt=(\w+)', url)
        if fmt:
            fmt_str = fmt.group(1).lower()
            fmt_map = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg", "gif": ".gif", "webp": ".webp"}
            return fmt_map.get(fmt_str, ".png")

    return ".png"


def setup_image_interceptor(page, image_dir: str):
    """
    Set up network response listener to capture and save images.

    Returns:
        A dict mapping sequential index (int, 0-based) to
        (filename, original_url) tuples, and the response handler.

        Key change: previously keyed by original URL (str), now keyed by
        download order (int) so that image_map[0] is the first image
        downloaded, image_map[1] the second, etc.  This avoids WeChat
        CDN URL version mismatches when matching against HTML data-src.
    """
    image_map = {}   # int -> (filename, original_url)
    counter = {"n": 0}

    os.makedirs(image_dir, exist_ok=True)

    def handle_response(response):
        try:
            url = response.url
            # Only intercept WeChat images
            if "mmbiz.qpic.cn" not in url and "mmbiz.wpimg.cn" not in url:
                return

            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                return

            # Skip tiny images (icons, tracking pixels, etc.)
            body = response.body()
            if len(body) < 1024:  # Skip images smaller than 1KB
                return

            ext = get_image_extension(content_type, url)
            filename = f"img-{counter['n']:03d}{ext}"
            filepath = os.path.join(image_dir, filename)

            with open(filepath, "wb") as f:
                f.write(body)

            # Store with 0-based sequential index as key
            image_map[counter["n"]] = (filename, url)
            counter["n"] += 1
        except Exception:
            # Silently skip failed image downloads
            pass

    page.on("response", handle_response)
    return image_map, handle_response


def scroll_page(page):
    """Scroll page to bottom to trigger lazy-loaded images."""
    try:
        page.evaluate("""
            () => {
                return new Promise((resolve) => {
                    let totalHeight = 0;
                    const distance = 300;
                    const timer = setInterval(() => {
                        window.scrollBy(0, distance);
                        totalHeight += distance;
                        if (totalHeight >= document.body.scrollHeight) {
                            clearInterval(timer);
                            window.scrollTo(0, 0);
                            resolve();
                        }
                    }, 100);
                });
            }
        """)
    except Exception:
        pass


def extract_metadata(page, soup, search_data: dict) -> dict:
    """
    Extract article metadata from the page.
    
    Tries multiple sources for each field, with fallbacks.
    """
    metadata = {
        "title": None,
        "author": None,
        "date": None,
    }

    # --- Title ---
    # Try #activity-name first (WeChat article title element)
    title_el = soup.select_one("#activity-name")
    if title_el:
        metadata["title"] = title_el.get_text(strip=True)

    # Fallback: og:title meta tag
    if not metadata["title"]:
        og_title = soup.select_one('meta[property="og:title"]')
        if og_title and og_title.get("content"):
            metadata["title"] = og_title["content"].strip()

    # Fallback: <title> tag
    if not metadata["title"]:
        title_tag = soup.select_one("title")
        if title_tag:
            metadata["title"] = title_tag.get_text(strip=True)

    # Final fallback: from search data
    if not metadata["title"]:
        metadata["title"] = search_data.get("title", "Untitled")

    # --- Author / Account ---
    # Try #js_name (WeChat account name element)
    author_el = soup.select_one("#js_name")
    if author_el:
        metadata["author"] = author_el.get_text(strip=True)

    # Fallback: from search data
    if not metadata["author"]:
        metadata["author"] = search_data.get("account", "")

    # --- Date ---
    # Try to extract from page's JavaScript data
    try:
        date_str = page.evaluate("""
            () => {
                // WeChat articles often have publish time in a script tag
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    const text = s.textContent;
                    // Look for publish_time or create_time pattern
                    const match = text.match(/var\\s+(?:publish_time|ct)\\s*=\\s*["']?(\\d+)["']?/);
                    if (match) {
                        const ts = parseInt(match[1]);
                        if (ts > 1000000000) {
                            const d = new Date(ts * 1000);
                            return d.toISOString().split('T')[0];
                        }
                    }
                }
                
                // Try #publish_time element
                const pubEl = document.querySelector('#publish_time');
                if (pubEl) return pubEl.textContent.trim();
                
                // Try the post-date class
                const dateEl = document.querySelector('.rich_media_meta_text');
                if (dateEl) return dateEl.textContent.trim();
                
                return null;
            }
        """)
        if date_str:
            # Try to normalize to YYYY-MM-DD
            date_match = re.search(r'(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})', date_str)
            if date_match:
                y, m, d = date_match.groups()
                metadata["date"] = f"{y}-{int(m):02d}-{int(d):02d}"
            elif re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                metadata["date"] = date_str[:10]
    except Exception:
        pass

    # Fallback: from search data
    if not metadata["date"]:
        search_date = search_data.get("date")
        if search_date:
            # Try to parse relative dates like "3天前" or absolute dates
            date_match = re.search(r'(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})', search_date)
            if date_match:
                y, m, d = date_match.groups()
                metadata["date"] = f"{y}-{int(m):02d}-{int(d):02d}"

    return metadata


def _download_image_direct(url: str, image_dir: str, index: int):
    """
    Download a single image directly via requests (fallback for lazy-loaded
    images the browser interceptor missed).

    Returns:
        (filename, original_url) tuple on success, or None on failure.
    """
    try:
        resp = _http.get(url, timeout=15, stream=True)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            return None
        body = resp.content
        if len(body) < 1024:
            return None
        ext = get_image_extension(content_type, url)
        filename = f"img-{index:03d}{ext}"
        filepath = os.path.join(image_dir, filename)
        os.makedirs(image_dir, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(body)
        return (filename, url)
    except Exception as e:
        print(f"[WARN] Direct download failed for img-{index:03d}: {e}")
        return None


def preprocess_html(html: str, image_map: dict, attachments_rel_path: str,
                    image_dir: str = "") -> str:
    """
    Preprocess raw HTML with BeautifulSoup:
    1. Extract main content area
    2. Remove script/style/svg/hidden elements
    3. Replace image src with local paths (intercepted or direct-downloaded)

    Returns:
        Preprocessed HTML string.
    """
    soup = BeautifulSoup(html, "lxml")

    # Try to extract main content area
    content = soup.select_one("#js_content")
    if not content:
        content = soup.select_one(".rich_media_content")
    if not content:
        content = soup.select_one("body")
    if not content:
        content = soup

    # Remove unwanted elements
    for tag in content.find_all(["script", "style", "svg", "noscript", "iframe"]):
        tag.decompose()

    # Remove hidden elements
    for tag in content.find_all(attrs={"style": re.compile(r"display\s*:\s*none", re.I)}):
        tag.decompose()
    for tag in content.find_all(attrs={"hidden": True}):
        tag.decompose()

    # Replace image src with local paths (sequential / position-based).
    #
    # image_map is keyed by 0-based download order (int) → (filename, original_url).
    # We consume it in the order images appear in the HTML so that
    # img_counter=0 gets image_map[0], img_counter=1 gets image_map[1], etc.
    # This avoids WeChat CDN URL version mismatches entirely.
    #
    # Fallback for images not captured by the network interceptor (e.g. those
    # that were never scrolled into the viewport so the browser never fetched
    # them): if the <img> has a data-src attribute we download it directly with
    # requests and add it to image_map so that subsequent processing works.
    img_counter = 0
    for img in content.find_all("img"):
        entry = image_map.get(img_counter)
        if entry is None:
            # Try to rescue via data-src direct download
            data_src = img.get("data-src", "").split("#")[0].strip()
            if data_src and ("mmbiz.qpic.cn" in data_src or "mmbiz.wpimg.cn" in data_src):
                rescued = _download_image_direct(
                    data_src, image_dir, img_counter
                )
                if rescued:
                    image_map[img_counter] = rescued
                    entry = rescued
                    print(f"[INFO] img #{img_counter} rescued via data-src direct download")
                else:
                    print(f"[WARN] img #{img_counter} not downloaded, skipping")

        if entry:
            filename, _orig_url = entry
            img["src"] = f"{attachments_rel_path}/{filename}"
            # Remove data attributes
            for attr in list(img.attrs.keys()):
                if attr not in ["src", "alt"]:
                    del img[attr]
        else:
            img["src"] = ""
            img["alt"] = img.get("alt", "image unavailable")
        img_counter += 1

    # ── Deep-clean pass (reduces HTML size ~40-70%) ──────────────────────────

    # 4.1 Remove all inline style attributes
    for tag in content.find_all(True):
        tag.attrs.pop("style", None)

    # 4.2 Remove label attributes (injected by WeChat KNB Formatter)
    for tag in content.find_all(True):
        tag.attrs.pop("label", None)

    # 4.3 Remove data-* attributes except data-src
    for tag in content.find_all(True):
        for attr in list(tag.attrs.keys()):
            if attr.startswith("data-") and attr != "data-src":
                del tag.attrs[attr]

    # 4.4 Remove empty section/div containers (only whitespace or <br>)
    #     Iterate in reverse document order (leaves first) so inner empties
    #     are removed before their parents are checked.
    for tag in reversed(list(content.find_all(["section", "div"]))):
        children = [c for c in tag.children if not (
            hasattr(c, 'name') and c.name is None  # NavigableString
        ) or str(c).strip()]
        # A tag is "empty" if all its NavigableString children are blank
        # and its only element children are <br> tags
        real_children = [c for c in tag.children
                         if getattr(c, 'name', None) and c.name not in (None,)]
        text_content = tag.get_text(strip=True)
        br_only = all(c.name == 'br' for c in real_children) if real_children else True
        if not text_content and br_only:
            tag.decompose()

    # 4.5 Flatten single-child <section> wrappers
    #     Replace <section><p>…</p></section> with just <p>…</p>
    for tag in list(content.find_all("section")):
        # Get direct element children (skip NavigableStrings)
        element_children = [c for c in tag.children
                            if getattr(c, 'name', None) is not None]
        if len(element_children) == 1:
            child = element_children[0]
            tag.replace_with(child)

    return str(content)


# ── Captcha detection & retry ─────────────────────────────────────────────────

_CAPTCHA_SIGNALS = [
    "tcaptcha_wrapper", "uab_agent", "尝试太多了",
    "环境异常", "验证码", "antispider", "weixin110",
]

CAPTCHA_WAIT_SECONDS = 30          # how long to wait before retrying
CAPTCHA_MAX_RETRIES = 1            # retry once after captcha


def _is_captcha_page(page) -> bool:
    """Check if the current page is a captcha / anti-bot block page."""
    try:
        url = page.url
        title = page.title()
    except Exception:
        url, title = "", ""
    html_snippet = ""
    try:
        html_snippet = page.content()[:3000]   # only check head of HTML
    except Exception:
        pass
    combined = url + title + html_snippet
    return any(s in combined for s in _CAPTCHA_SIGNALS)


def _wait_for_images(image_map_ref: dict, timeout: float = 10.0):
    """Poll image_map size until stable for 2 s, or until timeout."""
    deadline = time.time() + timeout
    last_count, stable_since = -1, None
    while time.time() < deadline:
        n = len(image_map_ref)
        if n != last_count:
            last_count = n
            stable_since = time.time()
        elif stable_since and (time.time() - stable_since) >= 2.0:
            break
        time.sleep(0.3)


def _load_article_with_retry(page, url: str, image_map: dict,
                              image_dir: str, response_handler) -> str:
    """
    Scroll the page, wait for images, then check for captcha.
    If a captcha is detected, wait CAPTCHA_WAIT_SECONDS and retry once.
    Returns the final page HTML.
    """
    for attempt in range(1 + CAPTCHA_MAX_RETRIES):
        # Scroll to trigger lazy-loaded images
        scroll_page(page)
        _wait_for_images(image_map)
        html = page.content()

        if not _is_captcha_page(page):
            return html                     # success — real article

        # Captcha detected
        if attempt < CAPTCHA_MAX_RETRIES:
            wait = CAPTCHA_WAIT_SECONDS
            print(f"[CAPTCHA] Anti-bot page detected. "
                  f"Waiting {wait}s before retry ({attempt + 1}/{CAPTCHA_MAX_RETRIES})...")
            time.sleep(wait)
            # Reset interceptor and re-navigate
            page.remove_listener("response", response_handler)
            image_map.clear()
            image_map_retry, response_handler = setup_image_interceptor(page, image_dir)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(1)
            # After re-navigation the loop will scroll & check again
        else:
            print(f"[CAPTCHA] Still blocked after {CAPTCHA_MAX_RETRIES} "
                  f"retry. Saving whatever is available.")

    # Exhausted retries — merge any images collected in the last attempt
    try:
        image_map.update(image_map_retry)       # type: ignore[possibly-undefined]
    except Exception:
        pass
    return page.content()


# ── Article processing ────────────────────────────────────────────────────────

def process_article(page, article: dict, index: int, config: dict,
                    seen_real_urls: set = None) -> dict:
    """
    Process a single article: navigate, download images, preprocess HTML.

    Returns:
        Preprocessed article dict, "duplicate" string if real URL already seen,
        or None if failed.
    """
    url = article["url"]
    slug = make_slug(article.get("title", f"article-{index}"))

    # Set up image directory and interceptor
    image_dir = os.path.join(config["output_dir"], config["attachments_dir"], slug)
    image_map, response_handler = setup_image_interceptor(page, image_dir)

    attachments_rel_path = f"{config['attachments_dir']}/{slug}"

    try:
        # Navigate to article
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(1)

        # --- Real-URL deduplication (after redirect) ---
        # Sogou wraps article links with session tokens; the same article can
        # appear with different token URLs.  After navigation the browser has
        # followed the redirect, so page.url holds the canonical article URL.
        real_url = page.url
        if seen_real_urls is not None:
            if real_url in seen_real_urls:
                return "duplicate"
            seen_real_urls.add(real_url)

        # --- Load page, scroll, fetch images (with captcha retry) ---
        html = _load_article_with_retry(
            page, url, image_map, image_dir, response_handler
        )

        # Parse with BeautifulSoup for metadata extraction
        soup = BeautifulSoup(html, "lxml")

        # Extract metadata
        metadata = extract_metadata(page, soup, article)
        
        # Preprocess HTML (pass image_dir so fallback direct-download works)
        cleaned_html = preprocess_html(html, image_map, attachments_rel_path,
                                       image_dir=image_dir)
        
        # Build image list in download order (0, 1, 2, ...) so that the
        # array index aligns with the position of each <img> in the HTML.
        embed_images = config.get("embed_images", False)
        images = []
        for i in range(len(image_map)):
            filename, orig_url = image_map[i]
            local_rel = f"{attachments_rel_path}/{filename}"
            entry = {
                "original": orig_url,
                "local": local_rel,
            }
            # Pre-encode to Base64 when embed_images is enabled
            if embed_images:
                img_abs_path = Path(config["output_dir"]) / local_rel
                if img_abs_path.exists():
                    try:
                        ext = img_abs_path.suffix.lower()
                        mime_map = {
                            ".png": "image/png",
                            ".jpg": "image/jpeg",
                            ".jpeg": "image/jpeg",
                            ".gif": "image/gif",
                            ".webp": "image/webp",
                            ".svg": "image/svg+xml",
                        }
                        mime = mime_map.get(ext, "image/png")
                        b64_data = base64.b64encode(img_abs_path.read_bytes()).decode("ascii")
                        entry["base64"] = f"data:{mime};base64,{b64_data}"
                    except Exception:
                        entry["base64"] = None
                else:
                    entry["base64"] = None
            images.append(entry)

        result = {
            "title": metadata["title"],
            "author": metadata["author"],
            "date": metadata["date"],
            "source_url": url,
            "html": cleaned_html,
            "images": images,
        }
        
        return result

    except Exception as e:
        print(f"[ERROR] Failed to process article: {url}")
        print(f"[ERROR] Detail: {e}")
        return None
    finally:
        # Remove the response listener to avoid accumulation
        try:
            page.remove_listener("response", response_handler)
        except Exception:
            pass


def main():
    args = parse_args()
    config = load_config(args.config)
    ensure_output_dirs(config)

    # Load URLs
    articles = load_urls(config)
    total = len(articles)

    # Connect to Chrome via CDP
    pw, browser, page = connect_cdp(config["cdp_endpoint"], user_data_dir=config.get("user_data_dir"))
    
    success_count = 0
    fail_count = 0
    skip_count = 0
    seen_real_urls: set = set()   # deduplicate by post-redirect real URL

    try:
        saved_index = 0  # sequential index for cleaned JSON filenames
        for i, article in enumerate(articles):
            print(f"\n[INFO] Processing {i + 1}/{total}: {article.get('title', 'Unknown')[:50]}...")

            result = process_article(page, article, saved_index, config,
                                     seen_real_urls=seen_real_urls)

            if result is None:
                fail_count += 1
            elif result == "duplicate":
                skip_count += 1
                print(f"[SKIP] Duplicate article (already collected), skipping.")
            else:
                # Save cleaned JSON
                cleaned_path = os.path.join(config["output_dir"], "cleaned", f"{saved_index}.json")
                with open(cleaned_path, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                saved_index += 1
                success_count += 1
                print(f"[OK] Saved cleaned data ({len(result.get('images', []))} images)")

            # Wait between articles — use a random jitter around fetch_interval
            # to reduce the risk of triggering WeChat's rate-limiter.
            if i < total - 1:
                base_interval = config["fetch_interval"]
                jitter = random.uniform(0, base_interval)
                sleep_time = base_interval + jitter
                print(f"[INFO] Waiting {sleep_time:.1f}s before next article...")
                time.sleep(sleep_time)

        print(f"\n[DONE] Processed {total} articles: "
              f"{success_count} success, {skip_count} skipped (duplicate), {fail_count} failed")

    finally:
        pw.stop()


if __name__ == "__main__":
    main()
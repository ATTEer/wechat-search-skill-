"""
search.py — Search weixin.sogou.com and collect article URLs.

Usage:
    python search.py --mode title --keyword "AI 大模型"
    python search.py --mode account --keyword "人民日报"
    python search.py --mode title --keyword "AI" --config path/to/config.yaml

Exit codes:
    0 — Success
    1 — CDP connection failure
    2 — No results found

Note: If a captcha is detected, the script pauses and waits for the user
      to solve it manually in the browser, then continues automatically.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from utils import load_config, connect_cdp, ensure_output_dirs


# =============================================================================
# DOM Selectors (may need updating if Sogou changes their page structure)
# =============================================================================
SELECTORS = {
    # Search page
    "search_input": 'input#query',
    "search_button": 'input[type="submit"]',
    
    # Search result items
    "result_items": '.news-list li .txt-box',
    "result_title_link": 'h3 a',
    "result_account": 'div.s-p span.all-time-y2',
    "result_date": 'div.s-p span.s2',
    
    # Pagination
    "next_page": '#sogou_next',
    
    # Captcha detection
    "captcha": '#seccodeImage',
}


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Search weixin.sogou.com for articles")
    parser.add_argument('--mode', choices=['title', 'account'], required=True,
                        help='Search mode: "title" for article title, "account" for account name')
    parser.add_argument('--keyword', required=True,
                        help='Search keyword (article title or account name)')
    parser.add_argument('--config', default=None,
                        help='Path to config.yaml (default: project root)')
    parser.add_argument('--max-results', type=int, default=None,
                        help='Maximum number of articles to collect (overrides config). 0 = no limit.')
    return parser.parse_args()


def check_captcha(page) -> bool:
    """Check if the current page shows a captcha."""
    try:
        captcha = page.query_selector(SELECTORS["captcha"])
        if captcha and captcha.is_visible():
            return True
    except Exception:
        pass
    
    # Also check for common captcha page indicators
    try:
        if "验证" in page.title() or "antispider" in page.url:
            return True
    except Exception:
        pass
    
    return False


def execute_search(page, keyword: str) -> None:
    """Navigate to weixin.sogou.com and execute search."""
    print(f"[INFO] Navigating to weixin.sogou.com...")
    page.goto("https://weixin.sogou.com/", wait_until="domcontentloaded")
    time.sleep(1)

    # Check for captcha after navigation
    if check_captcha(page):
        print("[CAPTCHA] Captcha detected on landing page!")
        print("[CAPTCHA] Please solve it manually in the browser, then press Enter here...")
        input()
    
    print(f"[INFO] Searching for: {keyword}")
    search_input = page.wait_for_selector(SELECTORS["search_input"], timeout=10000)
    search_input.fill(keyword)
    
    search_btn = page.query_selector(SELECTORS["search_button"])
    if search_btn:
        search_btn.click()
    else:
        search_input.press("Enter")
    
    # Wait for results page to load
    page.wait_for_load_state("domcontentloaded")
    time.sleep(1)


def parse_results_page(page) -> list:
    """
    Parse current search results page and extract article info.
    
    Returns:
        List of dicts: [{url, title, account, date}, ...]
    """
    results = []
    
    items = page.query_selector_all(SELECTORS["result_items"])
    
    for item in items:
        try:
            # Extract title and URL
            title_link = item.query_selector(SELECTORS["result_title_link"])
            if not title_link:
                continue
            
            url = title_link.get_attribute("href")
            title = title_link.inner_text().strip()
            
            # Make URL absolute if needed
            if url and url.startswith("/"):
                url = f"https://weixin.sogou.com{url}"
            
            # Extract source account name
            account = ""
            account_el = item.query_selector(SELECTORS["result_account"])
            if account_el:
                account = account_el.inner_text().strip()
            
            # Extract date
            date = None
            date_el = item.query_selector(SELECTORS["result_date"])
            if date_el:
                date = date_el.inner_text().strip()
            
            if url and title:
                results.append({
                    "url": url,
                    "title": title,
                    "account": account,
                    "date": date,
                })
        except Exception as e:
            print(f"[WARN] Failed to parse a result item: {e}")
            continue
    
    return results


def filter_by_account(results: list, target_account: str) -> list:
    """Filter results to include account name matches (case-insensitive)."""
    target_lower = target_account.lower()
    return [r for r in results if r["account"].lower() == target_lower]


def deduplicate(results: list) -> list:
    """Deduplicate results by URL."""
    seen = set()
    unique = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)
    return unique


def paginate_and_collect(page, mode: str, keyword: str, max_pages: int, interval: float,
                         max_results: int = 0) -> list:
    """
    Iterate through search result pages and collect all results.

    Args:
        max_results: Stop collecting once this many unique articles are gathered.
                     0 means no limit.

    Returns:
        List of all collected result dicts.
    """
    all_results = []

    for page_num in range(1, max_pages + 1):
        print(f"[INFO] Processing page {page_num}/{max_pages}...")

        # Check for captcha
        if check_captcha(page):
            print(f"[CAPTCHA] Captcha detected on page {page_num}!")
            print("[CAPTCHA] Please solve it manually in the browser, then press Enter here...")
            input()
            # Retry current page after captcha is solved
            page.reload(wait_until="domcontentloaded")
            time.sleep(1)

        # Parse current page
        page_results = parse_results_page(page)

        # Filter by account if in account mode
        if mode == "account":
            page_results = filter_by_account(page_results, keyword)

        print(f"[INFO] Page {page_num}: found {len(page_results)} results")
        all_results.extend(page_results)

        # Check max_results cap (applied after extending so whole-page records are intact)
        if max_results > 0 and len(all_results) >= max_results:
            all_results = all_results[:max_results]
            print(f"[INFO] Reached max_results ({max_results}), stopping early")
            break

        # Check if we've reached the last page
        if page_num >= max_pages:
            break

        next_btn = page.query_selector(SELECTORS["next_page"])
        if not next_btn:
            print(f"[INFO] No more pages available (stopped at page {page_num})")
            break

        # Wait before navigating to next page
        time.sleep(interval)

        next_btn.click()
        page.wait_for_load_state("domcontentloaded")
        time.sleep(1)

    return all_results


def main():
    args = parse_args()
    config = load_config(args.config)
    ensure_output_dirs(config)
    
    # Connect to Chrome via CDP
    pw, browser, page = connect_cdp(config["cdp_endpoint"], user_data_dir=config.get("user_data_dir"))
    
    try:
        # Execute search
        execute_search(page, args.keyword)
        
        # Check if there are any results
        time.sleep(1)
        initial_results = page.query_selector_all(SELECTORS["result_items"])
        if not initial_results:
            print("[INFO] No search results found.")
            sys.exit(2)
        
        # Resolve max_results: CLI arg overrides config
        max_results = args.max_results if args.max_results is not None else config.get("max_results", 20)

        # Collect results across pages
        all_results = paginate_and_collect(
            page,
            mode=args.mode,
            keyword=args.keyword,
            max_pages=config["pages_to_search"],
            interval=config["fetch_interval"],
            max_results=max_results,
        )
        
        # Deduplicate
        unique_results = deduplicate(all_results)
        
        if not unique_results:
            if args.mode == "account":
                print(f"[INFO] No matching articles found for account: {args.keyword}")
            else:
                print("[INFO] No results collected.")
            sys.exit(2)
        
        # Save to urls.json
        output_path = Path(config["output_dir"]) / "urls.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(unique_results, f, ensure_ascii=False, indent=2)
        
        print(f"\n[DONE] Collected {len(unique_results)} unique articles")
        print(f"[DONE] Saved to: {output_path}")
    
    finally:
        # Don't close the browser — it's the user's browser
        pw.stop()


if __name__ == "__main__":
    main()
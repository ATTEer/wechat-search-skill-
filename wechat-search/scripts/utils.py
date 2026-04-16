"""
Shared utilities for wechat-search scripts.
"""

import os
import re
import sys
import tempfile
import threading
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright


# Default configuration values
DEFAULTS = {
    "output_dir": "./output",
    "attachments_dir": "attachments",
    "cdp_endpoint": "http://127.0.0.1:9222",
    "pages_to_search": 20,
    "max_results": 20,
    "fetch_interval": 2,
    "batch_size": 5,
    "embed_images": False,
    "user_data_dir": "",
}


def load_config(config_path: str = None) -> dict:
    """
    Load and validate config.yaml, returning a config dict with defaults applied.
    
    Args:
        config_path: Path to config.yaml. If None, searches in the project root.
    
    Returns:
        dict with all configuration values.
    """
    if config_path is None:
        # Search for config.yaml relative to this script's parent directory
        project_root = Path(__file__).resolve().parent.parent
        config_path = project_root / "config.yaml"
    else:
        config_path = Path(config_path)

    config = dict(DEFAULTS)

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f)
            if user_config and isinstance(user_config, dict):
                config.update(user_config)
    else:
        print(f"[WARN] Config file not found at {config_path}, using defaults.")

    # Resolve output_dir to absolute path.
    # Use the current working directory (cwd) as the base for relative paths.
    # This is correct because scripts are always invoked with `cd <project_root>`
    # before calling the script, so cwd == project root regardless of where
    # config.yaml or utils.py physically lives.
    output_dir = Path(config["output_dir"])
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    config["output_dir"] = str(output_dir.resolve())

    return config


def connect_cdp(endpoint: str = None, user_data_dir: str = None):
    """
    Connect to Chrome via CDP using Playwright, with automatic fallback.

    Two-stage connection strategy:
    1. Try connect_over_cdp() to attach to a running Chrome instance.
    2. If that fails, launch a persistent Chromium window via
       launch_persistent_context(headless=False), preserving cookies
       across sessions via user_data_dir.

    Args:
        endpoint: CDP endpoint URL. If None, uses default.
        user_data_dir: Path to Chrome profile directory for persistent
            sessions. If None or empty, defaults to
            <tempdir>/wechat-search-chrome-profile.

    Returns:
        Tuple of (playwright_instance, browser_or_context, page)
    """
    if endpoint is None:
        endpoint = DEFAULTS["cdp_endpoint"]

    pw = sync_playwright().start()

    # --- Stage 1: try CDP connection ---
    try:
        browser = pw.chromium.connect_over_cdp(endpoint)
        print(f"[INFO] 已通过 CDP 连接到 Chrome", flush=True)
        # Use the first existing context/page, or create a new one
        if browser.contexts:
            context = browser.contexts[0]
            if context.pages:
                page = context.pages[0]
            else:
                page = context.new_page()
        else:
            context = browser.new_context()
            page = context.new_page()
        return pw, browser, page
    except Exception as cdp_err:
        print(f"[WARN] CDP 连接失败，将自动启动独立浏览器窗口...", flush=True)
        print(f"[WARN] CDP 错误详情: {cdp_err}", flush=True)

    # --- Stage 2: launch persistent Chromium window ---
    # Resolve user_data_dir
    if not user_data_dir:
        user_data_dir = os.path.join(tempfile.gettempdir(), "wechat-search-chrome-profile")
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)

    # Clean up stale lockfile to prevent exitCode=21
    lockfile = os.path.join(user_data_dir, "lockfile")
    if os.path.exists(lockfile):
        try:
            os.remove(lockfile)
            print(f"[INFO] 已清理残留的 lockfile", flush=True)
        except OSError:
            pass  # File may be locked by a running process; launch will fail naturally

    print(f"[INFO] 使用浏览器数据目录: {user_data_dir}", flush=True)
    print(f"[INFO] 若首次启动可能需要等待 30-60 秒，请稍候...", flush=True)

    try:
        # Print a heartbeat while waiting for the browser to start,
        # so that AI agents / terminal watchers don't mistake the
        # silence for a hang and kill the process.
        _stop_heartbeat = threading.Event()

        def _heartbeat():
            dots = 0
            while not _stop_heartbeat.wait(timeout=5):
                dots += 1
                print(f"[INFO] 等待浏览器启动中{'.' * dots}", flush=True)

        hb_thread = threading.Thread(target=_heartbeat, daemon=True)
        hb_thread.start()

        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,
            )
        finally:
            _stop_heartbeat.set()

        print(f"[INFO] 浏览器窗口已启动", flush=True)
        # Use existing page or open a new one
        if context.pages:
            page = context.pages[0]
        else:
            page = context.new_page()
        return pw, context, page
    except Exception as launch_err:
        pw.stop()
        print(f"[ERROR] 自动启动浏览器失败: {launch_err}", flush=True)
        print(f"[ERROR] 请先安装 Playwright 浏览器：playwright install chromium", flush=True)
        sys.exit(1)


def sanitize_filename(title: str, date: str = None, output_dir: str = None) -> str:
    """
    Generate a safe filename from article title and optional date.
    
    - Truncates title to 50 characters
    - Removes characters invalid for filenames
    - Prepends date (YYYY-MM-DD) if available
    - Handles duplicates with -2, -3, etc. suffix (if output_dir provided)
    
    Args:
        title: Article title.
        date: Optional publish date string (YYYY-MM-DD format).
        output_dir: Optional output directory to check for duplicates.
    
    Returns:
        Safe filename string (without .md extension).
    """
    # Remove invalid filename characters
    safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]', '', title).strip()
    
    # Truncate to 50 characters
    if len(safe_title) > 50:
        safe_title = safe_title[:50].rstrip()

    # Prepend date if available
    if date:
        base_name = f"{date}-{safe_title}"
    else:
        base_name = safe_title

    # Handle empty title edge case
    if not base_name:
        base_name = "untitled"

    # Handle duplicates if output_dir is provided
    if output_dir:
        final_name = base_name
        counter = 2
        while os.path.exists(os.path.join(output_dir, f"{final_name}.md")):
            final_name = f"{base_name}-{counter}"
            counter += 1
        return final_name

    return base_name


def ensure_output_dirs(config: dict) -> None:
    """
    Create all necessary output directories if they don't exist.
    
    Creates:
        - output_dir/
        - output_dir/cleaned/
        - output_dir/attachments_dir/
    """
    output_dir = Path(config["output_dir"])
    attachments_dir = output_dir / config["attachments_dir"]
    cleaned_dir = output_dir / "cleaned"

    output_dir.mkdir(parents=True, exist_ok=True)
    attachments_dir.mkdir(parents=True, exist_ok=True)
    cleaned_dir.mkdir(parents=True, exist_ok=True)


def make_slug(title: str) -> str:
    """
    Create a filesystem-safe slug from a title for use as directory name.

    Windows silently strips trailing dots from directory names, so we must
    do the same here to avoid path mismatches.
    """
    slug = re.sub(r'[\\/:*?"<>|\r\n\t]', '', title).strip()
    if len(slug) > 50:
        slug = slug[:50].rstrip()
    # Windows strips trailing dots/spaces from directory names
    slug = slug.rstrip('. ')
    if not slug:
        slug = "untitled"
    return slug

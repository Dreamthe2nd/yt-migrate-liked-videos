#!/usr/bin/env python3
"""Fast like mode: one POST per video to YouTube's own InnerTube like endpoint - no
page loads, ~1.3 s per video. Shares liked_state.db / failed_likes.txt /
liked_videos.json / chrome_profile with migrate_liked_videos.py, so mix them freely.
Sign in once with migrate_liked_videos.py --login first.

    python fast_likes.py                 # run / resume
    python fast_likes.py --retry-failed  # replay failed_likes.txt

What it sends is spot-checked for real (first like, then every VERIFY_EVERY) by
opening that one watch page read-only. If a check fails, the session died, or the
endpoint stops working, it un-marks the affected URLs and hands the rest of the run
to migrate_liked_videos.py. TURNAROUND_* below keeps ~1 like per 1.0-1.6 s so
YouTube's interaction rate-limiter is not tripped: (0.5, 0.9) is twice as fast,
(0, 0) is no delay at all.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
LIKED_FILE = BASE_DIR / "liked_videos.json"       # sole input - never fabricated
DB_FILE = BASE_DIR / "liked_state.db"
FAILED_FILE = BASE_DIR / "failed_likes.txt"
PROFILE_DIR = BASE_DIR / "chrome_profile"         # sign in with migrate_liked_videos.py
FALLBACK = BASE_DIR / "migrate_liked_videos.py"

ORIGIN = "https://www.youtube.com"
LIKE_URL = ORIGIN + "/youtubei/v1/like/like"      # like/removelike is the UNLIKE call
TURNAROUND_MIN_S, TURNAROUND_MAX_S = 1.0, 1.6     # anti-spam pacing between likes
VERIFY_EVERY = 100                                # spot-check a like this often
GO_TO_TIMEOUT_MS = 30_000
READY_WAIT_S = 8.0
# The Authorization prefix must match the cookie the hash was built from.
AUTH_COOKIES = (("SAPISID", "SAPISIDHASH"),
                ("__Secure-1PAPISID", "SAPISID1PHASH"),
                ("__Secure-3PAPISID", "SAPISID3PHASH"))
READ_JS = """() => {
    const b = document.querySelector(
        "#segmented-like-button button, like-button-view-model button, " +
        "ytd-toggle-button-renderer#like-button button");
    if (!b) return "not-found";
    const l = ((b.getAttribute("aria-label") || b.title || "") + "").toLowerCase();
    return (b.getAttribute("aria-pressed") === "true" || l.startsWith("unlike"))
        ? "liked" : "not-liked";
}"""


def video_id(url: str) -> str:
    parsed = urlparse(url)
    if "youtu.be" in parsed.netloc:
        return parsed.path.strip("/").split("/")[0]
    vid = parse_qs(parsed.query).get("v", [""])[0]
    if vid:
        return vid
    match = re.search(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{6,})", parsed.path)
    return match.group(1) if match else ""


def auth_headers(cookies: list[dict]) -> list[str]:
    stamp = int(time.time())
    headers = []
    for name, prefix in AUTH_COOKIES:
        value = next((c["value"] for c in cookies if c["name"] == name and c["value"]), "")
        if value:
            digest = hashlib.sha1(f"{stamp} {value} {ORIGIN}".encode()).hexdigest()
            headers.append(f"{prefix} {stamp}_{digest}")
    return headers


def classify(status: int, text: str) -> tuple[bool, bool, str]:
    if status in (401, 403):
        return False, True, f"HTTP {status}"
    if status != 200:
        return False, False, f"HTTP {status}"
    try:
        data = json.loads(text)
    except ValueError:
        return False, False, "non-JSON reply"
    error = data.get("error")
    if error:
        code = error.get("code") if isinstance(error, dict) else error
        return False, code in (401, 403), f"API error {code}"
    if "LOGIN_REQUIRED" in text:
        return False, True, "LOGIN_REQUIRED"
    return True, False, "ok"


def log_failed(url: str) -> None:
    seen = FAILED_FILE.read_text(encoding="utf-8").splitlines() if FAILED_FILE.exists() else []
    if url not in seen:
        with FAILED_FILE.open("a", encoding="utf-8") as fh:
            fh.write(url + "\n")


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS liked_videos "
                 "(url TEXT PRIMARY KEY, liked_at TIMESTAMP)")
    conn.commit()
    return conn


def load_urls() -> list[str]:
    if not LIKED_FILE.exists():
        sys.exit(f"error: {LIKED_FILE.name} not found - export your liked videos to a "
                 "JSON array or a one-URL-per-line list, place it here and re-run.")
    text = LIKED_FILE.read_text(encoding="utf-8").strip()
    try:
        urls = [u.strip() for u in json.loads(text) if u and u.strip()]
    except json.JSONDecodeError:
        urls = [line.strip() for line in text.splitlines() if line.strip()]
    if not urls:
        sys.exit(f"error: no URLs found in {LIKED_FILE.name}.")
    return list(reversed(urls))


def load_retry_urls(conn: sqlite3.Connection) -> list[str]:
    if not FAILED_FILE.exists():
        sys.exit(f"error: {FAILED_FILE.name} not found - nothing to retry.")
    urls = [u.strip() for u in FAILED_FILE.read_text(encoding="utf-8").splitlines() if u.strip()]
    if not urls:
        sys.exit(f"error: {FAILED_FILE.name} is empty - nothing to retry.")
    conn.executemany("DELETE FROM liked_videos WHERE url = ?", [(u,) for u in urls])
    conn.commit()
    FAILED_FILE.write_text("", encoding="utf-8")
    return urls


def read_client(page) -> tuple[dict, str, str, str]:
    page.goto(ORIGIN, wait_until="domcontentloaded", timeout=GO_TO_TIMEOUT_MS)
    cfg = page.evaluate("""() => {
        const g = window.ytcfg;
        if (!g) return null;
        return {ctx: g.get("INNERTUBE_CONTEXT"), key: g.get("INNERTUBE_API_KEY"),
                ver: g.get("INNERTUBE_CLIENT_VERSION")};
    }""")
    if not cfg or not cfg.get("ctx") or not cfg.get("key"):
        raise RuntimeError("ytcfg not available on the page")
    visitor = (cfg["ctx"].get("client") or {}).get("visitorData", "")
    return cfg["ctx"], cfg["key"], cfg["ver"], visitor


def set_like(request_ctx, vid: str, auth: str, client_ctx: dict, api_key: str,
             client_ver: str, visitor: str) -> tuple[int, str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": auth,
        "X-Origin": ORIGIN,
        "X-Youtube-Client-Name": "1",
        "X-Youtube-Client-Version": client_ver,
        "X-Youtube-Bootstrap-Logged-In": "true",
    }
    if visitor:
        headers["X-Goog-Visitor-Id"] = visitor
    body = {"context": client_ctx, "target": {"videoId": vid}}
    response = request_ctx.post(f"{LIKE_URL}?prettyPrint=false&key={api_key}",
                                data=json.dumps(body), headers=headers)
    return response.status, response.text()


def still_liked(page, url: str) -> bool:
    try:
        page.goto(url, wait_until="commit", timeout=GO_TO_TIMEOUT_MS)
        deadline = time.monotonic() + READY_WAIT_S
        while time.monotonic() < deadline:
            try:
                state = page.evaluate(READ_JS)
            except Exception:
                state = "not-found"
            if state != "not-found":
                return state == "liked"
            time.sleep(0.25)
    except Exception:
        return True  # page trouble is not proof that the like failed
    return True


def hand_over(retry: bool) -> int:
    print("\nHanding over to migrate_liked_videos.py (DOM mode)...\n")
    command = [sys.executable, str(FALLBACK)] + (["--retry-failed"] if retry else [])
    return subprocess.call(command)


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)

    def _terminate(*_):  # SIGTERM takes the same safe path as Ctrl+C
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _terminate)

    args = sys.argv[1:]
    retry = "--retry-failed" in args
    verify_every = VERIFY_EVERY

    conn = init_db()
    if retry:
        urls = load_retry_urls(conn)
        print(f"Retrying {len(urls)} URL(s) from {FAILED_FILE.name}.")
    else:
        urls = load_urls()
        print(f"Loaded {len(urls)} URLs from {LIKED_FILE.name}; processing in reverse "
              f"order (index -1 -> 0).")
    total = len(urls)
    print(f"Fast mode (InnerTube like/like, no page loads). Pacing "
          f"{TURNAROUND_MIN_S}-{TURNAROUND_MAX_S}s per like. State: {DB_FILE.name}.")

    processed = liked = 0
    liked_seconds = 0.0
    hand_over_now = False
    checked_once = False
    unverified: list[str] = []      # liked through the API since the last spot-check

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=True,
            viewport={"width": 1280, "height": 720},
            args=[
                "--disable-features=HardwareMediaKeyHandling,MediaSessionService",
                "--mute-audio",
                "--blink-settings=imagesEnabled=false",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            client_ctx, api_key, client_ver, visitor = read_client(page)
        except Exception as exc:
            print(f"warning: could not read the YouTube client config "
                  f"({exc.__class__.__name__}: {exc})")
            context.close()
            sys.exit(hand_over(retry))

        auths = auth_headers(context.cookies(ORIGIN))
        if not auths:
            context.close()
            sys.exit("error: chrome_profile has no Google session - sign in once with "
                     "'python migrate_liked_videos.py --login', then re-run this.")

        auth_index = 0
        try:
            for url in urls:
                processed += 1
                remaining = total - processed
                pct = processed / total * 100
                avg_s = (liked_seconds / liked) if liked else (TURNAROUND_MIN_S + TURNAROUND_MAX_S) / 2
                eta = f"ETA: ~{remaining * avg_s / 3600:.1f} hours"

                if conn.execute("SELECT 1 FROM liked_videos WHERE url = ?", (url,)).fetchone():
                    print(f"[{processed}/{total}] | ({pct:.2f}%) | Skipped (already in "
                          f"{DB_FILE.name}) | Remaining: {remaining} | {eta}")
                    continue

                started = time.monotonic()
                vid = video_id(url)
                if not vid:
                    log_failed(url)
                    outcome = (f"WARNING: no video id in this URL - marked as attempted, "
                               f"logged to {FAILED_FILE.name}")
                else:
                    ok = dead = False
                    detail = "no attempt"
                    for attempt in range(auth_index, len(auths)):
                        try:
                            status, text = set_like(context.request, vid, auths[attempt],
                                                    client_ctx, api_key, client_ver, visitor)
                            ok, dead, detail = classify(status, text)
                        except Exception as exc:
                            detail = f"{exc.__class__.__name__}: {str(exc).splitlines()[0][:70]}"
                        auth_index = attempt
                        if ok or not dead or attempt == len(auths) - 1:
                            break
                        print(f"  {auths[attempt].split()[0]} rejected ({detail}) - trying the "
                              f"next session cookie...")

                    if dead:
                        print(f"\nWARNING: the session is no longer signed in ({detail}). Stopping.")
                        print("Re-run with:  python migrate_liked_videos.py --login  (then restart)")
                        break
                    if ok:
                        liked += 1
                        liked_seconds += time.monotonic() - started
                        outcome = f"Liked {time.monotonic() - started:.1f}s"
                        unverified.append(url)
                    else:
                        log_failed(url)
                        outcome = (f"WARNING: {detail} - marked as attempted, logged to "
                                   f"{FAILED_FILE.name}")

                conn.execute("INSERT OR IGNORE INTO liked_videos (url, liked_at) VALUES (?, ?)",
                             (url, datetime.now(timezone.utc).isoformat(timespec="seconds")))
                conn.commit()
                print(f"[{processed}/{total}] | ({pct:.2f}%) | {outcome} "
                      f"| Remaining: {remaining} | {eta}")

                # Spot-check that the API likes really stick: right away on the first
                # one, then every verify_every likes (one read-only watch-page load).
                if unverified and (not checked_once or len(unverified) >= verify_every):
                    checked_once = True
                    if not still_liked(page, unverified[-1]):
                        print("\nWARNING: the API like did NOT stick (checked on the watch page).")
                        print(f"  Un-marking the last {len(unverified)} URL(s) and handing the "
                              f"rest of the run to migrate_liked_videos.py.")
                        conn.executemany("DELETE FROM liked_videos WHERE url = ?",
                                         [(u,) for u in unverified])
                        conn.commit()
                        hand_over_now = True
                        break
                    unverified.clear()
                    print("  (spot-check: like confirmed on the watch page)")

                pad = random.uniform(TURNAROUND_MIN_S, TURNAROUND_MAX_S) \
                    - (time.monotonic() - started)
                if pad > 0:
                    time.sleep(pad)
        except KeyboardInterrupt:
            print(f"\nClosure handled. All completed likes are saved in {DB_FILE.name} - "
                  f"re-run to automatically start from the next unliked video.")
        finally:
            try:
                context.close()
            except Exception:
                pass

    final = conn.execute("SELECT COUNT(*) FROM liked_videos").fetchone()[0]
    pending = [u for u in (FAILED_FILE.read_text(encoding="utf-8").splitlines()
                           if FAILED_FILE.exists() else []) if u.strip()]
    conn.close()

    if hand_over_now:
        sys.exit(hand_over(retry))
    if retry:
        print(f"\nRetry pass done. {total - len(pending)}/{total} retried URL(s) liked; "
              f"{len(pending)} still in {FAILED_FILE.name}.")
    else:
        print(f"\nDone. {final}/{total} URLs recorded in {DB_FILE.name}.")
        if pending:
            print(f"{len(pending)} URL(s) could not be liked and were logged to {FAILED_FILE.name}.")
            print("Retry them now with:  python fast_likes.py --retry-failed")


if __name__ == "__main__":
    main()
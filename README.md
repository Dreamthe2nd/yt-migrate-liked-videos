
# yt-migrate-liked-videos

A minimal, high-throughput **Python + Playwright** tool to re-like a list of YouTube videos on a new account[cite: 12]. 

Designed to bypass the standard Google Cloud YouTube Data API quota caps (which limit projects to ~200 likes/day) by utilizing direct **InnerTube API endpoints** and optimized headless browser interactions[cite: 12].

---

## Features

- **InnerTube API & DOM Fallback:** Likes videos rapidly via lightweight internal API calls (~1.0–1.6s per item) with automatic fallback to browser UI automation if validation fails[cite: 12].
- **Anti-Spam Pacing:** Built-in randomized jitter and turnaround delays to avoid triggering YouTube heuristic velocity limits[cite: 12].
- **State Persistence:** Tracks progress in a local `liked_state.db` (SQLite) after every video[cite: 12]. Safely resume at any point after Ctrl+C, system closures, or network drops[cite: 12].
- **Failure Logging:** Problematic links (e.g., deleted or private videos) are logged to `failed_likes.txt` without stalling the run and can be retried automatically[cite: 12].
- **Safe Authentication:** Stage 1 opens a clean, unautomated Chrome process for manual Google login, preventing bot-detection blocks during sign-in[cite: 12].

---

## Project Structure

```text
yt-migrate-liked-videos/
├── liked_videos.json         # Input: array of YouTube URLs (or one per line)
├── migrate_liked_videos.py   # DOM automation engine (fallback)
├── migrate_liked_videos_2.py # Fast InnerTube API engine
├── liked_state.db            # SQLite progress tracker (gitignored)
├── failed_likes.txt          # Logged failures for retrying (gitignored)
├── chrome_profile/           # Shared persistent browser profile (gitignored)
├── requirements.txt          # Python dependencies
├── install.bat               # Windows one-click setup
└── install.sh                # Linux / macOS one-click setup

```

---

## Exporting Liked Videos (Source Account)

To generate your `liked_videos.json` without dealing with Google Takeout:

1. Log into your **source account** on YouTube.
2. Go to your Liked Videos playlist: [youtube.com/playlist?list=LL](https://www.youtube.com/playlist?list=LL).
3. Scroll down to load all items into view.
4. Press `F12` to open DevTools, switch to the **Console** tab, paste the snippet below, and press **Enter**:

```javascript
(() => {
  const anchors = Array.from(document.querySelectorAll('a#video-title, a[href*="/watch?v="]'));
  const urls = anchors
    .map(a => a.href.split('&')[0])
    .filter((url, idx, arr) => arr.indexOf(url) === idx && url.includes('/watch?v='));

  if (urls.length === 0) {
    console.warn("No URLs found. Ensure playlist items have loaded.");
    return;
  }

  const blob = new Blob([JSON.stringify(urls, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'liked_videos.json';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);

  console.log(`Exported ${urls.length} liked videos to liked_videos.json!`);
})();

```

Move the downloaded `liked_videos.json` file into the root of this project folder.

---

## Installation

### Windows

Run the setup script:

```bat
install.bat

```

### Linux / macOS

Make the script executable and run:

```bash
chmod +x install.sh
./install.sh

```

### Manual Setup

```bash
python -m venv .venv

# Windows:
.venv\Scripts\activate.bat
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium

```

---

## Usage

### 1. Run the Migrator

#### Fast Mode (Recommended — ~1.3s / video)

Runs through the InnerTube endpoint without rendering full video pages:

```bash
python migrate_liked_videos_2.py

```

#### Standard UI Mode (DOM Automation)

Headless Chromium automation with aggressive media and image blocking:

```bash
python migrate_liked_videos.py

```

---

### 2. First-Time Authentication (Stage 1)

On the very first run (or when passing `--login`):

1. A clean Google Chrome window opens automatically.


2. Sign into your **destination** YouTube account.


3. Close the browser window when finished.


4. The authenticated session is preserved in `./chrome_profile/` for all subsequent runs.



---

### 3. Retrying Failed Likes

If any videos fail (due to network drops or temporary element lookup issues), replay just the failed items:

```bash
python migrate_liked_videos_2.py --retry-failed
# or
python migrate_liked_videos.py --retry-failed

```

---

## Performance & Optimization Notes

* **Network Routing:** Media streams, video chunk requests, images, and custom fonts are aborted at the network level to keep bandwidth and CPU usage minimal.
* **InnerTube vs. YouTube Data API v3:** The public Google Cloud API limits projects to 10,000 quota units/day (~200 likes daily). InnerTube endpoints interact directly with YouTube's internal service, avoiding project quota barriers.


* **Spot Checks:** The fast engine opens a real watch page every 100 items to ensure likes are actually persisting on your account. If a batch drops or encounters a rate limit, the script unmarks affected entries and falls back to DOM automation.



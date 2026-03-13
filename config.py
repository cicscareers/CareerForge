"""
Central configuration file for the Notion-to-CareerForge automation.

All the knobs and dials live here. If you need to tweak a timeout,
change a file path, or flip on dry-run mode, this is the only file
you need to touch. Nothing else in the project hard-codes these values.
"""

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
# Where to find the Notion CSV export, where to save progress, and where
# logs and screenshots end up.

CSV_PATH = "data/Employer Match ddd913f822e54cde99e819d90f97bf43_all.csv"
PROGRESS_FILE = "progress/checkpoint.json"
LOG_DIR = "logs/"

# ---------------------------------------------------------------------------
# Browser / CDP connection
# ---------------------------------------------------------------------------
# The automation talks to an already-open Chrome window over the Chrome
# DevTools Protocol (CDP). Make sure Chrome is running with
# --remote-debugging-port=9222 before you start.

CDP_URL = "http://localhost:9222"

# ---------------------------------------------------------------------------
# CareerForge URLs
# ---------------------------------------------------------------------------
# Base URL for the employer portal and the paginated employer list page.

CAREERFORGE_BASE_URL = "https://careerforge.us/cc-portal?tab=employers"

# ---------------------------------------------------------------------------
# Delays (in seconds)
# ---------------------------------------------------------------------------
# These slow the bot down so the CareerForge UI has time to react.
# Bump them up if you're on a slow connection. JITTER adds a small
# random extra wait so the timing looks more human.

ACTION_DELAY = 1.0              # Pause after each UI action (click, fill)
PAGE_LOAD_DELAY = 2.0           # Wait after page navigation
BETWEEN_EMPLOYERS_DELAY = 3.0   # Pause between processing each employer
SEARCH_DEBOUNCE_DELAY = 1.0     # Wait for search results to populate
DELAY_AFTER_SAVE = 2.0          # Wait after saving / closing the form
JITTER = 0.5                    # Max random extra delay added to sleeps

# ---------------------------------------------------------------------------
# Timeouts (in milliseconds)
# ---------------------------------------------------------------------------
# Playwright will throw if an action or navigation takes longer than these.

NAVIGATION_TIMEOUT_MS = 15_000  # Playwright navigation timeout
ACTION_TIMEOUT_MS = 3_000       # Playwright action timeout (click, fill)

# ---------------------------------------------------------------------------
# Retry settings
# ---------------------------------------------------------------------------
# How many times we retry a single employer before giving up and marking
# it as failed.

MAX_RETRIES_PER_EMPLOYER = 2

# ---------------------------------------------------------------------------
# Mode flags
# ---------------------------------------------------------------------------
# DRY_RUN: logs what *would* happen but never clicks or saves anything.
#           Great for testing without messing up real data.
# AUTO_MODE: skips the interactive "apply / edit / skip?" prompts and
#            just applies everything automatically.

DRY_RUN = False   # When True, log actions but don't click / save anything
AUTO_MODE = False  # When True, skip interactive prompts and auto-apply all

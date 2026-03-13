# 🏗️ Architecture: Notion → CareerForge Data Transfer

## 1. Overview

CareerForge is a career portal built on Bubble.io. It has **no API** — the only way to update employer data is by clicking through the web UI. We have ~1,232 employers in a Notion database whose custom properties (hire counts, sponsorship status, size, states, industry tags) need to get into CareerForge. Doing that by hand would take forever. So this project uses Python + Playwright to connect to an already-open Chrome browser (via CDP), search for each employer, open their properties modal, and fill in 5 fields automatically. The whole thing is built around the fact that Bubble.io renders everything as `<div>`s with no semantic HTML, which makes standard selectors useless and forces us into `page.evaluate()` JavaScript DOM walking and Material Icons text matching.

## 2. System Diagram

```
┌──────────────────┐
│  Notion CSV File  │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐       ┌─────────────────────────────┐
│  csv_parser.py   │       │  careerforge_properties.py  │
│  (parse, clean,  │──────▶│  (valid values, emoji maps, │
│   validate)      │       │   tag alias lookups)        │
└────────┬─────────┘       └─────────────────────────────┘
         │
         ▼
┌──────────────────┐       ┌──────────────────┐
│    main.py       │◀─────▶│  checkpoint.py   │
│  (TUI, retries,  │       │  (JSON progress, │
│   CLI args)      │       │   atomic writes)  │
└────────┬─────────┘       └──────────────────┘
         │                          ▲
         ▼                          │ saves after
┌──────────────────┐                │ each employer
│  automation.py   │────────────────┘
│  (Playwright/CDP │
│   browser work)  │
└────────┬─────────┘
         │ Chrome DevTools Protocol
         │ (localhost:9222)
         ▼
┌──────────────────┐
│  Chrome Browser  │
│  (logged into    │
│   CareerForge)   │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   CareerForge    │
│   (Bubble.io)    │
└──────────────────┘
```

[`config.py`](config.py) is referenced by almost every module — it holds all delays, timeouts, URLs, file paths, and feature flags.

## 3. Module Breakdown

### [`main.py`](main.py) — Entry Point & Interactive TUI (~787 lines)

This is what you run. It parses CLI flags, shows a colorful terminal preview for each employer, and drives the whole processing loop with retries and checkpointing.

| Function | What it does |
|---|---|
| [`setup_logging()`](main.py:65) | Dual logging — DEBUG to a timestamped log file, INFO to your terminal |
| [`parse_args()`](main.py) | CLI flags: `--dry-run`, `--auto`, `--csv`, `--start-from`, `--limit` |
| [`interactive_prompt()`](main.py) | Shows a preview of each employer's data, then a menu: **[a]**pprove, **[s]**kip, **[e]**dit, **[q]**uit |
| [`_edit_employer()`](main.py) | Sub-menu to change any of the 5 fields before approving |
| [`_pick_from_list()`](main.py) | Interactive multi-select picker (for states, industry tags) |
| [`_pick_one()`](main.py) | Single-select picker (for CICS Hires, Sponsoring, Size) |
| [`run()`](main.py) | The main async loop — parse CSV → load checkpoint → connect browser → process each employer with retries |

**Design decisions in this module:**

- **Interactive mode is the default.** You see a preview and approve each employer before anything gets clicked. This prevents accidentally blasting bad data into 1,200 records. Pass `--auto` once you trust your CSV.
- **Config mutation pattern.** [`main.py`](main.py) sets [`config.DRY_RUN`](config.py:73) and [`config.AUTO_MODE`](config.py:74) at runtime based on CLI flags. Other modules read those values from `config` directly — they don't take them as function args.
- **Retry logic.** Each employer gets up to [`MAX_RETRIES_PER_EMPLOYER`](config.py:63) attempts. If [`EmployerNotFoundError`](automation.py:45) is raised, that's a permanent failure — no retry. Any other exception (timeouts, stale DOM, etc.) triggers a retry with a delay.

---

### [`automation.py`](automation.py) — Browser Automation (~691 lines)

The heart of the project. Every Playwright interaction lives here.

| Function | What it does |
|---|---|
| [`connect_browser()`](automation.py:99) | Connects to Chrome via CDP at `localhost:9222` |
| [`navigate_to_employers_list()`](automation.py) | Goes to the CareerForge employers page |
| [`search_employer()`](automation.py) | Types the name into the search box (with fallback selectors) |
| [`click_employer_row()`](automation.py) | **Most complex function.** Uses `page.evaluate()` JS to walk Bubble's div DOM and click the right row |
| [`click_edit_custom_properties()`](automation.py) | Opens the "Edit Custom Properties" modal |
| [`select_radio()`](automation.py) | Picks a radio button with 2 fallback strategies. Skips if already selected. |
| [`select_checkboxes()`](automation.py) | Checks multiple checkboxes with 3 strategies (exact text, partial text, emoji-prefixed label) |
| [`close_modal()`](automation.py) | Clicks Close. There's no Save — changes apply immediately in CareerForge. |
| [`process_employer()`](automation.py) | Top-level function: runs all 6 steps for one employer |
| [`screenshot_on_error()`](automation.py:72) | Saves a full-page PNG to `logs/` with employer name + timestamp |

**Why CDP (Chrome DevTools Protocol)?**

We connect to a Chrome instance that you've already logged into CareerForge manually. This sidesteps all login/auth/2FA complexity. You launch Chrome with `--remote-debugging-port=9222`, log in once, and the script takes over from there.

**The Bubble.io DOM problem:**

Normal HTML has `<input type="radio">`, `<input type="checkbox">`, `<label>` elements. Playwright can find those with `get_by_role()` or `get_by_label()`. Bubble.io gives you this instead:

```html
<div class="clickable-element bubble-element">
  <div class="bubble-element">
    <i class="material-icons">radio_button_unchecked</i>
  </div>
  <div class="bubble-element">Large</div>
</div>
```

Everything is a `<div>`. No `name`, `value`, or `for` attributes. No real form elements at all.

**How we detect selection state:**

We read the text inside the `<i class="material-icons">` element:

| Icon text | Meaning |
|---|---|
| `radio_button_checked` | Radio is selected |
| `radio_button_unchecked` | Radio is not selected |
| `check_box` | Checkbox is checked |
| `check_box_outline_blank` | Checkbox is unchecked |

Before clicking anything, we check this text first. If it's already in the right state, we skip the click (important — clicking a checked checkbox would uncheck it).

**Multiple fallback selector strategies:**

Bubble.io's DOM is inconsistent. Class names shift, element nesting changes. So almost every interaction has 2–3 strategies:

- **Radio buttons:** (1) find label text → click parent clickable-element, (2) find all clickable-elements in section → match by inner text
- **Checkboxes:** (1) exact text match, (2) partial/contains match, (3) emoji-prefixed label match (for industry tags like "🤖 Robotics")
- **Search box:** (1) find input by placeholder, (2) CSS selector fallbacks

**Why `click_employer_row()` uses `page.evaluate()`:**

The employer list is a wall of identically-classed `<div>`s. Playwright selectors can't reliably tell employer rows apart from sidebar links or other clickable things. So we inject JavaScript that walks the DOM, filters out navigation elements, and matches by text content. It's the only reliable approach for Bubble.io's structure.

---

### [`csv_parser.py`](csv_parser.py) — CSV Parsing & Validation (~504 lines)

Takes the raw Notion CSV export and turns it into clean, validated employer dicts.

| Function | What it does |
|---|---|
| [`_normalise_header()`](csv_parser.py:61) | Strips emoji and non-ASCII characters from column headers, lowercases |
| [`_resolve_columns()`](csv_parser.py:77) | Maps CSV column indices to our field names (e.g., column 0 → `"name"`) |
| [`_split_multi()`](csv_parser.py) | Splits comma-separated values into lists |
| [`_validate_cics_hires()`](csv_parser.py) | Checks value against the whitelist in [`careerforge_properties.py`](careerforge_properties.py) |
| [`_validate_sponsoring()`](csv_parser.py) | Checks value against the whitelist |
| [`_validate_size()`](csv_parser.py) | Checks value against the whitelist |
| [`_validate_states()`](csv_parser.py) | Validates states, supports aliases |
| [`_validate_industry_tags()`](csv_parser.py) | Maps CSV tags → CareerForge tags via alias lookup. Returns matched, skipped, and raw lists. |
| [`parse_csv()`](csv_parser.py) | The public entry point. Reads file, returns `list[dict]` of employer data. |

**BOM handling:** Notion exports CSV files with a UTF-8 BOM (byte order mark) at the start. We open with `encoding="utf-8-sig"` which strips it automatically. Without this, the first column header would be `"\ufeffEmployer"` instead of `"Employer"`.

**Emoji stripping:** Notion headers look like `"🏢 Employer"` or `"🏷️ Industry Tags"`. The regex `[^\x00-\x7F]+` strips all non-ASCII characters so we can match against our plain-text column map.

**Industry tag mapping system:** The CSV might say `"pit"`, `"common good"`, `"ecommerce"`, or `"Public Interest Technology (PIT)"`. They all need to map to the correct CareerForge tag. [`INDUSTRY_TAG_MAPPING`](careerforge_properties.py:97) is a case-insensitive dict that handles all these aliases. Tags with no CareerForge equivalent (like "Tech", "Banking") get flagged as skipped so the TUI can show you what was dropped.

**URL-to-company-name extraction:** Some Notion entries have a URL instead of a company name (someone pasted a link). The parser detects this, extracts the domain or LinkedIn path, and converts it to a usable name. For example: `https://eyepointpharma.com/about/` → `"Eyepointpharma"`, `linkedin.com/company/weaver-biosciences` → `"Weaver Biosciences"`.

---

### [`checkpoint.py`](checkpoint.py) — Progress Tracking (~259 lines)

JSON-based checkpoint system so you can stop and resume anytime.

| Function | What it does |
|---|---|
| [`load_progress()`](checkpoint.py) | Loads checkpoint from disk. Returns empty checkpoint if file doesn't exist. |
| [`is_processed()`](checkpoint.py) | Was this employer completed successfully? |
| [`is_skipped()`](checkpoint.py) | Was this employer manually skipped? |
| [`mark_completed()`](checkpoint.py) | Records success, saves immediately |
| [`mark_failed()`](checkpoint.py) | Records failure + reason, saves immediately |
| [`mark_skipped()`](checkpoint.py) | Records manual skip, saves immediately |
| [`_save()`](checkpoint.py:72) | Atomic write: temp file → `os.replace()` |

**Atomic writes — why they matter:**

If the script crashes mid-write, a half-written JSON file would corrupt your progress. We avoid this by writing to a temp file first, then using `os.replace()` to swap it in:

```python
with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False) as fh:
    json.dump(data, fh, indent=2)
    tmp_path = fh.name
os.replace(tmp_path, real_path)
```

`os.replace()` is atomic on most OSes. The checkpoint file is always either the old version or the new version — never a broken in-between state.

**Resume semantics:**

| Status | On next run |
|---|---|
| `completed` | Skip — already done |
| `failed` | Retry — might work this time |
| `skipped` | Skip — user chose to skip |

To start fresh, just delete [`progress/checkpoint.json`](progress/checkpoint.json).

---

### [`config.py`](config.py) — Configuration (~70 lines)

All constants in one place. Nothing else in the project hard-codes these values.

See [Section 5](#5-configuration-reference) for the full reference table.

---

### [`careerforge_properties.py`](careerforge_properties.py) — Valid Values & Tag Mappings (~132 lines)

Pure data, no functions. Defines every value CareerForge accepts.

- [`VALID_CICS_HIRES`](careerforge_properties.py:23): `["1", "2", "3+", "4+", "5+", "10+", "20+", "50+", "100+"]`
- [`VALID_SPONSORING`](careerforge_properties.py:29): `["Yes (CPT/OPT)", "Sometimes", "No"]`
- [`VALID_SIZES`](careerforge_properties.py:49): `["Small", "Medium", "Large"]`
- [`VALID_STATES`](careerforge_properties.py:36): All 50 US states + DC, Remote, International (53 total)
- [`VALID_INDUSTRY_TAGS`](careerforge_properties.py:55): 14 tags (FinTech, Healthcare, AI, Robotics, PIT, Cybersecurity, Defense, Consulting, Research, Startup, Networking, Hardware, E-commerce, Gaming)
- [`INDUSTRY_TAG_EMOJI_MAP`](careerforge_properties.py:69): Maps plain tag → emoji-prefixed display string (e.g., `"AI"` → `"✨ AI"`)
- [`INDUSTRY_TAG_MAPPING`](careerforge_properties.py:97): Case-insensitive alias dict (e.g., `"pit"` → `"Public Interest Technology (PIT)"`)
- `_VALID_*_SET`: Set versions for O(1) membership checks during validation

## 4. Data Flow Walkthrough

Here's what happens when you run `python main.py --auto`, step by step:

1. **CLI parsing.** [`parse_args()`](main.py) reads `--auto` and sets [`config.AUTO_MODE = True`](config.py:74).
2. **Logging setup.** [`setup_logging()`](main.py:65) creates a log file at `logs/run_YYYYMMDD_HHMMSS.log` (DEBUG level) and attaches a console handler (INFO level).
3. **CSV parsing.** [`csv_parser.parse_csv()`](csv_parser.py) opens the Notion CSV with `utf-8-sig` encoding, strips emoji from headers, maps columns to field names, validates every value against the whitelists in [`careerforge_properties.py`](careerforge_properties.py), and returns a list of clean employer dicts.
4. **Checkpoint loading.** [`checkpoint.load_progress()`](checkpoint.py) reads `progress/checkpoint.json`. If the file doesn't exist, returns an empty checkpoint.
5. **Browser connection.** Playwright connects to Chrome via CDP at `localhost:9222`. Returns a page object.
6. **Main loop starts.** For each employer in the parsed list:
   - **a.** Check if already completed or skipped → skip if so.
   - **b.** In interactive mode, show a preview and wait for approval. In `--auto` mode, auto-approve.
   - **c.** Call [`automation.process_employer()`](automation.py) which runs the 6-step browser workflow:
     1. Navigate to the employers list page
     2. Type the employer name into search, wait for debounce
     3. Click the matching employer row (via `page.evaluate()` JS)
     4. Click "Edit Custom Properties" to open the modal
     5. Fill in radio buttons (CICS Hires, Sponsoring, Size) and checkboxes (States, Industry Tags)
     6. Close the modal (changes save automatically)
   - **d.** On success: [`checkpoint.mark_completed()`](checkpoint.py) — atomic write to disk.
   - **e.** On `EmployerNotFoundError`: [`checkpoint.mark_failed()`](checkpoint.py) — permanent, no retry.
   - **f.** On other exceptions: retry up to [`MAX_RETRIES_PER_EMPLOYER`](config.py:63) times. Screenshot saved on each failure.
   - **g.** Sleep for [`BETWEEN_EMPLOYERS_DELAY`](config.py:44) + random jitter before the next employer.
7. **Summary.** After all employers are processed, logs a final summary (completed, failed, skipped counts).

## 5. Configuration Reference

All values live in [`config.py`](config.py):

| Constant | Default | What it controls |
|---|---|---|
| `CSV_PATH` | `"data/Employer Match...csv"` | Path to your Notion CSV export |
| `PROGRESS_FILE` | `"progress/checkpoint.json"` | Where checkpoint data is saved |
| `LOG_DIR` | `"logs/"` | Log files and error screenshots |
| `CDP_URL` | `"http://localhost:9222"` | Chrome DevTools Protocol endpoint |
| `CAREERFORGE_BASE_URL` | `"https://careerforge.us/cc-portal?tab=employers"` | CareerForge employers page |
| `ACTION_DELAY` | `1.0` s | Pause after each UI action (click, fill) |
| `PAGE_LOAD_DELAY` | `2.0` s | Wait after page navigation |
| `BETWEEN_EMPLOYERS_DELAY` | `3.0` s | Pause between employers |
| `SEARCH_DEBOUNCE_DELAY` | `1.0` s | Wait for search results after typing |
| `DELAY_AFTER_SAVE` | `2.0` s | Wait after closing the modal |
| `JITTER` | `0.5` s | Max random extra delay (humanizes timing) |
| `NAVIGATION_TIMEOUT_MS` | `15000` ms | Playwright navigation timeout |
| `ACTION_TIMEOUT_MS` | `3000` ms | Playwright element interaction timeout |
| `MAX_RETRIES_PER_EMPLOYER` | `2` | Retry attempts before marking as failed |
| `DRY_RUN` | `False` | Log actions but don't click anything |
| `AUTO_MODE` | `False` | Skip interactive prompts, auto-approve all |

## 6. Error Handling

Errors fall into three categories:

| Error type | Example | Retries? | What happens |
|---|---|---|---|
| [`EmployerNotFoundError`](automation.py:45) | Search returns 0 results | **No** — permanent | Marked as failed, logged to `progress/not_found.log` |
| `PlaywrightTimeout` | Element didn't appear in time | **Yes** | Retry up to `MAX_RETRIES_PER_EMPLOYER` times |
| Generic `Exception` | Unexpected DOM state, stale element | **Yes** | Same retry logic as timeouts |

**On any failure**, [`screenshot_on_error()`](automation.py:72) saves a full-page screenshot to `logs/`:

```
logs/error_Google_20260304_112433.png
```

These are super helpful for debugging — you can see exactly what the page looked like when things broke.

**Retry flow (pseudocode):**

```python
for attempt in range(1, MAX_RETRIES_PER_EMPLOYER + 1):
    try:
        process_employer(employer)
        mark_completed(employer)
        break  # success!
    except EmployerNotFoundError:
        mark_failed(employer, "not found")
        break  # permanent, don't retry
    except Exception:
        screenshot_on_error(page, employer_name)
        if attempt == MAX_RETRIES_PER_EMPLOYER:
            mark_failed(employer, error_message)
        # otherwise: wait a bit and loop again
```

**Logging:** Every run creates `logs/run_YYYYMMDD_HHMMSS.log`. The file gets DEBUG-level detail; your terminal only shows INFO-level highlights.

## 7. Key Design Decisions

| # | Decision | Why |
|---|---|---|
| 1 | **CDP instead of launching a new browser** | Avoids login/auth/2FA. You log in manually once, the script connects to that session. Way simpler and more reliable. |
| 2 | **Browser automation (no API)** | CareerForge (Bubble.io) has zero API. Clicking through the UI is literally the only option. |
| 3 | **Atomic checkpoint writes** | If the process crashes mid-save, the checkpoint file won't be corrupted. Critical for a long-running batch process. |
| 4 | **Interactive mode as the default** | Safety net — you review each employer before anything gets clicked. Prevents accidentally pushing bad data to 1,200 records. Switch to `--auto` once you trust the CSV. |
| 5 | **Multiple selector fallback strategies** | Bubble.io's DOM is inconsistent across page loads. Having 2–3 fallback approaches per interaction keeps the script working when the DOM shifts. |
| 6 | **Jitter / random delays** | Perfectly regular timing looks robotic. Random jitter makes the traffic pattern more natural and reduces risk of rate limiting or bot detection. |
| 7 | **Emoji prefix matching for industry tags** | CareerForge displays tags like "🤖 Robotics" and "✨ AI". We maintain an emoji map so the script can match these DOM labels correctly. |
| 8 | **URL-to-name extraction in CSV parser** | Some Notion entries are URLs instead of company names. The parser detects this and extracts the company name from the domain/path rather than failing. |

## 8. Known Limitations

- **Bubble.io DOM can change at any time.** If Bubble updates its rendering, our selectors could break with no warning. There's no versioned DOM contract.
- **~58% of Notion employers don't exist in CareerForge.** Name mismatches between the two systems (abbreviations, punctuation, "Inc." vs "Inc") cause many `EmployerNotFoundError`s.
- **No test suite.** The project has no automated tests. Testing browser automation against a live third-party app is hard, but unit tests for CSV parsing and checkpoint logic would help.
- **Single-threaded.** One employer at a time, sequentially. Parallel processing could speed things up, but coordinating multiple browser tabs on Bubble.io's flaky DOM would be a nightmare.
- **No undo.** CareerForge saves changes immediately when you interact with the modal. There's no "revert" button, and the script doesn't record previous values.

# Notion → CareerForge Transition Tool 🚀

A Python script that reads employer data from a Notion CSV export and automatically enters it into CareerForge's web UI — because CareerForge has no API and doing 1,200+ employers by hand is not happening.

## 🤔 Why does this exist?

CareerForge is the web app where we track employer info. It's built on Bubble.io. Bubble.io has **no public API**. Zero. Zilch.

We have ~1,200 employers sitting in a Notion database with all their custom properties filled out. The only way to get that data into CareerForge is to literally click through the web UI for each one — open the employer, open the modal, click radio buttons, check checkboxes, save, repeat.

That would take... forever. So we built this tool to do the clicking for us.

## ⚡ How it works

Here's the short version of what happens when you run it:

1. Reads your Notion CSV export from the `data/` folder
2. Connects to your already-open Chrome/Brave browser via Chrome DevTools Protocol
3. Navigates to the CareerForge employer list
4. Searches for each employer by name
5. Opens their "Custom Properties" modal
6. Fills in all 5 fields (radio buttons + checkboxes)
7. Saves, closes the modal, and moves to the next one

The 5 fields it fills in:

- **CICS Hires** (radio button) — 1, 2, 3+, 10+, 100+, etc.
- **Sponsoring?** (radio button) — Yes (CPT/OPT), Sometimes, No
- **Size** (radio button) — Small, Medium, Large
- **State(s)** (checkboxes) — MA, CA, Remote, International, etc.
- **Industry Tags** (checkboxes with emoji) — 💰 FinTech, ✨ AI, 🤖 Robotics, etc.

## 📋 What you need

Before you start, make sure you have:

- **Python 3.10+**
- **Chrome or Brave browser**
- **A Notion CSV export** of the employer database (goes in the `data/` folder)
- **Access to CareerForge** — you need to be logged in already

## 🛠️ Getting set up

### 1. Clone the repo

```bash
git clone https://github.com/your-username/NotiontoCareerForgeTransition.git
cd NotiontoCareerForgeTransition
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Add your data

Export your Notion database as a CSV and drop it in the `data/` folder.

### 4. Start your browser with remote debugging

You need to launch Chrome or Brave with a special flag so the script can connect to it.

**macOS (Chrome):**
```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
```

**macOS (Brave):**
```bash
/Applications/Brave\ Browser.app/Contents/MacOS/Brave\ Browser --remote-debugging-port=9222
```

**Windows (Chrome):**
```bash
chrome.exe --remote-debugging-port=9222
```

### 5. Log into CareerForge

Open CareerForge in that browser window and log in. The script takes over from here — it doesn't open a new browser, it connects to the one you already have open.

### 6. Run the tool

```bash
python main.py
```

## 🎮 How to run it

There are a bunch of ways to run this depending on what you need:

```bash
# Interactive mode (asks you to approve each employer)
python main.py

# Auto mode (processes everything without asking)
python main.py --auto

# Dry run (just shows what would happen, doesn't click anything)
python main.py --dry-run

# Start from a specific employer (skip the first N)
python main.py --start-from 50

# Limit how many employers to process
python main.py --limit 10

# Combine flags
python main.py --auto --dry-run --limit 20

# Use a different CSV file
python main.py --csv data/my_other_export.csv
```

## 🕹️ Interactive mode controls

When you run without `--auto`, you get a prompt for each employer. Here's what you can do:

| Key | What it does |
|-----|--------------|
| `[a]` Approve | Process this employer — go ahead and fill in their data |
| `[s]` Skip | Skip this one and move on to the next |
| `[e]` Edit | Change the data before processing (opens a sub-menu) |
| `[q]` Quit | Stop the whole run — progress is saved, you can resume later |

## 📁 Project structure

```
├── main.py                  # entry point — CLI args, interactive UI, main loop
├── automation.py            # all the browser stuff — Playwright, clicking, typing
├── config.py                # settings — URLs, delays, timeouts, file paths
├── csv_parser.py            # reads the Notion CSV and validates everything
├── checkpoint.py            # tracks progress so you can resume later
├── careerforge_properties.py # valid values for all 5 CareerForge fields
├── requirements.txt         # Python dependencies
├── data/                    # put your Notion CSV exports here
├── logs/                    # run logs + error screenshots (auto-generated)
└── progress/                # checkpoint file + summary logs (auto-generated)
```

## 🧭 How to navigate this codebase

If you're new to this repo, use this path:

1. Start with `main.py`
   - Overall control flow, CLI flags, interactive approvals, retries.
2. Then read `automation.py`
   - Browser/CDP logic for the employer custom-properties workflow.
3. Then `csv_parser.py` + `careerforge_properties.py`
   - Data cleaning/validation and allowed field values/tag mappings.
4. Then `checkpoint.py`
   - Resume logic and progress persistence behavior.
5. Finally `config.py`
   - Timeouts, delays, paths, and runtime toggles.

For the contacts-sharing automation added on this branch:

- Start at `ContactSharing/run_contacts_sharing.py` (thin launcher).
- Main logic is in `ContactSharing/contacts_sharing_workflow.py`.
- Operational docs for that workflow:
  - `ContactSharing/README.md`
  - `ContactSharing/WORKFLOW.md`

## 💾 Progress & resuming

You don't have to do all 1,200 employers in one sitting. The tool has your back:

- Progress is saved after **every single employer** to `progress/checkpoint.json`
- If you stop and restart, it picks up where you left off
- Successfully processed employers are automatically skipped on re-runs
- Failed employers get retried on the next run
- Employers you manually skipped stay skipped (unless you remove them from the checkpoint)

Basically, you can `Ctrl+C` whenever you want and nothing is lost.

## 🔧 Troubleshooting

| Problem | Fix |
|---------|-----|
| "Cannot connect to browser" | Make sure Chrome/Brave is running with `--remote-debugging-port=9222`. Only one instance can use that port at a time. |
| "Employer not found" | The employer name in your Notion CSV might not match what's in CareerForge exactly. Check `progress/not_found.log`. |
| Elements not clicking | Bubble.io's DOM is weird — the tool has fallback strategies, but sometimes the site is just slow. Try increasing delays in `config.py`. |
| Tool seems stuck | Check `logs/` for the latest log file — it has detailed info about what happened. |
| Want to reprocess an employer | Delete them from `progress/checkpoint.json` and re-run. |
| Error screenshots | When something fails, a screenshot gets saved to `logs/` so you can see what the browser was showing. |

## 🧰 Tech stack

- **Python 3.10+**
- **Playwright** — browser automation (async API)
- **Chrome DevTools Protocol (CDP)** — connects to your already-running browser
- **Colorama** — colored terminal output so the interactive UI looks nice

## ⚠️ A note about Bubble.io

CareerForge is built on Bubble.io, which is... interesting to automate. Bubble renders pretty much everything as generic `<div>` elements with no semantic HTML. There are no proper `<input>`, `<button>`, or `<label>` tags to grab onto.

So we can't use normal CSS selectors like you would on a regular website. Instead, the code uses creative workarounds — like checking Material Icons text content to figure out if a radio button is selected, or matching elements by their visible text rather than class names. It's hacky, but it works.

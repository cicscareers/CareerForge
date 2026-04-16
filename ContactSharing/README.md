## Contact Sharing Automation (CareerForge Contacts)

Automates the CareerForge contacts sharing workflow for **Friendly Alum** contacts using Playwright over CDP.

## What It Does

For each run, the script:

1. Opens the Contacts tab (`tab=employers`, `emp_tab=contacts`).
2. Applies **Filter Contacts → Recruiting Role = Friendly Alum**.
3. Iterates contact rows through the `more_vert` action menu.
4. Opens **Share with Candidates → Sharing Settings**.
5. Enforces sharing rules:
   - Ensures **Share `<name>` with Candidates** toggle is ON.
   - If LinkedIn/Email/Phone are all OFF, enables only **Share LinkedIn**.
   - Leaves existing checkbox choices untouched otherwise.
6. Ensures Notes template is present; inserts it into TinyMCE when missing.
7. Saves changes and advances through contacts/pages until completion.

## Pagination + End Conditions

The script advances pages with URL query parameter `page_contacts` (for example `...&page_contacts=2`) and includes hard guards to avoid loops:

- Re-applies the Friendly Alum filter after URL navigation (Bubble can reset filters).
- Stops when requested page is redirected backward (for example page 5 request lands on page 1).
- Stops when footer range (`N - M of T`) does not advance.
- Stops when visible rows are already processed and no next page is available.

## Notes Template

Template includes clickable links for:

- **Guide to Networking** (Notion)
- **Career Coaching Meeting** (CareerForge student portal)

## Requirements

- Python 3.10+
- Playwright Chromium dependencies
- Chrome/Brave running with remote debugging on `9222`
- CareerForge logged in on that browser session

Install:

```bash
python3 -m pip install -r requirements.txt
playwright install chromium
```

## Run

From the `CareerForge` repo root:

```bash
python3 ContactSharing/run_contacts_sharing.py
```

Useful flags:

```bash
python3 ContactSharing/run_contacts_sharing.py --fast
python3 ContactSharing/run_contacts_sharing.py --dry-run
python3 ContactSharing/run_contacts_sharing.py --cdp-url http://localhost:9222
```

## Troubleshooting

- **No contact rows detected:** verify you are on the contacts tab and logged in on the CDP browser.
- **Filter modal does not open:** script falls back to manual open window; click filter icon when prompted.
- **Stuck on one page:** check logs for pagination/footer messages (`range did not advance`, `requested page ... landed on ...`).
- **Template not inserted:** ensure the Sharing Settings modal is visible and TinyMCE loads.

See `ContactSharing/WORKFLOW.md` for implementation-level details.


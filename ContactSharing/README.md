## Contact Sharing Automation (CareerForge Contacts)

Automates the **Contacts** flow on CareerForge:

- Filter contacts to **Recruiting Role = Friendly Alum**
- For each matching contact row:
  - Open the `...` row menu
  - Click **Share with Candidates**
  - In **Sharing Settings**:
    - If the top "Share … with Candidates" toggle is **OFF**:
      - Turn it **ON**
      - Enable **Share LinkedIn**
      - Disable **Share Email** and **Share Phone**
      - Save
    - If the top toggle is **ON**:
      - Paste the provided template into **Notes to Candidates**
      - Save

### Setup

This uses the same Playwright/CDP approach as the existing `CareerForge` automation:

1. Install deps (from `CareerForge/requirements.txt`)
2. Install Playwright browsers:

```bash
python3 -m pip install -r CareerForge/requirements.txt
playwright install chromium
```

3. Start Chrome/Brave with remote debugging:

```bash
/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222
```

4. Log into CareerForge in that browser.

### Run

```bash
python3 ContactSharing/run_contacts_sharing.py
```

Optional:

```bash
python3 ContactSharing/run_contacts_sharing.py --dry-run
```


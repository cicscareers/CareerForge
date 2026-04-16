# Contact Sharing Workflow Reference

This document describes the behavior of `ContactSharing/contacts_sharing_workflow.py`.

## Entry Point

- CLI launcher: `ContactSharing/run_contacts_sharing.py`
- Main module: `ContactSharing/contacts_sharing_workflow.py`
- Async runner: `run_once(settings)`

## Runtime Model

- Connects to an already-authenticated Chromium session via CDP (`http://localhost:9222` by default).
- Creates a fresh tab in the existing browser context.
- Uses short action delays and jitter to improve reliability with Bubble rendering.

## Core Steps

1. `goto_contacts()`
   - Navigates to contacts URL.
   - Waits for contact search UI hydration.
2. `open_filter_modal()` + `set_recruiting_role_filter_to_friendly_alum()`
   - Applies Friendly Alum recruiting role filter.
   - Uses role-based and text-based fallbacks for Bubble modals.
3. `_open_row_menu_for_first_visible_contact()`
   - Iterates visible `more_vert` row menu buttons using `row_cursor`.
   - Skips previously processed rows using `processed_names`.
4. `open_share_with_candidates_from_menu()`
   - Clicks `Share with Candidates` menu item.
5. `apply_sharing_settings()`
   - Ensures top share toggle is ON.
   - Checks LinkedIn/Email/Phone states.
   - Enables LinkedIn only when all three are OFF.
   - Detects Notes template in TinyMCE and inserts if missing.
   - Saves with retry until modal closes.
6. Pagination
   - `_contacts_table_go_next_page()` increments `page_contacts`.
   - Re-applies Friendly Alum filter after page transition.
   - Verifies movement using URL page number and footer range text.

## Pagination Safeguards

The script returns "no next page" when any of these happen:

- Requested next page redirects backward (for example `page_contacts=5` lands on page 1).
- Footer range does not advance (or moves backward).
- Current and next range text are unchanged after polling.

This prevents infinite loops when Bubble falls back to page 1 for out-of-range requests.

## Notes Template Rules

- Reads the modal-scoped TinyMCE body (`iframe.tox-edit-area__iframe` → `body#tinymce`).
- Uses phrase checks (including `Networking Essentials`) to determine whether template already exists.
- Writes line-by-line HTML paragraphs and preserves inline links via `innerHTML`.

## Key Settings

`Settings` fields:

- `action_timeout_ms`, `navigation_timeout_ms`
- `action_delay_s`, `page_load_delay_s`, `jitter_s`
- `dry_run`, `cdp_url`

`--fast` mode lowers delays/timeouts for throughput.

## Logging

- Uses INFO logs for workflow steps and stop conditions.
- Stores debug screenshots in `logs/` for modal/filter failures.

## Known Operational Assumptions

- CareerForge DOM uses Bubble-generated classes and icon text labels.
- Material icon text (`toggle_on`, `check_box`, etc.) remains stable.
- User is already authenticated in the CDP-connected browser.

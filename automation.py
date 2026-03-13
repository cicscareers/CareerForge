"""
automation.py - All the Playwright browser automation for CareerForge.

This is where the actual browser clicking happens. We connect to an already-running
Chrome window (via CDP), navigate around the CareerForge site, search for employers,
and fill in their custom properties.

Big picture: CareerForge is built on Bubble.io, which means the DOM is basically
all divs. No semantic HTML, no real form elements. So we have to get creative with
our selectors and use multiple fallback strategies for almost everything.

Key things to know:
    - The custom properties modal has NO Save button. Changes apply immediately.
    - There IS a Close button to dismiss the modal when we're done.
    - Industry tag labels have emoji prefixes (like "💰 FinTech").
    - Radio buttons and checkboxes are just divs with Material Icons text inside.
    - We check if something is already selected before clicking to avoid toggling it off.

The main function other files call is process_employer(), which runs the full
6-step workflow for a single employer.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
    TimeoutError as PlaywrightTimeout,
)

import config
import careerforge_properties as cfp


class EmployerNotFoundError(RuntimeError):
    """Raised when we search for an employer and it's just not on CareerForge.

    This is a permanent failure. Retrying won't help because the employer
    simply doesn't exist in the system. The retry loop in main.py knows
    to skip retries when it catches this.
    """
    pass

logger = logging.getLogger(__name__)

# ===========================================================================
#  Helper utilities
# ===========================================================================

async def _delay(base: float = config.ACTION_DELAY) -> None:
    """Pause for a bit to let the page catch up.

    Adds a small random jitter on top of the base delay so we don't
    look like a robot hammering the site at perfectly regular intervals.

    Args:
        base: The minimum number of seconds to sleep. Defaults to ACTION_DELAY from config.
    """
    await asyncio.sleep(base + random.uniform(0, config.JITTER))


async def screenshot_on_error(page: Page, employer_name: str) -> None:
    """Take a full-page screenshot and save it to the logs folder for debugging.

    Super helpful when something goes wrong and you want to see what the page
    looked like at that moment. The filename includes the employer name and a
    timestamp so you can match it to the log output.

    Args:
        page: The Playwright page object to screenshot.
        employer_name: Name of the employer we were processing (used in the filename).
    """
    # Clean up the employer name so it's safe for a filename
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in employer_name)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(config.LOG_DIR) / f"error_{safe_name}_{ts}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Screenshot saved → %s", path)
    except Exception as exc:
        logger.warning("Could not save screenshot: %s", exc)


# ===========================================================================
#  Browser connection
# ===========================================================================

async def connect_browser(playwright: Playwright) -> tuple[Browser, BrowserContext, Page]:
    """Connect to the user's already-running Chrome browser via CDP.

    This assumes you've started Chrome with remote debugging enabled
    (the --remote-debugging-port flag). We grab the first open tab
    and use that for all our automation.

    Args:
        playwright: The Playwright instance to use for connecting.

    Returns:
        A tuple of (browser, context, page). The caller is responsible
        for closing these when done.

    Raises:
        RuntimeError: If there are no open pages in the browser.
    """
    logger.info("Connecting to browser at %s …", config.CDP_URL)
    browser = await playwright.chromium.connect_over_cdp(config.CDP_URL)

    # Grab the default context (that's where the user is already logged in)
    context = browser.contexts[0]
    pages = context.pages
    if not pages:
        raise RuntimeError(
            "No open pages found in the browser. "
            "Please open CareerForge in the remote-debug Chrome first."
        )

    # Use the first tab and set up our timeouts
    page = pages[0]
    page.set_default_timeout(config.ACTION_TIMEOUT_MS)
    page.set_default_navigation_timeout(config.NAVIGATION_TIMEOUT_MS)

    logger.info("Connected — using page: %s", page.url)
    return browser, context, page


# ===========================================================================
#  Navigation helpers
# ===========================================================================

async def navigate_to_employers_list(page: Page, *, force: bool = False) -> None:
    """Go to the CareerForge employers list page.

    If we're already on the CareerForge site, we just click the sidebar
    "Employers" link to go back to the list (way faster than a full page load).
    If we're somewhere else entirely, we do a full page.goto().

    Args:
        page: The Playwright page to navigate.
        force: If True, always do a full page.goto() even if we're already on the site.
    """
    logger.debug("Navigating to employers list …")

    if config.DRY_RUN:
        logger.info("[DRY RUN] Would navigate to %s", config.CAREERFORGE_BASE_URL)
        return

    # Check if we're already on the CareerForge employers page
    current = page.url
    on_careerforge = "careerforge.us" in current and "tab=employers" in current

    if on_careerforge and not force:
        # We're already here, just click the sidebar link to refresh the list
        logger.debug("Already on employers page — clicking sidebar to return to list")
        try:
            sidebar = page.get_by_text("Employers", exact=True).first
            await sidebar.click()
            await _delay(config.ACTION_DELAY)
            return
        except Exception:
            logger.debug("Sidebar click failed — falling back to page.goto")

    # Full navigation as fallback
    await page.goto(config.CAREERFORGE_BASE_URL, wait_until="networkidle")
    await _delay(config.PAGE_LOAD_DELAY)


async def search_employer(page: Page, name: str) -> None:
    """Type an employer name into the search box on the list page.

    Tries a bunch of different selectors for the search input since Bubble.io
    apps can be inconsistent about how they label things.

    Args:
        page: The Playwright page with the employers list.
        name: The employer name to search for.
    """
    logger.debug("Searching for employer: %s", name)

    if config.DRY_RUN:
        logger.info("[DRY RUN] Would search for: %s", name)
        return

    # Try multiple selectors because Bubble.io search inputs are unpredictable
    # TODO: Verify selector if CareerForge DOM changes
    search_input = (
        page.get_by_placeholder("Search")               # most common
        .or_(page.get_by_placeholder("Search employers"))
        .or_(page.get_by_role("searchbox"))
        .or_(page.locator("input[type='search']"))
        .or_(page.locator("input[type='text']").first)   # last resort fallback
    )

    await search_input.click()
    await search_input.fill("")  # clear any leftover search text
    await _delay(0.3)
    await search_input.fill(name)
    await _delay(config.SEARCH_DEBOUNCE_DELAY)  # wait for the search results to filter


async def click_employer_row(page: Page, name: str) -> bool:
    """Find and click the row for a specific employer in the search results.

    This is the most complex function in the file because Bubble.io renders
    everything as divs. We use page.evaluate() to run JavaScript directly
    in the browser, which scans all div.clickable-element nodes and does
    exact matching first, then fuzzy matching as a fallback.

    The JS also filters out sidebar/nav items so we don't accidentally click
    "Employers" in the navigation instead of an actual employer row.

    Args:
        page: The Playwright page showing search results.
        name: The employer name we're looking for.

    Returns:
        True if we found and clicked a matching row, False otherwise.
        Returns False immediately if the page shows "Nothing found".
    """
    logger.debug("Looking for employer row: %s", name)

    if config.DRY_RUN:
        logger.info("[DRY RUN] Would click employer row: %s", name)
        return True

    # CareerForge is a Bubble.io app so the table is all divs.
    # No <tr>, <td>, or <a> tags to work with.
    # The employer name lives in a div.clickable-element somewhere.

    # Check if the page says "Nothing found" (no search results)
    nothing_found = page.get_by_text("Nothing found", exact=True)

    # Give Bubble.io some time to load the search results
    await _delay(config.SEARCH_DEBOUNCE_DELAY + 1.0)

    # Run JavaScript in the browser to find and click the right row.
    # This is way more reliable than Playwright locators for Bubble.io's
    # weird div-heavy DOM structure.
    result = await page.evaluate("""(searchName) => {
        // First check if the page says "Nothing found"
        const allText = document.body.innerText;
        if (allText.includes('Nothing found')) {
            return { status: 'nothing_found', debug: [] };
        }

        // Grab all clickable div elements and look through them
        const clickables = document.querySelectorAll('div.clickable-element');
        const debug = [];
        const searchLower = searchName.toLowerCase();
        const firstWord = searchName.split(/\\s+/)[0].toLowerCase();

        // These are sidebar/nav items we want to skip so we don't click them by mistake
        const skipSet = new Set([
            'employers', 'career coaching', 'events', 'jobs',
            'candidates', 'contacts', 'employer name', 'address',
            'engagement status', 'manage properties', ''
        ]);

        let bestMatch = null;
        let bestText = '';

        for (const el of clickables) {
            // Get the text content of this element
            const directText = Array.from(el.childNodes)
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => n.textContent.trim())
                .join('');
            const fullText = el.textContent.trim();
            const shortText = fullText.substring(0, 100);
            const classes = el.className.substring(0, 150);

            // Save debug info so we can log what we found if matching fails
            debug.push({
                direct: directText.substring(0, 80),
                full: shortText,
                cls: classes,
                tag: el.tagName
            });

            const textLower = fullText.toLowerCase();

            // Skip sidebar/nav items and elements that are too long (probably not employer names)
            if (skipSet.has(textLower)) continue;
            if (fullText.length > 200) continue;
            if (fullText.includes('\\n') && fullText.length > 50) continue;

            // Try exact match first (case-insensitive)
            if (textLower === searchLower) {
                el.click();
                return { status: 'clicked', text: fullText, match: 'exact', debug: debug };
            }

            // Fall back to partial match (one contains the other)
            if (textLower.includes(searchLower) || searchLower.includes(textLower)) {
                if (!bestMatch) {
                    bestMatch = el;
                    bestText = fullText;
                }
            }

            // Try matching just the first word (for names like "Google LLC")
            if (textLower.includes(firstWord) && firstWord.length >= 3) {
                if (!bestMatch) {
                    bestMatch = el;
                    bestText = fullText;
                }
            }
        }

        // If we found a fuzzy match but no exact match, click the fuzzy one
        if (bestMatch) {
            bestMatch.click();
            return { status: 'clicked', text: bestText, match: 'fuzzy', debug: debug };
        }

        return { status: 'not_found', debug: debug };
    }""", name)

    # Handle the result from our JavaScript
    if result["status"] == "nothing_found":
        logger.warning("Employer not on CareerForge (Nothing found): %s", name)
        return False
    elif result["status"] == "clicked":
        logger.debug(
            "Clicked employer row (%s match): '%s' → '%s'",
            result.get("match", "?"), name, result.get("text", "?")
        )
        await _delay(config.PAGE_LOAD_DELAY)
        return True

    # If we get here, we didn't find anything. Log some debug info about what we saw.
    debug_items = result.get("debug", [])
    if debug_items:
        logger.warning(
            "Employer row not found for '%s'. JS found %d clickable elements:",
            name, len(debug_items)
        )
        for item in debug_items[:10]:
            logger.warning(
                "  cls=%s full='%s'",
                item.get("cls", "")[:80], item.get("full", "")[:80],
            )
    else:
        logger.warning("Employer row not found — JS found 0 clickable elements for: %s", name)
    return False


# ===========================================================================
#  Edit-form helpers
# ===========================================================================
# These functions handle the custom properties modal (the popup that opens
# when you click "Edit Custom Properties" on an employer's detail page).

async def click_edit_custom_properties(page: Page) -> None:
    """Click the "Edit Custom Properties" button on the employer detail page.

    Uses multiple selector strategies because Bubble.io buttons are not always
    standard <button> elements with proper roles.

    Args:
        page: The Playwright page showing an employer's detail view.
    """
    logger.debug("Opening custom-properties editor …")

    if config.DRY_RUN:
        logger.info("[DRY RUN] Would click 'Edit Custom Properties'")
        return

    # Try several selectors, from most specific to most broad
    # TODO: Verify selector if CareerForge DOM changes
    edit_btn = (
        page.get_by_role("button", name="Edit Custom Properties")
        .or_(page.get_by_text("Edit Custom Properties"))
        .or_(page.locator("button:has-text('Edit Custom Properties')"))
        .or_(page.locator("button:has-text('Edit')"))  # broader fallback
    )

    await edit_btn.wait_for(state="visible", timeout=config.ACTION_TIMEOUT_MS)
    await edit_btn.click()
    await _delay(config.ACTION_DELAY)


async def _is_already_selected(page: Page, value: str, icon_on: str) -> bool:
    """Check if a radio button or checkbox is already in the "on" state.

    Bubble.io uses Material Icons to show the state of form controls.
    A selected radio shows "radio_button_checked" and a checked checkbox
    shows "check_box". We look for these icon texts to figure out if
    something is already selected.

    This check is important because clicking an already-selected item
    would toggle it OFF, which is not what we want.

    Args:
        page: The Playwright page with the modal open.
        value: The label text of the option to check (e.g. "10+", "MA").
        icon_on: The Material Icons text that means "selected"
                 (e.g. "radio_button_checked" or "check_box").

    Returns:
        True if the option is already selected, False otherwise.
        Also returns False if we can't find the element at all.
    """
    try:
        exact_text = page.get_by_text(value, exact=True)
        row = page.locator("div.clickable-element").filter(has=exact_text).first
        # Look at the Material Icons button inside this row
        icon_text = await row.locator("button.material-icons").first.inner_text()
        return icon_text.strip() == icon_on
    except Exception:
        return False


async def select_radio(page: Page, group_label: str, value: str) -> None:
    """Select a radio button option in the custom properties modal.

    Bubble.io doesn't use real <input type="radio"> elements. Instead,
    each option is a clickable div with a Material Icons button that shows
    either "radio_button_off" or "radio_button_checked".

    We try two strategies:
        1. Find the clickable-element div that contains the exact value text and click it.
        2. If that fails, click the text element directly (the click event bubbles up).

    If the radio is already selected, we skip it to avoid toggling it off.

    Args:
        page: The Playwright page with the modal open.
        group_label: The section heading like "CICS Hires" or "Sponsoring?" (used for logging).
        value: The option text to select (e.g. "10+", "Yes (CPT/OPT)", "Large").
    """
    logger.debug("Setting radio '%s' → %s", group_label, value)

    if config.DRY_RUN:
        logger.info("[DRY RUN] Would set radio '%s' → %s", group_label, value)
        return

    # Check if already selected so we don't accidentally toggle it off
    if await _is_already_selected(page, value, "radio_button_checked"):
        logger.debug("  ⏭️  Radio '%s' already selected — skipping", value)
        return

    # Strategy 1: Find the clickable-element div containing the exact text
    try:
        exact_text = page.get_by_text(value, exact=True)
        row = page.locator("div.clickable-element").filter(has=exact_text).first
        await row.scroll_into_view_if_needed()
        await row.click()
        await _delay(config.ACTION_DELAY)
        logger.debug("  → clicked Bubble radio row for '%s'", value)
        return
    except Exception as exc:
        logger.debug("  Strategy 1 (clickable parent) failed: %s", exc)

    # Strategy 2: Click the text element directly and hope the event bubbles up
    try:
        text_el = page.get_by_text(value, exact=True).first
        await text_el.scroll_into_view_if_needed()
        await text_el.click()
        await _delay(config.ACTION_DELAY)
        logger.debug("  → clicked text element directly for '%s'", value)
        return
    except Exception as exc:
        logger.debug("  Strategy 2 (text click) failed: %s", exc)

    logger.warning(
        "Could not find radio for '%s' = '%s' — skipping field", group_label, value
    )


async def select_checkboxes(
    page: Page,
    group_label: str,
    values: list[str],
    *,
    use_emoji_labels: bool = False,
) -> None:
    """Check one or more checkboxes in the custom properties modal.

    Similar to select_radio but handles multiple values. Bubble.io checkboxes
    are also just clickable divs with Material Icons showing "check_box" or
    "check_box_outline_blank".

    We try three strategies for each value:
        1. Find the clickable-element div with exact text and click it.
        2. Click the text element directly.
        3. (Only for emoji labels) Try a partial text match since emoji labels
           might not match exactly.

    If use_emoji_labels is True, we look up the emoji-prefixed display name
    from the careerforge_properties emoji map before trying to match.

    Skips any checkbox that's already checked to avoid unchecking it.

    Args:
        page: The Playwright page with the modal open.
        group_label: The section heading like "State(s)" or "Industry Tags" (used for logging).
        values: List of option labels to check (e.g. ["MA", "CA"] or ["FinTech"]).
        use_emoji_labels: If True, look up emoji-prefixed names for matching
                          (needed for industry tags which show as "💰 FinTech" in the DOM).
    """
    if not values:
        return

    logger.debug("Setting checkboxes '%s' → %s", group_label, values)

    if config.DRY_RUN:
        logger.info("[DRY RUN] Would check '%s' → %s", group_label, values)
        return

    for val in values:
        # Build a list of display strings to try matching against.
        # If we're using emoji labels, the emoji version gets tried first.
        candidates: list[str] = []
        if use_emoji_labels:
            emoji_val = cfp.INDUSTRY_TAG_EMOJI_MAP.get(val)
            if emoji_val:
                candidates.append(emoji_val)
        candidates.append(val)

        # Check if this checkbox is already checked before clicking
        already_on = False
        for display_val in candidates:
            if await _is_already_selected(page, display_val, "check_box"):
                logger.debug("  ⏭️  Checkbox '%s' already checked — skipping", val)
                already_on = True
                break
        if already_on:
            continue

        checked = False

        # Try each candidate display string with multiple strategies
        for display_val in candidates:
            if checked:
                break

            # Strategy 1: Find the clickable-element div with exact text
            try:
                exact_text = page.get_by_text(display_val, exact=True)
                row = page.locator("div.clickable-element").filter(has=exact_text).first
                await row.scroll_into_view_if_needed()
                await row.click()
                checked = True
                logger.debug("  ✓ %s (clickable parent, display='%s')", val, display_val)
                await _delay(0.3)
                break
            except Exception:
                pass

            # Strategy 2: Click the text element directly
            try:
                text_el = page.get_by_text(display_val, exact=True).first
                await text_el.scroll_into_view_if_needed()
                await text_el.click()
                checked = True
                logger.debug("  ✓ %s (text click, display='%s')", val, display_val)
                await _delay(0.3)
                break
            except Exception:
                pass

            # Strategy 3: Partial text match (for emoji labels that might not match exactly)
            if use_emoji_labels:
                try:
                    text_el = page.get_by_text(display_val, exact=False).first
                    row = page.locator("div.clickable-element").filter(has=text_el).first
                    await row.scroll_into_view_if_needed()
                    await row.click()
                    checked = True
                    logger.debug("  ✓ %s (partial match, display='%s')", val, display_val)
                    await _delay(0.3)
                    break
                except Exception:
                    pass

        if not checked:
            logger.warning(
                "Could not find checkbox for '%s' = '%s' — skipping", group_label, val
            )


async def close_modal(page: Page) -> None:
    """Click the Close button to dismiss the custom properties modal.

    Remember: there's no Save button in this modal. Changes are applied
    the moment you click a radio button or checkbox. The Close button
    just gets rid of the popup.

    Args:
        page: The Playwright page with the modal open.
    """
    logger.debug("Closing custom-properties modal …")

    if config.DRY_RUN:
        logger.info("[DRY RUN] Would click Close")
        return

    # Try finding the Close button by its id first, then by role/text
    close_btn = page.locator("button#primary").or_(
        page.get_by_role("button", name="Close", exact=True)
    )

    try:
        await close_btn.wait_for(state="visible", timeout=config.ACTION_TIMEOUT_MS)
        await close_btn.click()
        await _delay(config.DELAY_AFTER_SAVE)
        logger.debug("Modal closed.")
    except PlaywrightTimeout:
        logger.warning("Close button not found — modal may already be dismissed")


# ===========================================================================
#  Top-level: process a single employer
# ===========================================================================

async def process_employer(page: Page, employer: dict[str, Any]) -> None:
    """Run the full edit workflow for one employer. This is the main function.

    Here's what happens step by step:
        1. Navigate back to the employers list page.
        2. Search for the employer by name.
        3. Click the matching row in the search results.
        4. Click "Edit Custom Properties" to open the modal.
        5. Set all the radio buttons and checkboxes (changes apply immediately).
        6. Click "Close" to dismiss the modal.

    If the employer can't be found, raises EmployerNotFoundError so the caller
    knows not to bother retrying. For other errors, just raises normally and
    the retry loop in main.py will handle it.

    Args:
        page: The Playwright page to drive.
        employer: The employer dict from csv_parser with all the validated data.

    Raises:
        EmployerNotFoundError: If the employer doesn't exist on CareerForge.
    """
    name = employer["name"]
    logger.info("Processing employer: %s", name)

    # Step 1: Go to the employers list
    await navigate_to_employers_list(page)

    # Step 2: Search for this employer
    await search_employer(page, name)

    # Step 3: Click the matching row
    found = await click_employer_row(page, name)
    if not found:
        raise EmployerNotFoundError(f"Employer not found in search results: {name}")

    # Step 4: Open the edit modal
    await click_edit_custom_properties(page)

    # Step 5: Fill in each property (skip any fields that have no data from the CSV)
    if employer.get("cics_hires"):
        await select_radio(page, "CICS Hires", employer["cics_hires"])

    if employer.get("sponsoring"):
        await select_radio(page, "Sponsoring?", employer["sponsoring"])

    if employer.get("states"):
        await select_checkboxes(page, "State(s)", employer["states"])

    if employer.get("size"):
        await select_radio(page, "Size", employer["size"])

    if employer.get("industry_tags"):
        await select_checkboxes(
            page,
            "Industry Tags",
            employer["industry_tags"],
            use_emoji_labels=True,
        )

    # Step 6: Close the modal (no Save needed, changes already applied)
    await close_modal(page)

    logger.info("✅  Employer processed successfully: %s", name)

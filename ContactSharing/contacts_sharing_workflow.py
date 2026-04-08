from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import (
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)


logger = logging.getLogger(__name__)


CONTACTS_URL = "https://careerforge.us/cc-portal?tab=employers&emp_tab=contacts"

NOTES_TEMPLATE = """Networking Essentials

Focus on learning and answering questions important to you.

Let them decide how to help -- don't just ask for referrals.

Thank them and set a reminder to follow up.

💻 Read our Guide to Networking

✨ Schedule a Career Coaching Meeting
"""


@dataclass(frozen=True)
class Settings:
    cdp_url: str = "http://localhost:9222"
    dry_run: bool = False

    action_timeout_ms: int = 5_000
    navigation_timeout_ms: int = 20_000

    action_delay_s: float = 0.8
    page_load_delay_s: float = 1.5
    jitter_s: float = 0.4


async def _delay(s: float, *, jitter: float) -> None:
    await asyncio.sleep(s + random.uniform(0, jitter))


async def connect_page(playwright: Playwright, settings: Settings) -> Page:
    browser = await playwright.chromium.connect_over_cdp(settings.cdp_url)
    context = browser.contexts[0]
    if not context.pages:
        raise RuntimeError(
            "No open pages found in the remote-debug browser. "
            "Open CareerForge in that browser first."
        )
    page = context.pages[0]
    page.set_default_timeout(settings.action_timeout_ms)
    page.set_default_navigation_timeout(settings.navigation_timeout_ms)
    return page


async def goto_contacts(page: Page, settings: Settings) -> None:
    if settings.dry_run:
        logger.info("[DRY RUN] Would navigate to %s", CONTACTS_URL)
        return
    await page.goto(CONTACTS_URL, wait_until="networkidle")
    await _delay(settings.page_load_delay_s, jitter=settings.jitter_s)


async def open_filter_modal(page: Page, settings: Settings) -> None:
    if settings.dry_run:
        logger.info("[DRY RUN] Would open filter modal")
        return

    # The UI shows a "Filter Contacts" modal. The trigger is typically an icon button
    # near the search bar (sliders icon) with no stable role. We try several strategies.
    candidates = [
        page.get_by_role("button", name="Filter").first,
        page.get_by_text("Filter", exact=False).first,
        page.locator("button:has([data-icon='filter'])").first,
        page.locator("button:has(svg)").nth(0),
    ]

    last_exc: Exception | None = None
    for btn in candidates:
        try:
            await btn.click()
            await page.get_by_text("Filter Contacts", exact=True).wait_for(state="visible")
            await _delay(settings.action_delay_s, jitter=settings.jitter_s)
            return
        except Exception as exc:
            last_exc = exc

    raise RuntimeError(f"Could not open Filter Contacts modal: {last_exc}")


async def set_recruiting_role_filter_to_friendly_alum(page: Page, settings: Settings) -> None:
    if settings.dry_run:
        logger.info("[DRY RUN] Would set Recruiting Role = Friendly Alum")
        return

    modal = page.locator("div[role='dialog']").filter(has=page.get_by_text("Filter Contacts", exact=True)).first

    # Open the Recruiting Role dropdown by its label.
    role_label = modal.get_by_text("Recruiting Role", exact=True)
    await role_label.wait_for(state="visible")

    # Bubble-style dropdowns: clicking near the current value usually opens it.
    # Try clicking the element right of the label first, then fallback to clicking the current value.
    dropdown = role_label.locator("xpath=following::div[contains(@class,'dropdown')][1]").first
    try:
        await dropdown.click()
    except Exception:
        await modal.get_by_text("Choose an option", exact=False).click()

    await _delay(0.3, jitter=settings.jitter_s)

    # Select "Friendly Alum"
    await page.get_by_text("Friendly Alum", exact=True).click()
    await _delay(settings.action_delay_s, jitter=settings.jitter_s)

    # Apply filters
    await modal.get_by_role("button", name="Apply Filters").click()
    await _delay(settings.page_load_delay_s, jitter=settings.jitter_s)


async def _open_row_menu_for_first_visible_contact(page: Page, settings: Settings) -> None:
    if settings.dry_run:
        logger.info("[DRY RUN] Would open row '...' menu")
        return

    # Row menu appears as a 3-dots button at the far right of a row.
    # We target the first visible menu button in the table area.
    # Common pattern: button with aria-haspopup or a 3-dots icon.
    menu_btn = (
        page.locator("button[aria-haspopup='menu']").filter(has=page.locator("svg")).first
        .or_(page.locator("button:has-text('more_vert')").first)
        .or_(page.locator("button").filter(has=page.locator("svg")).nth(-1))
    )

    await menu_btn.scroll_into_view_if_needed()
    await menu_btn.click()
    await _delay(settings.action_delay_s, jitter=settings.jitter_s)


async def open_share_with_candidates_from_menu(page: Page, settings: Settings) -> None:
    if settings.dry_run:
        logger.info("[DRY RUN] Would click 'Share with Candidates'")
        return

    await page.get_by_text("Share with Candidates", exact=True).click()
    await page.get_by_text("Sharing Settings", exact=True).wait_for(state="visible")
    await _delay(settings.action_delay_s, jitter=settings.jitter_s)


async def _toggle_switch_if_needed(switch, desired_on: bool, *, settings: Settings) -> bool:
    """Returns True if a click happened."""
    try:
        aria = await switch.get_attribute("aria-checked")
        if aria is not None:
            current = aria.strip().lower() == "true"
        else:
            # Fallback: some Bubble toggles store state on input checked
            current = await switch.is_checked()  # type: ignore[attr-defined]
    except Exception:
        current = False

    if current == desired_on:
        return False

    if settings.dry_run:
        logger.info("[DRY RUN] Would toggle switch to %s", desired_on)
        return True

    await switch.click()
    await _delay(settings.action_delay_s, jitter=settings.jitter_s)
    return True


async def apply_sharing_settings(page: Page, settings: Settings) -> None:
    if settings.dry_run:
        logger.info("[DRY RUN] Would apply sharing settings + save")
        return

    modal = page.locator("div[role='dialog']").filter(has=page.get_by_text("Sharing Settings", exact=True)).first
    await modal.wait_for(state="visible")

    # Top-level share toggle: the switch next to "Share <name> with Candidates".
    top_switch = (
        modal.locator("[role='switch']").first
        .or_(modal.locator("button[role='switch']").first)
        .or_(modal.locator("div[role='switch']").first)
    )

    top_aria = await top_switch.get_attribute("aria-checked")
    top_is_on = (top_aria or "").strip().lower() == "true"

    if not top_is_on:
        # Turn on sharing, then ensure ONLY LinkedIn is enabled.
        await _toggle_switch_if_needed(top_switch, True, settings=settings)

        linkedin_cb = modal.get_by_text("Share LinkedIn", exact=True).locator("xpath=preceding::input[@type='checkbox'][1]").first
        email_cb = modal.get_by_text("Share Email", exact=True).locator("xpath=preceding::input[@type='checkbox'][1]").first
        phone_cb = modal.get_by_text("Share Phone", exact=True).locator("xpath=preceding::input[@type='checkbox'][1]").first

        async def set_checkbox(cb, desired: bool, label: str) -> None:
            try:
                current = await cb.is_checked()
            except Exception:
                # Fallback: click label if checkbox input isn't accessible
                current = False
            if current == desired:
                return
            if settings.dry_run:
                logger.info("[DRY RUN] Would set %s checkbox to %s", label, desired)
                return
            try:
                await cb.click()
            except Exception:
                await modal.get_by_text(label, exact=True).click()
            await _delay(0.3, jitter=settings.jitter_s)

        await set_checkbox(linkedin_cb, True, "Share LinkedIn")
        await set_checkbox(email_cb, False, "Share Email")
        await set_checkbox(phone_cb, False, "Share Phone")
    else:
        # Sharing already enabled: paste into Notes to Candidates rich text area.
        notes_label = modal.get_by_text("Notes to Candidates", exact=True)
        await notes_label.scroll_into_view_if_needed()

        # Common rich-text editors: a contenteditable div or an iframe body.
        editor = modal.locator("[contenteditable='true']").first
        if await editor.count() == 0:
            editor = modal.locator("div[role='textbox']").first

        await editor.click()
        await page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
        await page.keyboard.type(NOTES_TEMPLATE)
        await _delay(settings.action_delay_s, jitter=settings.jitter_s)

    # Save
    await modal.get_by_role("button", name="Save Changes").click()
    await _delay(settings.page_load_delay_s, jitter=settings.jitter_s)

    # Wait for modal to close (best-effort)
    try:
        await modal.wait_for(state="hidden", timeout=10_000)
    except PlaywrightTimeout:
        pass


async def run_once(settings: Settings) -> None:
    async with async_playwright() as pw:
        page = await connect_page(pw, settings)
        await goto_contacts(page, settings)
        await open_filter_modal(page, settings)
        await set_recruiting_role_filter_to_friendly_alum(page, settings)

        # Process rows (best-effort loop). We start with the first visible contact
        # repeatedly; after each Save, Bubble often re-renders the table and the first row advances.
        for i in range(500):  # hard cap safety
            logger.info("Processing next Friendly Alum contact (step %d)…", i + 1)
            try:
                await _open_row_menu_for_first_visible_contact(page, settings)
                await open_share_with_candidates_from_menu(page, settings)
                await apply_sharing_settings(page, settings)
                await _delay(0.5, jitter=settings.jitter_s)
            except Exception as exc:
                logger.warning("Stopping loop (no more rows or UI changed): %s", exc)
                break


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CareerForge Contacts sharing automation.")
    p.add_argument("--dry-run", action="store_true", default=False)
    p.add_argument("--cdp-url", type=str, default="http://localhost:9222")
    return p.parse_args()


def main() -> None:
    setup_logging()
    args = parse_args()
    settings = Settings(cdp_url=args.cdp_url, dry_run=args.dry_run)
    asyncio.run(run_once(settings))


if __name__ == "__main__":
    main()


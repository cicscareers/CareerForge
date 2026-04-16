from __future__ import annotations

import argparse
import asyncio
import logging
import random
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from playwright.async_api import (
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)


logger = logging.getLogger(__name__)

LOG_DIR = Path("logs")


CONTACTS_URL = "https://careerforge.us/cc-portal?tab=employers&emp_tab=contacts"

NOTES_TEMPLATE = """Networking Essentials

Focus on learning and answering questions important to you.
Let them decide how to help -- don't just ask for referrals.
Thank them and set a reminder to follow up.
💻 Read our <a href="https://www.notion.so/cicscareers/Guide-to-Networking-63b9cf618bf845f2991c52fe32f20df7?source=copy_link#2d35ada1554b80c68eb8fe1822f3bfc5">Guide to Networking</a>
✨ Schedule a <a href="https://careerforge.us/student-portal?tab=topic_details&topic_id=1724113189659x778791644328624100">Career Coaching Meeting</a>
"""


@dataclass(frozen=True)
class Settings:
    cdp_url: str = "http://localhost:9222"
    dry_run: bool = False

    # Tuned for bulk runs; use --fast for maximum throughput (slightly riskier on slow networks).
    action_timeout_ms: int = 3_500
    navigation_timeout_ms: int = 15_000

    action_delay_s: float = 0.12
    page_load_delay_s: float = 0.22
    jitter_s: float = 0.05


async def _delay(s: float, *, jitter: float) -> None:
    await asyncio.sleep(s + random.uniform(0, jitter))


def _dialog_with_title(page: Page, title: str):
    """Find dialogs by title text, tolerant of Bubble casing/whitespace drift."""
    return page.locator("div[role='dialog']").filter(
        has=page.get_by_text(title, exact=False)
    ).first


def _filter_contacts_panel(page: Page):
    """Find the visible Filter Contacts panel even when no role='dialog' is present."""
    # Bubble often renders modal shells without ARIA dialog roles.
    return page.locator(
        "div:visible:has-text('Filter Contacts'):has-text('Recruiting Role'):has-text('Apply Filters')"
    ).first


def _sharing_settings_panel(page: Page):
    """Find visible Sharing Settings panel even without role='dialog'."""
    return page.locator(
        "div:visible:has-text('Sharing Settings'):has-text('Save Changes')"
    ).first


async def connect_page(playwright: Playwright, settings: Settings) -> Page:
    browser = await playwright.chromium.connect_over_cdp(settings.cdp_url)
    context = browser.contexts[0]
    # Always use a fresh tab so we never steal focus or navigation from
    # whatever else you're doing in other tabs.
    page = await context.new_page()
    page.set_default_timeout(settings.action_timeout_ms)
    page.set_default_navigation_timeout(settings.navigation_timeout_ms)
    return page


async def _wait_for_contacts_search_ready(page: Page, settings: Settings) -> None:
    """Bubble may hydrate late; filter clicks fail if search row is not mounted yet."""
    await page.locator(
        'input[placeholder*="Search"], input[placeholder*="contact"]'
    ).first.wait_for(state="visible", timeout=min(settings.navigation_timeout_ms, 25_000))


async def goto_contacts(page: Page, settings: Settings) -> None:
    if settings.dry_run:
        logger.info("[DRY RUN] Would navigate to %s", CONTACTS_URL)
        return
    # `load` balances speed vs readiness better than domcontentloaded for Bubble SPAs.
    await page.goto(CONTACTS_URL, wait_until="load")
    await _delay(settings.page_load_delay_s, jitter=settings.jitter_s)
    await _wait_for_contacts_search_ready(page, settings)


async def open_filter_modal(page: Page, settings: Settings) -> None:
    if settings.dry_run:
        logger.info("[DRY RUN] Would open filter modal")
        return

    modal_title = page.get_by_text("Filter Contacts", exact=False)

    async def opened() -> bool:
        try:
            if await _dialog_with_title(page, "Filter Contacts").is_visible():
                return True
            return await _filter_contacts_panel(page).is_visible()
        except Exception:
            return False

    if await opened():
        return

    # Ensure toolbar mounted (esp. after --fast / thin delays).
    try:
        await _wait_for_contacts_search_ready(page, settings)
    except Exception:
        pass

    async def wait_for_opened() -> bool:
        try:
            await _dialog_with_title(page, "Filter Contacts").wait_for(
                state="visible", timeout=2_500
            )
            return True
        except Exception:
            return False

    # Try several click strategies because Bubble DOM/labels drift often.
    click_attempts = [
        page.get_by_role("button", name="Filter Contacts").first,
        page.get_by_role("button", name="Filter").first,
        page.locator("button:has-text('Filter')").first,
        page.locator("button:has-text('tune')").first,
        page.locator("button:has-text('filter_alt')").first,
        page.locator("button:has-text('filter_list')").first,
    ]

    for target in click_attempts:
        try:
            if await target.count() == 0:
                continue
            await target.click(timeout=2_500)
            if await wait_for_opened():
                await _delay(settings.action_delay_s, jitter=settings.jitter_s)
                return
        except Exception:
            continue

    # Fallback: click nearest control to the right of contact search input.
    try:
        await page.evaluate(
            """() => {
              const searchInput =
                document.querySelector('input[placeholder="Search for a contact"]') ||
                document.querySelector('input[placeholder*="Search"]');
              if (!searchInput) return { ok: false, why: "no_search_input" };

              const sr = searchInput.getBoundingClientRect();
              const candidates = Array.from(document.querySelectorAll('button, [role="button"]'))
                .map(el => ({ el, r: el.getBoundingClientRect(), txt: (el.textContent || '').trim() }))
                .filter(x =>
                  x.r.width > 0 && x.r.height > 0 &&
                  x.r.left >= sr.right - 10 &&
                  Math.abs(x.r.top - sr.top) < 80 &&
                  x.r.top < 260
                )
                .sort((a, b) => a.r.left - b.r.left);

              if (!candidates.length) return { ok: false, why: "no_candidates" };

              // Prefer obvious filter-ish controls if present.
              const preferred = candidates.find(x =>
                /filter|tune|slider|funnel/i.test(x.txt)
              );
              (preferred || candidates[0]).el.click();
              return { ok: true };
            }"""
        )
        if await wait_for_opened():
            await _delay(settings.action_delay_s, jitter=settings.jitter_s)
            return
    except Exception:
        pass

    # Final fallback: allow manual open if UI selectors drift.
    logger.warning(
        "Could not auto-open Filter Contacts. Please click the filter icon manually "
        "in the browser within 30 seconds..."
    )
    # ~30s of polling (was ~13s with 60×0.22s).
    for _ in range(120):
        if await opened():
            await _delay(0.08, jitter=settings.jitter_s)
            return
        await asyncio.sleep(0.25)

    last_exc: Exception | None = None

    # Save a screenshot to help debug selector drift
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "contacts_filter_open_failed.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Saved debug screenshot → %s", path)
    except Exception:
        pass

    raise RuntimeError(f"Could not open Filter Contacts modal (url={page.url}): {last_exc}")


async def set_recruiting_role_filter_to_friendly_alum(page: Page, settings: Settings) -> None:
    if settings.dry_run:
        logger.info("[DRY RUN] Would set Recruiting Role = Friendly Alum")
        return

    modal = _dialog_with_title(page, "Filter Contacts")
    try:
        await modal.wait_for(state="visible", timeout=5_000)
    except PlaywrightTimeout:
        # Fallback: Bubble may not mark the modal as role='dialog'.
        modal = _filter_contacts_panel(page)
        await modal.wait_for(state="visible", timeout=6_000)

    # Locate the Recruiting Role field robustly (Bubble can add extra whitespace/newlines).
    role_label = modal.get_by_text("Recruiting Role", exact=False).first
    try:
        await role_label.wait_for(state="visible", timeout=5_000)
    except Exception:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = LOG_DIR / "contacts_filter_missing_recruiting_role.png"
            await page.screenshot(path=str(path), full_page=True)
            logger.info("Saved debug screenshot → %s", path)
        except Exception:
            pass
        raise

    # Click ONLY the Recruiting Role chooser using the exact Bubble structure.
    opened_recruiting_dropdown = bool(
        await page.evaluate(
            """() => {
              const panel = Array.from(document.querySelectorAll('div')).find(el =>
                /Filter Contacts/i.test(el.textContent || '') &&
                /Recruiting Role/i.test(el.textContent || '') &&
                /Apply Filters/i.test(el.textContent || '')
              );
              if (!panel) return false;

              // Exact control from your screenshot:
              // div.clickable-element.bubble-element.Group.cpaOaXv ... containing "Choose an Option".
              let chooser = panel.querySelector('div.clickable-element.bubble-element.Group.cpaOaXv');
              if (!chooser) {
                chooser = Array.from(panel.querySelectorAll('div.clickable-element.bubble-element.Group'))
                  .find(el => /choose an option/i.test((el.textContent || '').trim()));
              }
              if (!chooser) return false;

              chooser.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
              return true;
            }"""
        )
    )

    if not opened_recruiting_dropdown:
        # Last-resort fallback from Recruiting Role label proximity.
        chooser = role_label.locator(
            "xpath=following::*[self::div[contains(@class,'clickable-element')] and contains(.,'Choose')][1]"
        ).first
        await chooser.click()

    await _delay(0.12, jitter=settings.jitter_s)

    # Wait until the dropdown list is open and a visible "Friendly Alum" option exists.
    await page.wait_for_function(
        """() => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };
          return Array.from(document.querySelectorAll('*')).some(
            el => visible(el) && /^\\s*Friendly Alum\\s*$/i.test((el.textContent || '').trim())
          );
        }""",
        timeout=5_000,
    )

    # Click the specific dropdown option row structure shared by user:
    # div.bubble-element.Group.cpaOaVm ... Friendly Alum ... button(material-icons)
    selected = False
    for _ in range(8):
        selected = bool(
            await page.evaluate(
                """() => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };

                  const rows = Array.from(document.querySelectorAll('div.bubble-element.Group.cpaOaVm'))
                    .filter(row => visible(row) && /Friendly Alum/i.test((row.textContent || '').trim()));
                  if (!rows.length) return false;

                  const row = rows[0];
                  const btn = row.querySelector('button.bubble-element.materialicons-Materialicon.clickable-element');
                  if (!btn) return false;

                  const state = (btn.textContent || '').trim();
                  if (state === 'check_box') return true; // already selected

                  btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                  const after = (btn.textContent || '').trim();
                  return after === 'check_box';
                }"""
            )
        )
        if selected:
            break
        await asyncio.sleep(0.1)

    if not selected:
        raise RuntimeError("Could not auto-select Friendly Alum in dropdown (cpaOaVm row).")

    await _delay(0.08, jitter=settings.jitter_s)
    await _delay(settings.action_delay_s, jitter=settings.jitter_s)

    # Apply filters
    await modal.get_by_role("button", name="Apply Filters").click()
    await _delay(settings.page_load_delay_s, jitter=settings.jitter_s)


# Returned when every visible row was skipped (already processed) — caller should paginate.
NEED_NEXT_PAGE = -2


def _next_contacts_page_url(current_url: str) -> str:
    """Bubble uses ?page_contacts=N on the contacts tab (page 1 often omitted)."""
    p = urlparse(current_url.strip())
    qs = dict(parse_qsl(p.query, keep_blank_values=True))
    raw = (qs.get("page_contacts") or "1").strip() or "1"
    try:
        cur = max(1, int(raw))
    except ValueError:
        cur = 1
    qs["page_contacts"] = str(cur + 1)
    new_query = urlencode(list(qs.items()))
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))


def _contacts_page_number(current_url: str) -> int:
    """Current contacts page from ?page_contacts, defaulting to 1."""
    p = urlparse(current_url.strip())
    qs = dict(parse_qsl(p.query, keep_blank_values=True))
    raw = (qs.get("page_contacts") or "1").strip() or "1"
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _parse_pagination_range(text: str | None) -> tuple[int, int, int] | None:
    """Parse footer like '1 - 10 of 46' → (start, end, total)."""
    if not text:
        return None
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)\s+of\s+(\d+)", text, re.I)
    if not m:
        return None
    start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if start > end or end > total or total < 1:
        return None
    return (start, end, total)


async def _pagination_summary_text(page: Page) -> str | None:
    """e.g. '1 - 10 of 46' near table footer."""
    try:
        return await page.evaluate(
            """() => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              };
              const el = Array.from(document.querySelectorAll('*')).find(n =>
                visible(n) && /\\d+\\s*[-–]\\s*\\d+\\s+of\\s+\\d+/i.test((n.textContent || '').trim())
              );
              if (!el) return null;
              const m = (el.textContent || '').match(/\\d+\\s*[-–]\\s*\\d+\\s+of\\s+\\d+/i);
              return m ? m[0].replace(/\\u2013/g, '-').trim() : null;
            }"""
        )
    except Exception:
        return None


async def _contacts_table_go_next_page(page: Page, settings: Settings) -> bool:
    """Advance contacts table via Bubble URL param page_contacts (reliable vs UI chevrons)."""
    current_page_num = _contacts_page_number(page.url)
    requested_page_num = current_page_num + 1
    before = await _pagination_summary_text(page)
    before_range = _parse_pagination_range(before)
    next_url = _next_contacts_page_url(page.url)
    logger.info("Loading next contacts page: %s", next_url)
    await page.goto(next_url, wait_until="load")
    await _delay(settings.page_load_delay_s * 1.5, jitter=settings.jitter_s)
    try:
        await _wait_for_contacts_search_ready(page, settings)
    except Exception:
        pass

    # Bubble may drop applied filters on URL-based page jumps; re-apply Friendly Alum.
    try:
        await open_filter_modal(page, settings)
        await set_recruiting_role_filter_to_friendly_alum(page, settings)
    except Exception as exc:
        logger.warning("Could not re-apply filters after page jump: %s", exc)

    after = before
    for _ in range(20):
        after = await _pagination_summary_text(page)
        if after != before:
            break
        await asyncio.sleep(0.15)
    after_range = _parse_pagination_range(after)
    landed_page_num = _contacts_page_number(page.url)

    # Guard against Bubble redirecting an out-of-range page back to page 1.
    if landed_page_num < requested_page_num:
        logger.info(
            "Requested page %d but landed on page %d; treating as last page.",
            requested_page_num,
            landed_page_num,
        )
        return False

    # Secondary guard: footer moved backwards or reset (e.g., 41-46 -> 1-10).
    if before_range and after_range and after_range[0] <= before_range[0]:
        logger.info(
            "Pagination range did not advance (%s → %s); treating as last page.",
            before,
            after,
        )
        return False

    if before and after and before != after:
        logger.info("Advanced to next page (%s → %s).", before, after)
        return True
    if before == after and before:
        logger.info("Pagination unchanged (%s); likely last page.", before)
        return False
    logger.info("Advanced via page_contacts (range: %s → %s).", before, after)
    return True


async def _open_row_menu_for_first_visible_contact(
    page: Page, settings: Settings, processed_names: set[str], row_cursor: int
) -> int:
    if settings.dry_run:
        logger.info("[DRY RUN] Would open row '...' menu")
        return 0

    # Row menu is the "more_vert" button on each contact row.
    # Avoid strict-mode unions; choose the top-most visible row menu button.
    menu_buttons = page.get_by_role("button", name="more_vert")
    count = await menu_buttons.count()
    if count == 0:
        raise RuntimeError("No row menu button ('more_vert') found.")

    saw_visible_menu = False
    every_visible_only_processed = True
    clicked = False
    clicked_slot = -1
    # Rotate through row menu buttons so we don't keep selecting the first row.
    for offset in range(count):
        i = (row_cursor + offset) % count
        btn = menu_buttons.nth(i)
        try:
            if not await btn.is_visible():
                continue
            saw_visible_menu = True
            row_text = (
                await btn.evaluate(
                    """(el) => {
                      let node = el;
                      for (let depth = 0; depth < 8 && node; depth++) {
                        node = node.parentElement;
                        if (!node) break;
                        const t = (node.innerText || '').trim();
                        if (t && t.length < 500) return t;
                      }
                      return (el.parentElement?.innerText || '').trim();
                    }"""
                )
            ).lower()
            if any(
                name and name.lower() in row_text for name in processed_names
            ):
                continue
            every_visible_only_processed = False
            await btn.scroll_into_view_if_needed()
            await btn.click(timeout=1_800)
            clicked = True
            clicked_slot = i
            break
        except Exception:
            continue

    if (
        not clicked
        and processed_names
        and saw_visible_menu
        and every_visible_only_processed
    ):
        return NEED_NEXT_PAGE

    if not clicked:
        raise RuntimeError("Could not click any visible row menu ('more_vert') button.")

    await _delay(settings.action_delay_s, jitter=settings.jitter_s)
    return clicked_slot


async def open_share_with_candidates_from_menu(page: Page, settings: Settings) -> None:
    if settings.dry_run:
        logger.info("[DRY RUN] Would click 'Share with Candidates'")
        return

    clicked = False
    # Bubble context-menu items are plain divs; click the visible one explicitly.
    for _ in range(4):
        try:
            clicked = bool(
                await page.evaluate(
                    """() => {
                      const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const items = Array.from(document.querySelectorAll('*')).filter(el =>
                        visible(el) &&
                        (el.textContent || '').trim() === 'Share with Candidates'
                      );
                      if (!items.length) return false;
                      items[0].click();
                      return true;
                    }"""
                )
            )
        except Exception:
            clicked = False
        if clicked:
            break
        await asyncio.sleep(0.08)

    if not clicked:
        # Fallback to Playwright text click.
        await page.get_by_text("Share with Candidates", exact=True).first.click()

    # Wait for Sharing Settings panel/modal.
    try:
        await page.get_by_text("Sharing Settings", exact=False).first.wait_for(
            state="visible", timeout=8_000
        )
    except Exception:
        await _sharing_settings_panel(page).wait_for(state="visible", timeout=8_000)

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


def _is_notes_template_filled(editor_text: str) -> bool:
    """True only if TinyMCE body has the full template (not empty / stray matches)."""
    t = editor_text.replace("\u00a0", " ").lower().strip()
    if len(t) < 100:
        return False
    return (
        "networking essentials" in t
        and "focus on learning" in t
        and "career coaching meeting" in t
    )


async def _tinymce_body_text_in_modal(modal) -> str:
    """Read Notes editor text from the iframe inside this modal only."""
    try:
        handle = await modal.locator("iframe.tox-edit-area__iframe").first.element_handle(
            timeout=2_500
        )
        if not handle:
            return ""
        frame = await handle.content_frame()
        if not frame:
            return ""
        body = frame.locator("body#tinymce")
        return (await body.inner_text()).strip()
    except Exception:
        return ""


async def apply_sharing_settings(page: Page, settings: Settings) -> str | None:
    if settings.dry_run:
        logger.info("[DRY RUN] Would apply sharing settings + save")
        return

    modal = page.locator("div[role='dialog']").filter(
        has=page.get_by_text("Sharing Settings", exact=False)
    ).first
    try:
        await modal.wait_for(state="visible", timeout=4_000)
    except PlaywrightTimeout:
        modal = _sharing_settings_panel(page)
        await modal.wait_for(state="visible", timeout=6_000)
    logger.info("Sharing Settings opened.")

    contact_name: str | None = await page.evaluate(
        """() => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };
          const rows = Array.from(document.querySelectorAll('div.clickable-element.bubble-element.Group.coxaNaF5'))
            .filter(el => visible(el) && /with Candidates/i.test((el.textContent || '').trim()));
          const list = rows.length ? rows : Array.from(document.querySelectorAll('div.clickable-element'))
            .filter(el => visible(el) && /^Share\\s+.+\\s+with Candidates$/im.test((el.innerText || '').trim()));
          for (const row of list) {
            const m = (row.innerText || '').match(/Share\\s+(.+?)\\s+with Candidates/i);
            if (!m) continue;
            const name = m[1].trim();
            if (!name || /^this\\s+contact$/i.test(name)) continue;
            return name;
          }
          return null;
        }"""
    )

    async def _click_save_changes() -> None:
        # Exact Bubble button target from UI: <button id="primary" ...>Save Changes</button>
        save_btn = modal.locator("button#primary:has-text('Save Changes')").first
        for _ in range(5):
            try:
                await save_btn.scroll_into_view_if_needed()
                await save_btn.click(timeout=1_800)
            except Exception:
                clicked = await page.evaluate(
                    """() => {
                      const btn = document.querySelector('button#primary.clickable-element.bubble-element.Button.coxaMaO5')
                        || document.querySelector('button#primary')
                        || Array.from(document.querySelectorAll('button'))
                            .find(el => /save changes/i.test((el.textContent || '').trim()));
                      if (!btn) return false;
                      btn.click();
                      return true;
                    }"""
                )
                if not clicked:
                    await asyncio.sleep(0.08)
                    continue

            # Verify save actually took by waiting for modal to close.
            try:
                await modal.wait_for(state="hidden", timeout=2_200)
                logger.info("Clicked Save Changes.")
                return
            except Exception:
                # Modal still open; try clicking save again.
                await asyncio.sleep(0.1)
        raise RuntimeError("Could not click Save Changes.")

    async def _toggle_state() -> str | None:
        return await page.evaluate(
            """() => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              };
              const rows = Array.from(document.querySelectorAll('div.clickable-element.bubble-element.Group.coxaNaF5'))
                .filter(el => visible(el) && /with Candidates/i.test((el.textContent || '').trim()));
              if (!rows.length) {
                // Fallback if class hash changes.
                rows.push(
                  ...Array.from(document.querySelectorAll('div.clickable-element'))
                    .filter(el => visible(el) && /with Candidates/i.test((el.textContent || '').trim()))
                );
              }
              if (!rows.length) return null;
              const row = rows[0];
              const btn = row.querySelector('button');
              if (!btn) return null;
              return (btn.textContent || '').trim(); // toggle_on / toggle_off
            }"""
        )

    async def _ensure_top_toggle_on() -> None:
        # Share <name> with Candidates must always be ON.
        for _ in range(5):
            state = await _toggle_state()
            if state == "toggle_on":
                return
            if state == "toggle_off":
                clicked = await page.evaluate(
                    """() => {
                      const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const rows = Array.from(document.querySelectorAll('div.clickable-element.bubble-element.Group.coxaNaF5'))
                        .filter(el => visible(el) && /with Candidates/i.test((el.textContent || '').trim()));
                      if (!rows.length) {
                        // Fallback if class hash changes.
                        rows.push(
                          ...Array.from(document.querySelectorAll('div.clickable-element'))
                            .filter(el => visible(el) && /with Candidates/i.test((el.textContent || '').trim()))
                        );
                      }
                      if (!rows.length) return false;
                      const row = rows[0];
                      const btn = row.querySelector('button');
                      // Bubble sometimes wires click handler on row, sometimes on icon button.
                      row.click();
                      if (btn) btn.click();
                      return true;
                    }"""
                )
                if not clicked:
                    await asyncio.sleep(0.1)
                    continue
                await _delay(0.12, jitter=settings.jitter_s)
                continue
            await asyncio.sleep(0.1)
        raise RuntimeError("Could not turn ON 'Share ... with Candidates' toggle.")

    async def _checkbox_state(label: str) -> bool:
        return bool(
            await page.evaluate(
                """(label) => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const rows = Array.from(document.querySelectorAll('div.clickable-element'))
                    .filter(el => visible(el) && (el.textContent || '').includes(label));
                  if (!rows.length) return false;
                  const btn = rows[0].querySelector('button');
                  if (!btn) return false;
                  return (btn.textContent || '').trim() === 'check_box';
                }""",
                label,
            )
        )

    async def _click_checkbox(label: str) -> None:
        clicked = await page.evaluate(
            """(label) => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              };
              const rows = Array.from(document.querySelectorAll('div.clickable-element'))
                .filter(el => visible(el) && (el.textContent || '').includes(label));
              if (!rows.length) return false;
              const btn = rows[0].querySelector('button');
              if (!btn) return false;
              btn.click();
              return true;
            }""",
            label,
        )
        if not clicked:
            raise RuntimeError(f"Could not click checkbox row: {label}")
        await _delay(0.08, jitter=settings.jitter_s)

    await _ensure_top_toggle_on()
    logger.info("Ensured top 'Share ... with Candidates' toggle is ON.")

    # In some contacts, only the top toggle is shown and no per-field checkboxes/notes are rendered.
    # In that case, just save immediately after enabling the top toggle.
    sharing_fields_visible = bool(
        await page.evaluate(
            """() => {
              const t = (document.body?.innerText || '');
              return t.includes('Share LinkedIn') || t.includes('Notes to Candidates');
            }"""
        )
    )
    if not sharing_fields_visible:
        logger.info("Only top toggle visible; saving immediately.")
        await _click_save_changes()
        await _delay(settings.page_load_delay_s, jitter=settings.jitter_s)
        try:
            await modal.wait_for(state="hidden", timeout=5_000)
        except PlaywrightTimeout:
            pass
        return contact_name

    linkedin_on = await _checkbox_state("Share LinkedIn")
    email_on = await _checkbox_state("Share Email")
    phone_on = await _checkbox_state("Share Phone")
    logger.info(
        "Checkbox states: LinkedIn=%s, Email=%s, Phone=%s",
        linkedin_on,
        email_on,
        phone_on,
    )

    # Only intervene when none are selected: turn on LinkedIn only.
    if not linkedin_on and not email_on and not phone_on:
        await _click_checkbox("Share LinkedIn")
        logger.info("All share checkboxes were OFF; turned ON Share LinkedIn.")

    # If notes are already present in *this* modal's TinyMCE, skip retyping.
    existing_notes_raw = await _tinymce_body_text_in_modal(modal)
    notes_already_present = _is_notes_template_filled(existing_notes_raw)
    if not notes_already_present:
        logger.info(
            "Notes empty or incomplete in modal editor (len=%s); will fill.",
            len(existing_notes_raw),
        )

    if notes_already_present:
        logger.info("Notes template already present; skipping note retype.")
    else:
        logger.info("Notes template missing; typing Notes to Candidates.")
        notes_label = modal.get_by_text("Notes to Candidates", exact=True)
        await notes_label.scroll_into_view_if_needed()

        typed = False
        # Primary path: TinyMCE iframe editor scoped to this modal.
        try:
            notes_iframe = modal.locator("iframe.tox-edit-area__iframe").first
            await notes_iframe.wait_for(state="visible", timeout=2_500)
            handle = await notes_iframe.element_handle()
            frame = await handle.content_frame() if handle else None
            if frame:
                editor_body = frame.locator("body#tinymce")
                await editor_body.click(timeout=1_500)
                await editor_body.evaluate(
                    """(el, template) => {
                      el.innerHTML = '';
                      const lines = template.split('\\n');
                      for (let i = 0; i < lines.length; i++) {
                        const line = lines[i];
                        const p = document.createElement('p');
                        p.style.margin = '0';
                        p.style.lineHeight = '1.35';
                        if (i === 0) {
                          const strong = document.createElement('strong');
                          strong.textContent = line;
                          p.appendChild(strong);
                          p.style.textAlign = 'center';
                        } else {
                          // Keep support for inline links in template lines.
                          p.innerHTML = line;
                        }
                        el.appendChild(p);
                      }
                      el.dispatchEvent(new Event('input', { bubbles: true }));
                      el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    NOTES_TEMPLATE,
                )
                typed = True
        except Exception:
            typed = False

        if not typed:
            # Secondary path: contenteditable in host document.
            try:
                editor = modal.locator("[contenteditable='true']").first
                if await editor.count() == 0:
                    editor = modal.locator("div[role='textbox']").first
                await editor.click(timeout=1_500)
                await page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
                await page.keyboard.type(NOTES_TEMPLATE, delay=0)
                typed = True
            except Exception:
                typed = False

        if not typed:
            raise RuntimeError("Could not type Notes to Candidates.")

        await _delay(settings.action_delay_s, jitter=settings.jitter_s)

    # Save
    await _click_save_changes()
    await _delay(settings.page_load_delay_s, jitter=settings.jitter_s)

    # Wait for modal to close (best-effort)
    try:
        await modal.wait_for(state="hidden", timeout=5_000)
    except PlaywrightTimeout:
        pass
    return contact_name


async def run_once(settings: Settings) -> None:
    async with async_playwright() as pw:
        page = await connect_page(pw, settings)
        await goto_contacts(page, settings)
        await open_filter_modal(page, settings)
        await set_recruiting_role_filter_to_friendly_alum(page, settings)

        processed_names: set[str] = set()
        row_cursor = 0
        successful_on_current_page = 0
        # Process rows (best-effort loop). We start with the first visible contact
        # repeatedly; after each Save, Bubble often re-renders the table and the first row advances.
        for i in range(500):  # hard cap safety
            logger.info("Processing next Friendly Alum contact (step %d)…", i + 1)
            try:
                clicked_slot = await _open_row_menu_for_first_visible_contact(
                    page, settings, processed_names, row_cursor
                )
                if clicked_slot == NEED_NEXT_PAGE:
                    if await _contacts_table_go_next_page(page, settings):
                        row_cursor = 0
                        successful_on_current_page = 0
                        await _delay(0.15, jitter=settings.jitter_s)
                        continue
                    logger.info("All visible rows processed and no next page; done.")
                    break

                await open_share_with_candidates_from_menu(page, settings)
                processed_name = await apply_sharing_settings(page, settings)
                if processed_name:
                    processed_names.add(processed_name)
                    logger.info("Processed contact: %s", processed_name)
                successful_on_current_page += 1

                summary = await _pagination_summary_text(page)
                pr = _parse_pagination_range(summary)
                if pr:
                    start, end, total = pr
                    capacity = end - start + 1
                    if successful_on_current_page >= capacity and end < total:
                        logger.info(
                            "Finished page (%d saves, footer %s); loading next page.",
                            successful_on_current_page,
                            summary,
                        )
                        if await _contacts_table_go_next_page(page, settings):
                            row_cursor = 0
                            successful_on_current_page = 0
                            await _delay(0.15, jitter=settings.jitter_s)
                            continue

                if clicked_slot >= 0:
                    row_cursor = clicked_slot + 1
                await _delay(0.1, jitter=settings.jitter_s)
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
    p.add_argument("--fast", action="store_true", default=False, help="Shorter waits/delays for bulk runs")
    p.add_argument("--cdp-url", type=str, default="http://localhost:9222")
    return p.parse_args()


def main() -> None:
    setup_logging()
    args = parse_args()
    settings = Settings(cdp_url=args.cdp_url, dry_run=args.dry_run)
    if args.fast:
        settings = replace(
            settings,
            action_timeout_ms=2_500,
            navigation_timeout_ms=12_000,
            action_delay_s=0.05,
            page_load_delay_s=0.10,
            jitter_s=0.02,
        )
        logger.info("Fast mode: reduced delays and timeouts.")
    asyncio.run(run_once(settings))


if __name__ == "__main__":
    main()


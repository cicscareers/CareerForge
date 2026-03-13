#!/usr/bin/env python3
"""
main.py - Entry point and interactive terminal UI for the Notion to CareerForge transfer.

This is the file you actually run. It ties everything together: parses the CSV,
shows you each employer in a nice colorful terminal UI, lets you approve/skip/edit
each one, and then drives the browser automation to apply the changes.

Usage examples:
    python main.py                          # interactive mode (approve each employer)
    python main.py --auto                   # auto-apply everything, no prompts
    python main.py --dry-run                # just validate, don't click anything
    python main.py --csv data/other.csv     # use a different CSV file
    python main.py --start-from "Google"    # skip ahead to a specific employer
    python main.py --limit 10               # only process 10 employers

The big pieces in this file:
    - Logging setup (console + file)
    - CLI argument parsing
    - Interactive terminal helpers (preview, menus, edit sub-menu)
    - The main async loop with retry logic and checkpointing
    - Summary log file generation
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Colorama setup with graceful fallback
# ---------------------------------------------------------------------------
# We try to import colorama for nice colored terminal output.
# If it's not installed, we just define dummy color constants so nothing breaks.
try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
except ImportError:
    # No colorama? No problem. Just use empty strings for all color codes.
    class _NoColor:
        def __getattr__(self, _: str) -> str:
            return ""
    Fore = _NoColor()  # type: ignore[assignment]
    Style = _NoColor()  # type: ignore[assignment]

import config
import checkpoint
import csv_parser
import automation
import careerforge_properties as cfp

# ===========================================================================
#  Logging setup
# ===========================================================================

def setup_logging() -> None:
    """Set up dual logging: verbose file logging and concise console logging.

    Creates a timestamped log file in the logs/ directory that captures
    everything at DEBUG level. The console only shows INFO and above
    so it's not too noisy while you're running interactively.
    """
    log_dir = Path(config.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"run_{ts}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # File handler: catches everything (DEBUG level) for later debugging
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    ))
    root.addHandler(fh)

    # Console handler: just the important stuff (INFO level) so it's readable
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(ch)

    logging.info("Log file: %s", log_file)


# ===========================================================================
#  CLI argument parsing
# ===========================================================================

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the script.

    Supports these flags:
        --dry-run:    Log what would happen without actually clicking in the browser.
        --auto:       Skip all interactive prompts and auto-approve every employer.
        --csv:        Use a different CSV file instead of the default.
        --start-from: Skip employers until we reach this name (for resuming).
        --limit:      Only process this many employers in one run.

    Returns:
        An argparse.Namespace with the parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="Transfer employer data from Notion CSV to CareerForge."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log actions without clicking anything in the browser.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        default=False,
        help="Skip interactive prompts and auto-apply all employers.",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=None,
        help=f"Path to the Notion CSV export (default: {config.CSV_PATH}).",
    )
    parser.add_argument(
        "--start-from",
        type=str,
        default=None,
        help="Skip employers in the CSV until this name is reached.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N employers in this run.",
    )
    return parser.parse_args()


# ===========================================================================
#  Interactive terminal helpers
# ===========================================================================
# These functions handle all the pretty-printing in the terminal.
# They show employer data, action menus, and interactive pickers.

def _print_header(index: int, total: int, name: str) -> None:
    """Print a colorful header banner for the current employer.

    Args:
        index: The 1-based index of this employer in the queue.
        total: The total number of employers to process.
        name: The employer's name.
    """
    bar = "═" * 64
    print(f"\n{Fore.CYAN}{bar}{Style.RESET_ALL}")
    print(f"{Fore.CYAN} EMPLOYER {index}/{total}: {Fore.WHITE}{Style.BRIGHT}{name}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{bar}{Style.RESET_ALL}")


def _print_employer_preview(employer: dict[str, Any]) -> None:
    """Print a summary of what data we have for this employer.

    Shows both the values that will be applied and any values from the CSV
    that got skipped because they don't match CareerForge's whitelist.

    Args:
        employer: The employer dict from csv_parser.
    """
    # Show each field, using a dash if we don't have data for it
    cics = employer.get("cics_hires") or "\u2014"
    spons = employer.get("sponsoring") or "\u2014"
    states = ", ".join(employer.get("states", [])) or "\u2014"
    size = employer.get("size") or "\u2014"
    tags = ", ".join(employer.get("industry_tags", [])) or "\u2014"

    print()
    print(f"  {Fore.YELLOW}\U0001f4ca CICS Hires:{Style.RESET_ALL}     {cics}")
    print(f"  {Fore.YELLOW}\U0001f30f Sponsoring?:{Style.RESET_ALL}    {spons}")
    print(f"  {Fore.YELLOW}\U0001f4cd State(s):{Style.RESET_ALL}       {states}")
    print(f"  {Fore.YELLOW}\U0001f50e Size:{Style.RESET_ALL}           {size}")
    print(f"  {Fore.YELLOW}\U0001f308 Industry Tags:{Style.RESET_ALL}  {tags}")

    # Show any values that didn't match CareerForge's whitelist
    skipped_tags = employer.get("skipped_tags", [])
    skipped_states = employer.get("skipped_states", [])

    if skipped_tags or skipped_states:
        print()
        print(f"  {Fore.RED}\u26a0\ufe0f  Tags/values from CSV not on CareerForge (will be skipped):{Style.RESET_ALL}")
        for t in skipped_tags:
            print(f"     {Fore.RED}- {t}{Style.RESET_ALL}")
        for s in skipped_states:
            print(f"     {Fore.RED}- State: {s}{Style.RESET_ALL}")

    # Show what WILL actually be applied
    print()
    print(f"  {Fore.GREEN}\u2705 Will apply to CareerForge:{Style.RESET_ALL}")
    if employer.get("cics_hires"):
        print(f"     CICS Hires:     {employer['cics_hires']}")
    if employer.get("sponsoring"):
        print(f"     Sponsoring?:    {employer['sponsoring']}")
    if employer.get("states"):
        print(f"     State(s):       {', '.join(employer['states'])}")
    if employer.get("size"):
        print(f"     Size:           {employer['size']}")
    if employer.get("industry_tags"):
        print(f"     Industry Tags:  {', '.join(employer['industry_tags'])}")

    # Let the user know if there's nothing to apply
    has_data = any([
        employer.get("cics_hires"),
        employer.get("sponsoring"),
        employer.get("states"),
        employer.get("size"),
        employer.get("industry_tags"),
    ])
    if not has_data:
        print(f"     {Fore.YELLOW}(nothing to apply){Style.RESET_ALL}")


def _print_action_menu() -> None:
    """Print the approve/skip/edit/quit action menu."""
    sep = "\u2500" * 64
    print(f"\n{Fore.CYAN}{sep}{Style.RESET_ALL}")
    print(f"  {Fore.GREEN}[a]{Style.RESET_ALL} Approve & apply    "
          f"{Fore.YELLOW}[s]{Style.RESET_ALL} Skip this employer")
    print(f"  {Fore.BLUE}[e]{Style.RESET_ALL} Edit before apply  "
          f"{Fore.RED}[q]{Style.RESET_ALL} Quit script")
    print(f"{Fore.CYAN}{sep}{Style.RESET_ALL}")


def _pick_from_list(title: str, options: list[str], current: list[str] | None = None) -> list[str] | None:
    """Show a numbered list and let the user pick multiple items.

    Displays all options with numbers. The user types comma-separated numbers
    to select items (like "1,3,5"). Type 'c' to cancel.

    Args:
        title: The heading to show above the list.
        options: The list of options to display.
        current: If provided, marks these options with a checkmark in the display.

    Returns:
        A list of selected option strings, or None if the user cancelled.
    """
    print(f"\n  {Fore.CYAN}{title}{Style.RESET_ALL}")
    for i, opt in enumerate(options, 1):
        marker = f" {Fore.GREEN}\u2713{Style.RESET_ALL}" if current and opt in current else ""
        print(f"    {Fore.WHITE}{i:>3}{Style.RESET_ALL}. {opt}{marker}")

    print(f"\n  Enter numbers separated by commas (e.g. 1,3,5), or 'c' to cancel:")
    raw = input(f"  {Fore.CYAN}>{Style.RESET_ALL} ").strip()

    if raw.lower() == "c":
        return None

    selected: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(options):
                if options[idx] not in selected:
                    selected.append(options[idx])
            else:
                print(f"    {Fore.RED}Ignoring invalid number: {part}{Style.RESET_ALL}")
        elif part:
            print(f"    {Fore.RED}Ignoring invalid input: {part}{Style.RESET_ALL}")

    return selected


def _pick_one(title: str, options: list[str], current: str | None = None) -> str | None:
    """Show a numbered list and let the user pick exactly one item.

    Like _pick_from_list but for single-select fields (radio buttons).
    The current value is marked in the display so you know what's already set.

    Args:
        title: The heading to show above the list.
        options: The list of options to display.
        current: If provided, marks this option with an arrow in the display.

    Returns:
        The selected option string, or None if the user cancelled.
    """
    print(f"\n  {Fore.CYAN}{title}{Style.RESET_ALL}")
    for i, opt in enumerate(options, 1):
        marker = f" {Fore.GREEN}\u2190 current{Style.RESET_ALL}" if opt == current else ""
        print(f"    {Fore.WHITE}{i:>3}{Style.RESET_ALL}. {opt}{marker}")

    print(f"\n  Enter a number, or 'c' to cancel:")
    raw = input(f"  {Fore.CYAN}>{Style.RESET_ALL} ").strip()

    if raw.lower() == "c":
        return None

    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx]

    print(f"    {Fore.RED}Invalid selection.{Style.RESET_ALL}")
    return None


def _edit_employer(employer: dict[str, Any]) -> dict[str, Any]:
    """Open an interactive edit sub-menu to modify employer data before applying.

    Makes a deep copy first so we don't accidentally mutate the original.
    The user can change any of the 5 property fields, add or remove states
    and industry tags, and then go back to the preview.

    Args:
        employer: The employer dict to edit.

    Returns:
        A (possibly modified) copy of the employer dict.
    """
    emp = copy.deepcopy(employer)

    while True:
        # Show the edit menu with current values
        sep_line = "─" * 50
        dash = "—"
        print(f"\n  {Fore.BLUE}{sep_line}{Style.RESET_ALL}")
        print(f"  {Fore.BLUE}EDIT MENU {dash} {emp['name']}{Style.RESET_ALL}")
        print(f"  {Fore.BLUE}{sep_line}{Style.RESET_ALL}")
        print(f"    {Fore.WHITE}1{Style.RESET_ALL}. Change CICS Hires      (current: {emp.get('cics_hires') or dash})")
        print(f"    {Fore.WHITE}2{Style.RESET_ALL}. Change Sponsoring       (current: {emp.get('sponsoring') or dash})")
        print(f"    {Fore.WHITE}3{Style.RESET_ALL}. Change Size             (current: {emp.get('size') or dash})")
        print(f"    {Fore.WHITE}4{Style.RESET_ALL}. Edit States             (current: {', '.join(emp.get('states', [])) or dash})")
        print(f"    {Fore.WHITE}5{Style.RESET_ALL}. Edit Industry Tags      (current: {', '.join(emp.get('industry_tags', [])) or dash})")
        print(f"    {Fore.WHITE}6{Style.RESET_ALL}. Remove a state")
        print(f"    {Fore.WHITE}7{Style.RESET_ALL}. Remove an industry tag")
        print(f"    {Fore.GREEN}d{Style.RESET_ALL}. Done editing \u2014 back to preview")
        print()

        choice = input(f"  {Fore.CYAN}Choice:{Style.RESET_ALL} ").strip().lower()

        # Handle each menu option
        if choice == "1":
            val = _pick_one("Select CICS Hires:", cfp.VALID_CICS_HIRES, emp.get("cics_hires"))
            if val is not None:
                emp["cics_hires"] = val
                print(f"    {Fore.GREEN}Set CICS Hires \u2192 {val}{Style.RESET_ALL}")

        elif choice == "2":
            val = _pick_one("Select Sponsoring:", cfp.VALID_SPONSORING, emp.get("sponsoring"))
            if val is not None:
                emp["sponsoring"] = val
                print(f"    {Fore.GREEN}Set Sponsoring \u2192 {val}{Style.RESET_ALL}")

        elif choice == "3":
            val = _pick_one("Select Size:", cfp.VALID_SIZES, emp.get("size"))
            if val is not None:
                emp["size"] = val
                print(f"    {Fore.GREEN}Set Size \u2192 {val}{Style.RESET_ALL}")

        elif choice == "4":
            # States are multi-select, so we merge new selections with existing ones
            result = _pick_from_list(
                "Select States (pick all that apply):",
                cfp.VALID_STATES,
                emp.get("states"),
            )
            if result is not None:
                existing = set(emp.get("states", []))
                for s in result:
                    existing.add(s)
                emp["states"] = sorted(existing, key=lambda x: cfp.VALID_STATES.index(x) if x in cfp.VALID_STATES else 999)
                print(f"    {Fore.GREEN}States \u2192 {', '.join(emp['states'])}{Style.RESET_ALL}")

        elif choice == "5":
            # Industry tags are also multi-select with merge behavior
            result = _pick_from_list(
                "Select Industry Tags (pick all that apply):",
                cfp.VALID_INDUSTRY_TAGS,
                emp.get("industry_tags"),
            )
            if result is not None:
                existing = set(emp.get("industry_tags", []))
                for t in result:
                    existing.add(t)
                emp["industry_tags"] = sorted(existing, key=lambda x: cfp.VALID_INDUSTRY_TAGS.index(x) if x in cfp.VALID_INDUSTRY_TAGS else 999)
                print(f"    {Fore.GREEN}Tags \u2192 {', '.join(emp['industry_tags'])}{Style.RESET_ALL}")

        elif choice == "6":
            # Remove states from the current list
            current_states = emp.get("states", [])
            if not current_states:
                print(f"    {Fore.YELLOW}No states to remove.{Style.RESET_ALL}")
                continue
            result = _pick_from_list("Select states to REMOVE:", current_states)
            if result is not None:
                emp["states"] = [s for s in current_states if s not in result]
                print(f"    {Fore.GREEN}States \u2192 {', '.join(emp['states']) or '(none)'}{Style.RESET_ALL}")

        elif choice == "7":
            # Remove industry tags from the current list
            current_tags = emp.get("industry_tags", [])
            if not current_tags:
                print(f"    {Fore.YELLOW}No tags to remove.{Style.RESET_ALL}")
                continue
            result = _pick_from_list("Select tags to REMOVE:", current_tags)
            if result is not None:
                emp["industry_tags"] = [t for t in current_tags if t not in result]
                print(f"    {Fore.GREEN}Tags \u2192 {', '.join(emp['industry_tags']) or '(none)'}{Style.RESET_ALL}")

        elif choice == "d":
            break

        else:
            print(f"    {Fore.RED}Invalid choice. Try again.{Style.RESET_ALL}")

    return emp


def interactive_prompt(
    employer: dict[str, Any],
    index: int,
    total: int,
) -> tuple[str, dict[str, Any]]:
    """Show the full interactive approval prompt for a single employer.

    Displays the employer preview, the action menu, and handles the user's
    choice. If the user picks "edit", it opens the edit sub-menu and then
    loops back to the preview with the updated data.

    Args:
        employer: The employer dict to show.
        index: The 1-based position of this employer in the queue.
        total: The total number of employers in the queue.

    Returns:
        A tuple of (action, employer_data) where action is one of
        "approve", "skip", or "quit", and employer_data is the
        (possibly edited) employer dict.
    """
    emp = copy.deepcopy(employer)

    while True:
        _print_header(index, total, emp["name"])
        _print_employer_preview(emp)
        _print_action_menu()

        choice = input(f"  {Fore.CYAN}Choice:{Style.RESET_ALL} ").strip().lower()

        if choice == "a":
            return "approve", emp
        elif choice == "s":
            return "skip", emp
        elif choice == "e":
            emp = _edit_employer(emp)
            # Loop back around to show the updated preview
            continue
        elif choice == "q":
            return "quit", emp
        else:
            print(f"  {Fore.RED}Invalid choice \u2014 please enter a, s, e, or q.{Style.RESET_ALL}")


# ===========================================================================
#  Main async loop
# ===========================================================================
# This is where everything comes together. Parse the CSV, connect to the
# browser, loop through employers, handle retries, and write summary logs.

async def run(args: argparse.Namespace) -> None:
    """The main async function that orchestrates the whole transfer process.

    Here's the high-level flow:
        1. Apply CLI overrides (dry-run, auto mode, custom CSV path).
        2. Parse the CSV file into employer dicts.
        3. Handle --start-from and --limit flags.
        4. Load the checkpoint file to skip already-processed employers.
        5. Connect to the browser (unless dry-run mode).
        6. Loop through each employer: prompt, process with retries, checkpoint.
        7. Print a summary and write log files.

    Args:
        args: The parsed CLI arguments from parse_args().
    """
    logger = logging.getLogger("main")

    # -- Apply CLI overrides --
    csv_path = args.csv or config.CSV_PATH
    if args.dry_run:
        config.DRY_RUN = True
        logger.info("\U0001f3dc\ufe0f  DRY RUN mode enabled \u2014 no changes will be made")
    if args.auto:
        config.AUTO_MODE = True
        logger.info("\U0001f916 AUTO mode enabled \u2014 skipping interactive prompts")

    # -- Parse the CSV file --
    employers = csv_parser.parse_csv(csv_path)
    total_csv = len(employers)
    logger.info("Loaded %d employers from CSV", total_csv)

    # -- Handle --start-from (skip ahead to a specific employer) --
    if args.start_from:
        found = False
        for idx, emp in enumerate(employers):
            if emp["name"] == args.start_from:
                employers = employers[idx:]
                found = True
                logger.info(
                    "Starting from '%s' (skipping first %d employers)",
                    args.start_from, idx,
                )
                break
        if not found:
            logger.error(
                "Employer '%s' not found in CSV \u2014 aborting", args.start_from
            )
            return

    # -- Handle --limit (only process N employers) --
    if args.limit:
        employers = employers[: args.limit]
        logger.info("Limiting to %d employers", len(employers))

    # -- Load checkpoint to skip already-done employers --
    progress = checkpoint.load_progress()
    already_done = len(progress["completed"])

    # -- Connect to the browser (skip in dry-run mode) --
    if config.DRY_RUN:
        logger.info("DRY RUN \u2014 skipping browser connection")
        page = None  # type: ignore[assignment]
    else:
        pw_ctx = async_playwright()
        pw = await pw_ctx.start()
        browser, context, page = await automation.connect_browser(pw)

    try:
        # Counters for the summary at the end
        succeeded = 0
        failed = 0
        skipped_already = 0
        skipped_user = 0

        # -- Main employer loop --
        for i, employer in enumerate(employers, start=1):
            name = employer["name"]

            # Skip employers we already processed in a previous run
            if checkpoint.is_processed(name, progress):
                logger.info(
                    "[%d/%d] SKIP (already completed): %s", i, len(employers), name
                )
                skipped_already += 1
                continue

            # Skip employers the user previously chose to skip
            if checkpoint.is_skipped(name, progress):
                logger.info(
                    "[%d/%d] SKIP (user skipped): %s", i, len(employers), name
                )
                skipped_user += 1
                continue

            # Skip employers we already know aren't on CareerForge
            fail_reason = progress.get("failed", {}).get(name, "")
            if "not found in search" in fail_reason.lower():
                logger.info(
                    "[%d/%d] SKIP (not on CareerForge): %s", i, len(employers), name
                )
                skipped_already += 1
                continue

            # -- Interactive prompt (unless --auto or --dry-run) --
            emp_to_apply = employer

            if not config.AUTO_MODE and not config.DRY_RUN:
                action, emp_to_apply = interactive_prompt(employer, i, len(employers))

                if action == "quit":
                    logger.info("User quit \u2014 saving progress and exiting.")
                    break
                elif action == "skip":
                    checkpoint.mark_skipped(name, progress)
                    skipped_user += 1
                    logger.info("[%d/%d] User skipped: %s", i, len(employers), name)
                    continue
                # action == "approve" means we proceed below

            logger.info("[%d/%d] Processing: %s", i, len(employers), name)

            # -- Dry-run mode: just log what we would do --
            if config.DRY_RUN:
                logger.info("[DRY RUN] Would process: %s", name)
                logger.info("[DRY RUN]   cics_hires:    %s", emp_to_apply.get("cics_hires"))
                logger.info("[DRY RUN]   sponsoring:    %s", emp_to_apply.get("sponsoring"))
                logger.info("[DRY RUN]   states:        %s", emp_to_apply.get("states"))
                logger.info("[DRY RUN]   size:          %s", emp_to_apply.get("size"))
                logger.info("[DRY RUN]   industry_tags: %s", emp_to_apply.get("industry_tags"))
                logger.info("[DRY RUN]   skipped_tags:  %s", emp_to_apply.get("skipped_tags"))
                logger.info("[DRY RUN]   skipped_states:%s", emp_to_apply.get("skipped_states"))
                succeeded += 1
                continue

            # -- Retry loop: try up to MAX_RETRIES_PER_EMPLOYER times --
            success = False
            not_found = False
            last_error: Exception | None = None
            for attempt in range(1, config.MAX_RETRIES_PER_EMPLOYER + 1):
                try:
                    await automation.process_employer(page, emp_to_apply)
                    checkpoint.mark_completed(name, progress)
                    succeeded += 1
                    success = True
                    break

                except automation.EmployerNotFoundError as exc:
                    # This employer just doesn't exist on CareerForge. No point retrying.
                    last_error = exc
                    not_found = True
                    logger.warning(
                        "Employer '%s' not found on CareerForge \u2014 skipping retries",
                        name,
                    )
                    break

                except Exception as exc:
                    # Something else went wrong. Take a screenshot and maybe retry.
                    last_error = exc
                    logger.error(
                        "Attempt %d/%d failed for '%s': %s",
                        attempt, config.MAX_RETRIES_PER_EMPLOYER, name, exc,
                        exc_info=True,
                    )
                    await automation.screenshot_on_error(page, name)

                    if attempt < config.MAX_RETRIES_PER_EMPLOYER:
                        logger.info("Retrying '%s' \u2026", name)
                        await asyncio.sleep(config.PAGE_LOAD_DELAY)

            # -- Manual retry prompt (interactive mode only, not for "not found") --
            # If all automatic retries failed and we're in interactive mode,
            # give the user a chance to retry manually or skip.
            while not success and not not_found and not config.AUTO_MODE and not config.DRY_RUN:
                err_short = str(last_error).split("\n")[0][:80] if last_error else "unknown"
                print()
                print(f"  {Fore.RED}\u274c All automatic retries failed for: {name}{Style.RESET_ALL}")
                print(f"     Error: {Fore.YELLOW}{err_short}{Style.RESET_ALL}")
                sep = "\u2500" * 64
                print(f"\n{Fore.CYAN}{sep}{Style.RESET_ALL}")
                print(f"  {Fore.GREEN}[r]{Style.RESET_ALL} Retry manually      "
                      f"{Fore.YELLOW}[s]{Style.RESET_ALL} Skip (mark failed)")
                print(f"  {Fore.RED}[q]{Style.RESET_ALL} Quit script")
                print(f"{Fore.CYAN}{sep}{Style.RESET_ALL}")
                retry_choice = input(f"  {Fore.CYAN}Choice:{Style.RESET_ALL} ").strip().lower()

                if retry_choice == "r":
                    logger.info("Manual retry for '%s' \u2026", name)
                    try:
                        await automation.process_employer(page, emp_to_apply)
                        checkpoint.mark_completed(name, progress)
                        succeeded += 1
                        success = True
                    except Exception as exc:
                        last_error = exc
                        logger.error("Manual retry failed for '%s': %s", name, exc, exc_info=True)
                        await automation.screenshot_on_error(page, name)
                        # Loop back to the prompt again
                elif retry_choice == "s":
                    break
                elif retry_choice == "q":
                    logger.info("User quit \u2014 saving progress and exiting.")
                    checkpoint.mark_failed(name, str(last_error), progress)
                    failed += 1
                    raise KeyboardInterrupt
                else:
                    print(f"  {Fore.RED}Invalid choice \u2014 please enter r, s, or q.{Style.RESET_ALL}")

            # If we still didn't succeed, mark it as failed
            if not success:
                checkpoint.mark_failed(name, str(last_error), progress)
                failed += 1

            # -- Delay between employers (with jitter so we're not too predictable) --
            await asyncio.sleep(
                config.BETWEEN_EMPLOYERS_DELAY
                + random.uniform(0, config.JITTER)
            )

        # -- Print run summary --
        total_skipped = skipped_already + skipped_user
        remaining = total_csv - already_done - succeeded - failed - total_skipped
        summary = (
            "\n"
            "\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\n"
            "          RUN SUMMARY\n"
            "\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\n"
            f"  Total employers in CSV:  {total_csv}\n"
            f"  Already processed:       {already_done}\n"
            f"  Processed this run:      {succeeded + failed}\n"
            f"    \u2705 Succeeded:           {succeeded}\n"
            f"    \u274c Failed:              {failed}\n"
            f"    \u23ed\ufe0f  Skipped (done):      {skipped_already}\n"
            f"    \U0001f464 Skipped (user):      {skipped_user}\n"
            f"  Remaining (approx):      {max(remaining, 0)}\n"
            "\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550"
        )
        logger.info(summary)

        # -- Write summary log files for easy review --
        progress_dir = Path("progress")
        progress_dir.mkdir(parents=True, exist_ok=True)

        # Write out all successfully processed employers
        if progress["completed"]:
            successes_path = progress_dir / "successes.log"
            with successes_path.open("w") as fh:
                fh.write(f"Successfully processed employers ({len(progress['completed'])}):\n\n")
                for emp_name in progress["completed"]:
                    fh.write(f"  \u2705 {emp_name}\n")
            logger.info("Successes written to %s", successes_path)

        # Write out all failures with their error messages
        if progress["failed"]:
            failures_path = progress_dir / "failures.log"
            with failures_path.open("w") as fh:
                fh.write(f"Failed employers ({len(progress['failed'])}):\n\n")
                for emp_name, reason in progress["failed"].items():
                    fh.write(f"  \u274c {emp_name}: {reason}\n")
            logger.info("Failures written to %s", failures_path)

        # Write out employers that weren't found on CareerForge (subset of failures)
        not_found = {
            name: reason
            for name, reason in progress["failed"].items()
            if "not found in search" in reason.lower()
        }
        if not_found:
            not_found_path = progress_dir / "not_found.log"
            with not_found_path.open("w") as fh:
                fh.write(f"Employers not found on CareerForge ({len(not_found)}):\n\n")
                for emp_name, reason in not_found.items():
                    fh.write(f"  \U0001f50d {emp_name}\n")
            logger.info("Not-found employers written to %s", not_found_path)

    finally:
        # Always clean up the Playwright connection
        if not config.DRY_RUN:
            await pw.stop()  # type: ignore[possibly-undefined]


# ===========================================================================
#  Entry point
# ===========================================================================

def main() -> None:
    """Sync entry point for the script. Parses args, sets up logging, and kicks off the async loop.

    Handles KeyboardInterrupt gracefully (progress is already checkpointed)
    and catches any fatal exceptions so they get logged properly.
    """
    args = parse_args()
    setup_logging()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logging.info("Interrupted by user \u2014 progress has been checkpointed.")
        sys.exit(1)
    except Exception:
        logging.critical("Fatal error", exc_info=True)
        sys.exit(2)


if __name__ == "__main__":
    main()

"""
csv_parser.py - Reads and cleans up the Notion CSV export for CareerForge.

This file takes the raw CSV that Notion spits out (with emoji column headers,
weird whitespace, etc.) and turns it into a nice clean list of employer dicts.

Here's the general flow:
    1. Open the CSV file (handles the BOM that Notion likes to add).
    2. Normalize the column headers (strip emoji, lowercase, match to our field names).
    3. For each row, parse multi-value fields like states and industry tags.
    4. Validate every value against the CareerForge whitelist so we only send good data.
    5. Track anything that got skipped so the TUI can show the user what won't transfer.

If an employer "name" is actually a URL (someone pasted a link instead of a company
name in Notion), we try to extract the company name from the domain. For example,
https://eyepointpharma.com/about/ becomes "Eyepointpharma". For LinkedIn URLs we
pull the name from the path instead (e.g. linkedin.com/company/weaver-biosciences
becomes "Weaver Biosciences").

The only public function you really need is parse_csv(). Everything else is internal.
"""

from __future__ import annotations

import csv
import re
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import careerforge_properties as cfp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column-name normalisation
# ---------------------------------------------------------------------------
# Notion CSV headers sometimes have emoji prefixes and extra spaces.
# We need to strip all that out so we can match them to our field names.

# This regex matches anything that's not basic ASCII (catches emoji and other unicode stuff)
_STRIP_RE = re.compile(r"[^\x00-\x7F]+")

# This maps cleaned-up header names to the field names we use internally.
# Some columns have multiple possible names (like "sponsoring?" vs "sponsoring")
# so we list all the variants we've seen.
_COLUMN_MAP: dict[str, str] = {
    "employer":      "name",
    "cics hires":    "cics_hires",
    "sponsoring?":   "sponsoring",
    "sponsoring":    "sponsoring",
    "industry tags": "industry_tags",
    "size":          "size",
    "state(s)":      "states",
    "states":        "states",
    "state":         "states",
}


def _normalise_header(raw: str) -> str:
    """Clean up a single CSV header string so we can look it up in _COLUMN_MAP.

    Strips out emoji and non-ASCII characters, collapses extra whitespace,
    and lowercases the whole thing.

    Args:
        raw: The raw header string straight from the CSV file.

    Returns:
        A cleaned-up, lowercase version of the header.
    """
    cleaned = _STRIP_RE.sub("", raw)
    return " ".join(cleaned.split()).strip().lower()


def _resolve_columns(raw_headers: list[str]) -> dict[int, str]:
    """Figure out which CSV columns map to which internal field names.

    Goes through each header in the CSV, cleans it up, and checks if it
    matches anything in our _COLUMN_MAP. Builds a dict that maps column
    index to field name.

    Args:
        raw_headers: The list of raw header strings from the first row of the CSV.

    Returns:
        A dict like {0: "name", 2: "cics_hires", ...} mapping column positions
        to our canonical field names.
    """
    mapping: dict[int, str] = {}
    for idx, raw in enumerate(raw_headers):
        normalised = _normalise_header(raw)
        if normalised in _COLUMN_MAP:
            mapping[idx] = _COLUMN_MAP[normalised]
    return mapping


# ---------------------------------------------------------------------------
# Value helpers - small utilities for cleaning up cell values
# ---------------------------------------------------------------------------

def _split_multi(value: str) -> list[str]:
    """Split a comma-separated cell into a list of trimmed strings.

    Drops any blank entries that would come from trailing commas or extra spaces.

    Args:
        value: The raw cell value, like "MA, CA, NY".

    Returns:
        A list of cleaned strings, like ["MA", "CA", "NY"].
    """
    return [v.strip() for v in value.split(",") if v.strip()]


def _clean_str(value: str) -> str | None:
    """Strip whitespace from a string and return None if it's blank.

    Args:
        value: The raw string to clean.

    Returns:
        The stripped string, or None if it was empty/whitespace-only.
    """
    v = value.strip()
    return v if v else None


# ---------------------------------------------------------------------------
# URL-to-company-name extraction
# ---------------------------------------------------------------------------
# Some rows in the Notion CSV have a URL pasted in the employer name column
# instead of an actual company name (mostly biotech/pharma companies).
# We detect those and try to pull the company name out of the domain.

# Regex to quickly check if a string looks like a URL
_URL_RE = re.compile(r"^https?://|^www\.", re.IGNORECASE)

# Common domain suffixes and path segments we want to strip out
_DOMAIN_JUNK = {"www", "com", "org", "net", "io", "ai", "bio", "co", "us", "health"}

# Domains where the company name is in the URL path, not the domain itself.
# For example, linkedin.com/company/weaver-biosciences gives us "Weaver Biosciences"
_PATH_NAME_DOMAINS = {"linkedin.com", "www.linkedin.com"}


def _looks_like_url(name: str) -> bool:
    """Check if an employer name is actually a URL.

    Args:
        name: The employer name string to check.

    Returns:
        True if it starts with http://, https://, or www.
    """
    return bool(_URL_RE.match(name.strip()))


def _titleize_slug(slug: str) -> str:
    """Turn a URL slug like 'weaver-biosciences' into 'Weaver Biosciences'.

    Splits on hyphens and underscores, capitalizes each word, and joins with spaces.

    Args:
        slug: A URL path segment like "weaver-biosciences" or "cool_startup"

    Returns:
        A title-cased string like "Weaver Biosciences" or "Cool Startup"
    """
    # Split on hyphens and underscores
    words = re.split(r"[-_]+", slug)
    # Capitalize each word and join with spaces
    return " ".join(w.capitalize() for w in words if w)


def _extract_name_from_url(url: str) -> str | None:
    """Try to pull a company name out of a URL.

    For most URLs, we grab the company name from the domain. For example,
    "https://eyepointpharma.com/about/" becomes "Eyepointpharma".

    For LinkedIn URLs (and similar sites where the name is in the path),
    we extract from the path instead. For example,
    "https://linkedin.com/company/weaver-biosciences" becomes "Weaver Biosciences".

    This isn't perfect but it gives a reasonable guess that's way better than
    searching CareerForge for a raw URL.

    Args:
        url: The full URL string.

    Returns:
        A cleaned-up company name string, or None if we couldn't extract anything.

    Examples:
        "https://eyepointpharma.com/about/"             -> "Eyepointpharma"
        "https://www.broadcom.com/careers"              -> "Broadcom"
        "https://linkedin.com/company/weaver-bio"       -> "Weaver Bio"
        "http://2123frontiers.com/"                     -> "2123Frontiers"
    """
    url = url.strip()

    # Make sure it has a scheme so urlparse works right
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return None

        # Special handling for LinkedIn and similar sites where the company
        # name lives in the URL path (like /company/weaver-biosciences/)
        if hostname.lower() in _PATH_NAME_DOMAINS:
            path_parts = [p for p in parsed.path.strip("/").split("/") if p]
            # LinkedIn pattern is /company/company-name or /company/company-name/
            # We want the last meaningful segment (skip "company", "in", etc.)
            skip_segments = {"company", "in", "school", "showcase", "posts", "about"}
            for part in reversed(path_parts):
                if part.lower() not in skip_segments:
                    return _titleize_slug(part)
            # If all parts were generic, fall through to domain extraction
            return None

        # For regular URLs, extract from the domain
        # Split the domain into parts like ["www", "eyepointpharma", "com"]
        parts = hostname.split(".")

        # Filter out common junk like "www", "com", "org", etc.
        meaningful = [p for p in parts if p.lower() not in _DOMAIN_JUNK]

        if not meaningful:
            # Everything got filtered out (like www.com), just use the biggest part
            meaningful = [max(parts, key=len)]

        # Take the first meaningful part as the company name
        raw_name = meaningful[0]

        # Title-case it so "eyepointpharma" becomes "Eyepointpharma"
        # But keep numbers at the start intact (like "2123Frontiers")
        if raw_name and raw_name[0].isdigit():
            # Find where the letters start and capitalize that
            for i, ch in enumerate(raw_name):
                if ch.isalpha():
                    raw_name = raw_name[:i] + raw_name[i:].capitalize()
                    break
        else:
            raw_name = raw_name.capitalize()

        return raw_name if raw_name else None

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Validation helpers - check values against the CareerForge whitelist
# ---------------------------------------------------------------------------
# Each of these functions checks a single value (or list of values) against
# what CareerForge actually accepts. If something doesn't match, we log it
# and return None (or put it in the "skipped" bucket).

def _validate_cics_hires(raw: str | None) -> str | None:
    """Check if a CICS Hires value is valid for CareerForge.

    Args:
        raw: The cleaned string from the CSV (e.g. "10+", "1-9").

    Returns:
        The value unchanged if it's valid, or None if it's not on the whitelist.
    """
    if raw and raw in cfp._VALID_CICS_HIRES_SET:
        return raw
    if raw:
        logger.debug("Invalid CICS Hires value: '%s'", raw)
    return None


def _validate_sponsoring(raw: str | None) -> str | None:
    """Check if a Sponsoring value is valid for CareerForge.

    Args:
        raw: The cleaned string from the CSV (e.g. "Yes (CPT/OPT)", "No").

    Returns:
        The value unchanged if it's valid, or None if it's not on the whitelist.
    """
    if raw and raw in cfp._VALID_SPONSORING_SET:
        return raw
    if raw:
        logger.debug("Invalid Sponsoring value: '%s'", raw)
    return None


def _validate_size(raw: str | None) -> str | None:
    """Check if a Size value is valid for CareerForge.

    Args:
        raw: The cleaned string from the CSV (e.g. "Large", "Small").

    Returns:
        The value unchanged if it's valid, or None if it's not on the whitelist.
    """
    if raw and raw in cfp._VALID_SIZES_SET:
        return raw
    if raw:
        logger.debug("Invalid Size value: '%s'", raw)
    return None


# These aliases handle common abbreviations we see in the Notion CSV.
# For example, "Int'l" should become "International" to match CareerForge.
_STATE_ALIASES: dict[str, str] = {
    "Int'l":        "International",
    "Intl":         "International",
    "international": "International",
    "remote":       "Remote",
}


def _validate_states(raw_list: list[str]) -> tuple[list[str], list[str]]:
    """Check each state value and split them into matched vs skipped.

    Tries alias mapping first (like "Int'l" to "International"), then checks
    against the CareerForge whitelist. Anything that doesn't match goes into
    the skipped list so the user can see what got dropped.

    Args:
        raw_list: List of state strings from the CSV (already split by comma).

    Returns:
        A tuple of (matched, skipped) where matched is the list of valid states
        and skipped is the list of states that didn't match anything.
    """
    matched: list[str] = []
    skipped: list[str] = []
    for s in raw_list:
        # Try alias mapping first
        normalised = _STATE_ALIASES.get(s, s)
        if normalised in cfp._VALID_STATES_SET:
            matched.append(normalised)
        else:
            skipped.append(s)
    return matched, skipped


def _validate_industry_tags(raw_list: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Map CSV industry tags to CareerForge tags and validate them.

    The CSV might use different names than CareerForge does, so we use the
    INDUSTRY_TAG_MAPPING to translate. Also handles deduplication since
    two different CSV names might map to the same CareerForge tag.

    Args:
        raw_list: List of industry tag strings from the CSV (already split by comma).

    Returns:
        A tuple of (matched_tags, skipped_tags, raw_tags):
            - matched_tags: valid CareerForge tag names after mapping
            - skipped_tags: CSV values that couldn't be mapped to anything
            - raw_tags: the original CSV values (kept around for display)
    """
    matched: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()  # avoid duplicates after mapping

    for tag in raw_list:
        mapped = cfp.INDUSTRY_TAG_MAPPING.get(tag.lower().strip())
        if mapped and mapped not in seen:
            matched.append(mapped)
            seen.add(mapped)
        elif mapped and mapped in seen:
            pass  # duplicate after mapping, just skip it quietly
        else:
            skipped.append(tag)

    return matched, skipped, list(raw_list)


# ---------------------------------------------------------------------------
# Public API - this is the main function other files call
# ---------------------------------------------------------------------------

def parse_csv(csv_path: str | Path) -> list[dict[str, Any]]:
    """Read the Notion CSV export and return a list of employer dicts.

    This is the main entry point for the csv_parser module. It opens the CSV,
    figures out the columns, and builds a validated employer dict for each row.

    If an employer name looks like a URL (starts with http/https/www), we try
    to extract the company name from the domain instead. For example,
    "https://eyepointpharma.com/about/" becomes "Eyepointpharma".

    Each employer dict looks like this:
        {
            "name":              "Google",
            "cics_hires":        "10+" or None,
            "sponsoring":        "Yes (CPT/OPT)" or None,
            "industry_tags":     ["FinTech", "Cloud"],
            "size":              "Large" or None,
            "states":            ["MA", "CA"],
            "skipped_tags":      ["SomeBadTag"],
            "skipped_states":    ["SomeBadState"],
            "raw_industry_tags": ["fintech", "cloud", "SomeBadTag"],
        }

    Args:
        csv_path: Path to the Notion CSV file. Can be a string or Path object.

    Returns:
        A list of employer dicts, one per row in the CSV (skipping blank rows).

    Raises:
        FileNotFoundError: If the CSV file doesn't exist.
        ValueError: If the CSV doesn't have a recognizable "Employer" column.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    employers: list[dict[str, Any]] = []

    # utf-8-sig handles the BOM (byte order mark) that Notion adds to CSV exports
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)

        # First row is headers - figure out which columns are which
        raw_headers = next(reader)
        col_map = _resolve_columns(raw_headers)

        # Make sure we at least found the employer name column
        if "name" not in col_map.values():
            raise ValueError(
                "Could not find an 'Employer' column in the CSV. "
                f"Headers found: {raw_headers}"
            )

        logger.info("Resolved CSV columns: %s", col_map)

        url_count = 0  # track how many URL names we fix up

        # Now go through each data row
        for row_num, row in enumerate(reader, start=2):
            # Build a dict of field name to raw value for this row
            raw: dict[str, str] = {}
            for idx, field in col_map.items():
                raw[field] = row[idx] if idx < len(row) else ""

            # Skip rows with no employer name
            name = raw.get("name", "").strip()
            if not name:
                logger.debug("Row %d: empty employer name, skipping", row_num)
                continue

            # Some Notion rows have a URL pasted as the employer name instead
            # of an actual company name. We try to extract the name from the domain.
            if _looks_like_url(name):
                extracted = _extract_name_from_url(name)
                if extracted:
                    logger.warning(
                        "Row %d: employer name is a URL (%s), extracted name: '%s'",
                        row_num, name, extracted
                    )
                    name = extracted
                    url_count += 1
                else:
                    logger.warning(
                        "Row %d: employer name is a URL (%s) and we couldn't "
                        "extract a name from it, skipping this row",
                        row_num, name
                    )
                    continue

            # Parse the multi-value fields (comma-separated stuff like states and tags)
            raw_states_list = _split_multi(raw.get("states", ""))
            raw_tags_list = _split_multi(raw.get("industry_tags", ""))

            # Validate everything against the CareerForge whitelist
            matched_states, skipped_states = _validate_states(raw_states_list)
            matched_tags, skipped_tags, raw_tags = _validate_industry_tags(raw_tags_list)

            # Build the final employer dict with both matched and skipped values
            employer: dict[str, Any] = {
                "name":              name,
                "cics_hires":        _validate_cics_hires(_clean_str(raw.get("cics_hires", ""))),
                "sponsoring":        _validate_sponsoring(_clean_str(raw.get("sponsoring", ""))),
                "industry_tags":     matched_tags,
                "size":              _validate_size(_clean_str(raw.get("size", ""))),
                "states":            matched_states,
                "skipped_tags":      skipped_tags,
                "skipped_states":    skipped_states,
                "raw_industry_tags": raw_tags,
            }
            employers.append(employer)

    if url_count:
        logger.info(
            "Fixed %d employer names that were URLs (extracted company name from domain)",
            url_count
        )
    logger.info("Parsed %d employers from %s", len(employers), csv_path)
    return employers

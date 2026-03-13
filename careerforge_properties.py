"""
Single source of truth for every valid CareerForge property value.

CareerForge has a fixed set of dropdown/checkbox options for things like
industry tags, company size, sponsoring status, etc. This file lists
every legal value so the rest of the codebase can validate CSV data
and build UI interactions without guessing.

Also includes:
  - INDUSTRY_TAG_EMOJI_MAP: the emoji-prefixed labels that actually
    appear in the CareerForge checkbox UI.
  - INDUSTRY_TAG_MAPPING: a case-insensitive lookup that converts
    Notion CSV tag names into their CareerForge equivalents (with
    handy aliases like "pit" and "ecommerce").
  - _VALID_*_SET: set versions of each list for fast O(1) membership
    checks during validation.
"""

# ---------------------------------------------------------------------------
# CICS Hires (how many co-ops a company has hired through CICS)
# ---------------------------------------------------------------------------

VALID_CICS_HIRES = ["1", "2", "3+", "4+", "5+", "10+", "20+", "50+", "100+"]

# ---------------------------------------------------------------------------
# Sponsoring (does the company sponsor international students?)
# ---------------------------------------------------------------------------

VALID_SPONSORING = ["Yes (CPT/OPT)", "Sometimes", "No"]

# ---------------------------------------------------------------------------
# US States (where the company has offices or remote positions)
# ---------------------------------------------------------------------------
# "Remote" and "International" are special non-state entries.

VALID_STATES = [
    "Remote", "International",
    "MA", "NY", "CA", "WA", "NH", "CT", "TX", "GA", "NC", "FL",
    "ME", "RI", "NJ", "PA", "VA", "OR", "MD", "TN", "NE", "NV",
    "AR", "WI", "CO", "UT", "DC", "OH", "SC", "MO", "AZ", "KY",
    "NM", "KS", "IN", "IA", "ID", "AL", "MN", "DE", "WY", "VT",
    "IL", "AK", "HI", "LA", "MI", "MS", "MT", "ND", "OK", "SD", "WV",
]

# ---------------------------------------------------------------------------
# Company size buckets
# ---------------------------------------------------------------------------

VALID_SIZES = ["Small", "Medium", "Large"]

# ---------------------------------------------------------------------------
# Industry tags (the checkbox labels in CareerForge, without emojis)
# ---------------------------------------------------------------------------

VALID_INDUSTRY_TAGS = [
    "FinTech", "Healthcare", "AI", "Robotics",
    "Public Interest Technology (PIT)", "Cybersecurity",
    "Defense", "Consulting", "Research", "Startup",
    "Networking", "Hardware", "E-commerce", "Gaming",
]

# ---------------------------------------------------------------------------
# Emoji map: plain tag name -> emoji-prefixed display string
# ---------------------------------------------------------------------------
# CareerForge renders each industry tag with an emoji in front.
# When the automation clicks checkboxes it needs to match these exact
# display strings, not just the plain tag name.

INDUSTRY_TAG_EMOJI_MAP: dict[str, str] = {
    "FinTech":                          "💰 FinTech",
    "Healthcare":                       "🚑 Healthcare",
    "AI":                               "✨ AI",
    "Robotics":                         "🤖 Robotics",
    "Public Interest Technology (PIT)": "🌱 Public Interest Technology (PIT)",
    "Cybersecurity":                    "🔒 Cybersecurity",
    "Defense":                          "🎖️ Defense",
    "Consulting":                       "📈 Consulting",
    "Research":                         "🧠 Research",
    "Startup":                          "🚀 Startup",
    "Networking":                       "📡 Networking",
    "Hardware":                         "⚙️ Hardware",
    "E-commerce":                       "🛍️ E-commerce",
    "Gaming":                           "🎮 Gaming",
}

# ---------------------------------------------------------------------------
# Notion CSV tag name -> CareerForge tag name (case-insensitive mapping)
# ---------------------------------------------------------------------------
# The keys here are lowercase so we can do a simple .lower() lookup.
# Some Notion tags have aliases (e.g. "pit" and "common good" both map
# to "Public Interest Technology (PIT)").
#
# Tags that exist in Notion but have no CareerForge equivalent (like
# "Tech", "Banking", "Insurance", etc.) are intentionally left out.
# The CSV parser will flag those as "skipped" in the terminal output.

INDUSTRY_TAG_MAPPING: dict[str, str] = {
    "fintech":                          "FinTech",
    "healthcare":                       "Healthcare",
    "ai":                               "AI",
    "robotics":                         "Robotics",
    "public interest technology (pit)": "Public Interest Technology (PIT)",
    "pit":                              "Public Interest Technology (PIT)",
    "common good":                      "Public Interest Technology (PIT)",
    "cybersecurity":                    "Cybersecurity",
    "defense":                          "Defense",
    "consulting":                       "Consulting",
    "research":                         "Research",
    "startup":                          "Startup",
    "networking":                       "Networking",
    "hardware":                         "Hardware",
    "e-commerce":                       "E-commerce",
    "ecommerce":                        "E-commerce",
    "gaming":                           "Gaming",
    # These Notion tags DON'T have a CareerForge equivalent:
    # "Tech", "Banking", "Software", "Insurance", "Data Science",
    # "Social Media", "Advertising", "VR"
    # They will be shown as "skipped" in the terminal.
}

# ---------------------------------------------------------------------------
# Quick-lookup sets for O(1) membership tests
# ---------------------------------------------------------------------------
# Checking "value in list" is O(n). These sets give us O(1) lookups,
# which is nicer when we're validating lots of rows from the CSV.

_VALID_CICS_HIRES_SET = set(VALID_CICS_HIRES)
_VALID_SPONSORING_SET = set(VALID_SPONSORING)
_VALID_STATES_SET = set(VALID_STATES)
_VALID_SIZES_SET = set(VALID_SIZES)
_VALID_INDUSTRY_TAGS_SET = set(VALID_INDUSTRY_TAGS)

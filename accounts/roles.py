"""Canonical role names. Import these; never hard-code the strings."""

GROUP_ORGANIZERS = "Organizers"
GROUP_ADVERTISERS = "Advertisers"
GROUP_SCANNERS = "Scanners"

ALL_GROUPS = (GROUP_ORGANIZERS, GROUP_ADVERTISERS, GROUP_SCANNERS)

# Convenience sets used by predicate functions.
ORGANIZER_GROUPS = {GROUP_ORGANIZERS}
SCANNER_GROUPS = {GROUP_SCANNERS}
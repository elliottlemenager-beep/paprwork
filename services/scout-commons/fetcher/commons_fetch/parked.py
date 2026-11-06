"""Is this page a real business, or a parked / for-sale / placeholder page?

Cheap rules only, run on every fetched homepage so junk domains don't reach the
embedding run. Returns a reason string, or None for a page that looks real.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

PARKING_HOSTS = (
    "sedo.com", "sedoparking.com", "bodis.com", "parkingcrew.net", "dan.com", "afternic.com",
    "hugedomains.com", "undeveloped.com", "godaddy.com", "domainmarket.com", "above.com",
    "parklogic.com", "namebright.com", "uniregistry.com", "squadhelp.com", "atom.com", "sav.com",
    "brandbucket.com", "buydomains.com", "dynadot.com", "porkbun.com", "epik.com",
)
_PHRASES = re.compile(
    r"this domain (?:name )?(?:is|may be) for sale|buy this domain|domain (?:is )?for sale|"
    r"make an offer on this domain|this domain has been registered|parked free|domain parking|"
    r"the domain .{0,40} is for sale|inquire about this domain|this page is parked|"
    r"website coming soon|under construction|future home of|default web site page|"
    r"welcome to nginx|apache2 (?:ubuntu|debian) default page|it works!|index of /|"
    r"account (?:has been )?suspended|this site can.t be reached",
    re.I,
)
MIN_TEXT = 200


def reason(final_url: str, text: str, domain: str) -> str | None:
    host = (urlsplit(final_url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in PARKING_HOSTS) and not domain.endswith(host):
        return "parking_host"
    head = text[:3000]
    if _PHRASES.search(head):
        return "parked_text"
    if len(text) < MIN_TEXT:
        return "thin"
    return None

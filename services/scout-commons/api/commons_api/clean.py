"""Serving-time cleanup of precomputed web-graph lookalikes.

The precomputed lists are raw co-citation, so big seeds (stripe.com) come back led by
sites linked from everywhere (facebook.com, googletagmanager.com) and by the seed's own
infrastructure (stripecdn.com, hsforms.net). Three passes, all cheap:

1. Hub filter: drop candidates in HUB_DOMAINS, with more than HUB_INBOUND linking sites,
   on .gov/.edu/.mil/.int, or that look like infrastructure (cdn, dns, static, cookie...).
2. Own-domain collapse: drop candidates whose label contains the seed's brand
   (stripe -> stripecdn.com), or that are almost only linked from sites that also link the
   seed (shared/inbound >= OWN_RATIO and smaller than the seed): hscta.net, stripe.network.
3. Per-seed rescale: divide raw scores by the seed's best surviving raw score, so each
   seed's top lookalike is 1.0 and scores spread out instead of all sitting at ~0.99
   (the old global squash raw/(raw+0.25) saturates for big seeds, whose raw scores reach 100+).
"""

from __future__ import annotations

import re

HUB_INBOUND = 1_000_000
OWN_RATIO = 0.9
HUB_DOMAINS = frozenset("""
facebook.com fb.com instagram.com twitter.com x.com linkedin.com youtube.com youtu.be tiktok.com pinterest.com
reddit.com tumblr.com vimeo.com whatsapp.com wa.me t.me discord.gg discord.com snapchat.com threads.net
google.com googletagmanager.com google-analytics.com googleapis.com gstatic.com googleusercontent.com
googlesyndication.com doubleclick.net goo.gl g.page apple.com microsoft.com live.com office.com amazon.com
amazonaws.com cloudfront.net cloudflare.com cloudflareinsights.com akamaihd.net fastly.net jsdelivr.net
unpkg.com cdnjs.com jquery.com bootstrapcdn.com fontawesome.com typekit.net fonts.com gravatar.com wp.com
wordpress.org wordpress.com w.org gmpg.org schema.org w3.org bit.ly tinyurl.com wikipedia.org wikimedia.org
github.com medium.com cookiebot.com complianz.io onetrust.com cookielaw.org hotjar.com
recaptcha.net hcaptcha.com imgix.net paypalobjects.com
""".split())
_BAD_TLD = re.compile(r"\.(gov|edu|mil|int)(\.[a-z]{2})?$")
# Infrastructure, not companies: kajabi-cdn.com, portfolioboxdns.com, hsappstatic.net, cookiedatabase.org.
_INFRA = re.compile(r"(cdn|dns|static|assets|cookie|usercontent|analytics|pixel|captcha|objects|cloudflare)|^[a-z0-9-]*(api|js)\.[a-z.]+$")


def brand(domain: str) -> str:
    """'stripe.com' -> 'stripe', 'revenue-reimagined.com' -> 'revenuereimagined'."""
    return domain.split(".")[0].replace("-", "")


def is_hub(domain: str, inbound: int | None) -> bool:
    return (domain in HUB_DOMAINS or bool(_BAD_TLD.search(domain)) or (inbound or 0) > HUB_INBOUND
            or bool(_INFRA.search(domain)))


def is_own(seed: str, seed_inbound: int | None, domain: str, shared: int | None, inbound: int | None) -> bool:
    b = brand(seed)
    if len(b) >= 4 and b in domain.replace("-", ""):
        return True
    if inbound and shared and inbound > 50 and inbound < (seed_inbound or 0):
        return shared / inbound >= OWN_RATIO
    return False


def squash(raw: float) -> float:
    return raw / (raw + 0.25) if raw > 0 else 0.0


def clean(seed: str, seed_inbound: int | None, lookalikes: list[dict], inbound: dict[str, int]) -> list[dict]:
    """Filter one seed's precomputed list and rescale. Each item: {entity_id, domain, score(raw), shared_sites}.
    Returns new dicts with score in 0-1 (top = 1.0), order preserved."""
    keep = [x for x in lookalikes
            if not is_hub(x["domain"], inbound.get(x["domain"]))
            and not is_own(seed, seed_inbound, x["domain"], x.get("shared_sites"), inbound.get(x["domain"]))]
    if not keep:
        return []
    top = max(x["score"] for x in keep) or 1.0
    return [{**x, "score": max(0.0, x["score"]) / top} for x in keep]

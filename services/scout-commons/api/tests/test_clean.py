"""Unit tests for serving-time lookalike cleanup (no DB needed)."""

from commons_api.clean import clean

INB = {"stripecdn.com": 108788, "stripe.network": 109448, "facebook.com": 17696362, "adyen.com": 18541,
       "checkout.com": 30000, "kajabi-cdn.com": 26779, "foxordering.com": 2291, "agency.gov": 500}


def ll(*pairs):
    return [{"entity_id": "c_" + d, "domain": d, "score": s, "shared_sites": sh} for d, s, sh in pairs]


def test_drops_hubs_own_domains_infra_and_gov():
    out = clean("stripe.com", 734475, ll(("stripe.network", 108, 109111), ("stripecdn.com", 107, 108473),
                                          ("facebook.com", 39, 525214), ("kajabi-cdn.com", 20, 10963),
                                          ("foxordering.com", 15, 2288), ("agency.gov", 12, 300),
                                          ("adyen.com", 10, 4000), ("checkout.com", 5, 3000)), INB)
    assert [x["domain"] for x in out] == ["adyen.com", "checkout.com"]


def test_rescales_per_seed_top_is_one():
    out = clean("stripe.com", 734475, ll(("adyen.com", 10, 4000), ("checkout.com", 5, 3000)), INB)
    assert out[0]["score"] == 1.0 and out[1]["score"] == 0.5


def test_keeps_real_competitor_with_high_overlap_but_larger_than_seed():
    # a candidate bigger than the seed is never treated as the seed's own infrastructure
    out = clean("adyen.com", 18541, ll(("checkout.com", 3, 17000)), INB)
    assert [x["domain"] for x in out] == ["checkout.com"]


def test_empty():
    assert clean("stripe.com", 1, [], {}) == []


def test_blocks_captcha_and_image_infra_but_keeps_peers():
    out = clean("stripe.com", 734475, ll(("recaptcha.net", 9, 100), ("hcaptcha.com", 8, 100), ("imgix.net", 7, 100),
                                          ("paypalobjects.com", 6, 100), ("cloudflarestream.com", 5, 100),
                                          ("paypal.com", 4, 100), ("klaviyo.com", 3, 100)), {})
    assert [x["domain"] for x in out] == ["paypal.com", "klaviyo.com"]

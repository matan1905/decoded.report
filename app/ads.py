"""Provider-agnostic ad slots.

One entry point, `render(placement)`, returns the markup for a named ad
placement (or an empty string when nothing is configured). The default
network is Google AdSense; a raw `AD_HTML_<PLACEMENT>` override wins for
any placement, so switching to Ezoic, Mediavine or a direct sponsor is a
config change, not a template change.

Placements: top, mid, footer, sidebar, anchor.
"""

from markupsafe import Markup

from . import config

PLACEMENTS = ("top", "mid", "footer", "sidebar", "anchor")


def enabled() -> bool:
    return config.ADS_ENABLED


def configured(placement: str) -> bool:
    """True when this placement has either a raw snippet or an AdSense unit."""
    if config.AD_RAW.get(placement):
        return True
    return bool(config.ADSENSE_CLIENT and config.AD_SLOTS.get(placement))


def client() -> str:
    return config.ADSENSE_CLIENT


def _adsense(slot_id: str) -> str:
    return (
        '<div class="adslot" role="complementary" aria-label="Advertisement">'
        '<span class="adtag">ADVERTISEMENT</span>'
        '<ins class="adsbygoogle" style="display:block;width:100%" '
        f'data-ad-client="{config.ADSENSE_CLIENT}" '
        f'data-ad-slot="{slot_id}" data-ad-format="auto" '
        'data-full-width-responsive="true"></ins>'
        '</div>'
        '<script>(adsbygoogle=window.adsbygoogle||[]).push({});</script>'
    )


def render(placement: str) -> Markup:
    """Markup for one placement, or empty when it is not configured."""
    raw = config.AD_RAW.get(placement)
    if raw:
        return Markup(raw)
    slot_id = config.AD_SLOTS.get(placement)
    if config.ADSENSE_CLIENT and slot_id:
        return Markup(_adsense(slot_id))
    return Markup("")

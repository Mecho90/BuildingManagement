from __future__ import annotations

from django import template
from django.utils.html import escape, linebreaks
from django.utils.safestring import mark_safe

register = template.Library()

# Allow common, safe HTML after Markdown render
ALLOWED_TAGS = [
    "p", "br", "pre", "code", "em", "strong", "a",
    "ul", "ol", "li", "h1", "h2", "h3", "blockquote",
]
ALLOWED_ATTRS = {"a": ["href", "title", "rel", "target"]}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


@register.filter(name="markdownify")
def markdownify(text: object) -> str:
    """
    Render Markdown if 'markdown' and 'bleach' are installed; otherwise fall back
    to escaped text with simple <p>/<br> formatting. (Why: avoid template library
    failing to register when deps are missing.)
    """
    if not text:
        return ""
    s = str(text)

    try:
        import markdown as md
        import bleach
    except Exception:
        # Fallback: escape + convert newlines to <p>/<br>
        return mark_safe(linebreaks(escape(s)))

    html = md.markdown(s, extensions=["extra", "sane_lists", "toc"], output_format="html5")
    cleaned = bleach.clean(
        html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, protocols=ALLOWED_PROTOCOLS, strip=True
    )
    linked = bleach.linkify(
        cleaned, callbacks=[bleach.callbacks.nofollow, bleach.callbacks.target_blank], skip_tags=["pre", "code"]
    )
    return mark_safe(linked)
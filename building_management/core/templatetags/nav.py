from __future__ import annotations

from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def active_nav(
    context,
    *patterns: str,
    css_class: str = "text-emerald-700 dark:text-emerald-200 border-emerald-500 border-b-2",
):
    """
    Return ``css_class`` when the current path begins with any supplied pattern.

    Example:
        class="link-classes {{ active_nav '/buildings/' }}"
    """
    request = context.get("request")
    if not request:
        return ""
    path = request.path or ""
    for pattern in patterns:
        if not pattern:
            continue
        if path.startswith(pattern):
            return css_class
    return ""

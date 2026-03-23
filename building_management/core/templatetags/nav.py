from __future__ import annotations

from urllib.parse import unquote

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
        matches = False
        exact = pattern.endswith("$")
        candidate = pattern[:-1] if exact else pattern
        if candidate == "/":
            matches = path == "/"
        elif exact:
            matches = path == candidate
        else:
            matches = path.startswith(candidate)
        if matches:
            return css_class
    return ""


@register.simple_tag(takes_context=True)
def active_nav_next_prefix(
    context,
    prefix: str,
    *,
    param: str = "next",
    css_class: str = "text-emerald-700 dark:text-emerald-200 border-emerald-500 border-b-2",
):
    """
    Return ``css_class`` when request.GET[param] starts with ``prefix``.
    Useful for keeping parent section highlighting when navigating via `next=`.
    """
    request = context.get("request")
    if not request or not prefix:
        return ""
    next_value = request.GET.get(param, "")
    if not next_value:
        return ""
    next_value = unquote(next_value)
    return css_class if next_value.startswith(prefix) else ""

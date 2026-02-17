from __future__ import annotations

from django import template
from django.http import QueryDict
from django.utils.html import format_html
from django.utils.translation import gettext as _

register = template.Library()


@register.simple_tag(takes_context=True)
def sortable_heading(
    context,
    label: str,
    field: str,
    current_sort: str | None = None,
    *,
    param: str = "sort",
    page_param: str | None = "page",
    align: str = "left",
):
    """
    Build metadata for a sortable table heading.

    Usage:
        {% sortable_heading 'Name' 'name' current_sort=sort as name_sort %}
        <th aria-sort="{{ name_sort.aria_sort|default:'' }}">
          {{ name_sort.html }}
        </th>
    """

    request = context.get("request")
    translated_label = _(label)

    if current_sort is None:
        current_sort = context.get(param)
    if current_sort is None and request:
        current_sort = request.GET.get(param, "")
    current_sort = current_sort or ""

    if current_sort == field:
        icon_state = "asc"
        aria_sort = "ascending"
        next_sort = f"-{field}"
    elif current_sort == f"-{field}":
        icon_state = "desc"
        aria_sort = "descending"
        next_sort = field
    else:
        icon_state = "none"
        aria_sort = ""
        next_sort = field

    query = QueryDict("", mutable=True)
    if request:
        query = request.GET.copy()

    if page_param:
        query.pop(page_param, None)
    query.pop(param, None)
    query[param] = next_sort

    path = request.path if request else ""
    query_string = query.urlencode()
    if query_string:
        url = f"{path}?{query_string}" if path else f"?{query_string}"
    else:
        url = path or "?"

    sr_text = _("Sort descending") if next_sort.startswith("-") else _("Sort ascending")

    align_map = {
        "left": "justify-start text-left",
        "center": "justify-center text-center",
        "right": "justify-end text-right",
    }
    align_class = align_map.get(align, align_map["left"])

    link_classes = f"sortable-link {align_class}"
    if icon_state != "none":
        link_classes += " sortable-link--active"

    icon_html = format_html(
        '<span class="sortable-link__icon" aria-hidden="true" data-state="{}">'
        '<svg viewBox="0 0 12 12" class="sortable-link__chevron sortable-link__chevron--up"><path d="M3 7.5 6 4.5l3 3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" /></svg>'
        '<svg viewBox="0 0 12 12" class="sortable-link__chevron sortable-link__chevron--down"><path d="m3 4.5 3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" /></svg>'
        "</span>",
        icon_state,
    )

    html = format_html(
        '<a class="{classes}" href="{url}"><span>{label}</span>{icon}<span class="sr-only">{sr}</span></a>',
        classes=link_classes,
        url=url,
        label=translated_label,
        icon=icon_html,
        sr=sr_text,
    )

    return {
        "html": html,
        "aria_sort": aria_sort,
        "icon_state": icon_state,
        "is_active": icon_state != "none",
        "align_class": align_class,
        "url": url,
        "label": translated_label,
        "sr_text": sr_text,
    }

from django import template

register = template.Library()


@register.filter
def dict_get(value, key):
    try:
        return value.get(key, "")
    except AttributeError:
        return ""


@register.simple_tag
def subrole_field_name(form, user_id):
    return form.subrole_field_name(user_id)


@register.filter
def choice_value(bound_checkbox):
    try:
        data = getattr(bound_checkbox, "data", None)
        if isinstance(data, dict):
            if "value" in data:
                return str(data["value"])
            if "value" in (data.get("attrs") or {}):
                return str(data["attrs"]["value"])
        value_method = getattr(bound_checkbox, "value", None)
        value = value_method() if callable(value_method) else value_method
        return "" if value in (None, "") else str(value)
    except Exception:
        return ""

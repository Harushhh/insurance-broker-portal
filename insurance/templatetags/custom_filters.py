from django import template
import builtins

register = template.Library()


@register.filter(name="get_attr")
def get_attr(obj, attr):
    return builtins.getattr(obj, attr, None)


@register.filter(name="get_item")
def get_item(dictionary, key):
    if dictionary is None:
        return None
    return dictionary.get(key)


@register.filter(name="display_value")
def display_value(val):
    if val is None:
        return "-"

    if hasattr(val, "name"):
        return val.name

    if hasattr(val, "code"):
        return val.code

    if isinstance(val, float):
        return f"{val:.2f}"

    if isinstance(val, int):
        return str(val)

    if isinstance(val, str):
        cleaned = val.strip()
        return cleaned if cleaned else "-"

    return str(val)


@register.filter(name='has_group')
def has_group(user, group_name):
    return user.is_superuser or user.groups.filter(name=group_name).exists()


@register.filter(name='replace')
def replace(value, arg):
    if arg == '_':
        return str(value).replace('_', ' ')
    return str(value).replace(arg, '')


@register.filter(name='split')
def split(value, delimiter=','):
    if value is None:
        return []
    return [x.strip() for x in str(value).split(delimiter) if x.strip()]

@register.filter(name="life_rate")
def life_rate(val):
    """A Life payout percent for display: up to 4 decimals without trailing zeros ("37.5", "0.075"); "-" when blank."""
    if val is None or val == "":
        return "-"
    try:
        text = f"{float(val):.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(val)
    return text or "0"

from django import template
import builtins

register = template.Library()

@register.filter(name="get_attr")
def get_attr(obj, attr):
    return builtins.getattr(obj, attr, "")

@register.filter(name="display_value")
def display_value(val):
    if val is None:
        return ""
    if hasattr(val, "name"):
        return val.name
    if hasattr(val, "code"):
        return val.code
    return val

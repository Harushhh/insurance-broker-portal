from django import template
import builtins

register = template.Library()

@register.filter(name="get_attr")
def get_attr(obj, attr):
    return builtins.getattr(obj, attr, "")

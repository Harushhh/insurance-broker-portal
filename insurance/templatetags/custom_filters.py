from django import template
import builtins

register = template.Library()

@register.filter(name="get_attr")
def get_attr(obj, attr):
    return builtins.getattr(obj, attr, None)   # None is better than ""

@register.filter(name="get_item")
def get_item(dictionary, key):
    if dictionary is None:
        return None
    return dictionary.get(key)

@register.filter(name="display_value")
def display_value(val):
    if val is None:
        return ""
    if hasattr(val, "name"):
        return val.name
    if hasattr(val, "code"):
        return val.code
    # ✅ NEW: Force all float numbers to display exactly 2 decimal places!
    if isinstance(val, float):
        return f"{val:.2f}"
    return val

@register.filter(name='has_group')
def has_group(user, group_name):
    # Returns True if the user is a superuser OR belongs to the specified group
    return user.is_superuser or user.groups.filter(name=group_name).exists()

@register.filter(name='replace')
def replace(value, arg):
    """
    Custom filter to clean up the group names for the UI.
    If we pass "_", it replaces it with a space.
    Otherwise, it just removes the string (like "Can_View_").
    """
    if arg == '_':
        return str(value).replace('_', ' ')
    return str(value).replace(arg, '')
"""Request middleware for the insurance app."""
from .services.activity import activity_source


class ActivitySourceMiddleware:
    """Remembers the URL path of the request being served, so every Team Activity event recorded while serving it says
    where it came from (`ActivityEvent.source_path`). The path only: the query string can hold a vehicle number."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with activity_source(request.path):
            return self.get_response(request)

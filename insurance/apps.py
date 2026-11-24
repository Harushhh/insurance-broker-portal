from django.apps import AppConfig


class InsuranceConfig(AppConfig):
    name = 'insurance'

    def ready(self):
        # Registers the receiver that copies every audit log entry onto the Team Activity feed.
        from . import signals  # noqa: F401

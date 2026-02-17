from django.shortcuts import render
from .models import RateMaster

def dashboard(request):
    data = RateMaster.objects.all().order_by("-id")  # optional ordering
    field_names = [f.name for f in RateMaster._meta.fields]

    return render(request, "dashboard.html", {
        "data": data,
        "field_names": field_names,
        "total": data.count(),
    })

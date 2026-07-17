from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .models import RateMaster


@login_required
def dashboard(request):
    data = RateMaster.objects.all().order_by("-id")
    field_names = [f.name for f in RateMaster._meta.fields]

    return render(request, "dashboard.html", {
        "data": data,
        "field_names": field_names,
        "total": data.count(),
    })
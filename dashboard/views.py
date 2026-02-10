from django.shortcuts import render
from insurance.models import RateMaster

def dashboard(request):
    data = RateMaster.objects.all()

    return render(request, 'dashboard.html', {
        'data': data
    })

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

if settings.DEBUG:

    fixed_time = timezone.datetime(2026, 6, 10, 5, 7, 12)
    if timezone.is_naive(fixed_time):
        fixed_time = timezone.make_aware(fixed_time)

    def now():
        return fixed_time

    @csrf_exempt
    def minutes_minus_one(request):
        global fixed_time
        fixed_time -= timezone.timedelta(minutes=1)
        return JsonResponse({})

    @csrf_exempt
    def minutes_plus_one(request):
        global fixed_time
        fixed_time += timezone.timedelta(minutes=1)
        return JsonResponse({})

else:

    def now():
        return timezone.now()

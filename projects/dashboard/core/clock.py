from django.conf import settings
from django.utils import timezone

if settings.DEBUG:
    from django.shortcuts import redirect
    from django.utils.http import url_has_allowed_host_and_scheme
    from django.views.decorators.http import require_POST

    fixed_time = timezone.datetime(2026, 6, 10, 5, 7, 12)
    if timezone.is_naive(fixed_time):
        fixed_time = timezone.make_aware(fixed_time)

    def now():
        return fixed_time

    def _back(request):
        # The debug console's buttons are plain forms (CSRF-protected); go
        # back to the page they were on.
        target = request.POST.get("next", "")
        if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
            target = "/"
        return redirect(target)

    @require_POST
    def minutes_minus_one(request):
        global fixed_time
        fixed_time -= timezone.timedelta(minutes=1)
        return _back(request)

    @require_POST
    def minutes_plus_one(request):
        global fixed_time
        fixed_time += timezone.timedelta(minutes=1)
        return _back(request)

else:

    def now():
        return timezone.now()

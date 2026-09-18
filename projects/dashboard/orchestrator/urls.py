from django.http import HttpRequest, HttpResponse
from django.urls import path


def index(request: HttpRequest):
    return HttpResponse("Hello World")


urlpatterns = [
    path("", index, name="index"),
]

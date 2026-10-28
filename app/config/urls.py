from django.contrib import admin
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import path


def healthz(_request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz),
    path("", lambda _r: redirect("admin:index")),
]

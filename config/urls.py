"""
URL configuration for biz-tech-analytics-assessment project.
"""

from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from django.conf import settings


def root_index_view(request):
    """
    Root index / health check view.
    Provides diagnostic status and service discovery for the assessment API.
    """
    return JsonResponse(
        {
            "status": "healthy",
            "service": "Biz-Tech Analytics Full-Stack Developer Assessment API",
            "version": "1.0.0",
            "endpoints": {
                "orders_summary": "/api/orders/summary/",
                "orders_unoptimized": "/api/orders/summary/unoptimized/",
                "benchmark_comparison": "/api/orders/benchmark/",
                "admin": "/admin/",
                "silk_profiler": "/silk/" if getattr(settings, "SILK_PROFILER_ENABLED", True) else "disabled",
            },
        },
        status=200,
    )


urlpatterns = [
    path("", root_index_view, name="root_index"),
    path("admin/", admin.site.urls),
    path("api/orders/", include("orders.urls")),
]

# Conditionally mount Silk profiler if enabled
if getattr(settings, "SILK_PROFILER_ENABLED", True):
    urlpatterns.insert(2, path("silk/", include("silk.urls", namespace="silk")))

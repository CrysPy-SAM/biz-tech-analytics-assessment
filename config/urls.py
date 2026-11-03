"""
URL configuration for biz-tech-analytics-assessment project.
"""

from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("silk/", include("silk.urls", namespace="silk")),
    path("api/orders/", include("orders.urls")),
]

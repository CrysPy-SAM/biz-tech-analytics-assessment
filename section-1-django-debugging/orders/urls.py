from django.urls import path
from .views import (
    OrderSummaryView,
    UnoptimizedOrderSummaryView,
    BenchmarkComparisonView,
)

app_name = "orders"

urlpatterns = [
    # Official assessment endpoint
    path("summary/", OrderSummaryView.as_view(), name="order-summary"),
    # Diagnostic / reproduction endpoint
    path("summary/unoptimized/", UnoptimizedOrderSummaryView.as_view(), name="order-summary-unoptimized"),
    # Live benchmark endpoint
    path("benchmark/", BenchmarkComparisonView.as_view(), name="order-benchmark"),
]


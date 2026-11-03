import time
from typing import Any, Dict
from django.db import connection, reset_queries
from django.db.models import Count, Sum, F, Prefetch
from django.conf import settings
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response

from .models import Order, OrderItem, Customer
from .serializers import (
    UnoptimizedOrderSummarySerializer,
    OptimizedOrderSummarySerializer,
)


def get_default_customer() -> Customer:
    """Helper to retrieve or create a test customer."""
    customer, _ = Customer.objects.get_or_create(
        email="assessment.user@biztech.com",
        defaults={"name": "Alex Mercer", "tier": "VIP"},
    )
    return customer


class UnoptimizedOrderSummaryView(APIView):
    """
    REPRODUCES THE BROKEN PRODUCTION STATE.

    Root Cause Scenario:
    The original view simply queried:
        Order.objects.filter(customer=customer)

    Before the routine deployment, OrderSerializer only output basic fields on Order.
    When a routine deployment introduced customer and order item fields into the serializer/model,
    this view was never updated with select_related() or prefetch_related().

    Query Complexity:
    - 1 query to fetch the N orders.
    - N queries to fetch customer for each order.
    - N queries to execute order.items.count().
    - N queries to execute order.items.all().
    - N * M queries to fetch product for each order item.
    - N * M queries to fetch category for each product.
    Total: O(N * M) queries. For 250 orders, this executes > 2,000 round-trip queries!
    """

    def get(self, request, *args, **kwargs):
        customer_id = request.query_params.get("customer_id")
        if customer_id:
            customer = Customer.objects.filter(id=customer_id).first()
        else:
            customer = get_default_customer()

        if not customer:
            return Response(
                {"error": "Customer not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Naive queryset - triggers N+1 queries during serialization
        orders = Order.objects.filter(customer=customer).order_by("-created_at")

        serializer = UnoptimizedOrderSummarySerializer(orders, many=True)
        return Response(
            {
                "customer": customer.name,
                "total_orders": len(serializer.data),
                "orders": serializer.data,
            },
            status=status.HTTP_200_OK,
        )


class OrderSummaryView(APIView):
    """
    OPTIMIZED PRODUCTION IMPLEMENTATION (/api/orders/summary/).

    Fix Description:
    1. select_related("customer"):
       Performs an SQL INNER JOIN / LEFT OUTER JOIN between 'orders' and 'customers',
       resolving customer details in the primary query.
    2. prefetch_related(Prefetch("items", queryset=OrderItem.objects.select_related("product__category"))):
       Fetches all related order items in a single separate query, while performing a join
       to products and categories (product__category).
    3. annotate():
       Pre-aggregates items_count and total_amount directly at the database engine level,
       avoiding Python loop calculations and redundant COUNT() queries.

    Query Complexity:
    - Exactly 2 queries, constant O(1) database round trips, regardless of order volume.
    """

    def get(self, request, *args, **kwargs):
        customer_id = request.query_params.get("customer_id")
        if customer_id:
            customer = Customer.objects.filter(id=customer_id).first()
        else:
            customer = get_default_customer()

        if not customer:
            return Response(
                {"error": "Customer not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Optimized queryset with eager loading and prefetch
        optimized_items_prefetch = Prefetch(
            "items",
            queryset=OrderItem.objects.select_related("product__category"),
        )

        orders = (
            Order.objects.filter(customer=customer)
            .select_related("customer")
            .prefetch_related(optimized_items_prefetch)
            .annotate(
                annotated_items_count=Count("items", distinct=True),
                annotated_total_amount=Sum(
                    F("items__quantity") * F("items__unit_price")
                ),
            )
            .order_by("-created_at")
        )

        serializer = OptimizedOrderSummarySerializer(orders, many=True)
        return Response(
            {
                "customer": customer.name,
                "total_orders": len(serializer.data),
                "orders": serializer.data,
            },
            status=status.HTTP_200_OK,
        )


class BenchmarkComparisonView(APIView):
    """
    Diagnostic endpoint that executes both implementations side-by-side
    and returns exact query counts, execution durations, and query logs.
    """

    def get(self, request, *args, **kwargs):
        customer_id = request.query_params.get("customer_id")
        if customer_id:
            customer = Customer.objects.filter(id=customer_id).first()
        else:
            customer = get_default_customer()

        if not customer:
            return Response(
                {"error": "Customer not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # 1. Profile Unoptimized View
        settings.DEBUG = True
        reset_queries()
        t0 = time.perf_counter()
        unopt_orders = list(
            Order.objects.filter(customer=customer).order_by("-created_at")
        )
        unopt_data = UnoptimizedOrderSummarySerializer(unopt_orders, many=True).data
        t1 = time.perf_counter()
        unoptimized_duration_ms = round((t1 - t0) * 1000, 2)
        unoptimized_query_count = len(connection.queries)
        unopt_queries_sample = [q["sql"] for q in connection.queries[:5]]

        # 2. Profile Optimized View
        reset_queries()
        t2 = time.perf_counter()
        optimized_items_prefetch = Prefetch(
            "items",
            queryset=OrderItem.objects.select_related("product__category"),
        )
        opt_orders = list(
            Order.objects.filter(customer=customer)
            .select_related("customer")
            .prefetch_related(optimized_items_prefetch)
            .annotate(
                annotated_items_count=Count("items", distinct=True),
                annotated_total_amount=Sum(
                    F("items__quantity") * F("items__unit_price")
                ),
            )
            .order_by("-created_at")
        )
        opt_data = OptimizedOrderSummarySerializer(opt_orders, many=True).data
        t3 = time.perf_counter()
        optimized_duration_ms = round((t3 - t2) * 1000, 2)
        optimized_query_count = len(connection.queries)
        opt_queries_all = [q["sql"] for q in connection.queries]

        query_reduction_pct = (
            round(
                ((unoptimized_query_count - optimized_query_count) / unoptimized_query_count)
                * 100,
                2,
            )
            if unoptimized_query_count > 0
            else 0.0
        )

        return Response(
            {
                "customer": customer.name,
                "order_count": len(opt_data),
                "unoptimized_metrics": {
                    "query_count": unoptimized_query_count,
                    "duration_ms": unoptimized_duration_ms,
                    "sample_queries": unopt_queries_sample,
                },
                "optimized_metrics": {
                    "query_count": optimized_query_count,
                    "duration_ms": optimized_duration_ms,
                    "all_queries": opt_queries_all,
                },
                "comparison": {
                    "query_count_reduction_pct": f"{query_reduction_pct}%",
                    "query_count_delta": unoptimized_query_count - optimized_query_count,
                    "data_parity_verified": unopt_data == opt_data,
                },
            },
            status=status.HTTP_200_OK,
        )


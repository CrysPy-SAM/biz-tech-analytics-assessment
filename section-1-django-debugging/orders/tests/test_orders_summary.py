import pytest
from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.db import connection, reset_queries
from rest_framework import status
from rest_framework.test import APIClient

from orders.models import Customer, Category, Product, Order, OrderItem


class OrderSummaryAPITestCase(TestCase):
    """
    Test suite verifying:
    1. Incident reproduction (N+1 query explosion on unoptimized view).
    2. Resolution verification (constant query count on optimized view).
    3. Exact data parity between original and optimized implementations.
    4. Edge case and error handling.
    """

    def setUp(self):
        self.client = APIClient()

        # Create customer
        self.customer = Customer.objects.create(
            name="Alex Mercer",
            email="alex.mercer@test.com",
            tier="VIP",
        )

        # Create category & products
        self.category_elec = Category.objects.create(
            name="Electronics", slug="electronics"
        )
        self.category_acc = Category.objects.create(
            name="Accessories", slug="accessories"
        )

        self.prod_laptop = Product.objects.create(
            category=self.category_elec,
            name="Pro Laptop 15",
            sku="LAP-001",
            price=Decimal("1200.00"),
            weight_grams=1800,
        )
        self.prod_mouse = Product.objects.create(
            category=self.category_acc,
            name="Wireless Mouse",
            sku="MOU-002",
            price=Decimal("45.00"),
            weight_grams=120,
        )
        self.prod_cable = Product.objects.create(
            category=self.category_acc,
            name="USB-C Fast Cable",
            sku="CAB-003",
            price=Decimal("15.00"),
            weight_grams=50,
        )

        # Create 15 orders for baseline assertion tests
        self.orders = []
        for i in range(1, 16):
            order = Order.objects.create(
                customer=self.customer,
                order_number=f"TEST-ORD-{i:04d}",
                status="COMPLETED",
                shipping_address=f"Suite {i}, Innovation Center",
            )
            OrderItem.objects.create(
                order=order,
                product=self.prod_laptop,
                quantity=1,
                unit_price=self.prod_laptop.price,
            )
            OrderItem.objects.create(
                order=order,
                product=self.prod_mouse,
                quantity=2,
                unit_price=self.prod_mouse.price,
            )
            self.orders.append(order)

    def test_order_summary_success_status_and_schema(self):
        """Verifies that the optimized API endpoint returns 200 OK and expected structure."""
        url = reverse("orders:order-summary")
        response = self.client.get(url, {"customer_id": self.customer.id})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["customer"], "Alex Mercer")
        self.assertEqual(data["total_orders"], 15)
        self.assertEqual(len(data["orders"]), 15)

        first_order = data["orders"][0]
        self.assertIn("order_number", first_order)
        self.assertIn("customer_name", first_order)
        self.assertIn("customer_tier", first_order)
        self.assertIn("items_count", first_order)
        self.assertIn("total_amount", first_order)
        self.assertIn("total_weight_grams", first_order)
        self.assertIn("categories", first_order)
        self.assertEqual(first_order["customer_name"], "Alex Mercer")
        self.assertEqual(first_order["customer_tier"], "VIP")
        self.assertEqual(first_order["items_count"], 2)
        # 1 laptop ($1200) + 2 mice (2 * $45 = $90) = $1290.0
        self.assertEqual(first_order["total_amount"], 1290.0)
        # 1800 + 2*120 = 2040 grams
        self.assertEqual(first_order["total_weight_grams"], 2040)
        self.assertListEqual(sorted(first_order["categories"]), ["Accessories", "Electronics"])

    def test_unoptimized_view_demonstrates_n_plus_one_explosion(self):
        """
        Demonstrates the root cause: The unoptimized view generates O(N) queries.
        For 15 orders, it executes > 15 queries (typically 1 + 15*5 = 76 queries).
        """
        url = reverse("orders:order-summary-unoptimized")

        reset_queries()
        with self.settings(DEBUG=True):
            response = self.client.get(url, {"customer_id": self.customer.id})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        query_count = len(connection.queries)

        # For 15 orders, naive execution requires 1 (orders) + 15*(customer + items count + items + products + categories)
        # Strictly assert that query count is proportional to N (greater than 30 queries for 15 orders)
        self.assertGreater(
            query_count,
            30,
            f"Expected N+1 query explosion (> 30 queries), but got {query_count}",
        )

    def test_optimized_view_has_constant_o_1_query_count(self):
        """
        Verifies the fix: Regardless of order count, the optimized view
        executes at most 3 queries (or 2 queries when using select_related + prefetch_related).
        """
        url = reverse("orders:order-summary")

        reset_queries()
        with self.settings(DEBUG=True):
            response = self.client.get(url, {"customer_id": self.customer.id})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        query_count = len(connection.queries)

        # Assert constant query count: 1 order query (with join) + 1 prefetch items query
        self.assertLessEqual(
            query_count,
            3,
            f"Expected constant O(1) queries (<= 3), but got {query_count}",
        )

    def test_data_parity_between_unoptimized_and_optimized_implementations(self):
        """
        Crucial safety test: Assert that the optimized implementation returns
        the exact same data as the unoptimized version.
        """
        unoptimized_url = reverse("orders:order-summary-unoptimized")
        optimized_url = reverse("orders:order-summary")

        unopt_res = self.client.get(unoptimized_url, {"customer_id": self.customer.id})
        opt_res = self.client.get(optimized_url, {"customer_id": self.customer.id})

        self.assertEqual(unopt_res.status_code, status.HTTP_200_OK)
        self.assertEqual(opt_res.status_code, status.HTTP_200_OK)
        self.assertEqual(unopt_res.json(), opt_res.json())

    def test_customer_not_found(self):
        """Verifies 404 response when a non-existent customer ID is requested."""
        url = reverse("orders:order-summary")
        response = self.client.get(url, {"customer_id": 999999})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_benchmark_endpoint_metrics(self):
        """Verifies that the /api/orders/benchmark/ endpoint provides accurate telemetry."""
        url = reverse("orders:order-benchmark")
        response = self.client.get(url, {"customer_id": self.customer.id})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("unoptimized_metrics", data)
        self.assertIn("optimized_metrics", data)
        self.assertIn("comparison", data)
        self.assertTrue(data["comparison"]["data_parity_verified"])
        self.assertGreater(
            data["unoptimized_metrics"]["query_count"],
            data["optimized_metrics"]["query_count"],
        )

    def test_large_scale_250_orders_performance_comparison(self):
        """
        Simulates the official scenario: User with > 200 orders.
        Proves:
        - Unoptimized implementation generates over 1,000 queries (O(N*M) explosion).
        - Optimized implementation stays at exactly 3 queries (O(1) constant).
        - Identical data returned for all 250 orders.
        """
        # Create a VIP power user with 250 orders
        vip_user = Customer.objects.create(
            name="Power User VIP",
            email="vip.poweruser@biztech.com",
            tier="VIP",
        )
        orders = [
            Order(
                customer=vip_user,
                order_number=f"SCALE-ORD-{idx:05d}",
                status="COMPLETED",
                shipping_address="VIP Penthouse",
            )
            for idx in range(1, 251)
        ]
        created_orders = Order.objects.bulk_create(orders)

        items = []
        for order in created_orders:
            items.append(
                OrderItem(
                    order=order,
                    product=self.prod_laptop,
                    quantity=1,
                    unit_price=self.prod_laptop.price,
                )
            )
            items.append(
                OrderItem(
                    order=order,
                    product=self.prod_mouse,
                    quantity=2,
                    unit_price=self.prod_mouse.price,
                )
            )
        OrderItem.objects.bulk_create(items)

        # 1. Test unoptimized query count for 250 orders
        reset_queries()
        with self.settings(DEBUG=True):
            unopt_res = self.client.get(
                reverse("orders:order-summary-unoptimized"),
                {"customer_id": vip_user.id},
            )
        self.assertEqual(unopt_res.status_code, status.HTTP_200_OK)
        unoptimized_queries = len(connection.queries)

        # 250 orders * (customer + items count + items + 2 products + 2 categories) > 1,000 queries!
        self.assertGreater(
            unoptimized_queries,
            1000,
            f"Expected > 1,000 queries for 250 orders, got {unoptimized_queries}",
        )

        # 2. Test optimized query count for 250 orders
        reset_queries()
        with self.settings(DEBUG=True):
            opt_res = self.client.get(
                reverse("orders:order-summary"),
                {"customer_id": vip_user.id},
            )
        self.assertEqual(opt_res.status_code, status.HTTP_200_OK)
        optimized_queries = len(connection.queries)

        # Stays constant at <= 3 queries regardless of 250 orders!
        self.assertLessEqual(
            optimized_queries,
            3,
            f"Expected <= 3 queries for 250 orders, got {optimized_queries}",
        )

        # 3. Verify 100% data parity
        self.assertEqual(unopt_res.json(), opt_res.json())

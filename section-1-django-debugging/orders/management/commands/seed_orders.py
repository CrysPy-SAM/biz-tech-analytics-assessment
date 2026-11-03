from decimal import Decimal
import random
from django.core.management.base import BaseCommand
from django.db import transaction
from orders.models import Customer, Category, Product, Order, OrderItem


class Command(BaseCommand):
    help = "Seed database with realistic e-commerce data to reproduce and benchmark the N+1 problem."

    def add_arguments(self, parser):
        parser.add_argument(
            "--orders",
            type=int,
            default=250,
            help="Number of orders to create for the test customer (default: 250).",
        )
        parser.add_argument(
            "--email",
            type=str,
            default="assessment.user@biztech.com",
            help="Email address of the test customer.",
        )

    def handle(self, *args, **options):
        orders_count = options["orders"]
        customer_email = options["email"]

        self.stdout.write(f"Seeding database with {orders_count} orders for {customer_email}...")

        with transaction.atomic():
            # 1. Customer
            customer, _ = Customer.objects.get_or_create(
                email=customer_email,
                defaults={"name": "Alex Mercer", "tier": "VIP"},
            )

            # 2. Categories
            categories_data = [
                ("Electronics", "electronics"),
                ("Office Supplies", "office-supplies"),
                ("Hardware", "hardware"),
                ("Networking", "networking"),
                ("Accessories", "accessories"),
            ]
            categories = []
            for name, slug in categories_data:
                cat, _ = Category.objects.get_or_create(name=name, defaults={"slug": slug})
                categories.append(cat)

            # 3. Products
            products = []
            for i in range(1, 26):
                cat = random.choice(categories)
                prod, _ = Product.objects.get_or_create(
                    sku=f"SKU-{cat.slug[:3].upper()}-{i:04d}",
                    defaults={
                        "category": cat,
                        "name": f"Enterprise Item {i} ({cat.name})",
                        "price": Decimal(str(random.randint(25, 450) + 0.99)),
                        "weight_grams": random.randint(150, 2500),
                    },
                )
                products.append(prod)

            # 4. Clear old orders for this customer to ensure clean benchmark
            Order.objects.filter(customer=customer).delete()

            # 5. Bulk create orders and order items
            orders = [
                Order(
                    customer=customer,
                    order_number=f"ORD-2026-{idx:05d}",
                    status=random.choice(["PENDING", "PROCESSING", "COMPLETED"]),
                    shipping_address=f"Tech Hub Avenue #{idx}, Innovation Park",
                )
                for idx in range(1, orders_count + 1)
            ]
            created_orders = Order.objects.bulk_create(orders)

            order_items = []
            for ord_obj in created_orders:
                # 2 to 5 items per order
                num_items = random.randint(2, 5)
                chosen_prods = random.sample(products, num_items)
                for prod in chosen_prods:
                    order_items.append(
                        OrderItem(
                            order=ord_obj,
                            product=prod,
                            quantity=random.randint(1, 4),
                            unit_price=prod.price,
                        )
                    )
            OrderItem.objects.bulk_create(order_items)

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully seeded {len(created_orders)} orders with "
                f"{len(order_items)} order items for customer '{customer.name}'."
            )
        )

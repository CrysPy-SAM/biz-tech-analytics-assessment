from decimal import Decimal
from rest_framework import serializers
from .models import Order, OrderItem, Customer, Product, Category


class UnoptimizedOrderSummarySerializer(serializers.ModelSerializer):
    """
    Serializer used prior to optimization.

    NOTE ON ROUTINE DEPLOYMENT REGRESSION:
    During a routine sprint deployment, new fields were added to the dashboard response
    (customer_name, customer_tier, items_count, total_amount, total_weight_grams, categories).
    Because the original view merely performed `Order.objects.filter(customer_id=...)`
    without `select_related` or `prefetch_related`, every single serialized record triggers
    multiple database roundtrips to resolve foreign keys and reverse relations.
    """

    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_tier = serializers.CharField(source="customer.tier", read_only=True)
    items_count = serializers.IntegerField(source="dynamic_items_count", read_only=True)
    total_amount = serializers.FloatField(source="dynamic_total_amount", read_only=True)
    total_weight_grams = serializers.IntegerField(
        source="dynamic_total_weight_grams", read_only=True
    )
    categories = serializers.ListField(source="dynamic_categories_list", read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "status",
            "customer_name",
            "customer_tier",
            "items_count",
            "total_amount",
            "total_weight_grams",
            "categories",
            "created_at",
        ]


class OptimizedOrderSummarySerializer(serializers.ModelSerializer):
    """
    Optimized serializer designed to work with pre-joined and prefetched querysets.

    It reads pre-computed annotations when available (annotated_items_count, annotated_total_amount)
    or iterates across in-memory prefetched related objects without triggering additional SQL queries.
    """

    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_tier = serializers.CharField(source="customer.tier", read_only=True)
    items_count = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()
    total_weight_grams = serializers.SerializerMethodField()
    categories = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "status",
            "customer_name",
            "customer_tier",
            "items_count",
            "total_amount",
            "total_weight_grams",
            "categories",
            "created_at",
        ]

    def get_items_count(self, obj: Order) -> int:
        if hasattr(obj, "annotated_items_count"):
            return obj.annotated_items_count
        # Uses in-memory prefetched items if available; avoid .count() which triggers SQL
        return len(obj.items.all())

    def get_total_amount(self, obj: Order) -> float:
        if hasattr(obj, "annotated_total_amount") and obj.annotated_total_amount is not None:
            return float(obj.annotated_total_amount)
        # Evaluates from prefetched collection in RAM
        return float(sum(item.quantity * item.unit_price for item in obj.items.all()))

    def get_total_weight_grams(self, obj: Order) -> int:
        # Evaluates from prefetched collection in RAM (product is select_related)
        return sum(item.quantity * item.product.weight_grams for item in obj.items.all())

    def get_categories(self, obj: Order) -> list:
        # Evaluates from prefetched collection in RAM (category is select_related)
        cats = {item.product.category.name for item in obj.items.all()}
        return sorted(list(cats))

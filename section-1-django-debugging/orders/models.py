from django.db import models
from django.utils.translation import gettext_lazy as _


class Customer(models.Model):
    """Customer account placing orders on the platform."""

    TIER_CHOICES = (
        ("STANDARD", "Standard"),
        ("PREMIUM", "Premium"),
        ("VIP", "VIP"),
    )

    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    tier = models.CharField(max_length=20, choices=TIER_CHOICES, default="STANDARD")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "customers"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.email})"


class Category(models.Model):
    """Product catalog category."""

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120, unique=True)

    class Meta:
        db_table = "categories"
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name


class Product(models.Model):
    """Catalog product item."""

    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="products",
    )
    name = models.CharField(max_length=255)
    sku = models.CharField(max_length=64, unique=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    weight_grams = models.PositiveIntegerField(default=100)

    class Meta:
        db_table = "products"

    def __str__(self):
        return f"{self.name} ({self.sku})"


class Order(models.Model):
    """Customer purchase order."""

    STATUS_CHOICES = (
        ("PENDING", "Pending"),
        ("PROCESSING", "Processing"),
        ("COMPLETED", "Completed"),
        ("CANCELLED", "Cancelled"),
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name="orders",
    )
    order_number = models.CharField(max_length=64, unique=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    shipping_address = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "orders"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Order #{self.order_number} - {self.customer.name}"

    # Properties that trigger related database lookups if not prefetched / annotated
    @property
    def dynamic_items_count(self) -> int:
        """Counts items via relation - triggers separate SQL query if not prefetched."""
        return self.items.count()

    @property
    def dynamic_total_amount(self) -> float:
        """Calculates total value - iterates over items."""
        return float(sum(item.quantity * item.unit_price for item in self.items.all()))

    @property
    def dynamic_total_weight_grams(self) -> int:
        """Calculates total weight - traverses item -> product relation."""
        return sum(item.quantity * item.product.weight_grams for item in self.items.all())

    @property
    def dynamic_categories_list(self) -> list:
        """Extracts unique category names - traverses item -> product -> category."""
        return sorted(list({item.product.category.name for item in self.items.all()}))


class OrderItem(models.Model):
    """Line item in an order."""

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="order_items",
    )
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "order_items"

    def __str__(self):
        return f"{self.quantity}x {self.product.name} (Order #{self.order.order_number})"

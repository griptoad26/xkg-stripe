"""
Stripe configuration for XKG products.
"""
from dataclasses import dataclass
from typing import Optional
import os

@dataclass
class Product:
    name: str
    price_cents: int
    description: str
    metadata: dict
    interval: Optional[str] = None  # 'month', 'year', or None for one-time

PRODUCTS = {
    'thick_client': Product(
        name="XKG Thick Client",
        price_cents=4900,
        interval=None,
        description="One-time license for XKG desktop app",
        metadata={'tier': 'thick_client', 'type': 'one-time'}
    ),
    'vps_monthly': Product(
        name="XKG VPS - Monthly",
        price_cents=900,
        interval='month',
        description="Hosted XKG instance - access anywhere",
        metadata={'tier': 'vps', 'type': 'subscription'}
    ),
    'hardware_bundle': Product(
        name="XKG Hardware Bundle",
        price_cents=29900,
        interval=None,  # device is one-time
        description="Pre-configured XKG device",
        metadata={'tier': 'hardware', 'type': 'one-time'}
    ),
}

# Subscription for hardware device (ongoing $5/mo)
HARDWARE_SUBSCRIPTION_PRICE_CENTS = 500  # $5/mo

# Stripe keys (set via environment variables)
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET', '')

def get_stripe_client():
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe
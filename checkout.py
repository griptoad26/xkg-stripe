"""
Stripe Checkout session creation for XKG products.
"""
from typing import Optional
import os

def create_checkout_session(
    product_id: str,
    success_url: str,
    cancel_url: str,
    customer_email: Optional[str] = None,
    metadata: Optional[dict] = None
) -> dict:
    """
    Create a Stripe Checkout session for one-time purchase or subscription.
    
    Args:
        product_id: One of 'thick_client', 'vps_monthly', 'hardware_bundle'
        success_url: URL to redirect after successful payment
        cancel_url: URL to redirect if payment is cancelled
        customer_email: Optional email for pre-fill
        metadata: Optional metadata dict
    
    Returns:
        Stripe checkout session dict with 'id' and 'url'
    """
    from stripe_config import PRODUCTS, HARDWARE_SUBSCRIPTION_PRICE_CENTS, get_stripe_client
    stripe = get_stripe_client()
    
    if product_id not in PRODUCTS:
        raise ValueError(f"Unknown product: {product_id}")
    
    product = PRODUCTS[product_id]
    
    # Determine if this is a subscription or one-time
    if product.interval:
        # Subscription product
        price = stripe.Price.create(
            unit_amount=product.price_cents,
            currency='usd',
            recurring={'interval': product.interval},
            product_data={'name': product.name, 'description': product.description},
        )
        
        session = stripe.checkout.Session.create(
            mode='subscription',
            line_items=[{'price': price.id, 'quantity': 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=customer_email,
            metadata=metadata or {},
        )
    else:
        # One-time payment
        price = stripe.Price.create(
            unit_amount=product.price_cents,
            currency='usd',
            product_data={'name': product.name, 'description': product.description},
        )
        
        session = stripe.checkout.Session.create(
            mode='payment',
            line_items=[{'price': price.id, 'quantity': 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=customer_email,
            metadata=metadata or {},
        )
    
    return {'id': session.id, 'url': session.url}


def create_customer(email: str, name: Optional[str] = None) -> str:
    """Create a Stripe customer and return customer ID."""
    from stripe_config import get_stripe_client
    stripe = get_stripe_client()
    
    customer = stripe.Customer.create(
        email=email,
        name=name or '',
    )
    
    return customer.id


def get_customer_subscriptions(customer_id: str) -> list:
    """Get all active subscriptions for a customer."""
    from stripe_config import get_stripe_client
    stripe = get_stripe_client()
    
    subscriptions = stripe.Subscription.list(
        customer=customer_id,
        status='active',
        limit=10,
    )
    
    return subscriptions.data
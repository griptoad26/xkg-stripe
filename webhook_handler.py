"""
Stripe webhook handler for XKG subscription events.
Handles: checkout.session.completed, customer.subscription.updated, 
         customer.subscription.deleted, invoice.payment_failed
"""
import hmac
import hashlib
from typing import Dict, Any, Optional
import json

class StripeWebhookHandler:
    def __init__(self, webhook_secret: str):
        self.webhook_secret = webhook_secret
    
    def verify_signature(self, payload: bytes, sig_header: str) -> bool:
        """Verify Stripe webhook signature."""
        try:
            elements = dict(x.split('=', 1) for x in sig_header.split(','))
            timestamp = elements.get('t', '')
            expected_sig = elements.get('v1', '')
            
            signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
            computed_sig = hmac.new(
                self.webhook_secret.encode('utf-8'),
                signed_payload.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            return hmac.compare_digest(computed_sig, expected_sig)
        except Exception:
            return False
    
    def handle_event(self, event_type: str, data: Dict[str, Any]) -> str:
        """Route events to appropriate handlers."""
        handlers = {
            'checkout.session.completed': self.handle_checkout_complete,
            'customer.subscription.updated': self.handle_subscription_updated,
            'customer.subscription.deleted': self.handle_subscription_deleted,
            'invoice.payment_failed': self.handle_payment_failed,
        }
        
        handler = handlers.get(event_type, self.handle_unknown)
        return handler(data)
    
    def handle_checkout_complete(self, data: Dict[str, Any]) -> str:
        """Handle successful payment/checkout."""
        session = data.get('object', {})
        customer_id = session.get('customer')
        subscription_id = session.get('subscription')
        metadata = session.get('metadata', {})
        tier = metadata.get('tier', 'unknown')
        
        # Log the purchase
        print(f"[Stripe] Checkout complete: customer={customer_id}, tier={tier}")
        
        # TODO: Activate account, send welcome email
        # For now, just log
        return f"Activated {tier} for customer {customer_id}"
    
    def handle_subscription_updated(self, data: Dict[str, Any]) -> str:
        """Handle subscription changes (upgrade, downgrade, renewal)."""
        sub = data.get('object', {})
        sub_id = sub.get('id')
        status = sub.get('status')
        
        print(f"[Stripe] Subscription updated: {sub_id}, status={status}")
        
        # TODO: Update customer tier, handle status changes
        return f"Updated subscription {sub_id} to {status}"
    
    def handle_subscription_deleted(self, data: Dict[str, Any]) -> str:
        """Handle subscription cancellation."""
        sub = data.get('object', {})
        sub_id = sub.get('id')
        
        print(f"[Stripe] Subscription deleted: {sub_id}")
        
        # TODO: Revoke access, send cancellation email
        return f"Cancelled subscription {sub_id}"
    
    def handle_payment_failed(self, data: Dict[str, Any]) -> str:
        """Handle failed payment."""
        invoice = data.get('object', {})
        customer_id = invoice.get('customer')
        
        print(f"[Stripe] Payment failed for customer: {customer_id}")
        
        # TODO: Send payment failure email, suspend access after retries
        return f"Payment failed for customer {customer_id}"
    
    def handle_unknown(self, data: Dict[str, Any]) -> str:
        """Handle unhandled event types."""
        event_type = data.get('type', 'unknown')
        print(f"[Stripe] Unhandled event: {event_type}")
        return f"Processed unknown event: {event_type}"
"""
Flask app for Stripe webhook handling.
"""
import os
from flask import Flask, request, jsonify
from stripe_config import STRIPE_WEBHOOK_SECRET
from webhook_handler import StripeWebhookHandler

app = Flask(__name__)
handler = StripeWebhookHandler(STRIPE_WEBHOOK_SECRET)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle incoming Stripe webhooks."""
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature', '')
    
    if not handler.verify_signature(payload, sig_header):
        return jsonify({'error': 'Invalid signature'}), 400
    
    event = request.get_json()
    event_type = event.get('type', '')
    
    result = handler.handle_event(event_type, event)
    
    return jsonify({'status': 'ok', 'result': result})


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({'status': 'healthy'})


if __name__ == '__main__':
    app.run(port=5000, debug=True)
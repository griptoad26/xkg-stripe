# XKG Stripe Integration

Stripe payment processing for XKG product tiers.

## Products

| Product | Price | Type |
|---------|-------|------|
| Thick Client | $49 | One-time |
| VPS Monthly | $9/mo | Subscription |
| Hardware Bundle | $299 + $5/mo | One-time + Subscription |

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file:

```env
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

Get these from your [Stripe Dashboard](https://dashboard.stripe.com/apikeys).

### 3. Local webhook testing

Use ngrok to expose your local server:

```bash
ngrok http 5000
```

Copy the ngrok URL (e.g., `https://abc123.ngrok.io`) and set it as your webhook endpoint in the Stripe Dashboard under **Developers > Webhooks**.

Events to listen for:
- `checkout.session.completed`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.payment_failed`

### 4. Run locally

```bash
python app.py
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/webhook` | Stripe webhook receiver |
| GET | `/health` | Health check |

## Production Deployment

Deploy to Railway, Render, or any platform with SSL support:

```bash
# Railway
railway up

# Render
# Connect repo, set env vars, deploy
```

Make sure to update the webhook URL in Stripe Dashboard to your production URL.
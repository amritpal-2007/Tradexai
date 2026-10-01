"""Pure Razorpay TEST subscription validation helpers.

Payment provider data is untrusted until obtained by authenticated server-to-Razorpay
API calls. Checkout callbacks and webhook payloads alone never grant membership.
"""

from datetime import datetime, timezone


ACTIVE_STATES = frozenset({'active'})
REUSABLE_STATES = frozenset({'created', 'authenticated', 'pending', 'halted', 'active'})


def validated_test_upi_subscription(remote, plan_id, verified_upi=False, now=None):
    """Fail closed unless provider confirms an eligible paid UPI monthly test subscription."""
    if not isinstance(remote, dict):
        return False
    now = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    try:
        return (
            remote.get('status') in ACTIVE_STATES
            and remote.get('plan_id') == plan_id
            and verified_upi is True
            and remote.get('payment_method') in (None, '', 'upi')
            and type(remote.get('quantity')) is int and remote['quantity'] == 1
            and type(remote.get('paid_count')) is int and remote['paid_count'] >= 1
            and type(remote.get('current_end')) is int and remote['current_end'] > now
        )
    except (AttributeError, KeyError, TypeError):
        return False


def public_subscription_status(remote, verified_upi=False):
    """Minimal status detail safe to show to the logged-in subscriber."""
    return {
        'state': str(remote.get('status', 'unknown'))[:32],
        'payment_method': 'upi' if verified_upi else 'not verified',
        'paid_cycles': remote.get('paid_count') if type(remote.get('paid_count')) is int else 0,
        'renews_at': remote.get('charge_at') if type(remote.get('charge_at')) is int else None,
        'current_period_end': remote.get('current_end') if type(remote.get('current_end')) is int else None,
        'cancel_scheduled': remote.get('has_scheduled_changes') is True,
    }

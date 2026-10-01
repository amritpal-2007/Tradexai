"""TradeX AI hosted TEST MVP. Fictional educational data. NO LIVE PAYMENTS."""
import hmac
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from market import INSTRUMENTS, price_paise, candles, is_valid_symbol
import stock_directory
from billing import REUSABLE_STATES, validated_test_upi_subscription, public_subscription_status

BASE_DIR = Path(__file__).resolve().parent
IS_RENDER = os.environ.get('RENDER') == 'true'
SECRET = os.environ.get('TRADEX_SECRET_KEY', '')
DB_URL = os.environ.get('DATABASE_URL', '')
if IS_RENDER and (len(SECRET) < 32 or not DB_URL):
    raise RuntimeError('Render deployment requires TRADEX_SECRET_KEY (32+ chars) and DATABASE_URL. Do not use temporary SQLite.')
if DB_URL.startswith('postgres://'):
    DB_URL = 'postgresql://' + DB_URL[len('postgres://'):]
if not DB_URL:
    (BASE_DIR / 'data').mkdir(exist_ok=True)
    DB_URL = 'sqlite:///' + str(BASE_DIR / 'data' / 'tradex_complete.db')
if not SECRET and not IS_RENDER:
    local_secret = BASE_DIR / 'data' / '.dev_secret'
    if local_secret.exists():
        SECRET = local_secret.read_text(encoding='utf8').strip()
    else:
        SECRET = secrets.token_urlsafe(48)
        local_secret.write_text(SECRET, encoding='utf8')

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET or 'LOCAL-DEVELOPMENT-ONLY-' + secrets.token_hex(32),
    SQLALCHEMY_DATABASE_URI=DB_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={'pool_pre_ping': True},
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=IS_RENDER or os.environ.get('TRADEX_HTTPS') == '1',
    MAX_CONTENT_LENGTH=262144,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)
db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60), nullable=False)
    email = db.Column(db.String(254), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    tier = db.Column(db.String(16), nullable=False, default='FREE')
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    cash_paise = db.Column(db.BigInteger, nullable=False, default=10000000)


class Holding(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    symbol = db.Column(db.String(20), primary_key=True)
    quantity = db.Column(db.Integer, nullable=False)


class Trade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    symbol = db.Column(db.String(20), nullable=False)
    side = db.Column(db.String(4), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    price_paise = db.Column(db.Integer, nullable=False)
    placed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    provider_id = db.Column(db.String(80), nullable=False, unique=True)
    status = db.Column(db.String(32), nullable=False, default='created')
    # Razorpay subscription objects do not reliably carry the payment method.
    # Verify payment.method from the authenticated payment fetch after signed checkout.
    auth_payment_id = db.Column(db.String(80), nullable=True)
    upi_verified = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class WebhookEvent(db.Model):
    event_id = db.Column(db.String(180), primary_key=True)
    arrived_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class LoginFailure(db.Model):
    fingerprint = db.Column(db.String(64), primary_key=True)
    failures = db.Column(db.Integer, nullable=False, default=0)
    last_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


with app.app_context():
    db.create_all()  # For a learning MVP; use migrations before a paid production launch.


def utcnow():
    return datetime.now(timezone.utc)


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']


app.jinja_env.globals['csrf_token'] = csrf_token


def test_checkout_ready():
    return (
        os.environ.get('TRADEX_ENABLE_TEST_CHECKOUT') == '1'
        and os.environ.get('RAZORPAY_KEY_ID', '').startswith('rzp_test_')
        and bool(os.environ.get('RAZORPAY_KEY_SECRET'))
        and bool(os.environ.get('RAZORPAY_TEST_PLAN_ID'))
        and bool(os.environ.get('RAZORPAY_WEBHOOK_SECRET'))
    )


def demo_upgrade_ready():
    return not IS_RENDER and os.environ.get('TRADEX_ALLOW_LOCAL_PRO') == '1' and request.remote_addr in ('127.0.0.1','::1') and request.remote_addr in ('127.0.0.1', '::1')


@app.before_request
def before_request():
    g.user = db.session.get(User, session.get('user_id')) if session.get('user_id') else None
    if request.endpoint != 'razorpay_webhook':
        csrf_token()
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.endpoint != 'razorpay_webhook':
        token = request.form.get('csrf_token', '') or request.headers.get('X-CSRF-Token', '')
        if not hmac.compare_digest(token, session['csrf']):
            return jsonify(error='Security token missing. Refresh the page.'), 403


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Cache-Control'] = 'no-store' if request.path.startswith('/api/') or request.endpoint in ('login','register') else 'no-cache'
    return response


def require_login(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not g.user:
            return jsonify(error='Sign in to continue.'), 401
        return func(*args, **kwargs)
    return wrapped


def is_pro(user):
    return user is not None and user.tier in ('LOCAL_PRO', 'PRO_TEST')


def local_research_ready():
    return (not IS_RENDER and os.environ.get('TRADEX_ENABLE_LOCAL_RESEARCH') == '1'
            and request.remote_addr in ('127.0.0.1', '::1'))


@app.get('/')
def index():
    return render_template('index.html', user=g.user, test_checkout=test_checkout_ready(),
                           local_demo=(demo_upgrade_ready() if g.user else False),
                           local_research=local_research_ready())


@app.get('/register')
@app.post('/register')
def register():
    if g.user:
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not (2 <= len(name) <= 60) or not (3 <= len(email) <= 254 and '@' in email and '.' in email.rsplit('@', 1)[-1]) or not (12 <= len(password) <= 128):
            error = 'Enter a valid name/email and a password of at least 12 characters.'
        else:
            try:
                user = User(name=name, email=email, password_hash=generate_password_hash(password), tier='FREE')
                db.session.add(user)
                db.session.commit()
                session.clear()
                session['user_id'] = user.id
                session.permanent = True
                csrf_token()
                return redirect(url_for('index'))
            except IntegrityError:
                db.session.rollback()
                error = 'An account with this email already exists.'
    return render_template('auth.html', mode='register', error=error)


def login_fingerprint(email):
    # Keyed fingerprint, not raw emails or IPs. Basic MVP throttle; use shared rate limiter in production.
    source = email.lower().strip() + '|' + request.remote_addr
    return hmac.new(app.secret_key.encode(), source.encode(), hashlib.sha256).hexdigest()


@app.get('/login')
@app.post('/login')
def login():
    if g.user:
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        key = login_fingerprint(email)
        failure = db.session.get(LoginFailure, key)
        if failure and (utcnow() - failure.last_at.replace(tzinfo=timezone.utc)).total_seconds() < 900 and failure.failures >= 8:
            error = 'Too many attempts. Wait 15 minutes.'
        else:
            user = User.query.filter_by(email=email).first()
            if user and check_password_hash(user.password_hash, password):
                if failure:
                    db.session.delete(failure)
                    db.session.commit()
                session.clear()
                session['user_id'] = user.id
                session.permanent = True
                csrf_token()
                return redirect(url_for('index'))
            if not failure:
                failure = LoginFailure(fingerprint=key, failures=0, last_at=utcnow())
                db.session.add(failure)
            if (utcnow() - failure.last_at.replace(tzinfo=timezone.utc)).total_seconds() >= 900:
                failure.failures = 0
            failure.failures += 1
            failure.last_at = utcnow()
            db.session.commit()
            error = 'Incorrect email or password.'
    return render_template('auth.html', mode='login', error=error)


@app.post('/logout')
@require_login
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.get('/health')
def health():
    return jsonify(status='ok', mode='INDIAN_DIRECTORY_FICTIONAL_CHARTS', checkout='test' if test_checkout_ready() else 'disabled')


@app.get('/api/stocks')
def stocks():
    q = request.args.get('q', '')[:100]
    exchange = request.args.get('exchange', 'ALL').upper()
    try:
        limit = min(30, max(1, int(request.args.get('limit', 20))))
        offset = min(100000, max(0, int(request.args.get('offset', 0))))
    except ValueError:
        return jsonify(error='Invalid pagination'), 400
    result = stock_directory.search(q=q, exchange=exchange, limit=limit, offset=offset)
    result['pricing'] = 'FICTIONAL_SIMULATED_NOT_EXCHANGE_DATA'
    return jsonify(result)


@app.get('/api/market/<symbol>')
def simulated_market(symbol):
    symbol = symbol.strip().upper()
    if not is_valid_symbol(symbol):
        return jsonify(error='Invalid symbol'), 400
    # Accept listed NSE/BSE companies in the cached directory, plus demo examples.
    # An unknown symbol is not silently presented as a verified listed company.
    if symbol not in INSTRUMENTS and stock_directory.find_stock(symbol) is None:
        return jsonify(error='Not in available stock directory. Try searching again.'), 404
    row = stock_directory.find_stock(symbol)
    return jsonify(symbol=symbol, name=row['name'] if row else INSTRUMENTS[symbol][0],
                   exchange=row['exchange'] if row else 'DEMO',
                   candles=candles(symbol), practice_price=price_paise(symbol)/100,
                   data_type='FICTIONAL', notice='Simulated exercise, NOT exchange prices.')


@app.get('/api/research/candles')
def historical_research_candles():
    """Private localhost-only Yahoo research view; never for public market data resale.

    Only displays historical candles. It NEVER changes fictional order execution.
    """
    if not local_research_ready():
        return jsonify(error='Private local research mode is disabled.'), 403
    symbol = request.args.get('symbol', '').strip().upper()
    interval = request.args.get('interval', '5m')
    if not is_valid_symbol(symbol) or symbol in INSTRUMENTS or interval not in ('1m','5m','15m','30m'):
        return jsonify(error='Select an NSE/BSE ticker and a supported interval.'), 400
    if stock_directory.find_stock(symbol) is None:
        return jsonify(error='Ticker not in current directory.'), 404
    try:
        import yfinance as yf
        import pandas as pd
        df = yf.Ticker(symbol).history(period='5d', interval=interval, prepost=False)
        if df.empty: return jsonify(error='No intraday data currently available for this ticker.'), 404
        index = df.index.tz_localize('Asia/Kolkata') if df.index.tz is None else df.index.tz_convert('Asia/Kolkata')
        df = df.copy()
        df.index = index
        df = df[df.index.date == df.index[-1].date()].between_time('09:15','15:30')
        output = []
        for at, row in df.iterrows():
            vals = [row.get(key) for key in ('Open','High','Low','Close')]
            if all(pd.notna(v) for v in vals):
                output.append({'minutes':at.hour*60+at.minute, 'open':round(float(vals[0]),2),
                               'high':round(float(vals[1]),2),'low':round(float(vals[2]),2),
                               'close':round(float(vals[3]),2)})
        if not output: return jsonify(error='No available intraday session candles.'), 404
        return jsonify(candles=output, symbol=symbol, interval=interval,
                       day=str(df.index[-1].date()), source='YAHOO_RESEARCH_DELAYED',
                       notice='LOCAL RESEARCH ONLY: data may be delayed; not licensed for your public paid website.')
    except ImportError:
        return jsonify(error='Install local research libraries using requirements-research.txt.'), 503
    except Exception:
        app.logger.exception('Private research history lookup failed')
        return jsonify(error='Research data unavailable. Try a different ticker or interval.'), 502


@app.get('/api/me')
def me():
    return jsonify(authenticated=bool(g.user), name=g.user.name if g.user else None,
                   tier=g.user.tier if g.user else 'FREE', checkout_enabled=test_checkout_ready())


def portfolio_data(user):
    holdings = Holding.query.filter_by(user_id=user.id).all()
    positions = {h.symbol: h.quantity for h in holdings if h.quantity > 0}
    invested_paise = sum(price_paise(sym) * qty for sym, qty in positions.items())
    trades = Trade.query.filter_by(user_id=user.id).order_by(Trade.id.desc()).limit(30).all()
    return dict(tier=user.tier, cash=user.cash_paise / 100, positions=positions,
                marks={symbol: price_paise(symbol)/100 for symbol in positions},
                invested=invested_paise / 100, equity=(user.cash_paise + invested_paise) / 100,
                history=[dict(side=t.side, symbol=t.symbol, qty=t.quantity, price=t.price_paise / 100,
                              at=t.placed_at.isoformat()) for t in trades])


def verified_pro_for_restricted_action(user):
    """Fail closed for billed TEST members if provider is unavailable or expired."""
    if user.tier == 'LOCAL_PRO':
        return not IS_RENDER and os.environ.get('TRADEX_ALLOW_LOCAL_PRO') == '1' and request.remote_addr in ('127.0.0.1','::1')
    if user.tier != 'PRO_TEST' or not test_checkout_ready():
        return False
    client = razorpay_client()
    get_verified_user_status(client, user.id)
    return user.tier == 'PRO_TEST'


@app.get('/api/portfolio')
@require_login
def portfolio():
    return jsonify(portfolio_data(g.user))


@app.post('/api/order')
@require_login
def order():
    try:
        allowed = verified_pro_for_restricted_action(g.user)
    except Exception:
        db.session.rollback()
        app.logger.exception('Cannot validate membership before a practice trade')
        return jsonify(error='Membership status cannot be verified now. Trading paused safely.'), 503
    if not allowed:
        return jsonify(error='Trading is available only with active verified test Pro.'), 403
    data = request.get_json(silent=True) or {}
    symbol, side, qty = data.get('symbol'), data.get('side'), data.get('quantity')
    if (not is_valid_symbol(symbol) or
        (symbol not in INSTRUMENTS and stock_directory.find_stock(symbol) is None) or
        side not in ('BUY', 'SELL') or type(qty) is not int or not (1 <= qty <= 10000)):
        return jsonify(error='Choose an available listed or demo ticker, side, and quantity (1–10,000).'), 400
    # Row lock is meaningful on PostgreSQL; local SQLite is single-developer only.
    user = db.session.query(User).filter_by(id=g.user.id).with_for_update().one()
    price = price_paise(symbol)
    amount = price * qty
    holding = db.session.get(Holding, (user.id, symbol))
    owned = holding.quantity if holding else 0
    if side == 'BUY':
        if user.cash_paise < amount:
            return jsonify(error='Not enough virtual cash.'), 400
        user.cash_paise -= amount
        if not holding:
            holding = Holding(user_id=user.id, symbol=symbol, quantity=0)
            db.session.add(holding)
        holding.quantity += qty
    else:
        if owned < qty:
            return jsonify(error='Insufficient fictional holdings.'), 400
        user.cash_paise += amount
        holding.quantity -= qty
    db.session.add(Trade(user_id=user.id, symbol=symbol, side=side, quantity=qty, price_paise=price))
    db.session.commit()
    return jsonify(ok=True, message=f'{side} simulated at ₹{price / 100:.2f}. No real trade.', portfolio=portfolio_data(user))


@app.post('/api/reset')
@require_login
def reset():
    try:
        allowed = verified_pro_for_restricted_action(g.user)
    except Exception:
        db.session.rollback()
        return jsonify(error='Membership temporarily unavailable. Please retry.'), 503
    if not allowed:
        return jsonify(error='Portfolio reset requires verified test Pro.'), 403
    user = db.session.query(User).filter_by(id=g.user.id).with_for_update().one()
    Holding.query.filter_by(user_id=user.id).delete()
    Trade.query.filter_by(user_id=user.id).delete()
    user.cash_paise = 10000000
    db.session.commit()
    return jsonify(ok=True, portfolio=portfolio_data(user))


@app.post('/api/dev/pro')
@require_login
def local_pro():
    if not demo_upgrade_ready():
        return jsonify(error='Local development only. Disabled on public hosting.'), 403
    g.user.tier = 'LOCAL_PRO'
    db.session.commit()
    return jsonify(ok=True, tier='LOCAL_PRO', notice='Local free Pro test. No paid subscription.')


def razorpay_client():
    import razorpay  # Installed from requirements.txt; TEST credentials only.
    return razorpay.Client(auth=(os.environ['RAZORPAY_KEY_ID'], os.environ['RAZORPAY_KEY_SECRET']))


def verified_test_plan(client):
    plan = client.plan.fetch(os.environ['RAZORPAY_TEST_PLAN_ID'])
    item = plan.get('item') or {}
    if (plan.get('period') != 'monthly' or plan.get('interval') != 1 or
            item.get('amount') != 99900 or item.get('currency') != 'INR'):
        raise ValueError('Your Razorpay TEST plan must be ₹999 INR, monthly.')


def account_test_subscriptions(user_id):
    return Subscription.query.filter_by(user_id=user_id).order_by(Subscription.id.desc()).all()


def apply_verified_remote(record, remote):
    """Use only authenticated Razorpay API fetch results, NEVER a browser callback."""
    valid = validated_test_upi_subscription(remote, os.environ['RAZORPAY_TEST_PLAN_ID'], record.upi_verified)
    record.status = 'active' if valid else ('active_unverified' if remote.get('status') == 'active' else str(remote.get('status', 'unknown'))[:32])
    user = db.session.get(User, record.user_id)
    if user.tier != 'LOCAL_PRO':
        # Other validated active subscriptions must not be downgraded by stale
        # events from a cancelled/failed second subscription.
        others = any(s.id != record.id and s.status == 'active'
                     for s in account_test_subscriptions(user.id))
        user.tier = 'PRO_TEST' if valid or others else 'FREE'
    return valid


def fetch_and_sync(client, record, commit=True):
    remote = client.subscription.fetch(record.provider_id)
    if not isinstance(remote, dict) or remote.get('id') != record.provider_id:
        raise ValueError('Razorpay returned an unexpected subscription.')
    apply_verified_remote(record, remote)
    if commit:
        db.session.commit()
    return remote


def get_verified_user_status(client, user_id):
    """Fetch ALL linked mandates before authorizing any test Pro privileges."""
    subscriptions = account_test_subscriptions(user_id)
    remotes = []
    for record in subscriptions:
        remote = client.subscription.fetch(record.provider_id)
        if not isinstance(remote, dict) or remote.get('id') != record.provider_id:
            raise ValueError('Unexpected provider subscription id')
        remotes.append((record, remote))
    for record, remote in remotes:
        record.status = ('active' if validated_test_upi_subscription(remote, os.environ['RAZORPAY_TEST_PLAN_ID'], record.upi_verified)
                         else 'active_unverified' if remote.get('status') == 'active'
                         else str(remote.get('status', 'unknown'))[:32])
    user = db.session.get(User, user_id)
    if user.tier != 'LOCAL_PRO':
        user.tier = 'PRO_TEST' if any(r.status == 'active' for r in subscriptions) else 'FREE'
    db.session.commit()
    return remotes


@app.post('/api/test/subscribe')
@require_login
def create_test_subscription():
    if not test_checkout_ready():
        return jsonify(error='Razorpay TEST checkout is not configured.'), 403
    if is_pro(g.user):
        return jsonify(error='Pro access is already active on this account.'), 409
    try:
        client = razorpay_client()
        verified_test_plan(client)  # Refuse wrong amount or billing frequency.
        # Reuse any pending authorization instead of creating duplicate mandates
        # when a customer clicks twice or closes checkout.
        # Serialize subscription setup per account on PostgreSQL.
        db.session.query(User).filter_by(id=g.user.id).with_for_update().one()
        for old in account_test_subscriptions(g.user.id):
            remote = fetch_and_sync(client, old, commit=False)
            if remote.get('status') in REUSABLE_STATES:
                if validated_test_upi_subscription(remote, os.environ['RAZORPAY_TEST_PLAN_ID'], old.upi_verified):
                    db.session.commit()
                    return jsonify(error='UPI AutoPay Test Pro is already active.'), 409
                if remote.get('status') == 'halted':
                    db.session.commit()
                    return jsonify(error='A previous TEST mandate is halted. Resolve or cancel it before restarting.'), 409
                if remote.get('plan_id') == os.environ['RAZORPAY_TEST_PLAN_ID']:
                    db.session.commit()
                    return jsonify(key_id=os.environ['RAZORPAY_KEY_ID'],
                                   subscription_id=old.provider_id, recurring_months=12,
                                   message='Resume your existing ₹999/month UPI TEST authorization. No real charge.')
        created = client.subscription.create({
            'plan_id': os.environ['RAZORPAY_TEST_PLAN_ID'],
            'total_count': 12,  # 12 monthly billing cycles: the test mandate is NOT indefinite.
            'quantity': 1,
            'customer_notify': True,
            'notes': {'purpose': 'TRADEX FICTIONAL EDUCATION - TEST', 'test_user_id': str(g.user.id)}
        })
        if not isinstance(created, dict) or not created.get('id', '').startswith('sub_'):
            raise ValueError('Razorpay did not create a valid test subscription.')
        db.session.add(Subscription(user_id=g.user.id, provider_id=created['id']))
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception('Cannot start TEST UPI AutoPay authorization')
        return jsonify(error='TEST subscription could not be created. Check Razorpay test keys, plan and UPI AutoPay eligibility.'), 502
    return jsonify(key_id=os.environ['RAZORPAY_KEY_ID'], subscription_id=created['id'],
                   recurring_months=12, message='₹999 each month for 12 test billing cycles. No real money is collected.')


@app.post('/api/test/verify')
@require_login
def verify_test_checkout():
    if not test_checkout_ready():
        return jsonify(error='Test checkout is disabled.'), 403
    data = request.get_json(silent=True) or {}
    sub_id = data.get('razorpay_subscription_id', '')
    link = Subscription.query.filter_by(provider_id=sub_id, user_id=g.user.id).first()
    if not link:
        return jsonify(error='Subscription does not belong to your account.'), 403
    try:
        client = razorpay_client()
        pay_id = data.get('razorpay_payment_id', '')
        if not isinstance(pay_id, str) or not pay_id.startswith('pay_') or len(pay_id) > 80:
            return jsonify(error='Invalid payment reference.'), 400
        client.utility.verify_subscription_payment_signature({
            'razorpay_subscription_id': link.provider_id,
            'razorpay_payment_id': pay_id,
            'razorpay_signature': data.get('razorpay_signature', '')
        })
        # The subscription fetch commonly omits payment_method. Use the
        # authenticated PAYMENT endpoint and never trust checkout's method field.
        payment = client.payment.fetch(pay_id)
        if not isinstance(payment, dict) or payment.get('id') != pay_id or payment.get('method') != 'upi':
            return jsonify(error='The verified authorization is not UPI. Pro remains locked.'), 400
        if payment.get('subscription_id') and payment['subscription_id'] != link.provider_id:
            return jsonify(error='Authorization belongs to another subscription.'), 400
        if payment.get('status') not in ('authorized', 'captured'):
            return jsonify(error='Authorization not yet successful.'), 400
        remote = client.subscription.fetch(link.provider_id)
        if remote.get('id') != link.provider_id or remote.get('plan_id') != os.environ['RAZORPAY_TEST_PLAN_ID']:
            return jsonify(error='Unexpected plan or subscription.'), 400
        link.auth_payment_id = pay_id
        link.upi_verified = True
        apply_verified_remote(link, remote)
        db.session.commit()
    except Exception:
        return jsonify(error='TEST checkout signature could not be verified.'), 400
    # Signature + provider payment method alone never unlock Pro; provider active paid cycle required.
    return jsonify(ok=True, message='UPI test authorization verified. Refresh status after Razorpay activates the paid subscription.')


@app.get('/api/test/subscription-status')
@require_login
def get_test_subscription_status():
    if not test_checkout_ready():
        return jsonify(enabled=False, message='TEST UPI AutoPay not configured.'), 200
    subscriptions = account_test_subscriptions(g.user.id)
    if not subscriptions:
        return jsonify(enabled=True, state='none', pro=is_pro(g.user))
    try:
        client = razorpay_client()
        remotes = get_verified_user_status(client, g.user.id)
        preferred_record, preferred = next(((record, remote) for record, remote in remotes if record.status == 'active'), remotes[0])
        return jsonify(enabled=True, pro=is_pro(g.user), **public_subscription_status(preferred, preferred_record.upi_verified))
    except Exception:
        db.session.rollback()
        app.logger.exception('TEST status refresh failed')
        return jsonify(error='Could not refresh Razorpay TEST status. Try again.'), 503


@app.post('/api/test/cancel')
@require_login
def cancel_test_subscription():
    if not test_checkout_ready():
        return jsonify(error='Razorpay TEST checkout is not configured.'), 403
    subscriptions = account_test_subscriptions(g.user.id)
    if not subscriptions:
        return jsonify(error='No TEST subscription belongs to this account.'), 404
    try:
        client = razorpay_client()
        cancellable = []
        for record in subscriptions:
            remote = client.subscription.fetch(record.provider_id)
            if remote.get('id') == record.provider_id and remote.get('status') in REUSABLE_STATES:
                cancellable.append((record, remote))
        # Cancel the paid active mandate first; a newer abandoned checkout
        # must not hide the real recurring charge from the account holder.
        active = next(((r, v) for r, v in cancellable if validated_test_upi_subscription(
            v, os.environ['RAZORPAY_TEST_PLAN_ID'], r.upi_verified)), None)
        if not active:
            active = cancellable[0] if cancellable else None
        if not active:
            return jsonify(error='There is no cancellable test mandate.'), 409
        record, remote = active
        if remote.get('has_scheduled_changes'):
            return jsonify(ok=True, message='A change or cancellation is already scheduled. See your Razorpay test dashboard.')
        # End-of-cycle cancellation preserves a paid, current billing cycle.
        # A never-activated mandate has no billing period, so cancel immediately.
        cycle_end = remote.get('status') == 'active' and remote.get('current_end') is not None
        cancelled = client.subscription.cancel(record.provider_id,
                                                 {'cancel_at_cycle_end': cycle_end})
        if cancelled.get('id') != record.provider_id:
            raise ValueError('Unexpected cancellation response')
        # Refresh all linked mandates to avoid stale Pro state.
        get_verified_user_status(client, g.user.id)
        return jsonify(ok=True, message=(
            'TEST UPI AutoPay cancellation scheduled at current cycle end. Pro stays until then.'
            if cycle_end else 'TEST UPI AutoPay mandate cancelled.'))
    except Exception:
        db.session.rollback()
        app.logger.exception('TEST cancellation failed')
        return jsonify(error='Cancellation could not be confirmed. Check your Razorpay TEST dashboard.'), 503


@app.post('/api/test/webhook')
def razorpay_webhook():
    if not test_checkout_ready():
        return jsonify(error='TEST webhooks are disabled.'), 403
    raw = request.get_data()
    signature = request.headers.get('X-Razorpay-Signature', '')
    expected = hmac.new(os.environ['RAZORPAY_WEBHOOK_SECRET'].encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return jsonify(error='Invalid webhook signature.'), 401
    try:
        event = json.loads(raw)
        event_id = request.headers.get('X-Razorpay-Event-Id', '')
        if not (0 < len(event_id) <= 180):
            return jsonify(error='Missing event identifier.'), 400
        if db.session.get(WebhookEvent, event_id):
            return jsonify(ok=True, duplicate=True)
        entity = event['payload']['subscription']['entity']
        record = Subscription.query.filter_by(provider_id=entity['id']).first()
        if not record:
            return jsonify(ok=True, ignored='Unknown test subscription.')
        # Authenticated provider fetch avoids trusting webhook payloads and
        # prevents reordered events from granting old or failed subscriptions.
        client = razorpay_client()
        get_verified_user_status(client, record.user_id)
        db.session.add(WebhookEvent(event_id=event_id))
        db.session.commit()
        return jsonify(ok=True)
    except Exception:
        db.session.rollback()
        app.logger.exception('TEST webhook processing failed')
        return jsonify(error='Retry test webhook later.'), 503


if __name__ == '__main__':
    app.run(debug=not IS_RENDER, port=int(os.environ.get('PORT', '5000')))

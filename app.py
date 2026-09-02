from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import sqlite3, os, heapq, re, html
from datetime import datetime
from functools import wraps

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from twilio.rest import Client
except ImportError:
    Client = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'queue.db')
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key')

SERVICES = ['General', 'Billing', 'Support', 'Registration']
PRIORITIES = {'Emergency': 1, 'Priority': 2, 'Normal': 3}
AVG_SERVICE_MINUTES = 4

TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        created_at TEXT NOT NULL,
        phone TEXT,
        notification_pref TEXT NOT NULL DEFAULT 'inapp'
    );
    CREATE TABLE IF NOT EXISTS tokens(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL,
        service TEXT NOT NULL,
        priority INTEGER NOT NULL,
        priority_name TEXT NOT NULL,
        user_id INTEGER,
        status TEXT NOT NULL DEFAULT 'waiting',
        counter_id INTEGER,
        created_at TEXT NOT NULL,
        called_at TEXT,
        completed_at TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS counters(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        current_token_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS queue_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token_id INTEGER,
        wait_minutes REAL,
        service_minutes REAL,
        completed_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS notification_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token_id INTEGER NOT NULL,
        event TEXT NOT NULL,
        channel TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        UNIQUE(token_id,event,channel)
    );
    ''')

    user_columns = {row['name'] for row in conn.execute('PRAGMA table_info(users)').fetchall()}
    if 'phone' not in user_columns:
        conn.execute('ALTER TABLE users ADD COLUMN phone TEXT')
    if 'notification_pref' not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN notification_pref TEXT NOT NULL DEFAULT 'inapp'")

    if conn.execute('SELECT COUNT(*) FROM counters').fetchone()[0] == 0:
        conn.executemany('INSERT INTO counters(name,active) VALUES (?,1)',
                         [('Counter 1',), ('Counter 2',), ('Counter 3',)])

    if conn.execute('SELECT COUNT(*) FROM users WHERE role="admin"').fetchone()[0] == 0:
        conn.execute(
            'INSERT INTO users(name,email,password,role,created_at,phone,notification_pref) VALUES (?,?,?,?,?,?,?)',
            ('System Admin', 'admin@queue.com', 'admin123', 'admin', datetime.now().isoformat(), None, 'inapp')
        )

    conn.commit()
    conn.close()


def normalize_phone(phone):
    phone = re.sub(r'[^0-9+]', '', (phone or '').strip())
    if phone.startswith('+'):
        return phone if re.fullmatch(r'\+[1-9]\d{7,14}', phone) else None
    if re.fullmatch(r'[6-9]\d{9}', phone):
        return '+91' + phone
    return None


def twilio_ready():
    return bool(Client and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER)


def send_sms(phone, message):
    """Send a real SMS through Twilio. Returns True only after Twilio accepts it."""
    if not phone or not twilio_ready():
        return False
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=message, from_=TWILIO_PHONE_NUMBER, to=phone)
        return True
    except Exception as exc:
        app.logger.warning('SMS notification failed: %s', exc)
        return False


def notify_user(conn, token_row, event, message):
    user = conn.execute(
        'SELECT phone,notification_pref FROM users WHERE id=?',
        (token_row['user_id'],)
    ).fetchone()
    if not user:
        return False

    # Only SMS is supported in this version; in-app notifications remain on the website.
    if (user['notification_pref'] or 'inapp') != 'sms':
        return False
    if not user['phone'] or not twilio_ready():
        return False

    already = conn.execute(
        'SELECT 1 FROM notification_log WHERE token_id=? AND event=? AND channel=?',
        (token_row['id'], event, 'sms')
    ).fetchone()
    if already:
        return True

    if send_sms(user['phone'], message):
        conn.execute(
            'INSERT OR IGNORE INTO notification_log(token_id,event,channel,sent_at) VALUES (?,?,?,?)',
            (token_row['id'], event, 'sms', datetime.now().isoformat())
        )
        conn.commit()
        return True
    return False


def login_required(role=None):
    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('auth'))
            if role and session.get('role') != role:
                return redirect(url_for('admin_dashboard' if session.get('role') == 'admin' else 'user_dashboard'))
            return view(*args, **kwargs)
        return wrapped
    return deco


def next_token_number(conn, service):
    prefix = service[0].upper()
    row = conn.execute(
        'SELECT token FROM tokens WHERE service=? ORDER BY id DESC LIMIT 1',
        (service,)
    ).fetchone()
    if not row:
        return f'{prefix}001'
    try:
        return f'{prefix}{int(row["token"][1:]) + 1:03d}'
    except Exception:
        count = conn.execute('SELECT COUNT(*) FROM tokens WHERE service=?', (service,)).fetchone()[0]
        return f'{prefix}{count + 1:03d}'


def build_waiting_heap(conn, exclude_user_id=None):
    heap = []
    rows = conn.execute('SELECT * FROM tokens WHERE status="waiting" ORDER BY id ASC').fetchall()
    for row in rows:
        if exclude_user_id is not None and row['user_id'] == exclude_user_id:
            continue
        heapq.heappush(heap, (row['priority'], row['id'], row))
    return heap


def waiting_before(conn, token_row):
    heap = build_waiting_heap(conn)
    ahead = 0
    while heap:
        _, _, row = heapq.heappop(heap)
        if row['id'] == token_row['id']:
            break
        ahead += 1
    return ahead


def estimate_wait(ahead, active_counters):
    return max(0, int(round((ahead * AVG_SERVICE_MINUTES) / max(active_counters, 1))))


@app.route('/')
def index():
    if session.get('role') == 'admin':
        return redirect(url_for('admin_dashboard'))
    if session.get('role') == 'user':
        return redirect(url_for('user_dashboard'))
    return render_template('index.html')


@app.route('/auth', methods=['GET', 'POST'])
def auth():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'login':
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')
            conn = get_db()
            user = conn.execute(
                'SELECT * FROM users WHERE email=? AND password=?', (email, password)
            ).fetchone()
            conn.close()
            if not user:
                flash('Incorrect email or password.', 'error')
            else:
                session.update(
                    user_id=user['id'], name=user['name'], role=user['role'],
                    email=user['email'], phone=user['phone'],
                    notification_pref=user['notification_pref']
                )
                return redirect(url_for('admin_dashboard' if user['role'] == 'admin' else 'user_dashboard'))

        elif action == 'register':
            name = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')
            role = request.form.get('role', 'user')
            code = request.form.get('admin_code', '')
            pref = request.form.get('notification_pref', 'inapp')
            phone_raw = request.form.get('phone', '')
            phone = normalize_phone(phone_raw) if phone_raw else None
            sms_consent = request.form.get('sms_consent') == 'yes'

            if not name or not email or not password:
                flash('Please fill all required fields.', 'error')
            elif role not in ('user', 'admin'):
                flash('Invalid account type.', 'error')
            elif role == 'admin' and code != 'ADMIN2026':
                flash('Admin registration requires the admin access code.', 'error')
            elif role == 'user' and not phone:
                flash('Please enter a valid 10-digit Indian mobile number.', 'error')
            elif role == 'user' and pref == 'sms' and not sms_consent:
                flash('Please accept SMS notification consent to enable SMS.', 'error')
            else:
                if role == 'admin':
                    phone, pref = None, 'inapp'
                conn = get_db()
                try:
                    conn.execute(
                        'INSERT INTO users(name,email,password,role,created_at,phone,notification_pref) VALUES (?,?,?,?,?,?,?)',
                        (name, email, password, role, datetime.now().isoformat(), phone, pref)
                    )
                    conn.commit()
                    flash('Registration successful. You can now log in.', 'success')
                except sqlite3.IntegrityError:
                    flash('An account with that email already exists.', 'error')
                finally:
                    conn.close()

    return render_template('auth.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/user')
@login_required('user')
def user_dashboard():
    conn = get_db()
    tokens = conn.execute(
        'SELECT * FROM tokens WHERE user_id=? ORDER BY id DESC', (session['user_id'],)
    ).fetchall()
    active = conn.execute('SELECT COUNT(*) FROM counters WHERE active=1').fetchone()[0]
    current = conn.execute('''
        SELECT t.*, c.name counter_name
        FROM tokens t LEFT JOIN counters c ON c.id=t.counter_id
        WHERE t.status='serving' ORDER BY t.called_at LIMIT 1
    ''').fetchone()
    waiting = conn.execute('SELECT COUNT(*) FROM tokens WHERE status="waiting"').fetchone()[0]

    data = []
    notifications = []
    for t in tokens:
        ahead = waiting_before(conn, t) if t['status'] == 'waiting' else 0
        item = dict(t)
        item['ahead'] = ahead
        item['wait'] = estimate_wait(ahead, active)
        data.append(item)
        if t['status'] == 'serving':
            notifications.append({
                'kind': 'success',
                'text': f'Your token {t["token"]} is being served now. Please proceed to the counter.'
            })
        elif t['status'] == 'waiting' and ahead <= 2:
            notifications.append({
                'kind': 'info',
                'text': f'Your token {t["token"]} is coming soon. {ahead} customer(s) are ahead of you.'
            })

    phone = session.get('phone')
    pref = session.get('notification_pref', 'inapp')
    sms_ready = bool(phone and twilio_ready() and pref == 'sms')
    conn.close()

    return render_template(
        'user.html', tokens=data, services=SERVICES, current=current,
        waiting_count=waiting, active_counters=active, notifications=notifications,
        sms_ready=sms_ready
    )


@app.post('/user/token')
@login_required('user')
def create_token():
    service = request.form.get('service', 'General')
    priority_name = request.form.get('priority_name', 'Normal')
    if service not in SERVICES or priority_name not in PRIORITIES:
        flash('Invalid service or priority.', 'error')
        return redirect(url_for('user_dashboard'))

    conn = get_db()
    token = next_token_number(conn, service)
    conn.execute(
        'INSERT INTO tokens(token,service,priority,priority_name,user_id,created_at) VALUES (?,?,?,?,?,?)',
        (token, service, PRIORITIES[priority_name], priority_name, session['user_id'], datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    flash(f'Token {token} generated successfully!', 'success')
    return redirect(url_for('user_dashboard'))


@app.post('/user/cancel/<int:token_id>')
@login_required('user')
def cancel_token(token_id):
    conn = get_db()
    conn.execute(
        'UPDATE tokens SET status="cancelled" WHERE id=? AND user_id=? AND status="waiting"',
        (token_id, session['user_id'])
    )
    conn.commit()
    conn.close()
    return redirect(url_for('user_dashboard'))


@app.route('/admin')
@login_required('admin')
def admin_dashboard():
    conn = get_db()
    counters = conn.execute('SELECT * FROM counters ORDER BY id').fetchall()
    queue = conn.execute('''
        SELECT t.*, u.name user_name, u.phone user_phone, c.name counter_name
        FROM tokens t
        LEFT JOIN users u ON u.id=t.user_id
        LEFT JOIN counters c ON c.id=t.counter_id
        WHERE t.status IN ('waiting','serving')
        ORDER BY t.priority,t.id
    ''').fetchall()
    total = conn.execute('SELECT COUNT(*) FROM tokens').fetchone()[0]
    waiting = conn.execute('SELECT COUNT(*) FROM tokens WHERE status="waiting"').fetchone()[0]
    completed = conn.execute('SELECT COUNT(*) FROM tokens WHERE status="completed"').fetchone()[0]
    cancelled = conn.execute('SELECT COUNT(*) FROM tokens WHERE status="cancelled"').fetchone()[0]
    avg_wait = conn.execute('SELECT AVG(wait_minutes) FROM queue_history').fetchone()[0] or 0
    avg_service = conn.execute('SELECT AVG(service_minutes) FROM queue_history').fetchone()[0] or AVG_SERVICE_MINUTES
    sms_configured = twilio_ready()
    conn.close()
    return render_template(
        'admin.html', counters=counters, queue=queue, total=total, waiting=waiting,
        completed=completed, cancelled=cancelled, avg_wait=round(avg_wait, 1),
        avg_service=round(avg_service, 1), sms_configured=sms_configured
    )


def choose_next(conn):
    heap = build_waiting_heap(conn)
    return heapq.heappop(heap)[2] if heap else None


@app.post('/admin/call-next')
@login_required('admin')
def call_next():
    conn = get_db()
    counter_id = request.form.get('counter_id', type=int)
    counter = (
        conn.execute('SELECT * FROM counters WHERE id=? AND active=1', (counter_id,)).fetchone()
        if counter_id else
        conn.execute('SELECT * FROM counters WHERE active=1 AND current_token_id IS NULL ORDER BY id LIMIT 1').fetchone()
    )
    if not counter:
        flash('No free active counter is available.', 'error')
        conn.close()
        return redirect(url_for('admin_dashboard'))
    if counter['current_token_id']:
        flash(f'{counter["name"]} is already serving a customer.', 'error')
        conn.close()
        return redirect(url_for('admin_dashboard'))

    token = choose_next(conn)
    if not token:
        flash('No customers are waiting.', 'error')
        conn.close()
        return redirect(url_for('admin_dashboard'))

    now = datetime.now()
    conn.execute(
        'UPDATE tokens SET status="serving",counter_id=?,called_at=? WHERE id=?',
        (counter['id'], now.isoformat(), token['id'])
    )
    conn.execute('UPDATE counters SET current_token_id=? WHERE id=?', (token['id'], counter['id']))
    conn.commit()

    sent = notify_user(
        conn, token, 'turn',
        f'Your SmartQueue token {token["token"]} is now being served at {counter["name"]}. Please proceed to the counter.'
    )
    conn.close()
    if sent:
        flash(f'Now serving {token["token"]}. SMS sent to the customer.', 'success')
    else:
        flash(f'Now serving {token["token"]}. In-app notification updated; SMS not sent.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/complete/<int:token_id>')
@login_required('admin')
def complete_token(token_id):
    conn = get_db()
    token = conn.execute('SELECT * FROM tokens WHERE id=?', (token_id,)).fetchone()
    if token and token['status'] == 'serving':
        now = datetime.now()
        created = datetime.fromisoformat(token['created_at'])
        called = datetime.fromisoformat(token['called_at']) if token['called_at'] else now
        wait = max(0, (called - created).total_seconds() / 60)
        service = max(.5, (now - called).total_seconds() / 60)
        conn.execute('UPDATE tokens SET status="completed",completed_at=? WHERE id=?', (now.isoformat(), token_id))
        conn.execute(
            'INSERT INTO queue_history(token_id,wait_minutes,service_minutes,completed_at) VALUES (?,?,?,?)',
            (token_id, wait, service, now.isoformat())
        )
        conn.execute('UPDATE counters SET current_token_id=NULL WHERE current_token_id=?', (token_id,))
        conn.commit()
        flash(f'{token["token"]} completed successfully.', 'success')
    conn.close()
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/skip/<int:token_id>')
@login_required('admin')
def skip_token(token_id):
    conn = get_db()
    token = conn.execute('SELECT * FROM tokens WHERE id=?', (token_id,)).fetchone()
    if token and token['status'] == 'serving':
        conn.execute('UPDATE tokens SET status="skipped" WHERE id=?', (token_id,))
        conn.execute('UPDATE counters SET current_token_id=NULL WHERE current_token_id=?', (token_id,))
        conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/counter/<int:counter_id>/toggle')
@login_required('admin')
def toggle_counter(counter_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM counters WHERE id=?', (counter_id,)).fetchone()
    if row:
        if row['current_token_id'] and row['active']:
            flash('Complete the current customer before closing this counter.', 'error')
        else:
            conn.execute('UPDATE counters SET active=? WHERE id=?', (0 if row['active'] else 1, counter_id))
            conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))


@app.route('/api/status')
def api_status():
    # Kept for admin/developer use. Normal users do not see this raw JSON page.
    conn = get_db()
    active = conn.execute('SELECT COUNT(*) FROM counters WHERE active=1').fetchone()[0]
    current = conn.execute('''
        SELECT t.token,t.service,t.priority_name,c.name counter_name
        FROM tokens t JOIN counters c ON c.current_token_id=t.id
        WHERE t.status='serving' ORDER BY t.called_at
    ''').fetchall()
    waiting = conn.execute('SELECT * FROM tokens WHERE status="waiting" ORDER BY priority,id').fetchall()
    result = [
        {'token': t['token'], 'service': t['service'], 'priority': t['priority_name'],
         'position': i + 1, 'wait': estimate_wait(i, active)}
        for i, t in enumerate(waiting)
    ]
    conn.close()
    return jsonify(
        current=[dict(x) for x in current], waiting_count=len(waiting),
        active_counters=active, waiting=result,
        updated_at=datetime.now().strftime('%I:%M:%S %p')
    )


@app.route('/api/user/<int:user_id>/status')
@login_required('user')
def user_status(user_id):
    if session.get('user_id') != user_id:
        return jsonify(error='Forbidden'), 403

    conn = get_db()
    active = conn.execute('SELECT COUNT(*) FROM counters WHERE active=1').fetchone()[0]
    current = conn.execute('''
        SELECT t.token,t.service,c.name counter_name
        FROM tokens t LEFT JOIN counters c ON c.current_token_id=t.id
        WHERE t.status='serving' ORDER BY t.called_at LIMIT 1
    ''').fetchone()
    tokens = conn.execute(
        'SELECT * FROM tokens WHERE user_id=? AND status IN ("waiting","serving") ORDER BY id DESC',
        (user_id,)
    ).fetchall()

    data = []
    for t in tokens:
        ahead = waiting_before(conn, t) if t['status'] == 'waiting' else 0
        data.append({
            'id': t['id'], 'token': t['token'], 'service': t['service'],
            'priority': t['priority_name'], 'status': t['status'],
            'position': ahead + 1 if t['status'] == 'waiting' else 0,
            'ahead': ahead,
            'wait': estimate_wait(ahead, active) if t['status'] == 'waiting' else 0
        })
        if t['status'] == 'waiting' and ahead <= 2:
            notify_user(
                conn, t, 'near_turn',
                f'Your SmartQueue token {t["token"]} is coming soon. There are {ahead} customer(s) ahead of you.'
            )

    conn.close()
    return jsonify(
        tokens=data,
        current=dict(current) if current else None,
        waiting_count=len([x for x in data if x['status'] == 'waiting']),
        active_counters=active,
        updated_at=datetime.now().strftime('%I:%M:%S %p'),
        notification_mode=session.get('notification_pref', 'inapp')
    )

init_db()
if __name__ == '__main__':
    
    app.run(debug=True)

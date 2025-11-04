from flask import Flask, request
import requests
import datetime
import time
import threading
import sqlite3
import os
from collections import defaultdict
import json

app = Flask(__name__)

# -------------------------------------------------
# CONFIG – uses Render environment variables
# -------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "7776677134:AAGJo3VfwiB5gDpCE5e5jvtHonhTcjv-NWc")
CHAT_ID   = os.getenv("CHAT_ID",   "-1002658080507")
# Free writable folder – NO DISK REQUIRED
DB_PATH   = os.getenv("DB_PATH",   "/opt/render/project/src/trades.db")

# -------------------------------------------------
# Trading rules
# -------------------------------------------------
SL_DISTANCES = {
    'USDJPY': 0.32,
    'XAUUSD': 26.0,
    'EURGBP': 0.0016,
    'US500': 45.0,
    'GER40': 120.0
}
BREAK_EVEN_THRESHOLD = 0.0001
VALID_PAIRS = {'USDJPY', 'XAUUSD', 'EURGBP', 'US500', 'GER40'}

# -------------------------------------------------
# In-memory state
# -------------------------------------------------
daily_signals = []
last_summary_sent = None
last_daily_report = None
last_weekly_report = None
last_monthly_report = None

# -------------------------------------------------
# Initialize DB (creates file automatically)
# -------------------------------------------------
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT,
    signal TEXT,
    entry REAL,
    sl REAL,
    timestamp TEXT,
    status TEXT DEFAULT 'open',
    exit_price REAL,
    exit_timestamp TEXT,
    exit_type TEXT,
    profit REAL
)
''')
conn.commit()

# -------------------------------------------------
# Telegram sender
# -------------------------------------------------
def send_telegram_message(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    if len(msg) > 4096:
        msg = msg[:4000] + "\n*Message truncated.*"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=payload).raise_for_status()
    except Exception as e:
        print(f"Telegram error: {e}")

# -------------------------------------------------
# Formatters
# -------------------------------------------------
def format_buy_sell_message(pair, signal, entry, sl, timestamp):
    try:
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
        t = dt.strftime('%d %b %H:%M UTC')
    except:
        t = datetime.datetime.now(datetime.UTC).strftime('%d %b %H:%M UTC')
    dp = pair if pair in ['US500', 'GER40'] else f"{pair[:3]}/{pair[3:]}"
    return f"""
**{dp} {signal}**
Entry: {entry}
SL: {sl}
Time: {t}
""".strip()

def format_exit_message(pair, exit_type, exit_price, timestamp, price_diff):
    try:
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
        t = dt.strftime('%d %b %H:%M UTC')
    except:
        t = datetime.datetime.now(datetime.UTC).strftime('%d %b %H:%M UTC')
    dp = pair if pair in ['US500', 'GER40'] else f"{pair[:3]}/{pair[3:]}"
    txt = {"TP": "Take Profit", "SL": "Stop Loss", "BE": "Break Even"}.get(exit_type, "Exit")
    return f"""
**{dp} {txt} Hit**
Exit: {exit_price}
Price Diff: {price_diff:+.4f}
Time: {t}
""".strip()

# -------------------------------------------------
# Exit logic
# -------------------------------------------------
def calculate_exit_type_and_profit(pair, signal, entry_price, exit_price, sl_distance):
    diff = exit_price - entry_price if signal == 'BUY' else entry_price - exit_price
    if abs(diff) <= BREAK_EVEN_THRESHOLD:
        return 'BE', 0.0, diff
    rr = round(diff / sl_distance, 2) if sl_distance else 0
    return ('TP' if diff > 0 else 'SL'), rr, diff

# -------------------------------------------------
# Webhook
# -------------------------------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data.decode('utf-8'))
        print(f"Webhook: {data}")
    except:
        return "Invalid JSON", 400

    pair = data.get('ticker')
    if not pair or pair not in VALID_PAIRS:
        return "Invalid pair", 400
    timestamp = data.get('time')
    if not timestamp:
        return "Missing time", 400
    signal = data.get('signal')

    # BUY / SELL
    if signal in ['BUY', 'SELL']:
        if pair in ['US500', 'GER40'] and signal == 'SELL':
            return "SELL not allowed", 200

        entry = data.get('entry')
        sl = data.get('sl')
        if entry is None or sl is None:
            return "Missing entry/sl", 400
        try:
            entry, sl = float(entry), float(sl)
        except:
            return "Invalid numbers", 400

        cursor.execute('SELECT id, signal, entry FROM trades WHERE pair=? AND status="open" ORDER BY id DESC LIMIT 1', (pair,))
        existing = cursor.fetchone()
        extra = ""

        if existing:
            tid, old_sig, old_entry = existing
            diff = abs(entry - old_entry)

            if old_sig != signal:  # reversal
                et, prof, pd = calculate_exit_type_and_profit(pair, old_sig, old_entry, entry, SL_DISTANCES[pair])
                cursor.execute('UPDATE trades SET status="closed", exit_price=?, exit_timestamp=?, exit_type=?, profit=? WHERE id=?',
                               (entry, timestamp, et, prof, tid))
                conn.commit()
                dp = pair if pair in ['US500','GER40'] else f"{pair[:3]}/{pair[3:]}"
                extra = f"**Reversal: {dp}**\nClosing {old_sig} → {et}\nExit: {entry}\nDiff: {pd:+.4f}\n"
            elif diff > BREAK_EVEN_THRESHOLD:
                pd = entry - old_entry if signal == 'BUY' else old_entry - entry
                cursor.execute('UPDATE trades SET status="closed", exit_price=?, exit_timestamp=?, exit_type="BE", profit=0.0 WHERE id=?',
                               (entry, timestamp, tid))
                conn.commit()
                dp = pair if pair in ['US500','GER40'] else f"{pair[:3]}/{pair[3:]}"
                extra = f"**Adjustment: {dp}**\nBreak Even Close\nExit: {entry}\nDiff: {pd:+.4f}\n"

        cursor.execute('INSERT INTO trades (pair, signal, entry, sl, timestamp) VALUES (?,?,?,?,?)',
                       (pair, signal, entry, sl, timestamp))
        conn.commit()

        msg = format_buy_sell_message(pair, signal, entry, sl, timestamp)
        send_telegram_message(extra + ("\n" + msg if extra else msg))
        daily_signals.append({"pair": pair, "signal": signal})

    # MANUAL EXIT
    elif 'exit_price' in data:
        exit_price = data.get('exit_price')
        try:
            exit_price = float(exit_price)
        except:
            return "Invalid exit_price", 400

        cursor.execute('SELECT id, signal, entry, sl FROM trades WHERE pair=? AND status="open" ORDER BY id DESC LIMIT 1', (pair,))
        trade = cursor.fetchone()
        if not trade:
            return "No open trade", 200
        tid, sig, entry, sl = trade
        et, prof, pd = calculate_exit_type_and_profit(pair, sig, entry, exit_price, SL_DISTANCES[pair])
        cursor.execute('UPDATE trades SET status="closed", exit_price=?, exit_timestamp=?, exit_type=?, profit=? WHERE id=?',
                       (exit_price, timestamp, et, prof, tid))
        conn.commit()
        send_telegram_message(format_exit_message(pair, et, exit_price, timestamp, pd))

    else:
        return "Invalid payload", 400

    return "OK", 200

# -------------------------------------------------
# Reports
# -------------------------------------------------
def send_daily_summary():
    global last_summary_sent
    now = datetime.datetime.now(datetime.UTC)
    if now.hour != 22 or (last_summary_sent and last_summary_sent.date() == now.date()) or not daily_signals:
        return
    lines = [f"*Today's Signals – {now.strftime('%d %b')}*"]
    for s in daily_signals:
        emoji = "BUY" if s['signal'] == 'BUY' else "SELL"
        dp = s['pair'] if s['pair'] in ['US500','GER40'] else f"{s['pair'][:3]}/{s['pair'][3:]}"
        lines.append(f"{dp}: {emoji} {s['signal']}")
    lines.append("\nReview and trade wisely!")
    send_telegram_message('\n'.join(lines))
    daily_signals.clear()
    last_summary_sent = now

def send_daily_report():
    global last_daily_report
    now = datetime.datetime.now(datetime.UTC)
    if now.hour != 22 or (last_daily_report and last_daily_report.date() == now.date()):
        return
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cursor.execute('SELECT pair, exit_type, profit FROM trades WHERE status="closed" AND exit_timestamp >= ? AND exit_timestamp <= ?',
                   (start.isoformat() + 'Z', now.isoformat() + 'Z'))
    rows = cursor.fetchall()
    if not rows: return
    metrics = defaultdict(lambda: {'wins':0,'losses':0,'be':0,'profit':0.0})
    for p, et, prof in rows:
        if et == 'TP': metrics[p]['wins'] += 1
        elif et == 'SL': metrics[p]['losses'] += 1
        elif et == 'BE': metrics[p]['be'] += 1
        metrics[p]['profit'] += prof
    total = sum(m['profit'] for m in metrics.values())
    lines = [f"*Daily Performance – {now.strftime('%d %b %Y')}*"]
    for p, m in metrics.items():
        dp = p if p in ['US500','GER40'] else f"{p[:3]}/{p[3:]}"
        lines.append(f"\n*{dp}*")
        lines.append(f"Wins: {m['wins']} | Losses: {m['losses']} | BE: {m['be']}")
        lines.append(f"Net: {m['profit']:+.2f}R")
    lines.append(f"\n*Total: {total:+.2f}R*")
    send_telegram_message('\n'.join(lines))
    last_daily_report = now

# Weekly / Monthly reports (same logic)
def send_weekly_report():
    global last_weekly_report
    now = datetime.datetime.now(datetime.UTC)
    if now.weekday() != 5 or now.hour != 22 or (last_weekly_report and last_weekly_report.date() == now.date()):
        return
    start = (now - datetime.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    cursor.execute('SELECT pair, exit_type, profit FROM trades WHERE status="closed" AND exit_timestamp >= ? AND exit_timestamp <= ?',
                   (start.isoformat() + 'Z', now.isoformat() + 'Z'))
    rows = cursor.fetchall()
    if not rows: return
    metrics = defaultdict(lambda: {'wins':0,'losses':0,'be':0,'profit':0.0})
    for p, et, prof in rows:
        if et == 'TP': metrics[p]['wins'] += 1
        elif et == 'SL': metrics[p]['losses'] += 1
        elif et == 'BE': metrics[p]['be'] += 1
        metrics[p]['profit'] += prof
    total = sum(m['profit'] for m in metrics.values())
    lines = [f"*Weekly Performance – Ending {now.strftime('%d %b')}*"]
    for p, m in metrics.items():
        dp = p if p in ['US500','GER40'] else f"{p[:3]}/{p[3:]}"
        lines.append(f"\n*{dp}*")
        lines.append(f"W: {m['wins']} | L: {m['losses']} | BE: {m['be']}")
        lines.append(f"Net: {m['profit']:+.2f}R")
    lines.append(f"\n*Total: {total:+.2f}R*")
    send_telegram_message('\n'.join(lines))
    last_weekly_report = now

def send_monthly_report():
    global last_monthly_report
    now = datetime.datetime.now(datetime.UTC)
    if now.day != 1 or now.hour != 0 or (last_monthly_report and last_monthly_report.date() == now.date()):
        return
    start = (now - datetime.timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - datetime.timedelta(seconds=1)
    cursor.execute('SELECT pair, exit_type, profit FROM trades WHERE status="closed" AND exit_timestamp >= ? AND exit_timestamp <= ?',
                   (start.isoformat() + 'Z', end.isoformat() + 'Z'))
    rows = cursor.fetchall()
    if not rows: return
    metrics = defaultdict(lambda: {'wins':0,'losses':0,'be':0,'profit':0.0})
    for p, et, prof in rows:
        if et == 'TP': metrics[p]['wins'] += 1
        elif et == 'SL': metrics[p]['losses'] += 1
        elif et == 'BE': metrics[p]['be'] += 1
        metrics[p]['profit'] += prof
    total = sum(m['profit'] for m in metrics.values())
    lines = [f"*Monthly Performance – {end.strftime('%b %Y')}*"]
    for p, m in metrics.items():
        dp = p if p in ['US500','GER40'] else f"{p[:3]}/{p[3:]}"
        lines.append(f"\n*{dp}*")
        lines.append(f"W: {m['wins']} | L: {m['losses']} | BE: {m['be']}")
        lines.append(f"Net: {m['profit']:+.2f}R")
    lines.append(f"\n*Total: {total:+.2f}R*")
    send_telegram_message('\n'.join(lines))
    last_monthly_report = now

# -------------------------------------------------
# Scheduler
# -------------------------------------------------
def scheduler():
    while True:
        send_daily_summary()
        send_daily_report()
        send_weekly_report()
        send_monthly_report()
        time.sleep(60)

threading.Thread(target=scheduler, daemon=True).start()

# -------------------------------------------------
# Run
# -------------------------------------------------
if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

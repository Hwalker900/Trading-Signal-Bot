from flask import Flask, request
import requests
import datetime
import time
import threading
import sqlite3
import os
from collections import defaultdict
import json
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# === HARDCODED FOR ORIGINAL GROUP (6v4b) ===
BOT_TOKEN = "7776677134:AAGJo3VfwiB5gDpCE5e5jvtHonhTcjv-NWc"
CHAT_ID   = "-1002123456789"

DB_PATH = "/opt/render/project/src/trades.db"
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

SL_DISTANCES = {
    'USDJPY': 0.32, 'XAUUSD': 26.0, 'EURGBP': 0.0016,
    'US500': 45.0, 'GER40': 120.0
}
BREAK_EVEN_THRESHOLD = 0.0001
VALID_PAIRS = {'USDJPY', 'XAUUSD', 'EURGBP', 'US500', 'GER40'}

daily_signals = []
last_summary_sent = None
last_daily_report = None
last_weekly_report = None
last_monthly_report = None

cursor.execute('''
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT, signal TEXT, entry REAL, sl REAL, timestamp TEXT,
    status TEXT DEFAULT 'open',
    exit_price REAL, exit_timestamp TEXT, exit_type TEXT, profit REAL
)
''')
conn.commit()

def send_telegram_message(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg[:4000], "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=payload, timeout=10)
        r.raise_for_status()
        log.info(f"Sent to original group: {CHAT_ID}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

def format_buy_sell_message(pair, signal, entry, sl, timestamp):
    try:
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
        t = dt.strftime('%d %b %H:%M UTC')
    except:
        t = datetime.datetime.now(datetime.UTC).strftime('%d %b %H:%M UTC')
    dp = pair if pair in ['US500', 'GER40'] else f"{pair[:3]}/{pair[3:]}"
    return f"**{dp} {signal}**\nEntry: {entry}\nSL: {sl}\nTime: {t}".strip()

def format_exit_message(pair, exit_type, exit_price, timestamp, price_diff):
    try:
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
        t = dt.strftime('%d %b %H:%M UTC')
    except:
        t = datetime.datetime.now(datetime.UTC).strftime('%d %b %H:%M UTC')
    dp = pair if pair in ['US500', 'GER40'] else f"{pair[:3]}/{pair[3:]}"
    txt = {"TP": "Take Profit", "SL": "Stop Loss", "BE": "Break Even"}.get(exit_type, "Exit")
    return f"**{dp} {txt} Hit**\nExit: {exit_price}\nPrice Diff: {price_diff:+.4f}\nTime: {t}".strip()

def calculate_exit_type_and_profit(pair, signal, entry_price, exit_price, sl_distance):
    diff = exit_price - entry_price if signal == 'BUY' else entry_price - exit_price
    if abs(diff) <= BREAK_EVEN_THRESHOLD:
        return 'BE', 0.0, diff
    rr = round(diff / sl_distance, 2) if sl_distance else 0
    return ('TP' if diff > 0 else 'SL'), rr, diff

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data.decode('utf-8'))
        log.info(f"Webhook: {data}")
    except:
        return "Invalid JSON", 400

    pair = data.get('ticker')
    if not pair or pair not in VALID_PAIRS:
        return "Invalid pair", 400

    timestamp = data.get('time') or datetime.datetime.now(datetime.UTC).isoformat() + "Z"
    signal = data.get('signal')

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
            if old_sig != signal:
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
        log.info(f"EXIT ALERT SENT TO ORIGINAL GROUP")

    else:
        return "Invalid payload", 400

    return "OK", 200

# Reports (same as before)
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

def scheduler():
    while True:
        send_daily_summary()
        time.sleep(60)

threading.Thread(target=scheduler, daemon=True).start()
log.info("Original service started")

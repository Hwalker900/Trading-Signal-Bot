from flask import Flask, request
import requests
import datetime
import time
import threading
import sqlite3
import os
from collections import defaultdict

app = Flask(__name__)

# --- Config ---
BOT_TOKEN = "7776677134:AAGJo3VfwiB5gDpCE5e5jvtHonhTcjv-NWc"
CHAT_ID = "-1002658080507"  # Private group ID
RISK_PER_TRADE = 50  # Fixed risk amount in GBP per trade
SL_DISTANCES = {
    'USDJPY': 0.32,   # 32 pips
    'XAUUSD': 26.0,   # 2600 points
    'EURGBP': 0.0016  # 16 pips
}
BREAK_EVEN_THRESHOLD = 0.0001  # Threshold for break even trades
VALID_PAIRS = {'USDJPY', 'XAUUSD', 'EURGBP'}
DB_PATH = '/data/trades.db'  # Persistent database path on Render disk

# --- Data Store ---
daily_signals = []
last_summary_sent = None

# --- Initialize Database ---
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS trades (
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
)''')
conn.commit()

# --- Telegram Sender ---
def send_telegram_message(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    if len(msg) > 4096:
        msg = msg[:4000] + "\n*Message truncated due to length.*"
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()
    except Exception as e:
        print(f"❌ Telegram error: {e}")

# --- Message Formatter for Buy/Sell Signals ---
def format_buy_sell_message(pair, signal, entry, sl, timestamp):
    try:
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
        readable_time = dt.strftime('%d %b %H:%M UTC')
    except:
        readable_time = datetime.datetime.now(datetime.UTC).strftime('%d %b %H:%M UTC')
    display_pair = f"{pair[:3]}/{pair[3:]}"
    return f"""
**{display_pair} {signal}**
💵 Entry: {entry}
🛑 SL: {sl}
🕒 Time: {readable_time}
""".strip()

# --- Message Formatter for Exit Signals ---
def format_exit_message(pair, exit_type, exit_price, timestamp):
    try:
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
        readable_time = dt.strftime('%d %b %H:%M UTC')
    except:
        readable_time = datetime.datetime.now(datetime.UTC).strftime('%d %b %H:%M UTC')
    display_pair = f"{pair[:3]}/{pair[3:]}"
    exit_type_text = {"TP": "Take Profit", "SL": "Stop Loss", "BE": "Break Even"}.get(exit_type, "Exit")
    return f"""
**{display_pair} {exit_type_text} Hit**
💵 Exit: {exit_price}
🕒 Time: {readable_time}
""".strip()

# --- Calculate Exit Type and Profit ---
def calculate_exit_type_and_profit(pair, signal, entry_price, exit_price, sl_distance):
    price_diff = exit_price - entry_price if signal == 'BUY' else entry_price - exit_price
    if abs(price_diff) <= BREAK_EVEN_THRESHOLD:
        return 'BE', 0.0
    rr_ratio = round(price_diff / sl_distance, 2) if sl_distance != 0 else 0
    profit = rr_ratio * RISK_PER_TRADE
    exit_type = 'TP' if (signal == 'BUY' and price_diff > 0) or (signal == 'SELL' and price_diff > 0) else 'SL'
    return exit_type, profit

# --- Webhook Handler ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    print(f"Received webhook: {data}")  # Log the payload
    pair = data.get('pair')
    signal = data.get('signal').upper()
    timestamp = data.get('time')
    if not all([pair, signal, timestamp]) or pair not in VALID_PAIRS:
        return "Invalid data or pair", 400
    if signal in ['BUY', 'SELL']:
        entry, sl = data.get('entry'), data.get('sl')
        if not all([entry, sl]):
            return "Missing entry or SL", 400
        try:
            entry, sl = float(entry), float(sl)
        except ValueError:
            return "Invalid entry or SL format", 400
        cursor.execute('INSERT INTO trades (pair, signal, entry, sl, timestamp) VALUES (?, ?, ?, ?, ?)',
                      (pair, signal, entry, sl, timestamp))
        conn.commit()
        message = format_buy_sell_message(pair, signal, entry, sl, timestamp)
        daily_signals.append({"pair": pair, "signal": signal})
        send_telegram_message(message)
    elif signal == 'EXIT':
        exit_price = data.get('exit_price')
        if not exit_price:
            return "Missing exit price", 400
        try:
            exit_price = float(exit_price)
        except ValueError:
            return "Invalid exit price format", 400
        cursor.execute('SELECT id, signal, entry FROM trades WHERE pair = ? AND status = "open" ORDER BY id DESC LIMIT 1', (pair,))
        row = cursor.fetchone()
        if row:
            trade_id, trade_signal, entry_price = row
            sl_distance = SL_DISTANCES.get(pair, 0.01)
            exit_type, profit = calculate_exit_type_and_profit(pair, trade_signal, entry_price, exit_price, sl_distance)
            cursor.execute('UPDATE trades SET status = "closed", exit_price = ?, exit_timestamp = ?, exit_type = ?, profit = ? WHERE id = ?',
                          (exit_price, timestamp, exit_type, profit, trade_id))
            conn.commit()
        else:
            exit_type = 'Unknown'
        message = format_exit_message(pair, exit_type, exit_price, timestamp)
        send_telegram_message(message)
    else:
        return "Invalid signal", 400
    return "Webhook received!", 200

# --- Daily Summary ---
def send_daily_summary():
    global last_summary_sent
    now = datetime.datetime.now(datetime.UTC)
    if now.hour != 21 or (last_summary_sent and last_summary_sent.date() == now.date()) or not daily_signals:
        return
    today = now.strftime('%d %b')
    lines = [f"*📅 Today's Signals – {today}*"]
    for s in daily_signals:
        emoji = "📈" if s['signal'] == 'BUY' else "📉"
        display_pair = f"{s['pair'][:3]}/{s['pair'][3:]}"
        lines.append(f"💱 {display_pair}: {emoji} {s['signal']}")
    lines.append("\n🌟 Review these and plan your next move!")
    send_telegram_message('\n'.join(lines))
    daily_signals.clear()
    last_summary_sent = now

# --- Weekly Performance Report ---
def send_weekly_report():
    now = datetime.datetime.now(datetime.UTC)
    days_since_saturday = (now.weekday() - 5) % 7
    start_date = now - datetime.timedelta(days=days_since_saturday)
    start_time = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
    cursor.execute('SELECT pair, exit_type, profit FROM trades WHERE status = "closed" AND exit_timestamp >= ? AND exit_timestamp <= ?',
                  (start_time.isoformat() + 'Z', now.isoformat() + 'Z'))
    trades = cursor.fetchall()
    metrics = defaultdict(lambda: {'wins': 0, 'losses': 0, 'break_even': 0, 'net_profit': 0.0})
    for pair, exit_type, profit in trades:
        if exit_type == 'TP':
            metrics[pair]['wins'] += 1
        elif exit_type == 'SL':
            metrics[pair]['losses'] += 1
        elif exit_type == 'BE':
            metrics[pair]['break_even'] += 1
        metrics[pair]['net_profit'] += profit / RISK_PER_TRADE
    total_net_profit = sum(m['net_profit'] for m in metrics.values())
    lines = [f"*📊 Weekly Performance – Week ending {now.strftime('%d %b %Y')}*"]
    for pair, m in metrics.items():
        display_pair = f"{pair[:3]}/{pair[3:]}"
        lines.append(f"\n*Pair: {display_pair}*")
        lines.append(f"- Wins: {m['wins']}")
        lines.append(f"- Losses: {m['losses']}")
        lines.append(f"- Break Even: {m['break_even']}")
        lines.append(f"- Net Profit: {m['net_profit']:.2f} RR (£{m['net_profit'] * RISK_PER_TRADE:.2f})")
    lines.append(f"\n*Total Net Profit: {total_net_profit:.2f} RR (£{total_net_profit * RISK_PER_TRADE:.2f})*")
    send_telegram_message('\n'.join(lines))

# --- Monthly Performance Report ---
def send_monthly_report():
    now = datetime.datetime.now(datetime.UTC)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_of_month = (start_of_month + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(seconds=1)
    cursor.execute('SELECT pair, exit_type, profit FROM trades WHERE status = "closed" AND exit_timestamp >= ? AND exit_timestamp <= ?',
                  (start_of_month.isoformat() + 'Z', end_of_month.isoformat() + 'Z'))
    trades = cursor.fetchall()
    metrics = defaultdict(lambda: {'wins': 0, 'losses': 0, 'break_even': 0, 'net_profit': 0.0})
    for pair, exit_type, profit in trades:
        if exit_type == 'TP':
            metrics[pair]['wins'] += 1
        elif exit_type == 'SL':
            metrics[pair]['losses'] += 1
        elif exit_type == 'BE':
            metrics[pair]['break_even'] += 1
        metrics[pair]['net_profit'] += profit / RISK_PER_TRADE
    total_net_profit = sum(m['net_profit'] for m in metrics.values())
    lines = [f"*📊 Monthly Performance – Month ending {end_of_month.strftime('%d %b %Y')}*"]
    for pair, m in metrics.items():
        display_pair = f"{pair[:3]}/{pair[3:]}"
        lines.append(f"\n*Pair: {display_pair}*")
        lines.append(f"- Wins: {m['wins']}")
        lines.append(f"- Losses: {m['losses']}")
        lines.append(f"- Break Even: {m['break_even']}")
        lines.append(f"- Net Profit: {m['net_profit']:.2f} RR (£{m['net_profit'] * RISK_PER_TRADE:.2f})")
    lines.append(f"\n*Total Net Profit: {total_net_profit:.2f} RR (£{total_net_profit * RISK_PER_TRADE:.2f})*")
    send_telegram_message('\n'.join(lines))

# --- Scheduler Thread ---
def background_tasks():
    while True:
        send_daily_summary()
        now = datetime.datetime.now(datetime.UTC)
        # Weekly report: Friday at 22:00 UTC
        if now.weekday() == 4 and now.hour == 22 and now.minute < 10:
            send_weekly_report()
        # Monthly report: Last trading day at 22:00 UTC
        if now.date() == ((now.replace(day=1) + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)).date() and now.weekday() < 5 and now.hour == 22 and now.minute < 10:
            send_monthly_report()
        time.sleep(600)

# --- App Startup ---
if __name__ == '__main__':
    threading.Thread(target=background_tasks, daemon=True).start()
    import os
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

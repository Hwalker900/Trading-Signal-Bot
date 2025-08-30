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

# --- Config ---
BOT_TOKEN = "7776677134:AAGJo3VfwiB5gDpCE5e5jvtHonhTcjv-NWc"
CHAT_ID = "-1002658080507"  # Private group ID
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
last_daily_report = None
last_weekly_report = None
last_monthly_report = None

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
def format_exit_message(pair, exit_type, exit_price, timestamp, price_diff):
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
📏 Price Diff from Entry: {price_diff:.4f}
🕒 Time: {readable_time}
""".strip()

# --- Calculate Exit Type and Profit ---
def calculate_exit_type_and_profit(pair, signal, entry_price, exit_price, sl_distance):
    price_diff = exit_price - entry_price if signal == 'BUY' else entry_price - exit_price
    if abs(price_diff) <= BREAK_EVEN_THRESHOLD:
        return 'BE', 0.0, price_diff
    rr_ratio = round(price_diff / sl_distance, 2) if sl_distance != 0 else 0
    profit = rr_ratio  # Now as percentage base (1.0 = 1%)
    exit_type = 'TP' if price_diff > 0 else 'SL'
    return exit_type, profit, price_diff

# --- Webhook Handler ---
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        raw_data = request.data.decode('utf-8')
        data = json.loads(raw_data)
        print(f"Received webhook payload: {data}")  # Log for debugging
    except json.JSONDecodeError as e:
        print(f"❌ JSON parse error: {e} - Raw data: {raw_data}")
        return "Invalid JSON", 400
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return "Server error", 500

    if not data:
        return "Invalid data", 400
    
    pair = data.get('ticker')
    if not pair or pair not in VALID_PAIRS:
        return "Invalid or missing pair", 400
    
    timestamp = data.get('time')
    if not timestamp:
        return "Missing timestamp", 400
    
    signal = data.get('signal')
    if signal in ['BUY', 'SELL']:
        entry = data.get('entry')
        sl = data.get('sl')
        if entry is None or sl is None:
            return "Missing entry or sl", 400
        try:
            entry = float(entry)
            sl = float(sl)
        except ValueError:
            return "Invalid entry or sl value", 400
        
        # Check for existing open trade
        cursor.execute('SELECT id, signal, entry, sl FROM trades WHERE pair = ? AND status = "open" ORDER BY id DESC LIMIT 1', (pair,))
        existing_trade = cursor.fetchone()
        
        if existing_trade:
            trade_id, existing_signal, existing_entry, existing_sl = existing_trade
            if existing_signal == signal:
                print(f"Ignored duplicate {signal} signal for {pair}")
                return "Ignored duplicate signal", 200
            else:
                # Opposite signal: close existing trade as exit
                exit_price = entry  # Use new entry price as exit price for reversal
                sl_distance = SL_DISTANCES[pair]
                exit_type, profit, price_diff = calculate_exit_type_and_profit(pair, existing_signal, existing_entry, exit_price, sl_distance)
                
                # Update the existing trade
                cursor.execute('UPDATE trades SET status = "closed", exit_price = ?, exit_timestamp = ?, exit_type = ?, profit = ? WHERE id = ?',
                               (exit_price, timestamp, exit_type, profit, trade_id))
                conn.commit()
                
                # Send exit message
                message = format_exit_message(pair, exit_type, exit_price, timestamp, price_diff)
                send_telegram_message(message)
        
        # Insert the new trade (whether new or after reversal)
        cursor.execute('INSERT INTO trades (pair, signal, entry, sl, timestamp) VALUES (?, ?, ?, ?, ?)',
                       (pair, signal, entry, sl, timestamp))
        conn.commit()
        
        # Send Telegram message for new entry
        message = format_buy_sell_message(pair, signal, entry, sl, timestamp)
        daily_signals.append({"pair": pair, "signal": signal})
        send_telegram_message(message)
    
    elif 'exit_price' in data:
        exit_price = data.get('exit_price')
        if exit_price is None:
            return "Missing exit_price", 400
        try:
            exit_price = float(exit_price)
        except ValueError:
            return "Invalid exit_price", 400
        
        # Find the latest open trade for this pair
        cursor.execute('SELECT id, signal, entry, sl FROM trades WHERE pair = ? AND status = "open" ORDER BY id DESC LIMIT 1', (pair,))
        trade = cursor.fetchone()
        if not trade:
            print(f"No open trade found for {pair}")
            return "No open trade", 200
        
        trade_id, sig, entry, sl = trade
        sl_distance = SL_DISTANCES[pair]
        exit_type, profit, price_diff = calculate_exit_type_and_profit(pair, sig, entry, exit_price, sl_distance)
        
        # Update the trade in DB
        cursor.execute('UPDATE trades SET status = "closed", exit_price = ?, exit_timestamp = ?, exit_type = ?, profit = ? WHERE id = ?',
                       (exit_price, timestamp, exit_type, profit, trade_id))
        conn.commit()
        
        # Send Telegram message
        message = format_exit_message(pair, exit_type, exit_price, timestamp, price_diff)
        send_telegram_message(message)
    
    else:
        return "Invalid payload", 400
    
    return "Webhook received!", 200

# --- Daily Signals Summary ---
def send_daily_summary():
    global last_summary_sent
    now = datetime.datetime.now(datetime.UTC)
    if now.hour != 22 or (last_summary_sent and last_summary_sent.date() == now.date()) or not daily_signals:
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

# --- Daily Performance Report ---
def send_daily_report():
    global last_daily_report
    now = datetime.datetime.now(datetime.UTC)
    if now.hour != 22 or (last_daily_report and last_daily_report.date() == now.date()):
        return
    start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cursor.execute('SELECT pair, exit_type, profit FROM trades WHERE status = "closed" AND exit_timestamp >= ? AND exit_timestamp <= ?',
                   (start_time.isoformat() + 'Z', now.isoformat() + 'Z'))
    trades = cursor.fetchall()
    if not trades:
        return
    metrics = defaultdict(lambda: {'wins': 0, 'losses': 0, 'break_even': 0, 'net_profit': 0.0})
    for pair, exit_type, profit in trades:
        if exit_type == 'TP':
            metrics[pair]['wins'] += 1
        elif exit_type == 'SL':
            metrics[pair]['losses'] += 1
        elif exit_type == 'BE':
            metrics[pair]['break_even'] += 1
        metrics[pair]['net_profit'] += profit
    total_net_profit = sum(m['net_profit'] for m in metrics.values())
    lines = [f"*📊 Daily Performance – {now.strftime('%d %b %Y')}*"]
    for pair, m in metrics.items():
        display_pair = f"{pair[:3]}/{pair[3:]}"
        lines.append(f"\n*Pair: {display_pair}*")
        lines.append(f"- Wins: {m['wins']}")
        lines.append(f"- Losses: {m['losses']}")
        lines.append(f"- Break Even: {m['break_even']}")
        lines.append(f"- Net Profit: {m['net_profit']:.2f}%")
    lines.append(f"\n*Total Net Profit: {total_net_profit:.2f}%*")
    send_telegram_message('\n'.join(lines))
    last_daily_report = now

# --- Weekly Performance Report ---
def send_weekly_report():
    global last_weekly_report
    now = datetime.datetime.now(datetime.UTC)
    if now.weekday() != 5 or now.hour != 22 or (last_weekly_report and last_weekly_report.date() == now.date()):
        return
    # Week from previous Sunday to now (Saturday)
    start_date = now - datetime.timedelta(days=6)  # Back to Sunday
    start_time = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
    cursor.execute('SELECT pair, exit_type, profit FROM trades WHERE status = "closed" AND exit_timestamp >= ? AND exit_timestamp <= ?',
                   (start_time.isoformat() + 'Z', now.isoformat() + 'Z'))
    trades = cursor.fetchall()
    if not trades:
        return
    metrics = defaultdict(lambda: {'wins': 0, 'losses': 0, 'break_even': 0, 'net_profit': 0.0})
    for pair, exit_type, profit in trades:
        if exit_type == 'TP':
            metrics[pair]['wins'] += 1
        elif exit_type == 'SL':
            metrics[pair]['losses'] += 1
        elif exit_type == 'BE':
            metrics[pair]['break_even'] += 1
        metrics[pair]['net_profit'] += profit
    total_net_profit = sum(m['net_profit'] for m in metrics.values())
    lines = [f"*📊 Weekly Performance – Week ending {now.strftime('%d %b %Y')}*"]
    for pair, m in metrics.items():
        display_pair = f"{pair[:3]}/{pair[3:]}"
        lines.append(f"\n*Pair: {display_pair}*")
        lines.append(f"- Wins: {m['wins']}")
        lines.append(f"- Losses: {m['losses']}")
        lines.append(f"- Break Even: {m['break_even']}")
        lines.append(f"- Net Profit: {m['net_profit']:.2f}%")
    lines.append(f"\n*Total Net Profit: {total_net_profit:.2f}%*")
    send_telegram_message('\n'.join(lines))
    last_weekly_report = now

# --- Monthly Performance Report ---
def send_monthly_report():
    global last_monthly_report
    now = datetime.datetime.now(datetime.UTC)
    if now.day != 1 or now.hour != 0 or (last_monthly_report and last_monthly_report.date() == now.date()):
        return
    start_of_month = (now - datetime.timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - datetime.timedelta(seconds=1)
    cursor.execute('SELECT pair, exit_type, profit FROM trades WHERE status = "closed" AND exit_timestamp >= ? AND exit_timestamp <= ?',
                   (start_of_month.isoformat() + 'Z', end_of_month.isoformat() + 'Z'))
    trades = cursor.fetchall()
    if not trades:
        return
    metrics = defaultdict(lambda: {'wins': 0, 'losses': 0, 'break_even': 0, 'net_profit': 0.0})
    for pair, exit_type, profit in trades:
        if exit_type == 'TP':
            metrics[pair]['wins'] += 1
        elif exit_type == 'SL':
            metrics[pair]['losses'] += 1
        elif exit_type == 'BE':
            metrics[pair]['break_even'] += 1
        metrics[pair]['net_profit'] += profit
    total_net_profit = sum(m['net_profit'] for m in metrics.values())
    month_name = end_of_month.strftime('%b %Y')
    lines = [f"*📊 Monthly Performance – {month_name}*"]
    for pair, m in metrics.items():
        display_pair = f"{pair[:3]}/{pair[3:]}"
        lines.append(f"\n*Pair: {display_pair}*")
        lines.append(f"- Wins: {m['wins']}")
        lines.append(f"- Losses: {m['losses']}")
        lines.append(f"- Break Even: {m['break_even']}")
        lines.append(f"- Net Profit: {m['net_profit']:.2f}%")
    lines.append(f"\n*Total Net Profit: {total_net_profit:.2f}%*")
    send_telegram_message('\n'.join(lines))
    last_monthly_report = now

# --- Scheduler Thread ---
def scheduler():
    while True:
        send_daily_summary()
        send_daily_report()
        send_weekly_report()
        send_monthly_report()
        time.sleep(60)  # Check every minute

# Start scheduler in background
threading.Thread(target=scheduler, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

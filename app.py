from flask import Flask, request
import requests
import datetime
import time
import threading
import sqlite3
from collections import defaultdict

app = Flask(__name__)

# --- Config ---
BOT_TOKEN = "7776677134:AAGJo3VfwiB5gDpCE5e5jvtHonhTcjv-NWc"
CHAT_ID = "@Supercellsignals"
DB_PATH = '/data/trades.db'  # Path to SQLite database on Render disk
RISK_PER_TRADE = 50  # Fixed risk amount in GBP per trade
SL_DISTANCES = {
    'USDJPY': 0.32,   # 32 pips
    'XAUUSD': 26.0,   # 2600 points
    'EURGBP': 0.0016  # 16 pips
}
BREAK_EVEN_THRESHOLD = 0.0001  # Threshold for break even trades

# --- Valid Pairs (Forex Only) ---
VALID_PAIRS = {'USDJPY', 'XAUUSD', 'EURGBP'}

# --- Data Store ---
daily_signals = []
last_summary_sent = None

# --- Initialize Database ---
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
    print(f"Sending Telegram message (length: {len(msg)}):\n{msg}")
    if len(msg) > 4096:
        msg = msg[:4000] + "\n*Message truncated due to length.*"
        print("⚠️ Message truncated to fit Telegram limit.")
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()
        print(f"✅ Message sent to Telegram. Response: {response.json()}")
    except Exception as e:
        print(f"❌ Telegram error: {e}, Response: {response.text if 'response' in locals() else 'No response'}")

# --- Message Formatter for Buy/Sell Signals ---
def format_buy_sell_message(pair, signal, entry, sl, timestamp):
    try:
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
        readable_time = dt.strftime('%d %b %H:%M UTC')
    except:
        readable_time = datetime.datetime.utcnow().strftime('%d %b %H:%M UTC')
    display_pair = f"{pair[:3]}/{pair[3:]}"
    message = f"""
*🌟 New Signal Alert!*

💱 *Pair*: {display_pair}
📢 *Action*: {'📈 Buy' if signal == 'BUY' else '📉 Sell'}
💵 *Entry Price*: {entry}
🛑 *Stop Loss*: {sl}
🕒 *Time*: {readable_time}
"""
    return message

# --- Message Formatter for Exit Signals ---
def format_exit_message(pair, exit_type, exit_price, timestamp):
    try:
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
        readable_time = dt.strftime('%d %b %H:%M UTC')
    except:
        readable_time = datetime.datetime.utcnow().strftime('%d %b %H:%M UTC')
    display_pair = f"{pair[:3]}/{pair[3:]}"
    message = f"""
*🚪 Exit Alert!*

💱 *Pair*: {display_pair}
📢 *Exit Type*: {exit_type}
💵 *Exit Price*: {exit_price}
🕒 *Time*: {readable_time}
"""
    return message

# --- Calculate Exit Type and Profit ---
def calculate_exit_type_and_profit(pair, signal, entry_price, exit_price, sl_distance):
    try:
        price_diff = exit_price - entry_price if signal == 'BUY' else entry_price - exit_price
        if abs(price_diff) <= BREAK_EVEN_THRESHOLD:
            return 'BE', 0.0
        rr_ratio = round(price_diff / sl_distance, 2) if sl_distance != 0 else 0
        profit = rr_ratio * RISK_PER_TRADE
        if signal == 'BUY':
            exit_type = 'TP' if price_diff > 0 else 'SL'
        else:  # SELL
            exit_type = 'TP' if price_diff > 0 else 'SL'
        return exit_type, profit
    except Exception as e:
        print(f"❌ Error calculating exit type/profit: {e}")
        return 'SL', -RISK_PER_TRADE  # Default to SL with loss

# --- Webhook Handler ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    print("🔔 Incoming webhook payload:", data)
    try:
        pair = data.get('pair')
        signal = data.get('signal').upper()
        timestamp = data.get('time')
        if not all([pair, signal, timestamp]):
            print("⚠️ Missing required fields in payload.")
            return "Incomplete data", 400
        if pair not in VALID_PAIRS:
            print(f"⚠️ Invalid pair: {pair} not in {VALID_PAIRS}")
            return f"Invalid pair: {pair}", 400
        if signal in ['BUY', 'SELL']:
            entry = data.get('entry')
            sl = data.get('sl')
            if not all([entry, sl]):
                print("⚠️ Missing entry or stop loss for BUY/SELL signal.")
                return "Missing entry or stop loss", 400
            try:
                entry = float(entry)
                sl = float(sl)
            except ValueError:
                print("⚠️ Invalid entry or stop loss format.")
                return "Invalid entry or stop loss format", 400
            cursor.execute('''INSERT INTO trades (pair, signal, entry, sl, timestamp) VALUES (?, ?, ?, ?, ?)''',
                          (pair, signal, entry, sl, timestamp))
            conn.commit()
            message = format_buy_sell_message(pair, signal, entry, sl, timestamp)
            daily_signals.append({"pair": pair, "signal": signal})
            send_telegram_message(message)
        elif signal == 'EXIT':
            exit_price = data.get('exit_price')
            if not exit_price:
                print("⚠️ Missing exit_price for EXIT signal.")
                return "Missing exit price", 400
            try:
                exit_price = float(exit_price)
            except ValueError:
                print("⚠️ Invalid exit_price format.")
                return "Invalid exit price format", 400
            cursor.execute('''SELECT id, signal, entry FROM trades WHERE pair = ? AND status = 'open' ORDER BY id DESC LIMIT 1''', (pair,))
            row = cursor.fetchone()
            if row:
                trade_id, trade_signal, entry_price = row
                sl_distance = SL_DISTANCES.get(pair, 0.01)  # Default to 0.01 if not specified
                exit_type, profit = calculate_exit_type_and_profit(pair, trade_signal, entry_price, exit_price, sl_distance)
                cursor.execute('''UPDATE trades SET status = 'closed', exit_price = ?, exit_timestamp = ?, exit_type = ?, profit = ? WHERE id = ?''',
                              (exit_price, timestamp, exit_type, profit, trade_id))
                conn.commit()
            else:
                print(f"No open trade found for pair {pair}")
                exit_type = 'Unknown'
            message = format_exit_message(pair, exit_type, exit_price, timestamp)
            send_telegram_message(message)
        else:
            print(f"⚠️ Invalid signal: {signal}")
            return "Invalid signal", 400
        return "Webhook received!", 200
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return "Error processing webhook", 500

# --- Daily Summary ---
def send_daily_summary():
    global last_summary_sent
    utc_now = datetime.datetime.utcnow()
    if utc_now.hour != 21 or (last_summary_sent and last_summary_sent.date() == utc_now.date()):
        return
    if not daily_signals:
        return
    today = utc_now.strftime('%d %b')
    lines = [f"*📅 Today's Signals – {today}*"]
    for s in daily_signals:
        emoji = "📈" if s['signal'] == 'BUY' else "📉"
        display_pair = f"{s['pair'][:3]}/{s['pair'][3:]}"
        lines.append(f"💱 {display_pair}: {emoji} {s['signal']}")
    lines.append("\n🌟 Review these and plan your next move!")
    summary = '\n'.join(lines)
    send_telegram_message(summary)
    daily_signals.clear()
    last_summary_sent = utc_now

# --- Weekly Performance Report ---
def send_weekly_report():
    now = datetime.datetime.utcnow()
    days_since_saturday = (now.weekday() - 5) % 7
    start_date = now - datetime.timedelta(days=days_since_saturday)
    start_time = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
    cursor.execute('''SELECT pair, exit_type, profit FROM trades WHERE status = 'closed' AND exit_timestamp >= ? AND exit_timestamp <= ?''',
                  (start_time.isoformat() + 'Z', now.isoformat() + 'Z'))
    trades = cursor.fetchall()
    metrics = defaultdict(lambda: {'wins': 0, 'losses': 0, 'break_even': 0, 'net_profit': 0.0})
    for trade in trades:
        pair, exit_type, profit = trade
        if exit_type == 'TP':
            metrics[pair]['wins'] += 1
        elif exit_type == 'SL':
            metrics[pair]['losses'] += 1
        elif exit_type == 'BE':
            metrics[pair]['break_even'] += 1
        metrics[pair]['net_profit'] += profit / RISK_PER_TRADE  # Convert to RR units
    total_net_profit = sum(m['net_profit'] for m in metrics.values())
    lines = [f"*📊 Weekly Performance Report – Week ending {now.strftime('%d %b %Y')}*"]
    for pair, m in metrics.items():
        display_pair = f"{pair[:3]}/{pair[3:]}"
        lines.append(f"\n*Pair: {display_pair}*")
        lines.append(f"- Winning trades: {m['wins']}")
        lines.append(f"- Losing trades: {m['losses']}")
        lines.append(f"- Break Even trades: {m['break_even']}")
        lines.append(f"- Net profit: {m['net_profit']:.2f} RR (multiply by £{RISK_PER_TRADE} for GBP)")
    lines.append(f"\n*Total Net Profit: {total_net_profit:.2f} RR (£{total_net_profit * RISK_PER_TRADE:.2f})*")
    report = '\n'.join(lines)
    send_telegram_message(report)

# --- Scheduler Thread ---
def background_tasks():
    while True:
        send_daily_summary()
        now = datetime.datetime.utcnow()
        if now.weekday() == 4 and now.hour == 22 and now.minute < 10:  # Friday 22:00 UTC
            send_weekly_report()
        time.sleep(600)

# --- App Startup ---
if __name__ == '__main__':
    threading.Thread(target=background_tasks, daemon=True).start()
    import os
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)

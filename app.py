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

# --- Config: Use environment variables (Render) with fallbacks ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "7776677134:AAGJo3VfwiB5gDpCE5e5jvtHonhTcjv-NWc")
CHAT_ID   = os.getenv("CHAT_ID", "-1002658080507")
DB_PATH   = os.getenv("DB_PATH", '/data/trades.db')

# SL Distances (in price units)
SL_DISTANCES = {
    'USDJPY': 0.32,   # 32 pips
    'XAUUSD': 26.0,   # 2600 points
    'EURGBP': 0.0016, # 16 pips
    'US500': 45.0,    # 45 points
    'GER40': 120.0    # 120 points
}
BREAK_EVEN_THRESHOLD = 0.0001  # Threshold for break even
VALID_PAIRS = {'USDJPY', 'XAUUSD', 'EURGBP', 'US500', 'GER40'}

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
        print(f"Telegram error: {e}")

# --- Message Formatter for Buy/Sell Signals ---
def format_buy_sell_message(pair, signal, entry, sl, timestamp):
    try:
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
        readable_time = dt.strftime('%d %b %H:%M UTC')
    except:
        readable_time = datetime.datetime.now(datetime.UTC).strftime('%d %b %H:%M UTC')
   
    display_pair = pair if pair in ['US500', 'GER40'] else f"{pair[:3]}/{pair[3:]}"
   
    return f"""
**{display_pair} {signal}**
Entry: {entry}
SL: {sl}
Time: {readable_time}
""".strip()

# --- Message Formatter for Exit Signals ---
def format_exit_message(pair, exit_type, exit_price, timestamp, price_diff):
    try:
        dt = datetime.datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
        readable_time = dt.strftime('%d %b %H:%M UTC')
    except:
        readable_time = datetime.datetime.now(datetime.UTC).strftime('%d %b %H:%M UTC')
   
    display_pair = pair if pair in ['US500', 'GER40'] else f"{pair[:3]}/{pair[3:]}"
    exit_type_text = {"TP": "Take Profit", "SL": "Stop Loss", "BE": "Break Even"}.get(exit_type, "Exit")
    return f"""
**{display_pair} {exit_type_text} Hit**
Exit: {exit_price}
Price Diff: {price_diff:+.4f}
Time: {readable_time}
""".strip()

# --- Calculate Exit Type and Profit ---
def calculate_exit_type_and_profit(pair, signal, entry_price, exit_price, sl_distance):
    price_diff = exit_price - entry_price if signal == 'BUY' else entry_price - exit_price
    if abs(price_diff) <= BREAK_EVEN_THRESHOLD:
        return 'BE', 0.0, price_diff
   
    rr_ratio = round(price_diff / sl_distance, 2) if sl_distance != 0 else 0
    profit = rr_ratio
    exit_type = 'TP' if price_diff > 0 else 'SL'
    return exit_type, profit, price_diff

# --- Webhook Handler ---
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        raw_data = request.data.decode('utf-8')
        data = json.loads(raw_data)
        print(f"Received webhook: {data}")
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e} - Raw: {raw_data}")
        return "Invalid JSON", 400
    except Exception as e:
        print(f"Webhook error: {e}")
        return "Server error", 500

    if not data:
        return "Invalid data", 400

    pair = data.get('ticker')
    if not pair or pair not in VALID_PAIRS:
        return "Invalid pair", 400

    timestamp = data.get('time')
    if not timestamp:
        return "Missing timestamp", 400

    signal = data.get('signal')

    # --- HANDLE BUY/SELL SIGNALS ---
    if signal in ['BUY', 'SELL']:
        if pair in ['US500', 'GER40'] and signal == 'SELL':
            print(f"Ignored SELL signal for {pair} (only BUY allowed)")
            return "SELL not allowed for indices", 200

        entry = data.get('entry')
        sl = data.get('sl')
        if entry is None or sl is None:
            return "Missing entry or sl", 400
        try:
            entry = float(entry)
            sl = float(sl)
        except ValueError:
            return "Invalid entry/sl", 400

        cursor.execute('SELECT id, signal, entry, sl FROM trades WHERE pair = ? AND status = "open" ORDER BY id DESC LIMIT 1', (pair,))
        existing_trade = cursor.fetchone()
        is_reversal = False
        is_adjustment = False
        exit_message_part = ""

        if existing_trade:
            trade_id, existing_signal, existing_entry, existing_sl = existing_trade
            price_diff = abs(entry - existing_entry)

            if existing_signal != signal:
                is_reversal = True
                exit_price = entry
                sl_distance = SL_DISTANCES[pair]
                exit_type, profit, diff = calculate_exit_type_and_profit(pair, existing_signal, existing_entry, exit_price, sl_distance)
                cursor.execute('UPDATE trades SET status = "closed", exit_price = ?, exit_timestamp = ?, exit_type = ?, profit = ? WHERE id = ?',
                               (exit_price, timestamp, exit_type, profit, trade_id))
                conn.commit()
                dp = pair if pair in ['US500', 'GER40'] else f"{pair[:3]}/{pair[3:]}"
                exit_type_text = {"TP":"Take Profit","SL":"Stop Loss","BE":"Break Even"}.get(exit_type, "Exit")
                exit_message_part = f"**Reversal: {dp}**\nClosing {existing_signal} → {exit_type_text}\nExit: {exit_price}\nDiff: {diff:+.4f}\n"
            else:
                if pair == 'XAUUSD':
                    if price_diff <= 5.0:
                        print(f"Ignored duplicate {signal} for {pair}")
                        return "Duplicate ignored", 200
                    else:
                        is_adjustment = True
                        exit_price = entry
                        diff = entry - existing_entry if signal == 'BUY' else existing_entry - entry
                        cursor.execute('UPDATE trades SET status = "closed", exit_price = ?, exit_timestamp = ?, exit_type = "BE", profit = 0.0 WHERE id = ?',
                                       (exit_price, timestamp, trade_id))
                        conn.commit()
                        exit_message_part = f"**Adjustment: {pair}**\nBreak Even Close\nExit: {exit_price}\nDiff: {diff:+.4f}\n"
                else:
                    if price_diff > BREAK_EVEN_THRESHOLD:
                        is_adjustment = True
                        exit_price = entry
                        diff = entry - existing_entry if signal == 'BUY' else existing_entry - entry
                        cursor.execute('UPDATE trades SET status = "closed", exit_price = ?, exit_timestamp = ?, exit_type = "BE", profit = 0.0 WHERE id = ?',
                                       (exit_price, timestamp, trade_id))
                        conn.commit()
                        dp = pair if pair in ['US500', 'GER40'] else f"{pair[:3]}/{pair[3:]}"
                        exit_message_part = f"**Adjustment: {dp}**\nBreak Even Close\nExit: {exit_price}\nDiff: {diff:+.4f}\n"
                    else:
                        print(f"Ignored true duplicate for {pair}")
                        return "Duplicate", 200

        cursor.execute('INSERT INTO trades (pair, signal, entry, sl, timestamp) VALUES (?, ?, ?, ?, ?)',
                       (pair, signal, entry, sl, timestamp))
        conn.commit()

        entry_message = format_buy_sell_message(pair, signal, entry, sl, timestamp)
        if is_reversal or is_adjustment:
            send_telegram_message(exit_message_part + "\n" + entry_message)
        else:
            send_telegram_message(entry_message)

        daily_signals.append({"pair": pair, "signal": signal})

    # --- HANDLE MANUAL EXIT ---
    elif 'exit_price' in data:
        exit_price = data.get('exit_price')
        if exit_price is None:
            return "Missing exit_price", 400
        try:
            exit_price = float(exit_price)
        except ValueError:
            return "Invalid exit_price", 400

        cursor.execute('SELECT id, signal, entry, sl FROM trades WHERE pair = ? AND status = "open" ORDER BY id DESC LIMIT 1', (pair,))
        trade = cursor.fetchone()
        if not trade:
            print(f"No open trade for {pair}")
            return "No open trade", 200
        trade_id, sig, entry, sl = trade
        sl_distance = SL_DISTANCES[pair]
        exit_type, profit, price_diff = calculate_exit_type_and_profit(pair, sig, entry, exit_price, sl_distance)
        cursor.execute('UPDATE trades SET status = "closed", exit_price = ?, exit_timestamp = ?, exit_type = ?, profit = ? WHERE id = ?',
                       (exit_price, timestamp, exit_type, profit, trade_id))
        conn.commit()
        message = format_exit_message(pair, exit_type, exit_price, timestamp, price_diff)
        send_telegram_message(message)

    else:
        return "Invalid payload", 400

    return "OK", 200

# --- Reports (unchanged) ---
def send_daily_summary():
    global last_summary_sent
    now = datetime.datetime.now(datetime.UTC)
    if now.hour != 22 or (last_summary_sent and last_summary_sent.date() == now.date()) or not daily_signals:
        return
    today = now.strftime('%d %b')
    lines = [f"*Today's Signals – {today}*"]
    for s in daily_signals:
        emoji = "BUY" if s['signal'] == 'BUY' else "SELL"
        dp = s['pair'] if s['pair'] in ['US500', 'GER40'] else f"{s['pair'][:3]}/{s['pair'][3:]}"
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
    cursor.execute('SELECT pair, exit_type, profit FROM trades WHERE status = "closed" AND exit_timestamp >= ? AND exit_timestamp <= ?',
                   (start.isoformat() + 'Z', now.isoformat() + 'Z'))
    trades = cursor.fetchall()
    if not trades: return
    metrics = defaultdict(lambda: {'wins':0,'losses':0,'be':0,'profit':0.0})
    for pair, et, p in trades:
        if et == 'TP': metrics[pair]['wins'] += 1
        elif et == 'SL': metrics[pair]['losses'] += 1
        elif et == 'BE': metrics[pair]['be'] += 1
        metrics[pair]['profit'] += p
    total = sum(m['profit'] for m in metrics.values())
    lines = [f"*Daily Performance – {now.strftime('%d %b %Y')}*"]
    for pair, m in metrics.items():
        dp = pair if pair in ['US500','GER40'] else f"{pair[:3]}/{pair[3:]}"
        lines.append(f"\n*{dp}*")
        lines.append(f"Wins: {m['wins']} | Losses: {m['losses']} | BE: {m['be']}")
        lines.append(f"Net: {m['profit']:+.2f}R")
    lines.append(f"\n*Total: {total:+.2f}R*")
    send_telegram_message('\n'.join(lines))
    last_daily_report = now

def send_weekly_report():
    global last_weekly_report
    now = datetime.datetime.now(datetime.UTC)
    if now.weekday() != 5 or now.hour != 22 or (last_weekly_report and last_weekly_report.date() == now.date()):
        return
    start = (now - datetime.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    cursor.execute('SELECT pair, exit_type, profit FROM trades WHERE status = "closed" AND exit_timestamp >= ? AND exit_timestamp <= ?',
                   (start.isoformat() + 'Z', now.isoformat() + 'Z'))
    trades = cursor.fetchall()
    if not trades: return
    metrics = defaultdict(lambda: {'wins':0,'losses':0,'be':0,'profit':0.0})
    for pair, et, p in trades:
        if et == 'TP': metrics[pair]['wins'] += 1
        elif et == 'SL': metrics[pair]['losses'] += 1
        elif et == 'BE': metrics[pair]['be'] += 1
        metrics[pair]['profit'] += p
    total = sum(m['profit'] for m in metrics.values())
    lines = [f"*Weekly Performance – Ending {now.strftime('%d %b')}*"]
    for pair, m in metrics.items():
        dp = pair if pair in ['US500','GER40'] else f"{pair[:3]}/{pair[3:]}"
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
    end   = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - datetime.timedelta(seconds=1)
    cursor.execute('SELECT pair, exit_type, profit FROM trades WHERE status = "closed" AND exit_timestamp >= ? AND exit_timestamp <= ?',
                   (start.isoformat() + 'Z', end.isoformat() + 'Z'))
    trades = cursor.fetchall()
    if not trades: return
    metrics = defaultdict(lambda: {'wins':0,'losses':0,'be':0,'profit':0.0})
    for pair, et, p in trades:
        if et == 'TP': metrics[pair]['wins'] += 1
        elif et == 'SL': metrics[pair]['losses'] += 1
        elif et == 'BE': metrics[pair]['be'] += 1
        metrics[pair]['profit'] += p
    total = sum(m['profit'] for m in metrics.values())
    month = end.strftime('%b %Y')
    lines = [f"*Monthly Performance – {month}*"]
    for pair, m in metrics.items():
        dp = pair if pair in ['US500','GER40'] else f"{pair[:3]}/{pair[3:]}"
        lines.append(f"\n*{dp}*")
        lines.append(f"W: {m['wins']} | L: {m['losses']} | BE: {m['be']}")
        lines.append(f"Net: {m['profit']:+.2f}R")
    lines.append(f"\n*Total: {total:+.2f}R*")
    send_telegram_message('\n'.join(lines))
    last_monthly_report = now

# --- Scheduler ---
def scheduler():
    while True:
        send_daily_summary()
        send_daily_report()
        send_weekly_report()
        send_monthly_report()
        time.sleep(60)

threading.Thread(target=scheduler, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)))

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

# --- Initialize Database (In-Memory for Free Tier) ---
conn = sqlite3.connect(':memory:', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE trades (
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
            cursor.execute('''INSERT INTO trades (pair, signal, entry, sl, timestamp

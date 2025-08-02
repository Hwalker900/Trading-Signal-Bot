from flask import Flask, request
import requests
import datetime
import time
import threading

app = Flask(__name__)

# --- Config ---
BOT_TOKEN = "7776677134:AAGJo3VfwiB5gDpCE5e5jvtHonhTcjv-NWc"
CHAT_ID = "@Supercellsignals"

# --- Valid Pairs ---
VALID_PAIRS = {'BABA', 'TSLA', 'BTCUSD', 'CADJPY', 'USDHUF', 'USDJPY'}

# --- Data Store ---
daily_signals = []
last_summary_sent = None

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
    
    if pair in {'BABA', 'TSLA'}:
        display_pair = pair
    elif pair in {'BTCUSD', 'CADJPY', 'USDHUF', 'USDJPY'}:
        display_pair = f"{pair[:3]}/{pair[3:]}"
    else:
        display_pair = pair
    
    message = f"""
*🌟 New Signal Alert!*

💱 *{'Stock' if pair in {'BABA', 'TSLA'} else 'Pair'}*: {display_pair}
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
    
    if pair in {'BABA', 'TSLA'}:
        display_pair = pair
    elif pair in {'BTCUSD', 'CADJPY', 'USDHUF', 'USDJPY'}:
        display_pair = f"{pair[:3]}/{pair[3:]}"
    else:
        display_pair = pair
    
    message = f"""
*🚪 Exit Alert!*

💱 *{'Stock' if pair in {'BABA', 'TSLA'} else 'Pair'}*: {display_pair}
📢 *Exit Type*: {exit_type}
💵 *Exit Price*: {exit_price}
🕒 *Time*: {readable_time}
"""
    return message

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

        # Normalize pair
        original_pair = pair
        if pair not in VALID_PAIRS and '/' not in pair and len(pair) >= 6:
            pair = f"{pair[:3]}/{pair[3:]}"
        pair_key = original_pair if original_pair in VALID_PAIRS else pair
        
        if pair_key not in VALID_PAIRS:
            print(f"⚠️ Invalid pair: {original_pair} (normalized to {pair}) not in {VALID_PAIRS}")
            return f"Invalid pair: {original_pair}", 400

        if signal in ['BUY', 'SELL']:
            entry = data.get('entry')
            sl = data.get('sl')
            if not all([entry, sl]):
                print("⚠️ Missing entry or stop loss for BUY/SELL signal.")
                return "Missing entry or stop loss", 400
            message = format_buy_sell_message(pair_key, signal, entry, sl, timestamp)
            daily_signals.append({"pair": pair_key, "signal": signal})
            send_telegram_message(message)
        elif signal == 'EXIT':
            exit_type = data.get('exit_type')
            exit_price = data.get('exit_price')
            if not all([exit_type, exit_price]):
                print("⚠️ Missing exit_type or exit_price for EXIT signal.")
                return "Missing exit details", 400
            message = format_exit_message(pair_key, exit_type, exit_price, timestamp)
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
        lines.append(f"💱 {s['pair']}: {emoji} {s['signal']}")
    lines.append("\n🌟 Review these and plan your next move!")
    summary = '\n'.join(lines)
    send_telegram_message(summary)
    daily_signals.clear()
    last_summary_sent = utc_now

# --- Scheduler Thread ---
def background_tasks():
    while True:
        send_daily_summary()
        time.sleep(600)

# --- App Startup ---
if __name__ == '__main__':
    threading.Thread(target=background_tasks, daemon=True).start()
    import os
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
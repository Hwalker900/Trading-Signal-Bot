from flask import Flask, request
import requests
import datetime
import time
import threading
import sqlite3
import os
import json
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = "7776677134:AAGJo3VfwiB5gDpCE5e5jvtHonhTcjv-NWc"
CHAT_ID = "-1002658080507"
DB_PATH = "/opt/render/project/src/trades.db"

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

SL_DISTANCES = {
    'USDJPY': 0.16,
    'XAUUSD': 26.0,
    'EURGBP': 0.0016,
    'US500': 45.0,
    'GER40': 180.0
}

BREAK_EVEN_THRESHOLD = 0.0001
VALID_PAIRS = {'USDJPY', 'XAUUSD', 'EURGBP', 'US500', 'GER40'}

recent_signals = []
last_weekly_sent = None
last_monthly_sent = None

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


def send_telegram_message(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": msg[:4000],
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, data=payload, timeout=10)
        r.raise_for_status()
        log.info("Sent to Telegram group")
    except Exception as e:
        log.error(f"Telegram error: {e}")


def format_pair(pair):
    return pair if pair in ['US500', 'GER40'] else f"{pair[:3]}/{pair[3:]}"


def format_buy_sell_message(pair, signal, entry, sl, timestamp):
    t = datetime.datetime.now(datetime.UTC).strftime('%d %b %H:%M UTC')
    dp = format_pair(pair)
    return f"*{dp} {signal}*\nEntry: {entry}\nSL: {sl}\nTime: {t}"


def format_exit_message(pair, exit_type, exit_price, timestamp, price_diff):
    t = datetime.datetime.now(datetime.UTC).strftime('%d %b %H:%M UTC')
    dp = format_pair(pair)
    txt = {"TP": "Take Profit", "SL": "Stop Loss", "BE": "Break Even"}.get(exit_type, "Exit")
    return f"*{dp} {txt} Hit*\nExit: {exit_price}\nPrice Diff: {price_diff:+.4f}\nTime: {t}"


def calculate_exit_type_and_profit(pair, signal, entry_price, exit_price, sl_distance):
    diff = exit_price - entry_price if signal == 'BUY' else entry_price - exit_price

    if abs(diff) <= BREAK_EVEN_THRESHOLD:
        return 'BE', 0.0, diff

    rr = round(diff / sl_distance, 2) if sl_distance else 0
    return ('TP' if diff > 0 else 'SL'), rr, diff


@app.route('/webhook', methods=['POST'])
def webhook():
    global recent_signals

    try:
        data = json.loads(request.data.decode('utf-8'))
        log.info(f"Webhook: {data}")
    except Exception:
        return "Invalid JSON", 400

    pair = data.get('ticker')
    if not pair or pair not in VALID_PAIRS:
        return "Invalid pair", 400

    timestamp = data.get('time') or datetime.datetime.now(datetime.UTC).isoformat() + "Z"
    signal = data.get('signal')

    if signal in ['BUY', 'SELL']:
        entry = data.get('entry')
        sl = data.get('sl')

        if entry is None or sl is None:
            return "Missing entry/sl", 400

        try:
            entry = float(entry)
            sl = float(sl)
        except Exception:
            return "Invalid numbers", 400

        cursor.execute(
            'SELECT id, signal, entry FROM trades WHERE pair=? AND status="open" ORDER BY id DESC LIMIT 1',
            (pair,)
        )
        existing = cursor.fetchone()
        extra = ""

        if existing:
            tid, old_sig, old_entry = existing
            diff = abs(entry - old_entry)

            if old_sig != signal:
                et, prof, pd = calculate_exit_type_and_profit(
                    pair, old_sig, old_entry, entry, SL_DISTANCES[pair]
                )

                cursor.execute('''
                    UPDATE trades
                    SET status="closed",
                        exit_price=?,
                        exit_timestamp=?,
                        exit_type=?,
                        profit=?
                    WHERE id=?
                ''', (entry, timestamp, et, prof, tid))
                conn.commit()

                dp = format_pair(pair)
                extra = (
                    f"*Reversal: {dp}*\n"
                    f"Closing {old_sig} -> {et}\n"
                    f"Exit: {entry}\n"
                    f"Diff: {pd:+.4f}\n\n"
                )

            elif diff > BREAK_EVEN_THRESHOLD:
                pd = entry - old_entry if signal == 'BUY' else old_entry - entry

                cursor.execute('''
                    UPDATE trades
                    SET status="closed",
                        exit_price=?,
                        exit_timestamp=?,
                        exit_type="BE",
                        profit=0.0
                    WHERE id=?
                ''', (entry, timestamp, tid))
                conn.commit()

                dp = format_pair(pair)
                extra = (
                    f"*Adjustment: {dp}*\n"
                    f"Break Even Close\n"
                    f"Exit: {entry}\n"
                    f"Diff: {pd:+.4f}\n\n"
                )

        cursor.execute(
            'INSERT INTO trades (pair, signal, entry, sl, timestamp) VALUES (?,?,?,?,?)',
            (pair, signal, entry, sl, timestamp)
        )
        conn.commit()

        msg = format_buy_sell_message(pair, signal, entry, sl, timestamp)
        send_telegram_message(extra + msg)

        recent_signals.append({
            "pair": pair,
            "signal": signal,
            "timestamp": datetime.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        })

    elif 'exit_price' in data:
        exit_price = data.get('exit_price')

        try:
            exit_price = float(exit_price)
        except Exception:
            return "Invalid exit_price", 400

        cursor.execute(
            'SELECT id, signal, entry, sl FROM trades WHERE pair=? AND status="open" ORDER BY id DESC LIMIT 1',
            (pair,)
        )
        trade = cursor.fetchone()

        if not trade:
            return "No open trade", 200

        tid, sig, entry, sl = trade

        et, prof, pd = calculate_exit_type_and_profit(
            pair, sig, entry, exit_price, SL_DISTANCES[pair]
        )

        cursor.execute('''
            UPDATE trades
            SET status="closed",
                exit_price=?,
                exit_timestamp=?,
                exit_type=?,
                profit=?
            WHERE id=?
        ''', (exit_price, timestamp, et, prof, tid))
        conn.commit()

        send_telegram_message(format_exit_message(pair, et, exit_price, timestamp, pd))
        log.info("EXIT ALERT SENT TO TELEGRAM")

    else:
        return "Invalid payload", 400

    return "OK", 200


def send_weekly_summary():
    global last_weekly_sent
    now = datetime.datetime.now(datetime.UTC)

    if now.weekday() != 6 or now.hour != 22 or not (0 <= now.minute <= 4):
        return

    if last_weekly_sent and last_weekly_sent.date() == now.date():
        return

    week_ago = now - datetime.timedelta(days=7)
    week_signals = [s for s in recent_signals if s['timestamp'] >= week_ago]

    if not week_signals:
        return

    lines = [f"*Weekly Signals Summary - {now.strftime('%d %b %Y')}*"]
    signal_count = {}

    for s in week_signals:
        dp = format_pair(s['pair'])
        lines.append(f"• {dp} {s['signal']} ({s['timestamp'].strftime('%d %b %H:%M')})")
        signal_count[s['pair']] = signal_count.get(s['pair'], 0) + 1

    lines.append("\n*By Pair:*")
    for pair, count in sorted(signal_count.items()):
        dp = format_pair(pair)
        lines.append(f"{dp}: {count} signal{'' if count == 1 else 's'}")

    lines.append("\nReview performance and trade wisely!")
    send_telegram_message('\n'.join(lines))
    last_weekly_sent = now


def send_monthly_summary():
    global last_monthly_sent
    now = datetime.datetime.now(datetime.UTC)

    if now.day != 1 or now.hour != 22 or not (0 <= now.minute <= 4):
        return

    if last_monthly_sent and last_monthly_sent.month == now.month and last_monthly_sent.year == now.year:
        return

    last_month = now - datetime.timedelta(days=1)
    month_start = last_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_end = last_month.replace(hour=23, minute=59, second=59, microsecond=999999)

    month_signals = [s for s in recent_signals if month_start <= s['timestamp'] <= month_end]

    cursor.execute('''
        SELECT exit_type, profit FROM trades
        WHERE status="closed"
        AND datetime(exit_timestamp) BETWEEN ? AND ?
    ''', (month_start.isoformat(), (month_end + datetime.timedelta(microseconds=1)).isoformat()))
    closed = cursor.fetchall()

    tp = sum(1 for et, _ in closed if et == 'TP')
    sl = sum(1 for et, _ in closed if et == 'SL')
    be = sum(1 for et, _ in closed if et == 'BE')
    total_rr = sum(p for et, p in closed if et in ('TP', 'SL'))

    lines = [f"*Monthly Report - {last_month.strftime('%B %Y')}*"]

    if month_signals or closed:
        lines.append(f"Signals issued: {len(month_signals)}")
        if closed:
            lines.append("\n*Closed Trades Performance:*")
            lines.append(f"TP: {tp} | SL: {sl} | BE: {be}")
            lines.append(f"Total R:R: {total_rr:+.2f}")
            win_rate = (tp / (tp + sl) * 100) if (tp + sl) > 0 else 0
            lines.append(f"Win Rate: {win_rate:.1f}% (excluding BE)")
    else:
        lines.append("No activity last month.")

    lines.append("\nStay disciplined!")
    send_telegram_message('\n'.join(lines))
    last_monthly_sent = now


def scheduler():
    while True:
        send_weekly_summary()
        send_monthly_summary()
        time.sleep(60)


threading.Thread(target=scheduler, daemon=True).start()

log.info("Service started - summaries protected from duplicates")

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

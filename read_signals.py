import psycopg2
from datetime import datetime

DB_CONFIG = {
    "host": "192.168.0.121",
    "port": 5432,
    "user": "asx",
    "password": "asxAdmin1",
    "dbname": "signals_db"
}

def read_signals_from_db(limit=100):
    """Читает последние сигналы из базы данных Postgres"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute('''
            SELECT symbol, direction, entry_price, stop_loss, take_profit, risk_percent, reward_percent, rr_ratio, move_percent, signal_quality, confirmations, warnings, signal_time
            FROM signals
            ORDER BY signal_time DESC
            LIMIT %s
        ''', (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        print(f"Ошибка чтения сигналов из БД: {e}")
        return []

if __name__ == "__main__":
    signals = read_signals_from_db(10)
    for s in signals:
        print(s)

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = "bookmaker.db"

DAILY_AMOUNT = 100
DAILY_COOLDOWN_HOURS = 24


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 1000,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                last_daily TEXT NOT NULL DEFAULT '2000-01-01'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team1 TEXT NOT NULL,
                team2 TEXT NOT NULL,
                heroes1 TEXT,
                heroes2 TEXT,
                coef1 REAL NOT NULL,
                coef2 REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                winner INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                match_id INTEGER NOT NULL,
                team INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                coef REAL NOT NULL,
                paid INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    _migrate_columns()
    init_settings()


def _migrate_columns():
    """Добавляет новые колонки в users, если база старая."""
    with db() as conn:
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "wins" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN wins INTEGER NOT NULL DEFAULT 0")
        if "losses" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN losses INTEGER NOT NULL DEFAULT 0")
        if "last_daily" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN last_daily TEXT NOT NULL DEFAULT '2000-01-01'")


# ---------- Settings ----------
def init_settings():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)


def set_setting(key, value):
    init_settings()
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, str(value)))


def get_setting(key):
    init_settings()
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


# ---------- Users ----------
def _ensure_user(conn, user_id):
    conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))


def get_user(user_id: int):
    with db() as conn:
        _ensure_user(conn, user_id)
        row = conn.execute(
            "SELECT balance, wins, losses, last_daily FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        return dict(row)


def get_balance(user_id: int) -> int:
    return get_user(user_id)["balance"]


def change_balance(user_id: int, delta: int):
    with db() as conn:
        _ensure_user(conn, user_id)
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))


def set_balance(user_id: int, amount: int):
    with db() as conn:
        _ensure_user(conn, user_id)
        conn.execute("UPDATE users SET balance=? WHERE user_id=?", (amount, user_id))


def claim_daily(user_id: int, amount: int = DAILY_AMOUNT, cooldown_hours: int = DAILY_COOLDOWN_HOURS):
    """Возвращает (success, message, new_balance)."""
    now = datetime.now(timezone.utc)
    with db() as conn:
        _ensure_user(conn, user_id)
        row = conn.execute(
            "SELECT balance, last_daily FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        try:
            last = datetime.fromisoformat(row["last_daily"]).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            last = datetime(2000, 1, 1, tzinfo=timezone.utc)
        elapsed = (now - last).total_seconds()
        if elapsed < cooldown_hours * 3600:
            remaining = cooldown_hours * 3600 - elapsed
            h = int(remaining // 3600)
            m = int((remaining % 3600) // 60)
            return False, f"⏳ Ещё рано. Приходи через **{h}ч {m}мин**.", row["balance"]
        new_bal = row["balance"] + amount
        conn.execute(
            "UPDATE users SET balance=?, last_daily=? WHERE user_id=?",
            (new_bal, now.isoformat(), user_id)
        )
        return True, f"✅ +{amount} фантиков! Новый баланс: **{new_bal}**", new_bal


def get_user_stats(user_id: int):
    with db() as conn:
        _ensure_user(conn, user_id)
        row = conn.execute(
            "SELECT balance, wins, losses, last_daily FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
        agg = conn.execute("""
            SELECT
                COUNT(*) AS total_bets,
                COALESCE(SUM(amount), 0) AS total_staked,
                COALESCE(SUM(CASE WHEN paid=1 THEN amount*coef ELSE 0 END), 0) AS total_won
            FROM bets WHERE user_id=?
        """, (user_id,)).fetchone()
        return {
            "balance": row["balance"],
            "wins": row["wins"],
            "losses": row["losses"],
            "last_daily": row["last_daily"],
            "total_bets": agg["total_bets"],
            "total_staked": int(agg["total_staked"]),
            "total_won": int(agg["total_won"]),
        }


# ---------- Matches ----------
def create_match(team1, team2, heroes1, heroes2, coef1, coef2) -> int:
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO matches(team1, team2, heroes1, heroes2, coef1, coef2)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (team1, team2, heroes1, heroes2, coef1, coef2))
        return cur.lastrowid


def get_match(match_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()


def get_open_matches():
    with db() as conn:
        return conn.execute("SELECT * FROM matches WHERE status='open' ORDER BY id DESC").fetchall()


def get_matches_for_manage():
    """Матчи, которые можно закрыть/завершить/посмотреть."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE status IN ('open','closed') ORDER BY id DESC LIMIT 25"
        ).fetchall()


def get_all_matches(limit=25):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM matches ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def close_match(match_id: int):
    with db() as conn:
        conn.execute("UPDATE matches SET status='closed' WHERE id=?", (match_id,))


def finish_match(match_id: int, winner: int):
    m = get_match(match_id)
    if not m:
        return None
    with db() as conn:
        conn.execute("UPDATE matches SET status='finished', winner=? WHERE id=?", (winner, match_id))
        bets = conn.execute("SELECT * FROM bets WHERE match_id=? AND paid=0", (match_id,)).fetchall()
        payouts = []
        for b in bets:
            if b["team"] == winner:
                payout = int(b["amount"] * b["coef"])
                conn.execute(
                    "UPDATE users SET balance = balance + ?, wins = wins + 1 WHERE user_id=?",
                    (payout, b["user_id"])
                )
                conn.execute("UPDATE bets SET paid=1 WHERE id=?", (b["id"],))
                payouts.append((b["user_id"], payout))
            else:
                conn.execute("UPDATE users SET losses = losses + 1 WHERE user_id=?", (b["user_id"],))
                conn.execute("UPDATE bets SET paid=-1 WHERE id=?", (b["id"],))
        return payouts


# ---------- Bets ----------
def place_bet(user_id, match_id, team, amount):
    m = get_match(match_id)
    if not m or m["status"] != "open":
        return None, "Матч не найден или приём ставок закрыт."
    if team not in (1, 2):
        return None, "Команда должна быть 1 или 2."
    if amount <= 0:
        return None, "Сумма должна быть больше 0."
    bal = get_balance(user_id)
    if bal < amount:
        return None, f"Недостаточно фантиков. Твой баланс: {bal}."
    coef = m["coef1"] if team == 1 else m["coef2"]
    with db() as conn:
        conn.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amount, user_id))
        conn.execute("""
            INSERT INTO bets(user_id, match_id, team, amount, coef)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, match_id, team, amount, coef))
    return coef, None


def get_user_bets(user_id, active_only=True):
    with db() as conn:
        q = """
            SELECT b.*, m.team1, m.team2, m.status AS mstatus
            FROM bets b JOIN matches m ON m.id = b.match_id
            WHERE b.user_id=?
        """
        if active_only:
            q += " AND m.status IN ('open','closed')"
        q += " ORDER BY b.id DESC"
        return conn.execute(q, (user_id,)).fetchall()


def get_history(user_id=None, status=None):
    with db() as conn:
        q = """
            SELECT b.*, m.team1, m.team2, m.status AS mstatus, m.winner
            FROM bets b JOIN matches m ON m.id = b.match_id
            WHERE 1=1
        """
        params = []
        if user_id is not None:
            q += " AND b.user_id=?"
            params.append(user_id)
        if status is not None:
            q += " AND m.status=?"
            params.append(status)
        q += " ORDER BY b.id DESC LIMIT 50"
        return conn.execute(q, params).fetchall()


# ---------- Stats ----------
def global_stats():
    with db() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) AS total_bets,
                COALESCE(SUM(amount), 0) AS total_staked,
                COALESCE(SUM(CASE WHEN paid=1 THEN amount*coef ELSE 0 END), 0) AS total_paid
            FROM bets
        """).fetchone()
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        matches = conn.execute("SELECT COUNT(*) AS c FROM matches").fetchone()["c"]
        finished = conn.execute("SELECT COUNT(*) AS c FROM matches WHERE status='finished'").fetchone()["c"]
        return {
            "total_bets": row["total_bets"],
            "total_staked": int(row["total_staked"]),
            "total_paid": int(row["total_paid"]),
            "profit": int(row["total_staked"] - row["total_paid"]),
            "users": users,
            "matches": matches,
            "finished": finished,
        }


def match_stats(match_id):
    with db() as conn:
        m = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not m:
            return None
        row = conn.execute("""
            SELECT
                COUNT(*) AS bets,
                COALESCE(SUM(amount), 0) AS staked,
                COALESCE(SUM(CASE WHEN team=1 THEN amount ELSE 0 END), 0) AS staked1,
                COALESCE(SUM(CASE WHEN team=2 THEN amount ELSE 0 END), 0) AS staked2,
                COALESCE(SUM(CASE WHEN paid=1 THEN amount*coef ELSE 0 END), 0) AS paid
            FROM bets WHERE match_id=?
        """, (match_id,)).fetchone()
        return {
            "match": m,
            "bets": row["bets"],
            "staked": int(row["staked"]),
            "staked1": int(row["staked1"]),
            "staked2": int(row["staked2"]),
            "paid": int(row["paid"]),
        }
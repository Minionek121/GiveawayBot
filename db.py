import aiosqlite
from config import DB_PATH

db_lock = None


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            exp INTEGER DEFAULT 0,
            gamble_tokens INTEGER DEFAULT 0,
            last_daily INTEGER
        );

        CREATE TABLE IF NOT EXISTS games (
            guild_id INTEGER,
            game_name TEXT,
            reward_balance INTEGER DEFAULT 0,
            reward_exp INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, game_name)
        );

        CREATE TABLE IF NOT EXISTS game_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            game_name TEXT,
            answer TEXT
        );

        CREATE TABLE IF NOT EXISTS chest_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            chest TEXT,
            item_type TEXT,
            value INTEGER,
            weight INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS codes (
            code TEXT PRIMARY KEY,
            guild_id INTEGER,
            level_req INTEGER DEFAULT 0,
            balance INTEGER DEFAULT 0,
            exp INTEGER DEFAULT 0,
            role_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS system_settings (
            guild_id INTEGER,
            system TEXT,
            enabled INTEGER,
            PRIMARY KEY (guild_id, system)
        );

        CREATE TABLE IF NOT EXISTS game_config (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            answer_time INTEGER,
            interval_seconds INTEGER
        );
        """)
        await db.commit()


async def migrate_db():
    """
    SAFE migrations ONLY (never destructive).
    Railway-safe: runs every startup.
    """

    async with aiosqlite.connect(DB_PATH) as db:

        # users table safety upgrades
        await db.execute("""
        ALTER TABLE users ADD COLUMN gamble_tokens INTEGER DEFAULT 0
        """) if await _column_missing(db, "users", "gamble_tokens") else None

        await db.commit()


async def _column_missing(db, table, column):
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        cols = await cur.fetchall()
        return column not in [c[1] for c in cols]


async def get_db():
    return await aiosqlite.connect(DB_PATH)

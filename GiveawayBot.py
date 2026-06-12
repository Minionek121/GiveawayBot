import os
import json
import random
import asyncio
import aiosqlite
from datetime import datetime, timedelta, UTC
from contextlib import asynccontextmanager
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN    = os.getenv("DISCORD_TOKEN")
DATABASE = "/app/data/giveaways.db"
db_lock  = asyncio.Lock()

intents = discord.Intents.default()
intents.members         = True
intents.guilds          = True
intents.message_content = True

_BOT_PREFIX = "!"

def _get_prefix(bot, message):
    return _BOT_PREFIX

bot = commands.Bot(command_prefix=_get_prefix, intents=intents, help_command=None)

# ═══════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════

VIP_CHEST_KEY       = "VIP Chest Key"
GAMBLE_TOKEN        = "Gamble Token"
RAFFLE_TICKET_PRICE = 100
RAFFLE_PRIZE        = 0
CHEST_COST          = 1000
LEVEL_DIVISOR       = 700
BOT_OWNER_ID = 906291437895843901
COUNTING_BOT_ID      = 510016054391734273
_COUNTING_FAIL_EMOJI = frozenset({'❌', '⚠️', '⚠'})
# {(guild_id, channel_id): {role_id: allowed_bool}}
# role_id 0 means the "everyone" rule for that channel
prefix_channel_rules: dict[tuple[int, int], dict[int, bool]] = {}
# Daily message count buffer (flushed to DB every 2 minutes)
_msg_buf: dict[tuple[int, int], int] = {}   # (guild_id, user_id) -> count since midnight
_msg_buf_date: str = ""                      # YYYY-MM-DD the buffer belongs to

def is_owner(uid: int) -> bool:
    return uid == BOT_OWNER_ID

TEMPLATES = {
    "gold":  discord.Color.gold(),
    "red":   discord.Color.red(),
    "blue":  discord.Color.blue(),
    "green": discord.Color.green(),
}

DEFAULT_CHEST_PRIZES = [
    {"name": "250 EXP",     "exp": 250,   "balance": 0,     "chance": 40},
    {"name": "450 EXP",     "exp": 450,   "balance": 0,     "chance": 30},
    {"name": "1k EXP",      "exp": 1000,  "balance": 0,     "chance": 6},
    {"name": "1k Balance",  "exp": 0,     "balance": 1000,  "chance": 15},
    {"name": "1 Huge",      "exp": 0,     "balance": 0,     "chance": 4},
    {"name": "25m Gems",    "exp": 0,     "balance": 0,     "chance": 4},
    {"name": "40k Balance", "exp": 0,     "balance": 40000, "chance": 1},
]
DEFAULT_VIP_PRIZES = [
    {"name": "2k EXP",       "exp": 2000,  "balance": 0,      "chance": 28},
    {"name": "5k EXP",       "exp": 5000,  "balance": 0,      "chance": 18},
    {"name": "5k Balance",   "exp": 0,     "balance": 5000,   "chance": 18},
    {"name": "15k Balance",  "exp": 0,     "balance": 15000,  "chance": 12},
    {"name": "1 Huge",       "exp": 0,     "balance": 0,      "chance": 10},
    {"name": "25m Gems",     "exp": 0,     "balance": 0,      "chance": 9},
    {"name": "100k Balance", "exp": 0,     "balance": 100000, "chance": 5},
]
RARE_CHEST_PRIZES = {"1 Huge", "25m Gems", "40k Balance"}
RARE_VIP_PRIZES   = {"1 Huge", "25m Gems", "100k Balance"}

# ═══════════════════════════════════════════════════════
# DISABLED COMMANDS
# ═══════════════════════════════════════════════════════

# Per-guild: {guild_id: set of disabled command names}
disabled_commands: dict[int, set[str]] = {}
# Global: applies to every server (owner-only)
global_disabled_commands: set[str] = set()
def _cmd_disabled(guild_id: int, cmd_name: str) -> bool:
    """True if the command is disabled globally or in this guild."""
    return (cmd_name in global_disabled_commands or
            cmd_name in disabled_commands.get(guild_id, set()))

def command_enabled():
    async def predicate(interaction: discord.Interaction) -> bool:
        name     = interaction.command.name if interaction.command else ""
        guild_id = interaction.guild.id if interaction.guild else 0
        if _cmd_disabled(guild_id, name):
            await interaction.response.send_message(
                "❌ This command is currently disabled.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

async def is_system_enabled(guild_id: int, flag: str) -> bool:
    # Global flag takes precedence — if globally off, it's off everywhere
    async with get_db() as db:
        async with db.execute(
            "SELECT enabled FROM global_system_flags WHERE flag_name=?", (flag,)) as cur:
            grow = await cur.fetchone()
    if grow is not None and grow[0] == 0:
        return False
    # Guild-specific flag
    async with get_db() as db:
        async with db.execute(
            "SELECT enabled FROM system_flags WHERE guild_id=? AND flag_name=?",
            (guild_id, flag)) as cur:
            row = await cur.fetchone()
    return row[0] == 1 if row else True

# ═══════════════════════════════════════════════════════
# PERMISSION CHECK
# ═══════════════════════════════════════════════════════

async def is_allowed_to_giveaway(interaction: discord.Interaction) -> bool:
    if interaction.user.id == BOT_OWNER_ID:          # owner is always allowed
        return True
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if any(role.name.lower() == "bot developer" for role in member.roles):
        return True
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT role_id FROM giveaway_roles WHERE guild_id=?",
                                  (interaction.guild.id,)) as cur:
                rows = await cur.fetchall()
    allowed = {r[0] for r in rows}
    return any(role.id in allowed for role in member.roles)
    
# ═══════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════

@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DATABASE)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout=30000")
    try:
        yield db
    finally:
        await db.close()

async def setup_database():
    async with db_lock:
        async with get_db() as db:
            # Core tables
            await db.execute("CREATE TABLE IF NOT EXISTS giveaway_roles(guild_id INTEGER, role_id INTEGER)")
            await db.execute("""CREATE TABLE IF NOT EXISTS giveaways(
                message_id INTEGER, channel_id INTEGER, prize TEXT, winners INTEGER,
                reward INTEGER, end_time INTEGER, required_role INTEGER, template TEXT)""")
            await db.execute("CREATE TABLE IF NOT EXISTS balances(user_id INTEGER PRIMARY KEY, balance INTEGER)")
            await db.execute("CREATE TABLE IF NOT EXISTS exp_history(user_id INTEGER, amount INTEGER, timestamp INTEGER, is_bonus INTEGER DEFAULT 0)")
            await db.execute("""CREATE TABLE IF NOT EXISTS raffle(
                guild_id INTEGER, user_id INTEGER, tickets INTEGER, PRIMARY KEY(guild_id,user_id))""")
            await db.execute("CREATE TABLE IF NOT EXISTS giveaway_winners(message_id INTEGER PRIMARY KEY, winner_id INTEGER, reward INTEGER)")
            await db.execute("CREATE TABLE IF NOT EXISTS raffle_config(guild_id INTEGER PRIMARY KEY, channel_id INTEGER)")
            await db.execute("CREATE TABLE IF NOT EXISTS spent_exp(user_id INTEGER PRIMARY KEY, amount INTEGER)")
            await db.execute("CREATE TABLE IF NOT EXISTS item_store(item_name TEXT PRIMARY KEY, price INTEGER, role_id INTEGER, description TEXT)")
            await db.execute("""CREATE TABLE IF NOT EXISTS user_stats(
                user_id INTEGER PRIMARY KEY, total_exp INTEGER DEFAULT 0,
                gifted_balance INTEGER DEFAULT 0, chests_opened INTEGER DEFAULT 0,
                raffle_tickets_bought INTEGER DEFAULT 0)""")
            # New tables
            await db.execute("""CREATE TABLE IF NOT EXISTS inventory(
                user_id INTEGER, item_name TEXT, quantity INTEGER DEFAULT 0,
                PRIMARY KEY(user_id, item_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS exp_boosts(
                guild_id INTEGER, role_id INTEGER, boost_percent REAL,
                PRIMARY KEY(guild_id, role_id))""")
            await db.execute("CREATE TABLE IF NOT EXISTS rare_drop_config(guild_id INTEGER PRIMARY KEY, channel_id INTEGER)")
            await db.execute("""CREATE TABLE IF NOT EXISTS raffle_info_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER, message_id INTEGER)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS abuse_boxes(
                guild_id INTEGER, box_name TEXT, PRIMARY KEY(guild_id, box_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS abuse_box_prizes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, box_name TEXT,
                prize_type TEXT, prize_value TEXT, prize_amount INTEGER DEFAULT 0, chance REAL)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS games(
                guild_id INTEGER, game_name TEXT, enabled INTEGER DEFAULT 1,
                reward_balance INTEGER DEFAULT 0, reward_exp INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, game_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS game_answers(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, game_name TEXT, answer TEXT)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS game_config(
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER,
                answer_time INTEGER DEFAULT 30, interval_seconds INTEGER DEFAULT 60)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS daily_key_log(
                guild_id INTEGER, user_id INTEGER, date TEXT,
                PRIMARY KEY(guild_id, user_id, date))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS daily_gamble_log(
                guild_id INTEGER, user_id INTEGER, date TEXT,
                PRIMARY KEY(guild_id, user_id, date))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS chest_prizes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, chest_type TEXT,
                name TEXT, exp INTEGER DEFAULT 0, balance INTEGER DEFAULT 0, chance REAL)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS system_flags(
                guild_id INTEGER, flag_name TEXT, enabled INTEGER DEFAULT 1,
                PRIMARY KEY(guild_id, flag_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS redeem_codes(
                guild_id INTEGER, code TEXT, prize_json TEXT,
                uses_left INTEGER DEFAULT 1, min_level INTEGER DEFAULT 0,
                min_balance INTEGER DEFAULT 0, required_role_id INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, code))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS code_uses(
                guild_id INTEGER, code TEXT, user_id INTEGER,
                PRIMARY KEY(guild_id, code, user_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS rare_chest_config(
                guild_id INTEGER, chest_type TEXT, prize_name TEXT,
                PRIMARY KEY(guild_id, chest_type, prize_name))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS rare_box_config(
                guild_id INTEGER, box_name TEXT, prize_id INTEGER,
                PRIMARY KEY(guild_id, box_name, prize_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS log_channels(
                guild_id INTEGER, log_type TEXT, channel_id INTEGER,
                PRIMARY KEY(guild_id, log_type))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS disabled_commands_persist(
                command_name TEXT PRIMARY KEY)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS welcome_config(
                guild_id INTEGER PRIMARY KEY,
                enabled  INTEGER DEFAULT 0,
                message  TEXT)""")
            # ── welcome_config channel-welcome columns ─────────────────────────────
            for _wc_col in [("channel_id",      "INTEGER DEFAULT 0"),
                        ("channel_enabled",  "INTEGER DEFAULT 0"),
                        ("channel_message",  "TEXT")]:
                try:
                    await db.execute(
                        f"ALTER TABLE welcome_config ADD COLUMN {_wc_col[0]} {_wc_col[1]}")
                except aiosqlite.OperationalError:
                    pass
            await db.execute("""CREATE TABLE IF NOT EXISTS auto_giveaway_pool(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                prize TEXT,
                winners INTEGER DEFAULT 1,
                chance REAL DEFAULT 1.0,
                reward_balance INTEGER DEFAULT 0,
                reward_exp INTEGER DEFAULT 0,
                reward_tickets INTEGER DEFAULT 0,
                reward_gamble_tokens INTEGER DEFAULT 0,
                reward_vip_keys INTEGER DEFAULT 0,
                reward_role_id INTEGER DEFAULT 0,
                reward_item TEXT,
                reward_item_qty INTEGER DEFAULT 1)""")

            await db.execute("""CREATE TABLE IF NOT EXISTS auto_giveaway_config(
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                interval_seconds INTEGER NOT NULL,
                duration_seconds INTEGER NOT NULL,
                running INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS raffle_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                draw_timestamp INTEGER,
                winner_id INTEGER,
                winner_tickets INTEGER,
                total_tickets INTEGER,
                top_json TEXT)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS counting_config(
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                channel_id INTEGER DEFAULT 0,
                announce_channel_id INTEGER DEFAULT 0)""")

            await db.execute("""CREATE TABLE IF NOT EXISTS counting_prizes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                prize_type TEXT,
                prize_value TEXT,
                prize_amount INTEGER DEFAULT 0,
                weight_formula TEXT DEFAULT '1')""")

            await db.execute("""CREATE TABLE IF NOT EXISTS counting_special_prizes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                number INTEGER,
                prize_type TEXT,
                prize_value TEXT,
                prize_amount INTEGER DEFAULT 0,
                label TEXT)""")
            # ── Per-guild schemas (migration: drop & recreate if guild_id column is missing) ──

            for table, new_sql in [
                ("balances",
                 "CREATE TABLE balances(guild_id INTEGER, user_id INTEGER, balance INTEGER, PRIMARY KEY(guild_id,user_id))"),
                ("exp_history",
                 "CREATE TABLE exp_history(guild_id INTEGER, user_id INTEGER, amount INTEGER, timestamp INTEGER, is_bonus INTEGER DEFAULT 0)"),
                ("user_stats",
                 "CREATE TABLE user_stats(guild_id INTEGER, user_id INTEGER, "
                 "total_exp INTEGER DEFAULT 0, gifted_balance INTEGER DEFAULT 0, "
                 "chests_opened INTEGER DEFAULT 0, raffle_tickets_bought INTEGER DEFAULT 0, "
                 "PRIMARY KEY(guild_id, user_id))"),
                ("inventory",
                 "CREATE TABLE inventory(guild_id INTEGER, user_id INTEGER, item_name TEXT, quantity INTEGER DEFAULT 0, PRIMARY KEY(guild_id,user_id,item_name))"),
                ("item_store",
                 "CREATE TABLE item_store(guild_id INTEGER, item_name TEXT, price INTEGER, role_id INTEGER, description TEXT, PRIMARY KEY(guild_id,item_name))"),
                ("disabled_commands_persist",
                 "CREATE TABLE disabled_commands_persist(guild_id INTEGER, command_name TEXT, PRIMARY KEY(guild_id,command_name))"),
            ]:
                async with db.execute(f"PRAGMA table_info({table})") as cur:
                    cols = {row[1] for row in await cur.fetchall()}
                if cols and "guild_id" not in cols:
                    await db.execute(f"DROP TABLE {table}")
                    await db.execute(new_sql)
                elif not cols:
                    await db.execute(new_sql)

            async with db.execute("PRAGMA table_info(exp_boosts)") as cur:
                _eb_cols = {row[1] for row in await cur.fetchall()}
            if _eb_cols and "channel_id" not in _eb_cols:
                await db.execute("ALTER TABLE exp_boosts RENAME TO exp_boosts_old")
                await db.execute("""CREATE TABLE exp_boosts(
                    guild_id INTEGER, role_id INTEGER, boost_percent REAL,
                    channel_id INTEGER DEFAULT 0, category_id INTEGER DEFAULT 0,
                    PRIMARY KEY(guild_id, role_id, channel_id, category_id))""")
                await db.execute(
                    "INSERT OR IGNORE INTO exp_boosts "
                    "SELECT guild_id, role_id, boost_percent, 0, 0 FROM exp_boosts_old")
                await db.execute("DROP TABLE exp_boosts_old")

            # ── Global tables (new) ──
            await db.execute("""CREATE TABLE IF NOT EXISTS global_disabled_commands(
                command_name TEXT PRIMARY KEY)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS global_system_flags(
                flag_name TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS global_redeem_codes(
                code TEXT PRIMARY KEY, prize_json TEXT,
                uses_left INTEGER DEFAULT -1, min_level INTEGER DEFAULT 0, min_balance INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS global_code_uses(
                code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id))""")
            # Migrations
            for col in [("ended", "INTEGER DEFAULT 0")]:
                try:
                    await db.execute(f"ALTER TABLE giveaways ADD COLUMN {col[0]} {col[1]}")
                except aiosqlite.OperationalError:
                    pass
            try:
                await db.execute("ALTER TABLE exp_history ADD COLUMN is_bonus INTEGER DEFAULT 0")
            except aiosqlite.OperationalError:
                pass
            # ── Game system expansions ─────────────────────────────────────
            await db.execute("""CREATE TABLE IF NOT EXISTS game_hints(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, game_name TEXT, answer_id INTEGER,
                hint_text TEXT, hint_order INTEGER DEFAULT 1)""")

            for _col in [
                ("reward_tickets",       "INTEGER DEFAULT 0"),
                ("reward_gamble_tokens", "INTEGER DEFAULT 0"),
                ("reward_vip_keys",      "INTEGER DEFAULT 0"),
                ("reward_item",          "TEXT"),
                ("reward_item_qty",      "INTEGER DEFAULT 1"),
                ("reward_role_id",       "INTEGER DEFAULT 0"),
                ("chance",               "REAL DEFAULT 1.0"),
                ("answer_time",          "INTEGER DEFAULT 30"),
            ]:
                try:
                    await db.execute(f"ALTER TABLE games ADD COLUMN {_col[0]} {_col[1]}")
                except aiosqlite.OperationalError:
                    pass

            try:
                await db.execute("ALTER TABLE game_config ADD COLUMN hint_delays TEXT")
            except aiosqlite.OperationalError:
                pass
            await db.execute("""CREATE TABLE IF NOT EXISTS prefix_restrictions(
                guild_id INTEGER,
                channel_id INTEGER,
                role_id INTEGER,   -- 0 = "everyone" default for this channel
                allowed INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, channel_id, role_id))""")
            await db.execute("""CREATE TABLE IF NOT EXISTS counting_state(
                guild_id INTEGER PRIMARY KEY,
                current_count INTEGER DEFAULT 0,
                last_user_id INTEGER DEFAULT 0,
                last_message_id INTEGER DEFAULT 0,
                record INTEGER DEFAULT 0,
                notify_message_id INTEGER DEFAULT 0)""")

            await db.execute("""CREATE TABLE IF NOT EXISTS counting_bans(
                guild_id INTEGER,
                user_id INTEGER,
                unban_time INTEGER,
                PRIMARY KEY(guild_id, user_id))""")

            await db.execute("""CREATE TABLE IF NOT EXISTS verification_config(
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER DEFAULT 0,
                message_id INTEGER DEFAULT 0,
                verified_role_id INTEGER DEFAULT 0,
                unverified_role_id INTEGER DEFAULT 0,
                message TEXT)""")

            await db.execute("""CREATE TABLE IF NOT EXISTS auto_entry_roles(
                guild_id INTEGER,
                role_id INTEGER,
                PRIMARY KEY(guild_id, role_id))""")

            await db.execute("""CREATE TABLE IF NOT EXISTS auto_entry_users(
                guild_id INTEGER,
                user_id INTEGER,
                enabled INTEGER DEFAULT 1,
                PRIMARY KEY(guild_id, user_id))""")

            await db.execute("""CREATE TABLE IF NOT EXISTS chest_channel_config(
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER DEFAULT 0,
                message_id INTEGER DEFAULT 0)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS daily_message_counts(
                guild_id INTEGER,
                user_id INTEGER,
                date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY(guild_id, user_id, date))""")

            await db.execute("""CREATE TABLE IF NOT EXISTS stats_channel_config(
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER DEFAULT 0,
                message_id INTEGER DEFAULT 0)""")

            # Migration: add message requirement to auto-entry roles
            try:
                await db.execute(
                    "ALTER TABLE auto_entry_roles ADD COLUMN message_requirement INTEGER DEFAULT 0")
            except aiosqlite.OperationalError:
                pass
            # EXP bug fix: zero spent_exp so new negative-entry system takes over
            await db.execute("""CREATE TABLE IF NOT EXISTS bot_config(
                key TEXT PRIMARY KEY, value TEXT)""")
            await db.commit()

# ═══════════════════════════════════════════════════════
# SYSTEM FLAGS
# ═══════════════════════════════════════════════════════

async def set_system_flag(guild_id: int, flag: str, enabled: bool):
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO system_flags(guild_id, flag_name, enabled) VALUES(?,?,?)",
                             (guild_id, flag, 1 if enabled else 0))
            await db.commit()

# ═══════════════════════════════════════════════════════
# GIVEAWAY WATCHER
# ═══════════════════════════════════════════════════════

async def giveaway_watcher():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = int(datetime.now(UTC).timestamp())
        async with get_db() as db:
            async with db.execute("SELECT message_id FROM giveaways WHERE ended=0 AND end_time<=?", (now,)) as cur:
                rows = await cur.fetchall()
        for (mid,) in rows:
            try:
                await end_giveaway(mid)
            except Exception as e:
                print(f"[Watcher] {mid}: {e}")
        await asyncio.sleep(15)

# ═══════════════════════════════════════════════════════
# BALANCE  (now guild-scoped)
# ═══════════════════════════════════════════════════════

async def get_balance(guild_id: int, user_id: int) -> int:
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT balance FROM balances WHERE guild_id=? AND user_id=?",
                (guild_id, user_id)) as cur:
                row = await cur.fetchone()
            if row is None:
                await db.execute("INSERT INTO balances VALUES(?,?,?)", (guild_id, user_id, 0))
                await db.commit()
                return 0
            return row[0]

async def add_balance(guild_id: int, user_id: int, amount: int):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO balances VALUES(?,?,0)", (guild_id, user_id))
            await db.execute(
                "UPDATE balances SET balance=balance+? WHERE guild_id=? AND user_id=?",
                (amount, guild_id, user_id))
            await db.execute(
                "UPDATE balances SET balance=0 WHERE guild_id=? AND user_id=? AND balance<0",
                (guild_id, user_id))
            await db.commit()

# ═══════════════════════════════════════════════════════
# STATS  (now guild-scoped)
# ═══════════════════════════════════════════════════════

async def ensure_stats(guild_id: int, user_id: int):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO user_stats(guild_id,user_id) VALUES(?,?)",
                (guild_id, user_id))
            await db.commit()

async def add_stat(guild_id: int, user_id: int, column: str, amount: int):
    await ensure_stats(guild_id, user_id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                f"UPDATE user_stats SET {column}={column}+? WHERE guild_id=? AND user_id=?",
                (amount, guild_id, user_id))
            await db.commit()

# --- COUNTING STUFF -----------------------------

import ast
import math as _math

def _eval_counting_expr(expr: str) -> float | None:
    """Safely evaluate a counting math expression. Returns float or None if invalid."""
    if len(expr) > 60:
        return None
    expr = expr.replace('^', '**')
    try:
        tree = ast.parse(expr.strip(), mode='eval')
    except SyntaxError:
        return None
    _ALLOWED = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Num,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.FloorDiv, ast.Mod,
        ast.USub, ast.UAdd, ast.Call, ast.Name, ast.Load,
    )
    _ALLOWED_NAMES = {'sqrt', 'abs', 'floor', 'ceil', 'pi', 'e'}
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            return None
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_NAMES:
            return None
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_NAMES):
                return None
    ns = {
        '__builtins__': {},
        'sqrt': _math.sqrt, 'abs': abs,
        'floor': _math.floor, 'ceil': _math.ceil,
        'pi': _math.pi, 'e': _math.e,
    }
    try:
        result = eval(compile(tree, '<string>', 'eval'), ns)
        return float(result)
    except Exception:
        return None

def _is_int_like(val: float) -> bool:
    try:
        return abs(val - round(val)) < 1e-9 and 0 < val < 1e15
    except (OverflowError, ValueError):
        return False

async def _counting_break(message: discord.Message, broken_at: int, double: bool = False):
    """React, ban the counter for 1 h, reset the count, send the break message."""
    gid  = message.guild.id
    user = message.author
    try: await message.add_reaction("❌")
    except Exception: pass

    unban_ts = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO counting_state(guild_id) VALUES(?)", (gid,))
            await db.execute(
                "UPDATE counting_state SET current_count=0, last_user_id=0, "
                "last_message_id=0, notify_message_id=0 WHERE guild_id=?", (gid,))
            await db.execute(
                "INSERT OR REPLACE INTO counting_bans(guild_id, user_id, unban_time) "
                "VALUES(?,?,?)", (gid, user.id, unban_ts))
            await db.commit()

    if double:
        text = (f"❌ {user.mention} counted twice in a row and broke the count at **{broken_at}**! "
                f"They are banned from counting for 1 hour. Counting restarts from **1**.")
    else:
        text = (f"❌ {user.mention} broke the count at **{broken_at}**! "
                f"They are banned from counting for 1 hour. Counting restarts from **1**.")
    try: await message.channel.send(text)
    except Exception: pass

async def _process_counting(message: discord.Message):
    """Handle built-in counting logic for a message."""
    if not message.guild:
        return

    # Check if counting is enabled and this is the counting channel
    async with get_db() as db:
        async with db.execute(
            "SELECT enabled, channel_id, announce_channel_id "
            "FROM counting_config WHERE guild_id=?", (message.guild.id,)) as cur:
            cfg = await cur.fetchone()
    if not cfg or not cfg[0] or not cfg[1]:
        return
    _, ch_id, ann_ch_id = cfg
    if message.channel.id != ch_id:
        return

    # Get text before the first space
    raw = message.content.strip()
    if not raw:
        return
    text = raw.split()[0]

    val = _eval_counting_expr(text)
    if val is None or not _is_int_like(val):
        return  # Not a valid integer expression — skip silently

    num = round(val)

    # Fetch current counting state
    async with get_db() as db:
        async with db.execute(
            "SELECT current_count, last_user_id, last_message_id, record, notify_message_id "
            "FROM counting_state WHERE guild_id=?", (message.guild.id,)) as cur:
            state = await cur.fetchone()

    if not state:
        async with db_lock:
            async with get_db() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO counting_state(guild_id) VALUES(?)",
                    (message.guild.id,))
                await db.commit()
        cur_count, last_uid, last_msg_id, record, notify_msg_id = 0, 0, 0, 0, 0
    else:
        cur_count, last_uid, last_msg_id, record, notify_msg_id = state

    # Check ban
    now_ts = int(datetime.now(UTC).timestamp())
    async with get_db() as db:
        async with db.execute(
            "SELECT unban_time FROM counting_bans WHERE guild_id=? AND user_id=?",
            (message.guild.id, message.author.id)) as cur:
            ban_row = await cur.fetchone()
    if ban_row and ban_row[0] > now_ts:
        return  # Silently ignore banned user

    if num == cur_count + 1:
        if message.author.id == last_uid:
            await _counting_break(message, cur_count, double=True)
        else:
            # Valid count!
            new_record = max(record, num)
            async with db_lock:
                async with get_db() as db:
                    await db.execute(
                        "UPDATE counting_state SET current_count=?, last_user_id=?, "
                        "last_message_id=?, record=?, notify_message_id=0 WHERE guild_id=?",
                        (num, message.author.id, message.id, new_record, message.guild.id))
                    await db.commit()

            # React
            try:
                if num == 100:
                    await message.add_reaction("💯")
                elif num > record:          # new record
                    await message.add_reaction("☑️")
                else:
                    await message.add_reaction("✅")
            except Exception:
                pass

            # Give prizes if a pool is configured
            async with get_db() as db:
                async with db.execute(
                    "SELECT id, prize_type, prize_value, prize_amount, weight_formula "
                    "FROM counting_prizes WHERE guild_id=?", (message.guild.id,)) as cur:
                    pool = await cur.fetchall()
            if pool:
                weights = [max(1e-9, _eval_weight(r[4], num)) for r in pool]
                chosen  = random.choices(pool, weights=weights, k=1)[0]
                _, p_type, p_value, p_amount, _ = chosen
                prize_desc = await _give_counting_prize(
                    message.guild.id, message.author.id, p_type, p_value, p_amount)

                async with get_db() as db:
                    async with db.execute(
                        "SELECT prize_type, prize_value, prize_amount, label "
                        "FROM counting_special_prizes WHERE guild_id=? AND number=?",
                        (message.guild.id, num)) as cur:
                        specials = await cur.fetchall()
                special_parts = []
                for sp_type, sp_value, sp_amount, sp_label in specials:
                    sp_desc = await _give_counting_prize(
                        message.guild.id, message.author.id, sp_type, sp_value, sp_amount)
                    special_parts.append(sp_label or sp_desc)

                if prize_desc or special_parts:
                    ann_ch = bot.get_channel(ann_ch_id or ch_id)
                    if ann_ch:
                        lines = ([f"🎉 {message.author.mention} counted **{num:,}** and won **{prize_desc}**!"]
                                 if prize_desc else
                                 [f"🎉 {message.author.mention} counted **{num:,}**!"])
                        for sp in special_parts:
                            lines.append(f"✨ **Special prize:** {sp}!")
                        try: await ann_ch.send("\n".join(lines))
                        except Exception: pass
    else:
        await _counting_break(message, cur_count, double=False)

def _eval_weight(formula: str, n: int) -> float:
    """Evaluate a weight formula where {n} is the count number. Returns ≥ 0."""
    n = max(1, n)
    try:
        ns = {
            '__builtins__': {}, 'n': float(n),
            'sqrt': _math.sqrt, 'log': _math.log, 'log2': _math.log2,
            'log10': _math.log10, 'pow': _math.pow,
            'abs': abs, 'min': min, 'max': max,
            'floor': _math.floor, 'ceil': _math.ceil, 'pi': _math.pi,
        }
        result = eval(formula.replace('{n}', 'n'), ns)
        return max(0.0, float(result))
    except Exception:
        return 1.0

def _extract_count(text: str) -> int | None:
    """Pull the leading integer from a counting message, or None."""
    import re
    m = re.match(r'^\s*([0-9]+)', text.strip())
    return int(m.group(1)) if m else None

async def _give_counting_prize(guild_id: int, user_id: int,
                                prize_type: str, prize_value: str,
                                prize_amount: int) -> str:
    """Distribute one counting prize. Returns a human-readable description."""
    if prize_type == "balance":
        await add_balance(guild_id, user_id, prize_amount)
        return f"💰 {prize_amount:,} coins"
    elif prize_type == "exp":
        await add_exp(guild_id, user_id, prize_amount, is_bonus=True)
        return f"⭐ {prize_amount:,} EXP"
    elif prize_type == "tickets":
        await add_tickets(guild_id, user_id, prize_amount)
        return f"🎟 {prize_amount} ticket(s)"
    elif prize_type == "gamble_tokens":
        await inventory_add(guild_id, user_id, GAMBLE_TOKEN, prize_amount)
        return f"🎲 {prize_amount} Gamble Token(s)"
    elif prize_type == "vip_keys":
        await inventory_add(guild_id, user_id, VIP_CHEST_KEY, prize_amount)
        return f"🔑 {prize_amount} VIP Key(s)"
    elif prize_type == "item":
        qty = prize_amount or 1
        await inventory_add(guild_id, user_id, prize_value, qty)
        return f"🎒 {qty}x {prize_value}"
    elif prize_type == "nothing":
        return ""   # caller checks for empty string to stay silent
    else:           # custom
        return prize_value or "a special prize"

# ═══════════════════════════════════════════════════════
# EXP  (now guild-scoped)
# ═══════════════════════════════════════════════════════

last_message_exp: dict[tuple[int, int], float] = {}  # (guild_id, user_id) → timestamp

async def add_exp(guild_id: int, user_id: int, amount: int, is_bonus: bool = False):
    if amount > 0 and not is_bonus:
        await add_stat(guild_id, user_id, "total_exp", amount)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                (guild_id, user_id, amount, int(datetime.now(UTC).timestamp()), 1 if is_bonus else 0))
            await db.commit()

async def get_exp(guild_id: int, user_id: int) -> int:
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    async with get_db() as db:
        async with db.execute(
            "SELECT SUM(amount) FROM exp_history "
            "WHERE guild_id=? AND user_id=? AND timestamp>=?",
            (guild_id, user_id, week_ago)) as cur:
            row = await cur.fetchone()
    return max(row[0] or 0, 0)

async def get_level_exp(guild_id: int, user_id: int) -> int:
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    async with get_db() as db:
        async with db.execute(
            "SELECT SUM(amount) FROM exp_history "
            "WHERE guild_id=? AND user_id=? AND timestamp>=? AND amount>0 AND is_bonus=0",
            (guild_id, user_id, week_ago)) as cur:
            row = await cur.fetchone()
    return max(row[0] or 0, 0)

async def get_level(guild_id: int, user_id: int) -> int:
    return min((await get_level_exp(guild_id, user_id)) // LEVEL_DIVISOR + 1, 100)

async def _add_chest_spending(guild_id: int, user_id: int, amount: int):
    """
    Record chest-spending by inserting negative entries whose timestamps
    match the oldest positive entries they consume, so both sides of the
    deduction expire at the same time and can never leave a phantom
    negative balance.
    """
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    remaining = amount
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT rowid, amount, timestamp FROM exp_history "
                "WHERE guild_id=? AND user_id=? AND timestamp>=? AND amount>0 "
                "ORDER BY timestamp ASC",
                (guild_id, user_id, week_ago)) as cur:
                entries = await cur.fetchall()
            for rowid, entry_amount, entry_ts in entries:
                if remaining <= 0:
                    break
                consume = min(entry_amount, remaining)
                await db.execute(
                    "INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) "
                    "VALUES(?,?,?,?,?)",
                    (guild_id, user_id, -consume, entry_ts, 0))
                remaining -= consume
            await db.commit()

# ═══════════════════════════════════════════════════════
# INVENTORY  (now guild-scoped)
# ═══════════════════════════════════════════════════════

async def inventory_add(guild_id: int, user_id: int, item_name: str, quantity: int = 1):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO inventory(guild_id,user_id,item_name,quantity) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id,user_id,item_name) DO UPDATE SET quantity=quantity+excluded.quantity",
                (guild_id, user_id, item_name, quantity))
            await db.commit()

async def inventory_remove(guild_id: int, user_id: int, item_name: str, quantity: int = 1) -> bool:
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT quantity FROM inventory WHERE guild_id=? AND user_id=? AND item_name=?",
                (guild_id, user_id, item_name)) as cur:
                row = await cur.fetchone()
            if not row or row[0] < quantity:
                return False
            new_qty = row[0] - quantity
            if new_qty == 0:
                await db.execute(
                    "DELETE FROM inventory WHERE guild_id=? AND user_id=? AND item_name=?",
                    (guild_id, user_id, item_name))
            else:
                await db.execute(
                    "UPDATE inventory SET quantity=? WHERE guild_id=? AND user_id=? AND item_name=?",
                    (new_qty, guild_id, user_id, item_name))
            await db.commit()
    return True

async def inventory_get(guild_id: int, user_id: int) -> list[tuple[str, int]]:
    async with get_db() as db:
        async with db.execute(
            "SELECT item_name,quantity FROM inventory "
            "WHERE guild_id=? AND user_id=? ORDER BY item_name",
            (guild_id, user_id)) as cur:
            return await cur.fetchall()

# ═══════════════════════════════════════════════════════
# ITEM STORE  (now guild-scoped)
# ═══════════════════════════════════════════════════════

async def add_item(guild_id: int, item_name: str, price: int, role_id: int, description: str):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO item_store VALUES(?,?,?,?,?)",
                (guild_id, item_name, price, role_id, description))
            await db.commit()

async def remove_item(guild_id: int, item_name: str):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM item_store WHERE guild_id=? AND item_name=?", (guild_id, item_name))
            await db.commit()

async def get_item(guild_id: int, item_name: str):
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM item_store WHERE guild_id=? AND LOWER(item_name)=LOWER(?)",
            (guild_id, item_name)) as cur:
            return await cur.fetchone()

async def get_all_items(guild_id: int) -> list:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM item_store WHERE guild_id=?", (guild_id,)) as cur:
            return await cur.fetchall()

# ═══════════════════════════════════════════════════════
# GAMBLE TOKENS  (now guild-scoped)
# ═══════════════════════════════════════════════════════

async def get_gamble_tokens(guild_id: int, user_id: int) -> int:
    inv   = await inventory_get(guild_id, user_id)
    owned = {n.lower(): q for n, q in inv}
    return owned.get(GAMBLE_TOKEN.lower(), 0)

# ═══════════════════════════════════════════════════════
# RAFFLE HELPERS
# ═══════════════════════════════════════════════════════

async def get_tickets(guild_id, user_id):
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT tickets FROM raffle WHERE guild_id=? AND user_id=?",
                                  (guild_id, user_id)) as cur:
                row = await cur.fetchone()
            if not row:
                await db.execute("INSERT INTO raffle VALUES(?,?,?)", (guild_id, user_id, 0))
                await db.commit()
                return 0
            return row[0]

async def add_tickets(guild_id, user_id, amount):
    tickets = await get_tickets(guild_id, user_id)
    new_tickets = max(0, tickets + amount)
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE raffle SET tickets=? WHERE guild_id=? AND user_id=?",
                             (new_tickets, guild_id, user_id))
            await db.commit()

# --- MESSAGE HELPERS --------------------------

def _bump_msg_count(guild_id: int, user_id: int):
    global _msg_buf_date
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if today != _msg_buf_date:
        _msg_buf.clear()
        _msg_buf_date = today
    key = (guild_id, user_id)
    _msg_buf[key] = _msg_buf.get(key, 0) + 1

async def _get_today_msg_count(guild_id: int, user_id: int) -> int:
    """Return total messages sent today: flushed DB rows + unflushed in-memory buffer."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    mem = _msg_buf.get((guild_id, user_id), 0) if _msg_buf_date == today else 0
    async with get_db() as db:
        async with db.execute(
            "SELECT count FROM daily_message_counts WHERE guild_id=? AND user_id=? AND date=?",
            (guild_id, user_id, today)) as cur:
            row = await cur.fetchone()
    return mem + (row[0] if row else 0)

async def _msg_count_flush_loop():
    """Persist the in-memory message count buffer to DB every 2 minutes."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(120)
        if not _msg_buf:
            continue
        today = _msg_buf_date or datetime.now(UTC).strftime("%Y-%m-%d")
        snapshot = {k: v for k, v in _msg_buf.items() if v > 0}
        for k in snapshot:
            _msg_buf.pop(k, None)
        if not snapshot:
            continue
        async with db_lock:
            async with get_db() as db:
                for (gid, uid), cnt in snapshot.items():
                    await db.execute(
                        "INSERT INTO daily_message_counts(guild_id,user_id,date,count) "
                        "VALUES(?,?,?,?) "
                        "ON CONFLICT(guild_id,user_id,date) DO UPDATE SET count=count+?",
                        (gid, uid, today, cnt, cnt))
                await db.commit()

# ═══════════════════════════════════════════════════════
# ACTIVE GAME SESSIONS
# ═══════════════════════════════════════════════════════

active_game_sessions: dict[int, dict] = {}
game_tasks:           dict[int, asyncio.Task] = {}

# ═══════════════════════════════════════════════════════
# GIVEAWAY TIMER
# ═══════════════════════════════════════════════════════

async def giveaway_timer(message_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        await end_giveaway(message_id)
    except Exception as e:
        print(f"[GiveawayTimer] {message_id}: {e}")

async def _send_hint_at(channel: discord.TextChannel, hint_text: str,
                         delay_secs: float, stop_event: asyncio.Event):
    """Wait delay_secs, then post a hint — unless the game was already answered."""
    try:
        await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=delay_secs)
        # Event fired before timeout: game already answered, skip hint
    except asyncio.TimeoutError:
        if not stop_event.is_set():
            await channel.send(f"💡 **Hint:** {hint_text}")
    except (asyncio.CancelledError, Exception):
        pass

# ═══════════════════════════════════════════════════════
# ON_MESSAGE  (game guesses + EXP with boosts)
# ═══════════════════════════════════════════════════════

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.guild:
        _bump_msg_count(message.guild.id, message.author.id)
        session = active_game_sessions.get(message.guild.id)
        if session and not session.get("answered") and message.channel.id == session.get("channel_id"):
            if message.content.strip().lower() == session["answer"].lower():
                session["answered"] = True
                session["winner"] = message.author
                if "event" in session:
                    session["event"].set()
        await _process_counting(message)
    now = datetime.now().timestamp()
    key = (message.guild.id, message.author.id)
    last_time = last_message_exp.get(key, 0)
    if now - last_time >= 30:
        content_length = len(message.content.strip())
        gained = min(50, 30 + random.randint(0, max(1, min(20, content_length // 10))))
        if message.guild and isinstance(message.author, discord.Member):
            member_role_ids = {role.id for role in message.author.roles}
            if member_role_ids:
                placeholders = ",".join("?" * len(member_role_ids))
                async with get_db() as db:
                    _ch_id  = message.channel.id
                    _cat_id = message.channel.category_id or 0
                    async with db.execute(
                        f"SELECT boost_percent FROM exp_boosts "
                        f"WHERE guild_id=? AND role_id IN ({placeholders}) "
                        f"AND ((channel_id=0 AND category_id=0)"      # global
                        f"  OR channel_id=?"                           # this channel
                        f"  OR (category_id!=0 AND category_id=?))",   # this category (!=0 avoids false match)
                        (message.guild.id, *member_role_ids, _ch_id, _cat_id)) as cur:
                        boost_rows = await cur.fetchall()
                if boost_rows:
                    total_boost = sum(r[0] for r in boost_rows)
                    gained = max(0, int(gained * (1 + total_boost / 100)))
        await add_exp(message.guild.id, message.author.id, gained)
        last_message_exp[key] = now
    if message.content.startswith(_BOT_PREFIX) and not _prefix_channel_allowed(message):
        return   # prefix commands are blocked for this author in this channel
    await bot.process_commands(message)

# WELCOME MESSAGE

@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return

    # ── join log ──────────────────────────────────────────────────────────
    await log_event(member.guild.id, "join", _log_embed(
        "📥 Member Joined", discord.Color.green(),
        Member=f"{member} ({member.mention})",
        Account_Age=f"<t:{int(member.created_at.timestamp())}:R>",
        ID=str(member.id),
        Member_Count=str(member.guild.member_count)))

    # ── welcome config ────────────────────────────────────────────────────
    async with get_db() as db:
        async with db.execute(
            "SELECT enabled, message, channel_id, channel_enabled, channel_message "
            "FROM welcome_config WHERE guild_id=?",
            (member.guild.id,)) as cur:
            row = await cur.fetchone()

    if not row:
        return

    dm_enabled, dm_message, ch_id, ch_enabled, ch_message = row

    # ── DM welcome ────────────────────────────────────────────────────────
    if dm_enabled and dm_message:
        text = (dm_message
                .replace("{member}", member.mention)
                .replace("{server}", member.guild.name))
        embed = discord.Embed(description=text, color=discord.Color.blurple())
        embed.set_author(
            name=member.guild.name,
            icon_url=member.guild.icon.url if member.guild.icon else None)
        try:
            await member.send(embed=embed, view=_WelcomeView(member.guild.name))
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"[Welcome DM] {member} / {member.guild.name}: {e}")

    # ── channel welcome ───────────────────────────────────────────────────
    if ch_enabled and ch_id:
        ch = member.guild.get_channel(ch_id)
        if ch:
            fallback = "Welcome {member} to **{server}**! 🎉"
            msg_text = (ch_message or dm_message or fallback)
            msg_text  = msg_text.replace("{member}", member.mention).replace("{server}", member.guild.name)
            embed_ch  = discord.Embed(description=msg_text, color=discord.Color.green())
            embed_ch.set_author(
                name=member.guild.name,
                icon_url=member.guild.icon.url if member.guild.icon else None)
            embed_ch.set_thumbnail(url=member.display_avatar.url)
            embed_ch.set_footer(text=f"Member #{member.guild.member_count}")
            try:
                await ch.send(member.mention, embed=embed_ch)
            except Exception as e:
                print(f"[Welcome Channel] {member} / {member.guild.name}: {e}")
    # ── Verification: assign unverified role ──────────────────────────────────
    async with get_db() as db:
        async with db.execute(
            "SELECT unverified_role_id FROM verification_config WHERE guild_id=?",
            (member.guild.id,)) as cur:
            ver_cfg = await cur.fetchone()
    if ver_cfg and ver_cfg[0]:
        unver_role = member.guild.get_role(ver_cfg[0])
        if unver_role:
            try:
                await member.add_roles(unver_role, reason="Verification: new member")
            except Exception as e:
                print(f"[Verification] {member} / {member.guild.name}: {e}")

@bot.event
async def on_member_remove(member: discord.Member):
    if member.bot:
        return
    joined = (f"<t:{int(member.joined_at.timestamp())}:R>"
              if member.joined_at else "Unknown")
    roles = [r.mention for r in member.roles[1:]]   # skip @everyone
    roles_str = ", ".join(roles[:12]) + (f" +{len(roles)-12} more" if len(roles) > 12 else "")
    await log_event(member.guild.id, "leave", _log_embed(
        "📤 Member Left", discord.Color.orange(),
        Member=f"{member} ({member.id})",
        Joined=joined,
        Roles=roles_str or "None"))

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # Only care about the counting bot
    if payload.user_id != COUNTING_BOT_ID or not payload.guild_id:
        return

    # Fail emojis mean the count was wrong — no prize
    e     = payload.emoji
    e_str = str(e)
    if e_str in _COUNTING_FAIL_EMOJI:
        return
    if e.name and e.name.lower() in ('x', 'cross_mark', 'warning'):
        return

    # Check counting config
    async with get_db() as db:
        async with db.execute(
            "SELECT enabled, channel_id, announce_channel_id "
            "FROM counting_config WHERE guild_id=?",
            (payload.guild_id,)) as cur:
            cfg = await cur.fetchone()
    if not cfg or not cfg[0]:
        return
    _, cfg_channel, cfg_announce = cfg
    if cfg_channel and payload.channel_id != cfg_channel:
        return

    # Fetch the message (to get author + content)
    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        return
    if message.author.bot:
        return

    count_n = _extract_count(message.content)
    if count_n is None:
        return

    gid  = payload.guild_id
    user = message.author

    # Load prize pool and compute weights for this count
    async with get_db() as db:
        async with db.execute(
            "SELECT id, prize_type, prize_value, prize_amount, weight_formula "
            "FROM counting_prizes WHERE guild_id=?", (gid,)) as cur:
            pool = await cur.fetchall()
    if not pool:
        return

    weights = [max(1e-9, _eval_weight(row[4], count_n)) for row in pool]
    chosen  = random.choices(pool, weights=weights, k=1)[0]
    _, p_type, p_value, p_amount, _ = chosen
    prize_desc = await _give_counting_prize(gid, user.id, p_type, p_value, p_amount)

    # Check for special prizes at this exact number
    async with get_db() as db:
        async with db.execute(
            "SELECT prize_type, prize_value, prize_amount, label "
            "FROM counting_special_prizes WHERE guild_id=? AND number=?",
            (gid, count_n)) as cur:
            specials = await cur.fetchall()

    special_parts = []
    for sp_type, sp_value, sp_amount, sp_label in specials:
        sp_desc = await _give_counting_prize(gid, user.id, sp_type, sp_value, sp_amount)
        special_parts.append(sp_label or sp_desc)

    # Nothing + no special → silent
    if not prize_desc and not special_parts:
        return

    # Build announcement
    if prize_desc:
        lines = [f"🎉 {user.mention} counted **{count_n:,}** and won **{prize_desc}**!"]
    else:
        lines = [f"🎉 {user.mention} counted **{count_n:,}**!"]
    for sp in special_parts:
        lines.append(f"✨ **Special prize:** {sp}!")

    announce_ch = bot.get_channel(cfg_announce) if cfg_announce else channel
    if announce_ch:
        try:
            await announce_ch.send("\n".join(lines))
        except Exception:
            pass

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if not payload.guild_id:
        return
    async with get_db() as db:
        async with db.execute(
            "SELECT channel_id FROM counting_config WHERE guild_id=? AND enabled=1",
            (payload.guild_id,)) as cur:
            cfg = await cur.fetchone()
    if not cfg or cfg[0] != payload.channel_id:
        return

    async with get_db() as db:
        async with db.execute(
            "SELECT current_count, last_message_id, notify_message_id "
            "FROM counting_state WHERE guild_id=?", (payload.guild_id,)) as cur:
            state = await cur.fetchone()
    if not state:
        return
    cur_count, last_msg_id, notify_msg_id = state

    channel = bot.get_channel(payload.channel_id)
    if not channel or cur_count == 0:
        return

    async def _resend_notify():
        try:
            notify = await channel.send(
                f"🗑️ **{cur_count}** was the last number but was deleted. "
                f"The next number is **{cur_count + 1}**.")
            async with db_lock:
                async with get_db() as db:
                    await db.execute(
                        "UPDATE counting_state SET notify_message_id=? WHERE guild_id=?",
                        (notify.id, payload.guild_id))
                    await db.commit()
        except Exception:
            pass

    if payload.message_id == last_msg_id:
        await _resend_notify()
    elif notify_msg_id and payload.message_id == notify_msg_id:
        await _resend_notify()  # Bot's own notify was deleted — resend it

# ═══════════════════════════════════════════════════════
# READY EVENT
# ═══════════════════════════════════════════════════════

@bot.event
async def on_ready():
    await setup_database()
    await _load_prefix()
    await load_disabled_commands()   # ← restore persisted disabled commands
    await load_prefix_restrictions()
    # Register persistent views (must happen before any interaction can fire)
    bot.add_view(VerificationView())
    bot.add_view(ChestChannelView())
    # Add StatsChannelView to the persistent view registrations:
    bot.add_view(StatsChannelView())   # alongside VerificationView() and ChestChannelView()

    # Restore stats channel panels (add alongside the chest panel restore loop):
    try: await _refresh_stats_channel(_guild)
    except Exception as e: print(f"[StatsPanel restore] {_guild.name}: {e}")

    # Add to the task list:
    for task_fn in [..., _msg_count_flush_loop]:
        bot.loop.create_task(task_fn())

    # Restore verification embeds and chest panels for all guilds
    for _guild in bot.guilds:
        # Verification
        async with get_db() as db:
            async with db.execute(
                "SELECT channel_id FROM verification_config WHERE guild_id=?",
                (_guild.id,)) as cur:
                _ver = await cur.fetchone()
        if _ver and _ver[0]:
            _vch = bot.get_channel(_ver[0])
            if _vch:
                try: await _post_verification_embed(_guild, _vch)
                except Exception as e: print(f"[Verification restore] {_guild.name}: {e}")

        # Chest panel
        try: await _refresh_chest_channel(_guild)
        except Exception as e: print(f"[ChestPanel restore] {_guild.name}: {e}")
    # Resume any auto giveaway loops that were running before the restart
    async with get_db() as db:
        async with db.execute(
            "SELECT guild_id FROM auto_giveaway_config WHERE running=1") as cur:
            _ag_guilds = [r[0] for r in await cur.fetchall()]
    for _gid in _ag_guilds:
        auto_giveaway_tasks[_gid] = asyncio.create_task(auto_giveaway_loop(_gid))
        print(f"[AutoGiveaway] Resumed for guild {_gid}")
    # Sync to every guild the bot is currently in.
    # Guild-scoped syncs appear instantly (no 1-hour delay).
    # For 2–10 servers this completes in a few seconds total.
    ok, fail = 0, 0
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"[Sync] ✅  {guild.name} ({guild.id})")
            ok += 1
        except discord.HTTPException as e:
            print(f"[Sync] ❌  {guild.name} ({guild.id}): {e}")
            fail += 1

    # Clear global commands so nothing appears twice in any server.
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync(guild=None)

    print(f"[Sync] Done — {ok} succeeded, {fail} failed")
    print(f"Logged in as {bot.user}")

    for task_fn in [raffle_loop, giveaway_watcher, raffle_info_loop,
                    game_loop, daily_key_loop, daily_gamble_loop]:
        bot.loop.create_task(task_fn())


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Sync slash commands the moment the bot is added to a new server."""
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"[Sync] Joined + synced: {guild.name} ({guild.id})")
    except discord.HTTPException as e:
        print(f"[Sync] Failed on join for {guild.name}: {e}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return
    raise error

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Invalid argument: {error}")
    elif isinstance(error, commands.CommandNotFound):
        pass  # silently ignore unknown prefix commands
    else:
        raise error

# ═══════════════════════════════════════════════════════
# GIVEAWAY ROLES
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="addgiveawayrole", description="Allow a role to manage giveaways")
@app_commands.check(is_allowed_to_giveaway)
@command_enabled()
async def addgiveawayrole(interaction: discord.Interaction, role: discord.Role):
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO giveaway_roles VALUES(?,?)", (interaction.guild.id, role.id))
            await db.commit()
    await interaction.response.send_message(f"✅ {role.mention} can now manage giveaways.")

@bot.tree.command(name="removegiveawayrole", description="Remove giveaway permissions from a role")
@app_commands.check(is_allowed_to_giveaway)
@command_enabled()
async def removegiveawayrole(interaction: discord.Interaction, role: discord.Role):
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM giveaway_roles WHERE guild_id=? AND role_id=?",
                             (interaction.guild.id, role.id))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed giveaway permissions from {role.mention}")

# ═══════════════════════════════════════════════════════
# BALANCE COMMANDS
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="balance", description="Check a balance")
@command_enabled()
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    bal = await get_balance(interaction.guild.id, user.id)
    embed = discord.Embed(title=f"💰 {user.display_name}'s Balance",
                          description=f"{bal:,} coins", color=discord.Color.green())
    await interaction.response.send_message(embed=embed)

# ═══════════════════════════════════════════════════════
# EXP COMMANDS
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="activityrank", description="Check a user's Activity Rank and EXP")
@command_enabled()
async def level(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    gid    = interaction.guild.id
    exp    = await get_level_exp(gid, user.id)
    usable = await get_exp(gid, user.id)
    lvl    = await get_level(gid, user.id)
    embed = discord.Embed(title=f"⭐ {user.display_name}'s Activity Rank", color=discord.Color.gold())
    embed.add_field(name="Activity Rank",           value=str(lvl),    inline=False)
    embed.add_field(name="Total EXP (7d)",  value=f"{exp:,}",  inline=False)
    embed.add_field(name="Usable EXP",      value=f"{usable:,}", inline=False)
    await interaction.response.send_message(embed=embed)

# ═══════════════════════════════════════════════════════
# CREATE GIVEAWAY
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="giveaway", description="Create a giveaway")
@app_commands.describe(
    prize="Prize description", seconds="Duration in seconds",
    winners="Number of winners",
    reward_balance="Coin reward per winner",
    reward_exp="EXP reward per winner",
    reward_tickets="Raffle tickets per winner",
    reward_gamble_tokens="Gamble tokens per winner",
    reward_vip_keys="VIP Chest Keys per winner",
    reward_role="Role to give each winner (must be below your highest role)",
    reward_item="Item/box name per winner",
    reward_item_qty="How many of the item (default 1)",
    channel="Channel to post in", required_role="Required role to enter",
    template="Color (gold/red/blue/green)"
)
@command_enabled()
async def giveaway(
    interaction: discord.Interaction,
    prize: str, seconds: int, winners: int,
    reward_balance: int = 0, reward_exp: int = 0, reward_tickets: int = 0,
    reward_gamble_tokens: int = 0, reward_vip_keys: int = 0,
    reward_role: discord.Role = None,
    reward_item: str = None, reward_item_qty: int = 1,
    channel: discord.TextChannel = None,
    required_role: discord.Role = None, template: str = "gold"
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if seconds <= 0:
        await interaction.response.send_message("❌ Duration must be > 0 seconds.", ephemeral=True); return
    if reward_role and reward_role >= interaction.user.top_role:
        await interaction.response.send_message(
            f"❌ You can only give away roles below your highest role ({interaction.user.top_role.mention}).",
            ephemeral=True); return

    resolved_item = reward_item.strip() if reward_item else None
    if resolved_item and reward_item_qty < 1:
        reward_item_qty = 1

    target_channel = channel or interaction.channel
    end_time = datetime.now(UTC) + timedelta(seconds=seconds)

    reward_parts = []
    if reward_balance > 0:       reward_parts.append(f"💰 {reward_balance:,} coins")
    if reward_exp > 0:           reward_parts.append(f"⭐ {reward_exp:,} EXP")
    if reward_tickets > 0:       reward_parts.append(f"🎟 {reward_tickets} ticket(s)")
    if reward_gamble_tokens > 0: reward_parts.append(f"🎲 {reward_gamble_tokens} gamble token(s)")
    if reward_vip_keys > 0:      reward_parts.append(f"🔑 {reward_vip_keys} VIP key(s)")
    if reward_role:              reward_parts.append(f"👑 {reward_role.mention}")
    if resolved_item:            reward_parts.append(f"🎒 {reward_item_qty}x {resolved_item}")
    reward_summary = " + ".join(reward_parts) if reward_parts else "No reward"

    embed = discord.Embed(
        title="🎉 GIVEAWAY 🎉",
        description=(
            f"React with 🎉 to enter\n\n"
            f"**Prize:** {prize}\n**Reward:** {reward_summary}\n"
            f"**Winners:** {winners}\n**Ends:** <t:{int(end_time.timestamp())}:R>"
        ),
        color=TEMPLATES.get(template, discord.Color.gold())
    )
    if required_role:
        embed.add_field(name="Required Role", value=required_role.mention, inline=False)

    message = await target_channel.send(embed=embed)
    await message.add_reaction("🎉")

    prize_meta = json.dumps({
        "label": prize, "balance": reward_balance, "exp": reward_exp,
        "tickets": reward_tickets, "gamble_tokens": reward_gamble_tokens,
        "vip_keys": reward_vip_keys,
        "role_id": reward_role.id if reward_role else 0,
        "item": resolved_item,
        "item_qty": reward_item_qty if resolved_item else 0,
    })

    async with get_db() as db:
        await db.execute(
            "INSERT INTO giveaways(message_id,channel_id,prize,winners,reward,end_time,required_role,template,ended) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (message.id, target_channel.id, prize_meta, winners, reward_balance,
             int(end_time.timestamp()), required_role.id if required_role else 0, template, 0))
        await db.commit()

    await interaction.response.send_message("✅ Giveaway created.", ephemeral=True)
    asyncio.create_task(giveaway_timer(message.id, seconds))

# ═══════════════════════════════════════════════════════
# GIVEAWAY PRIZE DISTRIBUTOR (shared by end_giveaway + reroll)
# ═══════════════════════════════════════════════════════

async def distribute_prizes(guild, winners, meta):
    prize_balance     = int(meta.get("balance", 0))
    prize_exp         = int(meta.get("exp", 0))
    prize_tickets     = int(meta.get("tickets", 0))
    prize_gamble      = int(meta.get("gamble_tokens", 0))
    prize_vip_keys    = int(meta.get("vip_keys", 0))
    prize_role_id     = int(meta.get("role_id", 0))
    prize_item        = meta.get("item")
    prize_item_qty    = int(meta.get("item_qty", 1))
    for winner in winners:
        if prize_balance > 0:
            await add_balance(guild.id, winner.id, prize_balance)
        if prize_exp > 0:
            await add_exp(guild.id, winner.id, prize_exp)
        if prize_tickets > 0:
            await add_tickets(guild.id, winner.id, prize_tickets)
        if prize_gamble > 0:
            await inventory_add(guild.id, winner.id, GAMBLE_TOKEN, prize_gamble)
        if prize_vip_keys > 0:
            await inventory_add(guild.id, winner.id, VIP_CHEST_KEY, prize_vip_keys)
        if prize_role_id:
            role   = guild.get_role(prize_role_id)
            member = guild.get_member(winner.id)
            if role and member:
                try: await member.add_roles(role)
                except Exception: pass
        if prize_item:
            await inventory_add(guild.id, winner.id, prize_item, prize_item_qty)

def build_reward_summary(meta, guild=None) -> str:
    parts = []
    if int(meta.get("balance", 0)) > 0:       parts.append(f"💰 {int(meta['balance']):,} coins")
    if int(meta.get("exp", 0)) > 0:           parts.append(f"⭐ {int(meta['exp']):,} EXP")
    if int(meta.get("tickets", 0)) > 0:       parts.append(f"🎟 {meta['tickets']} ticket(s)")
    if int(meta.get("gamble_tokens", 0)) > 0: parts.append(f"🎲 {meta['gamble_tokens']} gamble token(s)")
    if int(meta.get("vip_keys", 0)) > 0:      parts.append(f"🔑 {meta['vip_keys']} VIP key(s)")
    if int(meta.get("role_id", 0)) > 0 and guild:
        role = guild.get_role(int(meta["role_id"]))
        if role: parts.append(f"👑 {role.mention}")
    if meta.get("item"):                       parts.append(f"🎒 {meta['item_qty']}x {meta['item']}")
    return " + ".join(parts) if parts else "No reward"

# ═══════════════════════════════════════════════════════
# END GIVEAWAY
# ═══════════════════════════════════════════════════════

async def end_giveaway(message_id, reroll=False):
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT message_id,channel_id,prize,winners,reward,end_time,required_role,template,ended "
                "FROM giveaways WHERE message_id=?", (message_id,)) as cur:
                data = await cur.fetchone()
            if not data:
                print(f"[Giveaway] Not found: {message_id}"); return
            (message_id, channel_id, prize_raw, winner_count, legacy_reward,
             end_time, required_role, template, ended) = data
            if ended and not reroll:
                print(f"[Giveaway] Already ended: {message_id}"); return
            if not reroll:
                await db.execute("UPDATE giveaways SET ended=1 WHERE message_id=?", (message_id,))
                await db.commit()

    try:
        _parsed = json.loads(prize_raw)
        if not isinstance(_parsed, dict):
            raise TypeError
        meta        = _parsed
        prize_label = meta.get("label", prize_raw)
    except (json.JSONDecodeError, TypeError, AttributeError):
        meta        = {"label": str(prize_raw), "balance": legacy_reward}
        prize_label = str(prize_raw)

    channel = bot.get_channel(channel_id)
    if not channel: print(f"[Giveaway] Channel not found: {channel_id}"); return
    try:
        message = await channel.fetch_message(message_id)
    except Exception as e:
        print(f"[Giveaway] Fetch failed {message_id}: {e}"); return

    reaction = next((r for r in message.reactions if str(r.emoji) == "🎉"), None)
    if not reaction:
        await channel.send("❌ Giveaway reaction was missing."); return

    users = []
    async for user in reaction.users():
        if user.bot: continue
        member = channel.guild.get_member(user.id)
        if not member: continue
        if required_role and required_role not in {r.id for r in member.roles}: continue
        users.append(user)
    # ── Auto-entry: include opted-in users (re-validates role at draw time) ──────
    async with get_db() as db:
        async with db.execute(
            "SELECT user_id FROM auto_entry_users WHERE guild_id=? AND enabled=1",
            (channel.guild.id,)) as cur:
            auto_uids = {r[0] for r in await cur.fetchall()}
        async with db.execute(
            "SELECT role_id FROM auto_entry_roles WHERE guild_id=?",
            (channel.guild.id,)) as cur:
            auto_role_ids = {r[0] for r in await cur.fetchall()}
    existing_uids = {u.id for u in users}
    for auid in auto_uids:
        if auid in existing_uids:
            continue
        ae_member = channel.guild.get_member(auid)
        if not ae_member or ae_member.bot:
            continue
        member_rids = {r.id for r in ae_member.roles}
        if auto_role_ids and not (auto_role_ids & member_rids):
            continue  # Lost the eligible role since they enabled auto-entry
        if required_role and required_role not in member_rids:
            continue
        users.append(ae_member)
        
     if not users:
        await channel.send("No valid participants."); return

    weighted = []
    for user in users:
        lvl = await get_level(channel.guild.id, user.id)
        weighted.extend([user] * random.randint(1, max(1, lvl // 10)))

    winners = []
    while len(winners) < min(winner_count, len(users)) and weighted:
        s = random.choice(weighted)
        if s not in winners: winners.append(s)

    if reroll:
        async with db_lock:
            async with get_db() as db:
                async with db.execute("SELECT winner_id, reward FROM giveaway_winners WHERE message_id=?",
                                      (message_id,)) as cur:
                    old = await cur.fetchone()
                await db.commit()
                if old:
                    await add_balance(channel.guild.id, old[0], -old[1])

    async with db_lock:
        async with get_db() as db:
            for w in winners:
                await db.execute("INSERT OR REPLACE INTO giveaway_winners VALUES(?,?,?)",
                                 (message_id, w.id, int(meta.get("balance", 0))))
            await db.commit()

    await distribute_prizes(channel.guild, winners, meta)

    reward_summary  = build_reward_summary(meta, channel.guild)
    winner_mentions = ", ".join(w.mention for w in winners)
    embed = discord.Embed(
        title="🎊 Giveaway Ended",
        description=f"**Prize:** {prize_label}\n**Reward:** {reward_summary}\n**Winners:** {winner_mentions}",
        color=discord.Color.green())
    await channel.send(embed=embed)

# ═══════════════════════════════════════════════════════
# AUTO GIVEAWAY
# ═══════════════════════════════════════════════════════

auto_giveaway_tasks: dict[int, asyncio.Task] = {}   # guild_id → task

async def auto_giveaway_loop(guild_id: int):
    await bot.wait_until_ready()
    while not bot.is_closed():
        # Load config fresh each cycle (handles /stopgiveaways gracefully)
        async with get_db() as db:
            async with db.execute(
                "SELECT channel_id, interval_seconds, duration_seconds, running "
                "FROM auto_giveaway_config WHERE guild_id=?", (guild_id,)) as cur:
                cfg = await cur.fetchone()
        if not cfg or not cfg[3]:   # config gone or running=0
            auto_giveaway_tasks.pop(guild_id, None)
            break
        channel_id, interval_secs, duration_secs, _ = cfg

        channel = bot.get_channel(channel_id)
        if not channel:
            await asyncio.sleep(30); continue

        # Load pool and pick one entry by weight
        async with get_db() as db:
            async with db.execute(
                "SELECT id,prize,winners,chance,reward_balance,reward_exp,"
                "reward_tickets,reward_gamble_tokens,reward_vip_keys,"
                "reward_role_id,reward_item,reward_item_qty "
                "FROM auto_giveaway_pool WHERE guild_id=?", (guild_id,)) as cur:
                pool = await cur.fetchall()
        if not pool:
            await asyncio.sleep(interval_secs); continue

        gd = random.choices(pool, weights=[r[3] for r in pool], k=1)[0]
        (_id, prize, winners, chance,
         rb, re, rt, rgt, rvk, rrole, ri, riq) = gd

        guild      = bot.get_guild(guild_id)
        end_time   = datetime.now(UTC) + timedelta(seconds=duration_secs)

        reward_parts = []
        if rb  > 0: reward_parts.append(f"💰 {rb:,} coins")
        if re  > 0: reward_parts.append(f"⭐ {re:,} EXP")
        if rt  > 0: reward_parts.append(f"🎟 {rt} ticket(s)")
        if rgt > 0: reward_parts.append(f"🎲 {rgt} gamble token(s)")
        if rvk > 0: reward_parts.append(f"🔑 {rvk} VIP key(s)")
        if rrole and guild:
            role = guild.get_role(rrole)
            if role: reward_parts.append(f"👑 {role.mention}")
        if ri:      reward_parts.append(f"🎒 {riq}x {ri}")
        reward_summary = " + ".join(reward_parts) if reward_parts else "No reward"

        embed = discord.Embed(
            title="🎉 AUTOMATIC GIVEAWAY 🎉",
            description=(
                f"React with 🎉 to enter\n\n"
                f"**Prize:** {prize}\n**Reward:** {reward_summary}\n"
                f"**Winners:** {winners}\n**Ends:** <t:{int(end_time.timestamp())}:R>"
            ),
            color=discord.Color.gold())
        msg = await channel.send(embed=embed)
        await msg.add_reaction("🎉")

        prize_meta = json.dumps({
            "label": prize, "balance": rb, "exp": re,
            "tickets": rt, "gamble_tokens": rgt, "vip_keys": rvk,
            "role_id": rrole, "item": ri, "item_qty": riq if ri else 0,
        })
        async with get_db() as db:
            await db.execute(
                "INSERT INTO giveaways(message_id,channel_id,prize,winners,reward,"
                "end_time,required_role,template,ended) VALUES(?,?,?,?,?,?,?,?,?)",
                (msg.id, channel_id, prize_meta, winners, rb,
                 int(end_time.timestamp()), 0, "gold", 0))
            await db.commit()

        asyncio.create_task(giveaway_timer(msg.id, duration_secs))
        await asyncio.sleep(interval_secs)

@bot.tree.command(name="addautogiveaway", description="Add a giveaway to the auto pool")
@app_commands.describe(
    prize="Prize description",
    winners="Number of winners (default 1)",
    chance="Selection weight — higher = picked more often (default 1.0)",
    reward_balance="Coin reward per winner",
    reward_exp="EXP reward per winner",
    reward_tickets="Raffle tickets per winner",
    reward_gamble_tokens="Gamble tokens per winner",
    reward_vip_keys="VIP Chest Keys per winner",
    reward_role="Role to give each winner",
    reward_item="Item or box name per winner",
    reward_item_qty="Quantity of item reward (default 1)"
)
@command_enabled()
async def addautogiveaway(
    interaction: discord.Interaction,
    prize: str, winners: int = 1, chance: float = 1.0,
    reward_balance: int = 0, reward_exp: int = 0,
    reward_tickets: int = 0, reward_gamble_tokens: int = 0,
    reward_vip_keys: int = 0, reward_role: discord.Role = None,
    reward_item: str = None, reward_item_qty: int = 1
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if winners < 1:
        await interaction.response.send_message("❌ Winners must be ≥ 1.", ephemeral=True); return
    if chance <= 0:
        await interaction.response.send_message("❌ Chance must be > 0.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            cur = await db.execute(
                "INSERT INTO auto_giveaway_pool(guild_id,prize,winners,chance,"
                "reward_balance,reward_exp,reward_tickets,reward_gamble_tokens,"
                "reward_vip_keys,reward_role_id,reward_item,reward_item_qty) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (interaction.guild.id, prize, winners, chance,
                 reward_balance, reward_exp, reward_tickets, reward_gamble_tokens,
                 reward_vip_keys, reward_role.id if reward_role else 0,
                 reward_item, reward_item_qty))
            new_id = cur.lastrowid
            await db.commit()
    parts = []
    if reward_balance > 0:       parts.append(f"💰 {reward_balance:,}")
    if reward_exp > 0:           parts.append(f"⭐ {reward_exp:,} EXP")
    if reward_tickets > 0:       parts.append(f"🎟 {reward_tickets}")
    if reward_gamble_tokens > 0: parts.append(f"🎲 {reward_gamble_tokens}")
    if reward_vip_keys > 0:      parts.append(f"🔑 {reward_vip_keys}")
    if reward_role:              parts.append(f"👑 {reward_role.mention}")
    if reward_item:              parts.append(f"🎒 {reward_item_qty}x {reward_item}")
    await interaction.response.send_message(
        f"✅ Added **{prize}** to auto pool (`#{new_id}`)\n"
        f"Winners: {winners} | Weight: {chance} | Reward: {' + '.join(parts) or 'None'}")

@bot.tree.command(name="startgiveaways", description="Start automatic giveaways")
@app_commands.describe(interval_seconds="Seconds between giveaways",
                       giveaway_duration_seconds="How long each lasts",
                       channel="Channel (default current)")
@command_enabled()
async def startgiveaways(interaction: discord.Interaction,
                         interval_seconds: int, giveaway_duration_seconds: int,
                         channel: discord.TextChannel = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid = interaction.guild.id
    if gid in auto_giveaway_tasks and not auto_giveaway_tasks[gid].done():
        await interaction.response.send_message("❌ Already running.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM auto_giveaway_pool WHERE guild_id=?", (gid,)) as cur:
            if (await cur.fetchone())[0] == 0:
                await interaction.response.send_message(
                    "❌ No auto giveaways in the pool. Use `/addautogiveaway` first.",
                    ephemeral=True); return
    target = channel or interaction.channel
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO auto_giveaway_config VALUES(?,?,?,?,?)",
                (gid, target.id, interval_seconds, giveaway_duration_seconds, 1))
            await db.commit()
    auto_giveaway_tasks[gid] = asyncio.create_task(auto_giveaway_loop(gid))
    await interaction.response.send_message(
        f"✅ Automatic giveaways started in {target.mention}!\n"
        f"Interval: **{interval_seconds}s** | Duration: **{giveaway_duration_seconds}s**")

# ═══════════════════════════════════════════════════════
# RAFFLE SYSTEM
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="buytickets", description="Buy raffle tickets (100 coins each)")
@command_enabled()
async def buytickets(interaction: discord.Interaction, amount: int):
    if not await is_system_enabled(interaction.guild.id, "raffle"):
        await interaction.response.send_message("❌ Raffle system is disabled.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0."); return
    price = amount * RAFFLE_TICKET_PRICE
    bal   = await get_balance(interaction.guild.id, interaction.user.id)
    if bal < price:
        await interaction.response.send_message("❌ Not enough balance."); return
    await add_balance(interaction.guild.id, interaction.user.id, -price)
    await add_tickets(interaction.guild.id, interaction.user.id, amount)
    user_tickets = await get_tickets(interaction.guild.id, interaction.user.id)
    async with get_db() as db:
        async with db.execute("SELECT SUM(tickets) FROM raffle WHERE guild_id=?",
                              (interaction.guild.id,)) as cur:
            total = (await cur.fetchone())[0] or 0
    chance = (user_tickets / total * 100) if total > 0 else 0
    await interaction.response.send_message(
        f"🎟 Bought {amount} tickets.\nYou now have **{user_tickets}** tickets.\n"
        f"Win chance: **{chance:.2f}%**")
    await add_stat(interaction.guild.id, interaction.user.id, "raffle_tickets_bought", amount)

@bot.tree.command(name="rafflechance", description="Check raffle tickets and win chance")
@command_enabled()
async def rafflechance(interaction: discord.Interaction, user: discord.Member = None):
    user    = user or interaction.user
    tickets = await get_tickets(interaction.guild.id, user.id)
    async with get_db() as db:
        async with db.execute("SELECT SUM(tickets) FROM raffle WHERE guild_id=?",
                              (interaction.guild.id,)) as cur:
            total = (await cur.fetchone())[0] or 0
    chance = (tickets / total * 100) if total > 0 else 0
    embed = discord.Embed(title="🎟 Raffle Stats", color=discord.Color.gold())
    embed.add_field(name="User",        value=user.mention,       inline=False)
    embed.add_field(name="Tickets",     value=f"{tickets:,}",     inline=False)
    embed.add_field(name="Win Chance",  value=f"{chance:.2f}%",   inline=False)
    embed.add_field(name="Total Pool",  value=f"{total:,}",       inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setrafflechannel", description="Set the channel where raffle winners are announced")
@command_enabled()
async def setrafflechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO raffle_config VALUES(?,?)",
                             (interaction.guild.id, channel.id))
            await db.commit()
    await interaction.response.send_message(f"✅ Raffle announcements → {channel.mention}")

async def raffle_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now    = datetime.now(UTC)
        target = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if now >= target: target += timedelta(days=1)
        print(f"[Raffle] Next draw in {(target-now).total_seconds():.0f}s")
        await asyncio.sleep((target - now).total_seconds())

        for guild in bot.guilds:
            if not await is_system_enabled(guild.id, "raffle"): continue
            async with get_db() as db:
                async with db.execute(
                    "SELECT user_id,tickets FROM raffle WHERE guild_id=? ORDER BY tickets DESC",
                    (guild.id,)) as cur:
                    entries = await cur.fetchall()
            if not entries:
                continue

            total   = sum(t for _, t in entries)
            pool    = []
            for uid, t in entries: pool.extend([uid] * t)
            winner_id      = random.choice(pool)
            winner_tickets = next((t for uid, t in entries if uid == winner_id), 0)
            top5           = entries[:5]   # already sorted desc by SQL

            # ── Save history ─────────────────────────────────────────
            async with db_lock:
                async with get_db() as db:
                    await db.execute(
                        "INSERT INTO raffle_history"
                        "(guild_id,draw_timestamp,winner_id,winner_tickets,total_tickets,top_json) "
                        "VALUES(?,?,?,?,?,?)",
                        (guild.id, int(datetime.now(UTC).timestamp()),
                         winner_id, winner_tickets, total,
                         json.dumps([[uid, t] for uid, t in top5])))
                    await db.commit()

            await add_balance(guild.id, winner_id, RAFFLE_PRIZE)

            async with get_db() as db:
                async with db.execute("SELECT channel_id FROM raffle_config WHERE guild_id=?",
                                      (guild.id,)) as cur:
                    row = await cur.fetchone()
            ann = bot.get_channel(row[0]) if row else guild.system_channel
            if ann:
                await ann.send(f"🎉 <@{winner_id}> won the daily raffle and will receive a huge pet!")
                await log_event(guild.id, "raffle", _log_embed(
                    "🎟 Daily Raffle Draw", discord.Color.gold(),
                    Winner=f"<@{winner_id}>", Guild=guild.name))

            async with db_lock:
                async with get_db() as db:
                    await db.execute("DELETE FROM raffle WHERE guild_id=?", (guild.id,))
                    await db.commit()

# ═══════════════════════════════════════════════════════
# CHEST PRIZE MANAGEMENT
# ═══════════════════════════════════════════════════════

async def get_chest_prizes(guild_id: int, chest_type: str) -> list[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT id,name,exp,balance,chance FROM chest_prizes WHERE guild_id=? AND chest_type=?",
            (guild_id, chest_type)) as cur:
            rows = await cur.fetchall()
    if rows:
        return [{"id": r[0], "name": r[1], "exp": r[2], "balance": r[3], "chance": r[4]} for r in rows]
    return DEFAULT_CHEST_PRIZES if chest_type == "chest" else DEFAULT_VIP_PRIZES

@bot.tree.command(name="addchestprize", description="Add a custom prize to the chest or VIP chest loot table")
@app_commands.describe(chest_type="'chest' or 'vipchest'", name="Prize name",
                       exp="EXP (0 for none)", balance="Balance (0 for none)", chance="Weight (e.g. 40)")
@app_commands.choices(chest_type=[
    app_commands.Choice(name="EXP Chest",  value="chest"),
    app_commands.Choice(name="VIP Chest",  value="vipchest")])
@command_enabled()
async def addchestprize(interaction: discord.Interaction, chest_type: str, name: str,
                        exp: int = 0, balance: int = 0, chance: float = 10.0):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if chance <= 0:
        await interaction.response.send_message("❌ Chance must be > 0.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO chest_prizes(guild_id,chest_type,name,exp,balance,chance) VALUES(?,?,?,?,?,?)",
                (interaction.guild.id, chest_type, name, exp, balance, chance))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Added **{name}** to **{chest_type}** (weight: {chance})\n"
        f"ℹ️ Custom prizes are now active — defaults are replaced for this server.")

@bot.tree.command(name="removechestprize", description="Remove a prize from the chest loot table by ID (see /listchestprizes)")
@app_commands.choices(chest_type=[
    app_commands.Choice(name="EXP Chest",  value="chest"),
    app_commands.Choice(name="VIP Chest",  value="vipchest")])
@command_enabled()
async def removechestprize(interaction: discord.Interaction, chest_type: str, prize_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT id FROM chest_prizes WHERE id=? AND guild_id=? AND chest_type=?",
                (prize_id, interaction.guild.id, chest_type)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Prize #{prize_id} not found.", ephemeral=True); return
            await db.execute("DELETE FROM chest_prizes WHERE id=?", (prize_id,))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed prize #{prize_id} from **{chest_type}**.")

# ═══════════════════════════════════════════════════════
# CHEST COMMAND
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="chest", description="Open EXP chest(s)")
@command_enabled()
async def chest(interaction: discord.Interaction, amount: int = 1):
    await interaction.response.defer()
    if amount <= 0:
        await interaction.followup.send("❌ Amount must be > 0."); return
    exp = await get_exp(interaction.guild.id, interaction.user.id)
    if exp >= 1400:
        amount = min(amount, exp // CHEST_COST)
    else:
        amount = 1
    total_cost = CHEST_COST * amount
    if exp < total_cost:
        await interaction.followup.send(f"❌ You need {total_cost:,} EXP (you have {exp:,})."); return

    prizes     = await get_chest_prizes(interaction.guild.id, "chest")
    rare_names = await get_rare_chest_names(interaction.guild.id, "chest")
    results: dict = {}
    total_balance = 0
    total_exp_won = 0
    for _ in range(amount):
        prize = random.choices(prizes, weights=[p["chance"] for p in prizes], k=1)[0]
        results[prize["name"]] = results.get(prize["name"], 0) + 1
        total_balance += prize["balance"]
        total_exp_won += prize["exp"]

    gid = interaction.guild.id
    await _add_chest_spending(gid, interaction.user.id, total_cost)
    if total_balance > 0: await add_balance(gid, interaction.user.id, total_balance)
    if total_exp_won > 0: await add_exp(gid, interaction.user.id, total_exp_won)
    await add_stat(gid, interaction.user.id, "chests_opened", amount)

    result_text = "\n".join(f"• {count}x {name}" for name, count in results.items())
    embed = discord.Embed(title="📦 Chest Results", description=result_text, color=discord.Color.purple())
    embed.set_footer(text=f"Opened {amount} chest(s) | Cost: {total_cost:,} EXP")
    await interaction.followup.send(embed=embed)
    results_log = ", ".join(f"{c}x {n}" for n, c in results.items())
    await log_event(gid, "chest", _log_embed(
        "📦 Chest Opened", discord.Color.purple(),
        User=interaction.user.mention, Opened=str(amount),
        Cost=f"{total_cost:,} EXP", Won=results_log[:1024]))


    rare_won = {n: c for n, c in results.items() if n in rare_names}
    if rare_won:
        rcid = await get_rare_drop_channel(interaction.guild.id)
        if rcid:
            rc = bot.get_channel(rcid)
            if rc:
                text = " and ".join(f"**{c}x {n}**" for n, c in rare_won.items())
                re = discord.Embed(title="🌟 Rare Drop!",
                    description=f"{interaction.user.mention} got {text} from a chest! 🎉",
                    color=discord.Color.gold())
                re.set_thumbnail(url=interaction.user.display_avatar.url)
                await rc.send(embed=re)

# ═══════════════════════════════════════════════════════
# VIP CHEST
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="vipchest", description="Open VIP Chest(s) — costs 1 VIP Chest Key each")
@command_enabled()
async def vipchest(interaction: discord.Interaction, amount: int = 1):
    if not await is_system_enabled(interaction.guild.id, "vipkey"):
        await interaction.response.send_message("❌ VIP chest system is disabled.", ephemeral=True); return
    await interaction.response.defer()
    if amount <= 0:   await interaction.followup.send("❌ Amount must be ≥ 1."); return
    if amount > 10:   await interaction.followup.send("❌ Max 10 VIP chests at once."); return

    inv  = await inventory_get(interaction.guild.id, interaction.user.id)
    owned = {n.lower(): q for n, q in inv}
    available_keys = owned.get(VIP_CHEST_KEY.lower(), 0)
    if available_keys < amount:
        await interaction.followup.send(
            f"❌ Need {amount}x **{VIP_CHEST_KEY}** but only have {available_keys}.\n"
            f"Keys are given by admins; Nitro Boosters get one daily!"); return
    if not await inventory_remove(interaction.guild.id, interaction.user.id, VIP_CHEST_KEY, amount):
        await interaction.followup.send("❌ Failed to consume keys."); return

    prizes     = await get_chest_prizes(interaction.guild.id, "vipchest")
    rare_names = await get_rare_chest_names(interaction.guild.id, "vipchest")
    results: dict = {}
    total_balance = 0
    total_exp_won = 0
    for _ in range(amount):
        prize = random.choices(prizes, weights=[p["chance"] for p in prizes], k=1)[0]
        results[prize["name"]] = results.get(prize["name"], 0) + 1
        total_balance += prize["balance"]
        total_exp_won += prize["exp"]

    if total_balance > 0: await add_balance(interaction.guild.id, interaction.user.id, total_balance)
    if total_exp_won > 0: await add_exp(interaction.guild.id, interaction.user.id, total_exp_won)

    result_text = "\n".join(f"• {count}x {name}" for name, count in results.items())
    embed = discord.Embed(title="💎 VIP Chest Results", description=result_text,
                          color=discord.Color.from_rgb(148, 0, 211))
    embed.set_footer(text=f"Opened {amount} VIP chest(s) | {available_keys - amount} key(s) remaining")
    await interaction.followup.send(embed=embed)
    results_log = ", ".join(f"{c}x {n}" for n, c in results.items())
    await log_event(interaction.guild.id, "chest", _log_embed(
        "💎 VIP Chest Opened", discord.Color.from_rgb(148, 0, 211),
        User=interaction.user.mention, Opened=str(amount),
        Keys_Used=str(amount), Won=results_log[:1024]))
    
    rare_won = {n: c for n, c in results.items() if n in rare_names}
    if rare_won:
        rcid = await get_rare_drop_channel(interaction.guild.id)
        if rcid:
            rc = bot.get_channel(rcid)
            if rc:
                text = " and ".join(f"**{c}x {n}**" for n, c in rare_won.items())
                re = discord.Embed(title="💎 VIP Rare Drop!",
                    description=f"{interaction.user.mention} got {text} from a **VIP Chest**! 👑",
                    color=discord.Color.from_rgb(148, 0, 211))
                re.set_thumbnail(url=interaction.user.display_avatar.url)
                await rc.send(embed=re)

async def daily_key_loop():
    """1 VIP Chest Key per day per Nitro Booster."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now    = datetime.now(UTC)
        target = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if now >= target: target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        for guild in bot.guilds:
            if not await is_system_enabled(guild.id, "vipkey"): continue
            for member in guild.members:
                if member.bot or member.premium_since is None: continue
                try:
                    async with db_lock:
                        async with get_db() as db:
                            try:
                                await db.execute(
                                    "INSERT INTO daily_key_log(guild_id,user_id,date) VALUES(?,?,?)",
                                    (guild.id, member.id, today))
                                await db.commit()
                            except aiosqlite.IntegrityError:
                                continue
                    await inventory_add(guild.id, member.id, VIP_CHEST_KEY, 1)
                except Exception as e:
                    print(f"[DailyKey] {member} / {guild.name}: {e}")

# ─── RARE CHEST DROP CONFIG ───────────────────────────────────────────────────

_CHEST_TYPE_CHOICES = [
    app_commands.Choice(name="EXP Chest",  value="chest"),
    app_commands.Choice(name="VIP Chest",  value="vipchest"),
]

@bot.tree.command(name="addrarechestdrop",
                  description="Mark a chest prize as a rare drop (triggers announcement)")
@app_commands.describe(chest_type="Which chest", prize="Prize name, or its numeric ID from /listchestprizes")
@app_commands.choices(chest_type=_CHEST_TYPE_CHOICES)
@command_enabled()
async def addrarechestdrop(interaction: discord.Interaction, chest_type: str, prize: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    prize = prize.strip()
    # If the user typed a numeric ID, resolve it to a name
    try:
        pid = int(prize)
        async with get_db() as db:
            async with db.execute(
                "SELECT name FROM chest_prizes WHERE id=? AND guild_id=? AND chest_type=?",
                (pid, interaction.guild.id, chest_type)) as cur:
                row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(
                f"❌ No custom prize #{pid} found in **{chest_type}**. "
                f"Use `/listchestprizes` to see IDs.", ephemeral=True); return
        prize = row[0]
    except ValueError:
        pass  # already a name string
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute(
                    "INSERT INTO rare_chest_config(guild_id, chest_type, prize_name) VALUES(?,?,?)",
                    (interaction.guild.id, chest_type, prize))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(
                    f"❌ **{prize}** is already a rare drop for **{chest_type}**.",
                    ephemeral=True); return
    label = "EXP Chest" if chest_type == "chest" else "VIP Chest"
    await interaction.response.send_message(
        f"✅ **{prize}** is now a rare drop for the **{label}**.\n"
        f"ℹ️ Once any custom rare drop is added, the hardcoded defaults are replaced for this server.")

@bot.tree.command(name="removerarechestdrop",
                  description="Unmark a chest prize as a rare drop")
@app_commands.describe(chest_type="Which chest", prize="Prize name, or its numeric ID from /listchestprizes")
@app_commands.choices(chest_type=_CHEST_TYPE_CHOICES)
@command_enabled()
async def removerarechestdrop(interaction: discord.Interaction, chest_type: str, prize: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    prize = prize.strip()
    try:
        pid = int(prize)
        async with get_db() as db:
            async with db.execute(
                "SELECT name FROM chest_prizes WHERE id=? AND guild_id=? AND chest_type=?",
                (pid, interaction.guild.id, chest_type)) as cur:
                row = await cur.fetchone()
        if row: prize = row[0]
    except ValueError:
        pass
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM rare_chest_config WHERE guild_id=? AND chest_type=? AND prize_name=?",
                (interaction.guild.id, chest_type, prize))
            await db.commit()
    label = "EXP Chest" if chest_type == "chest" else "VIP Chest"
    await interaction.response.send_message(f"🗑 **{prize}** removed from rare drops for **{label}**.")

# ═══════════════════════════════════════════════════════
# ITEM STORE
# ═══════════════════════════════════════════════════════

item_group = app_commands.Group(name="item", description="Item store commands")
bot.tree.add_command(item_group)

@item_group.command(name="add", description="Add item to store")
@command_enabled()
async def item_add(interaction: discord.Interaction, name: str, price: int,
                   role: discord.Role, description: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_item(interaction.guild.id, name, price, role.id, description)
    await interaction.response.send_message(f"✅ Added **{name}** to the store.")

@item_group.command(name="remove", description="Remove item from store")
@command_enabled()
async def item_remove(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if not await get_item(interaction.guild.id, name):
        await interaction.response.send_message("❌ Item not found."); return
    await remove_item(interaction.guild.id, name)
    await interaction.response.send_message(f"🗑 Removed **{name}** from the store.")

@item_group.command(name="info", description="View item or box info")
@command_enabled()
async def item_info(interaction: discord.Interaction, name: str):
    item = await get_item(interaction.guild.id, name)
    if item:
        _, item_name, price, role_id, description = item
        role  = interaction.guild.get_role(role_id)
        embed = discord.Embed(title=f"🛒 {item_name}", color=discord.Color.blurple())
        embed.add_field(name="Price",       value=f"{price:,} coins",                  inline=False)
        embed.add_field(name="Role",        value=role.mention if role else "?",        inline=False)
        embed.add_field(name="Description", value=description,                          inline=False)
        await interaction.response.send_message(embed=embed); return
    async with get_db() as db:
        async with db.execute(
            "SELECT box_name FROM abuse_boxes WHERE guild_id=? AND LOWER(box_name)=LOWER(?)",
            (interaction.guild.id, name)) as cur:
            box_row = await cur.fetchone()
    if box_row:
        box_name = box_row[0]
        async with get_db() as db:
            async with db.execute(
                "SELECT id,prize_type,prize_value,chance FROM abuse_box_prizes "
                "WHERE guild_id=? AND box_name=? ORDER BY id",
                (interaction.guild.id, box_name)) as cur:
                prizes = await cur.fetchall()
        embed = discord.Embed(title=f"📦 {box_name}", color=discord.Color.orange())
        if not prizes:
            embed.description = "*No prizes configured yet.*"
        else:
            total_w = sum(p[3] for p in prizes)
            lines = []
            for p_id, p_type, p_value, p_chance in prizes:
                pct = (p_chance / total_w * 100) if total_w > 0 else 0
                if p_type == "balance": desc = f"💰 {int(p_value):,} coins"
                elif p_type == "exp":   desc = f"⭐ {int(p_value):,} EXP"
                elif p_type == "item":  desc = f"🎒 {p_value}"
                else:                   desc = f"✨ {p_value}"
                lines.append(f"`#{p_id}` {desc} — **{pct:.1f}%**")
            embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed); return
    await interaction.response.send_message("❌ Item or box not found.")

@item_group.command(name="store", description="View item store")
@command_enabled()
async def item_store_cmd(interaction: discord.Interaction):
    items = await get_all_items(interaction.guild.id)
    if not items:
        await interaction.response.send_message("❌ Store is empty."); return
    embed = discord.Embed(title="🛒 Item Store", color=discord.Color.green())
    for _, item_name, price, role_id, description in items:
        role = interaction.guild.get_role(role_id)
        embed.add_field(name=item_name,
                        value=f"💰 {price:,} coins\n🎭 {role.mention if role else '?'}",
                        inline=False)
    await interaction.response.send_message(embed=embed)

@item_group.command(name="buy", description="Buy an item — goes to your inventory")
@command_enabled()
async def item_buy(interaction: discord.Interaction, name: str):
    item = await get_item(interaction.guild.id, name)
    if not item:
        await interaction.response.send_message("❌ Item not found."); return
    _, item_name, price, role_id, description = item
    bal = await get_balance(interaction.guild.id, interaction.user.id)
    if bal < price:
        await interaction.response.send_message("❌ Not enough balance."); return
    if not interaction.guild.get_role(role_id):
        await interaction.response.send_message("❌ Role no longer exists."); return
    await add_balance(interaction.guild.id, interaction.user.id, -price)
    await inventory_add(interaction.guild.id, interaction.user.id, item_name, 1)
    await interaction.response.send_message(
        f"✅ Bought **{item_name}** for {price:,} coins. Use `/item use {item_name}` to redeem!")

@item_group.command(name="use", description="Use a store item to receive its role")
@command_enabled()
async def item_use(interaction: discord.Interaction, name: str):
    item = await get_item(interaction.guild.id, name)
    if not item:
        await interaction.response.send_message("❌ Item not found."); return
    _, item_name, price, role_id, description = item
    inv   = await inventory_get(interaction.guild.id, interaction.user.id)
    owned = {n.lower(): q for n, q in inv}
    if owned.get(item_name.lower(), 0) < 1:
        await interaction.response.send_message(f"❌ You don't have **{item_name}** in your inventory."); return
    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.response.send_message("❌ Role no longer exists."); return
    member = interaction.guild.get_member(interaction.user.id)
    if role in member.roles:
        await interaction.response.send_message(f"❌ You already have **{role.name}**."); return
    if not await inventory_remove(interaction.guild.id, interaction.user.id, item_name, 1):
        await interaction.response.send_message("❌ Failed to remove item."); return
    await member.add_roles(role)
    await interaction.response.send_message(f"✅ Used **{item_name}** — you now have {role.mention}!")

@item_group.command(name="give", description="Give an item, box, or key to a user (admin only)")
@app_commands.describe(user="Target user", name="Item or box name", quantity="How many (default 1)")
@command_enabled()
async def item_give(interaction: discord.Interaction, user: discord.Member, name: str, quantity: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if quantity <= 0:
        await interaction.response.send_message("❌ Quantity must be ≥ 1.", ephemeral=True); return
    store_item = await get_item(interaction.guild.id, name)
    canonical  = store_item[1] if store_item else name.strip()   # [1] = item_name, not [0]
    if not store_item:
        async with get_db() as db:
            async with db.execute(
                "SELECT box_name FROM abuse_boxes WHERE guild_id=? AND LOWER(box_name)=LOWER(?)",
                (interaction.guild.id, name)) as cur:
                box_row = await cur.fetchone()
        if not box_row and name.strip() not in (VIP_CHEST_KEY, GAMBLE_TOKEN):
            await interaction.response.send_message(f"❌ **{name}** not found.", ephemeral=True); return
        if box_row:
            canonical = box_row[0]
    await inventory_add(interaction.guild.id, user.id, canonical, quantity)
    await interaction.response.send_message(f"✅ Gave **{quantity}x {canonical}** to {user.mention}.")

@item_group.command(name="take", description="Take an item, box, or key from a user (admin only)")
@app_commands.describe(user="Target user", name="Item or box name", quantity="How many (default 1)")
@command_enabled()
async def item_take(interaction: discord.Interaction, user: discord.Member, name: str, quantity: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if quantity <= 0:
        await interaction.response.send_message("❌ Quantity must be ≥ 1.", ephemeral=True); return
    store_item = await get_item(interaction.guild.id, name)
    canonical  = store_item[1] if store_item else name.strip()   # [1] = item_name
    if not store_item:
        async with get_db() as db:
            async with db.execute(
                "SELECT box_name FROM abuse_boxes WHERE guild_id=? AND LOWER(box_name)=LOWER(?)",
                (interaction.guild.id, name)) as cur:
                box_row = await cur.fetchone()
        if box_row: canonical = box_row[0]
    if not await inventory_remove(interaction.guild.id, user.id, canonical, quantity):
        await interaction.response.send_message(f"❌ {user.mention} doesn't have {quantity}x **{canonical}**."); return
    await interaction.response.send_message(f"🗑 Took **{quantity}x {canonical}** from {user.mention}.")

@item_group.command(name="inv", description="Check a user's inventory")
@app_commands.describe(user="User to check (defaults to yourself)")
@command_enabled()
async def item_inv(interaction: discord.Interaction, user: discord.Member = None):
    user  = user or interaction.user
    inv   = await inventory_get(interaction.guild.id, user.id)
    embed = discord.Embed(title=f"🎒 {user.display_name}'s Inventory", color=discord.Color.blurple())
    if not inv:
        embed.description = "Inventory is empty."
    else:
        lines = []
        for item_name, quantity in inv:
            si = await get_item(interaction.guild.id, item_name)
            if si:
                role   = interaction.guild.get_role(si[3])   # [3] = role_id (was [2] before guild_id was added)
                suffix = f" → {role.mention}" if role else ""
                lines.append(f"• **{item_name}** x{quantity}{suffix}")
            elif item_name == VIP_CHEST_KEY:
                lines.append(f"• 🔑 **{item_name}** x{quantity}")
            elif item_name == GAMBLE_TOKEN:
                lines.append(f"• 🎲 **{item_name}** x{quantity}")
            else:
                lines.append(f"• 📦 **{item_name}** x{quantity}")
        embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)

# ═══════════════════════════════════════════════════════
# ENABLE / DISABLE COMMANDS
# ═══════════════════════════════════════════════════════

async def load_disabled_commands():
    """Restore per-guild and global disabled commands from DB into memory."""
    async with get_db() as db:
        # Per-guild
        async with db.execute("SELECT guild_id, command_name FROM disabled_commands_persist") as cur:
            for guild_id, cmd in await cur.fetchall():
                disabled_commands.setdefault(guild_id, set()).add(cmd)
        # Global
        async with db.execute("SELECT command_name FROM global_disabled_commands") as cur:
            global_disabled_commands.update(r[0] for r in await cur.fetchall())
    total = sum(len(v) for v in disabled_commands.values()) + len(global_disabled_commands)
    if total:
        print(f"[DisabledCmds] Restored {total} disabled command(s)")

async def _load_prefix():
    global _BOT_PREFIX
    async with get_db() as db:
        async with db.execute("SELECT value FROM bot_config WHERE key='prefix'") as cur:
            row = await cur.fetchone()
    if row:
        _BOT_PREFIX = row[0]


async def _is_allowed_ctx(ctx: commands.Context) -> bool:
    """Permission check for prefix commands."""
    if ctx.author.id == BOT_OWNER_ID:
        return True
    if any(r.name.lower() == "bot developer" for r in ctx.author.roles):
        return True
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT role_id FROM giveaway_roles WHERE guild_id=?",
                                  (ctx.guild.id,)) as cur:
                rows = await cur.fetchall()
    allowed = {r[0] for r in rows}
    return any(role.id in allowed for role in ctx.author.roles)

async def load_prefix_restrictions():
    async with get_db() as db:
        async with db.execute(
            "SELECT guild_id,channel_id,role_id,allowed FROM prefix_restrictions") as cur:
            for gid, cid, rid, allowed in await cur.fetchall():
                key = (gid, cid)
                if key not in prefix_channel_rules:
                    prefix_channel_rules[key] = {}
                prefix_channel_rules[key][rid] = bool(allowed)
    total = sum(len(v) for v in prefix_channel_rules.values())
    if total:
        print(f"[PrefixRules] Loaded {total} rule(s) across {len(prefix_channel_rules)} channel(s)")


def _prefix_channel_allowed(message: discord.Message) -> bool:
    """True if this author may use prefix commands in this channel."""
    if not message.guild:
        return True
    if message.author.id == BOT_OWNER_ID:
        return True
    key   = (message.guild.id, message.channel.id)
    rules = prefix_channel_rules.get(key, {})
    if not rules:
        return True
    user_role_ids = (
        {role.id for role in message.author.roles}
        if isinstance(message.author, discord.Member) else set()
    )
    # Any explicitly-allowed role wins over everything else
    for rid in user_role_ids:
        if rules.get(rid) is True:
            return True
    # Check blocking rules
    if rules.get(0) is False:           # everyone is blocked
        return False
    for rid in user_role_ids:           # a specific role of this user is blocked
        if rules.get(rid) is False:
            return False
    return True


async def _do_reset(guild_id: int, user_id: int, reset_type: str):
    """Wipe one or all data categories for a single user in a guild."""
    async with db_lock:
        async with get_db() as db:
            if reset_type in ("balance", "all"):
                await db.execute(
                    "UPDATE balances SET balance=0 WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
            if reset_type in ("exp", "all"):
                await db.execute(
                    "DELETE FROM exp_history WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
            if reset_type in ("inventory", "all"):
                await db.execute(
                    "DELETE FROM inventory WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
            if reset_type in ("tickets", "all"):
                await db.execute(
                    "UPDATE raffle SET tickets=0 WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
            if reset_type in ("stats", "all"):
                await db.execute(
                    "UPDATE user_stats SET total_exp=0, gifted_balance=0, "
                    "chests_opened=0, raffle_tickets_bought=0 "
                    "WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
            await db.commit()

_UNDISABLEABLE = {"disablecmd", "enablecmd", "listcmds"}

@bot.tree.command(name="disablecmd", description="Disable a command in this server")
async def disablecmd(interaction: discord.Interaction, command_name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if command_name in _UNDISABLEABLE:
        await interaction.response.send_message(
            "❌ That command cannot be disabled.", ephemeral=True); return
    gid = interaction.guild.id
    disabled_commands.setdefault(gid, set()).add(command_name)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO disabled_commands_persist(guild_id,command_name) VALUES(?,?)",
                (gid, command_name))
            await db.commit()
    await interaction.response.send_message(f"🔒 `/{command_name}` disabled in this server.")

@bot.tree.command(name="enablecmd", description="Re-enable a command in this server")
async def enablecmd(interaction: discord.Interaction, command_name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid = interaction.guild.id
    if command_name not in disabled_commands.get(gid, set()):
        await interaction.response.send_message(
            f"ℹ️ `/{command_name}` is not disabled in this server.", ephemeral=True); return
    disabled_commands[gid].discard(command_name)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM disabled_commands_persist WHERE guild_id=? AND command_name=?",
                (gid, command_name))
            await db.commit()
    await interaction.response.send_message(f"🔓 `/{command_name}` re-enabled in this server.")

@bot.tree.command(name="listcmds", description="Show disabled commands in this server")
async def listcmds(interaction: discord.Interaction):
    gid          = interaction.guild.id if interaction.guild else 0
    local_off    = sorted(disabled_commands.get(gid, set()))
    global_off   = sorted(global_disabled_commands)
    embed = discord.Embed(title="🔒 Command Status", color=discord.Color.orange())
    embed.add_field(
        name="Disabled in this server",
        value=("\n".join(f"• `/{c}`" for c in local_off) if local_off else "None"),
        inline=False)
    embed.add_field(
        name="Disabled globally (all servers)",
        value=("\n".join(f"• `/{c}`" for c in global_off) if global_off else "None"),
        inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ═══════════════════════════════════════════════════════
# SYSTEM TOGGLES  (raffle / vipkey / gamble)
# ═══════════════════════════════════════════════════════

_SYSTEM_CHOICES = [
    app_commands.Choice(name="Raffle (buying tickets, daily draw)",      value="raffle"),
    app_commands.Choice(name="VIP Key (daily keys, vipchest command)",   value="vipkey"),
    app_commands.Choice(name="Gambling (tokens, blackjack, roulette)",   value="gamble"),
]
_SYSTEM_LABELS = {"raffle": "🎟 Raffle system", "vipkey": "🔑 VIP Key system", "gamble": "🎲 Gambling system"}

@bot.tree.command(name="enablesystem", description="Enable a major bot system")
@app_commands.describe(system="Which system to enable")
@app_commands.choices(system=_SYSTEM_CHOICES)
@command_enabled()
async def enablesystem(interaction: discord.Interaction, system: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await set_system_flag(interaction.guild.id, system, True)
    await interaction.response.send_message(f"✅ **{_SYSTEM_LABELS[system]}** is now **enabled**.")

@bot.tree.command(name="disablesystem", description="Disable a major bot system")
@app_commands.describe(system="Which system to disable")
@app_commands.choices(system=_SYSTEM_CHOICES)
@command_enabled()
async def disablesystem(interaction: discord.Interaction, system: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await set_system_flag(interaction.guild.id, system, False)
    await interaction.response.send_message(f"🔒 **{_SYSTEM_LABELS[system]}** is now **disabled**.")

# --- CHEST EMBED -------------------------------------

async def _build_chest_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="📦 Chest Shop",
        description="Open chests to win prizes! Results are only visible to you.",
        color=discord.Color.purple())

    for chest_type, label, cost_str in [
        ("chest",    "📦 EXP Chest",  "Cost: 1,000 EXP"),
        ("vipchest", "💎 VIP Chest",  "Cost: 1 VIP Key"),
    ]:
        prizes  = await get_chest_prizes(guild.id, chest_type)
        total_w = sum(p["chance"] for p in prizes) or 1
        lines   = []
        for p in prizes:
            pct  = p["chance"] / total_w * 100
            desc = []
            if p["exp"] > 0:     desc.append(f"⭐ {p['exp']:,} EXP")
            if p["balance"] > 0: desc.append(f"💰 {p['balance']:,} coins")
            if not desc:         desc.append("✨ Special")
            lines.append(f"• **{p['name']}** — {', '.join(desc)} — {pct:.1f}%")
        embed.add_field(
            name=f"{label} ({cost_str})",
            value="\n".join(lines) or "*No prizes configured*",
            inline=False)

    embed.set_footer(text="Use the buttons below • responses are only visible to you")
    return embed


async def _refresh_chest_channel(guild: discord.Guild):
    """Update the chest embed in the configured channel."""
    async with get_db() as db:
        async with db.execute(
            "SELECT channel_id, message_id FROM chest_channel_config WHERE guild_id=?",
            (guild.id,)) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return
    ch_id, msg_id = row
    channel = bot.get_channel(ch_id)
    if not channel:
        return
    embed = await _build_chest_embed(guild)
    view  = ChestChannelView()
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
            return
        except discord.NotFound:
            pass
    new_msg = await channel.send(embed=embed, view=view)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "UPDATE chest_channel_config SET message_id=? WHERE guild_id=?",
                (new_msg.id, guild.id))
            await db.commit()


async def _do_open_exp_chests(interaction: discord.Interaction, amount: int):
    """Shared EXP chest opening for the panel. amount=-1 means open max."""
    await interaction.response.defer(ephemeral=True)
    gid = interaction.guild.id
    uid = interaction.user.id
    exp = await get_exp(gid, uid)

    if exp < CHEST_COST:
        await interaction.followup.send(
            f"❌ You need {CHEST_COST:,} EXP (you have {exp:,}).", ephemeral=True); return

    max_open = exp // CHEST_COST
    if amount == -1:
        amount = min(max_open, 100)   # cap at 100 per click
    else:
        amount = min(amount, max_open)
    if amount == 0:
        await interaction.followup.send("❌ Not enough EXP.", ephemeral=True); return

    total_cost    = CHEST_COST * amount
    prizes        = await get_chest_prizes(gid, "chest")
    rare_names    = await get_rare_chest_names(gid, "chest")
    results: dict[str, int] = {}
    total_balance = total_exp_won = 0

    for _ in range(amount):
        prize = random.choices(prizes, weights=[p["chance"] for p in prizes], k=1)[0]
        results[prize["name"]] = results.get(prize["name"], 0) + 1
        total_balance += prize["balance"]
        total_exp_won += prize["exp"]

    await _add_chest_spending(gid, uid, total_cost)
    if total_balance > 0: await add_balance(gid, uid, total_balance)
    if total_exp_won > 0: await add_exp(gid, uid, total_exp_won)
    await add_stat(gid, uid, "chests_opened", amount)

    embed = discord.Embed(
        title=f"📦 Chest Results ×{amount}",
        description="\n".join(f"• {c}x **{n}**" for n, c in results.items()),
        color=discord.Color.purple())
    embed.set_footer(text=f"Cost: {total_cost:,} EXP | Remaining: {exp - total_cost:,} EXP")
    await interaction.followup.send(embed=embed, ephemeral=True)

    results_log = ", ".join(f"{c}x {n}" for n, c in results.items())
    await log_event(gid, "chest", _log_embed("📦 Chest Opened", discord.Color.purple(),
        User=interaction.user.mention, Opened=str(amount),
        Cost=f"{total_cost:,} EXP", Won=results_log[:1024]))

    rare_won = {n: c for n, c in results.items() if n in rare_names}
    if rare_won:
        rcid = await get_rare_drop_channel(gid)
        if rcid:
            rc = bot.get_channel(rcid)
            if rc:
                text = " and ".join(f"**{c}x {n}**" for n, c in rare_won.items())
                re   = discord.Embed(title="🌟 Rare Drop!",
                    description=f"{interaction.user.mention} got {text} from "
                                f"{'a chest' if amount == 1 else f'{amount} chests'}! 🎉",
                    color=discord.Color.gold())
                re.set_thumbnail(url=interaction.user.display_avatar.url)
                await rc.send(embed=re)


async def _do_open_vip_chests(interaction: discord.Interaction, amount: int):
    """Shared VIP chest opening for the panel."""
    await interaction.response.defer(ephemeral=True)
    gid = interaction.guild.id
    uid = interaction.user.id

    if not await is_system_enabled(gid, "vipkey"):
        await interaction.followup.send("❌ VIP chest system is disabled.", ephemeral=True); return

    inv  = await inventory_get(gid, uid)
    keys = next((q for n, q in inv if n.lower() == VIP_CHEST_KEY.lower()), 0)

    if keys < 1:
        await interaction.followup.send(
            f"❌ You have no **{VIP_CHEST_KEY}** (Nitro Boosters get one daily!).",
            ephemeral=True); return

    amount = min(amount, keys, 10)   # cap at 10 and available keys
    if not await inventory_remove(gid, uid, VIP_CHEST_KEY, amount):
        await interaction.followup.send("❌ Failed to consume keys.", ephemeral=True); return

    prizes        = await get_chest_prizes(gid, "vipchest")
    rare_names    = await get_rare_chest_names(gid, "vipchest")
    results: dict[str, int] = {}
    total_balance = total_exp_won = 0

    for _ in range(amount):
        prize = random.choices(prizes, weights=[p["chance"] for p in prizes], k=1)[0]
        results[prize["name"]] = results.get(prize["name"], 0) + 1
        total_balance += prize["balance"]
        total_exp_won += prize["exp"]

    if total_balance > 0: await add_balance(gid, uid, total_balance)
    if total_exp_won > 0: await add_exp(gid, uid, total_exp_won)

    embed = discord.Embed(
        title=f"💎 VIP Chest Results ×{amount}",
        description="\n".join(f"• {c}x **{n}**" for n, c in results.items()),
        color=discord.Color.from_rgb(148, 0, 211))
    embed.set_footer(text=f"{amount} key(s) used | {keys - amount} remaining")
    await interaction.followup.send(embed=embed, ephemeral=True)

    results_log = ", ".join(f"{c}x {n}" for n, c in results.items())
    await log_event(gid, "chest", _log_embed("💎 VIP Chest Opened", discord.Color.from_rgb(148, 0, 211),
        User=interaction.user.mention, Opened=str(amount),
        Keys_Used=str(amount), Won=results_log[:1024]))

    rare_won = {n: c for n, c in results.items() if n in rare_names}
    if rare_won:
        rcid = await get_rare_drop_channel(gid)
        if rcid:
            rc = bot.get_channel(rcid)
            if rc:
                text = " and ".join(f"**{c}x {n}**" for n, c in rare_won.items())
                re   = discord.Embed(title="💎 VIP Rare Drop!",
                    description=f"{interaction.user.mention} got {text} from "
                                f"**{amount} VIP Chest{'s' if amount > 1 else ''}**! 👑",
                    color=discord.Color.from_rgb(148, 0, 211))
                re.set_thumbnail(url=interaction.user.display_avatar.url)
                await rc.send(embed=re)


class ChestChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # ── Row 0: EXP chests ─────────────────────────────────────────────────
    @discord.ui.button(label="⭐ My EXP", style=discord.ButtonStyle.secondary,
                       custom_id="chest_panel:check_exp", row=0)
    async def check_exp(self, interaction: discord.Interaction, btn: discord.ui.Button):
        gid    = interaction.guild.id
        uid    = interaction.user.id
        exp    = await get_exp(gid, uid)
        lvl    = await get_level(gid, uid)
        inv    = await inventory_get(gid, uid)
        keys   = next((q for n, q in inv if n.lower() == VIP_CHEST_KEY.lower()), 0)
        embed  = discord.Embed(title=f"⭐ {interaction.user.display_name}", color=discord.Color.gold())
        embed.add_field(name="Activity Rank",    value=str(lvl),              inline=True)
        embed.add_field(name="Usable EXP",       value=f"{exp:,}",            inline=True)
        embed.add_field(name="Chests Available", value=f"{exp // CHEST_COST}", inline=True)
        embed.add_field(name="VIP Keys",         value=str(keys),             inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="📦 ×1", style=discord.ButtonStyle.primary,
                       custom_id="chest_panel:open_exp_1", row=0)
    async def open_exp_1(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await _do_open_exp_chests(interaction, 1)

    @discord.ui.button(label="📦 ×10", style=discord.ButtonStyle.primary,
                       custom_id="chest_panel:open_exp_10", row=0)
    async def open_exp_10(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await _do_open_exp_chests(interaction, 10)

    @discord.ui.button(label="📦 ×Max", style=discord.ButtonStyle.primary,
                       custom_id="chest_panel:open_exp_max", row=0)
    async def open_exp_max(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await _do_open_exp_chests(interaction, -1)

    # ── Row 1: VIP chests ─────────────────────────────────────────────────
    @discord.ui.button(label="💎 VIP ×1", style=discord.ButtonStyle.success,
                       custom_id="chest_panel:open_vip_1", row=1)
    async def open_vip_1(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await _do_open_vip_chests(interaction, 1)

    @discord.ui.button(label="💎 VIP ×5", style=discord.ButtonStyle.success,
                       custom_id="chest_panel:open_vip_5", row=1)
    async def open_vip_5(self, interaction: discord.Interaction, btn: discord.ui.Button):
        await _do_open_vip_chests(interaction, 5)

@bot.tree.command(name="setchestchannel",
                  description="Post the chest panel embed in a channel (updates on restart)")
@app_commands.describe(channel="Channel to post the panel in")
@command_enabled()
async def setchestchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await interaction.response.defer()
    gid = interaction.guild.id

    # Delete old panel if it was in a different channel
    async with get_db() as db:
        async with db.execute(
            "SELECT channel_id, message_id FROM chest_channel_config WHERE guild_id=?",
            (gid,)) as cur:
            old = await cur.fetchone()
    if old and old[0] and old[0] != channel.id and old[1]:
        old_ch = bot.get_channel(old[0])
        if old_ch:
            try: await (await old_ch.fetch_message(old[1])).delete()
            except Exception: pass

    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO chest_channel_config(guild_id, channel_id, message_id) VALUES(?,?,0) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=0",
                (gid, channel.id))
            await db.commit()

    await _refresh_chest_channel(interaction.guild)
    await interaction.followup.send(f"✅ Chest panel posted in {channel.mention}.")

@bot.command(name="setchestchannel")
async def pfx_setchestchannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setchestchannel._callback(FakeInteraction(ctx), channel)

# --- VERIFICATION --------------------------------------

class VerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Verify", style=discord.ButtonStyle.green,
                       custom_id="verification:verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with get_db() as db:
            async with db.execute(
                "SELECT verified_role_id, unverified_role_id "
                "FROM verification_config WHERE guild_id=?",
                (interaction.guild.id,)) as cur:
                cfg = await cur.fetchone()
        if not cfg:
            await interaction.response.send_message(
                "❌ Verification is not configured.", ephemeral=True); return

        verified_rid, unverified_rid = cfg
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            await interaction.response.send_message("❌ Member not found.", ephemeral=True); return

        try:
            if unverified_rid:
                role = interaction.guild.get_role(unverified_rid)
                if role and role in member.roles:
                    await member.remove_roles(role, reason="Verification")
            if verified_rid:
                role = interaction.guild.get_role(verified_rid)
                if role and role not in member.roles:
                    await member.add_roles(role, reason="Verification")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Bot lacks permission to manage roles.", ephemeral=True); return
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True); return

        await interaction.response.send_message(
            "✅ Verified! Welcome to the server.", ephemeral=True)

async def _post_verification_embed(guild: discord.Guild, channel: discord.TextChannel):
    async with get_db() as db:
        async with db.execute(
            "SELECT message_id, message FROM verification_config WHERE guild_id=?",
            (guild.id,)) as cur:
            row = await cur.fetchone()
    if not row:
        return
    msg_id, msg_text = row
    embed = discord.Embed(
        title="✅ Verification",
        description=msg_text or "Click the button below to verify and access the server!",
        color=discord.Color.green())
    embed.set_footer(text=f"{guild.name} • Click the button to verify")
    view = VerificationView()

    if msg_id:
        try:
            existing = await channel.fetch_message(msg_id)
            await existing.edit(embed=embed, view=view)
            return
        except discord.NotFound:
            pass

    new_msg = await channel.send(embed=embed, view=view)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "UPDATE verification_config SET message_id=? WHERE guild_id=?",
                (new_msg.id, guild.id))
            await db.commit()

# ═══════════════════════════════════════════════════════
# VERIFICATION SYSTEM
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="setverification",
                  description="Set up the verification system and post the verification embed")
@app_commands.describe(
    channel="Channel where the verify button will be posted",
    verified_role="Role to give when verified (optional)",
    unverified_role="Role new members get that restricts access until they verify (optional)",
    message="Custom message text shown in the embed"
)
@command_enabled()
async def setverification(interaction: discord.Interaction,
                           channel: discord.TextChannel,
                           verified_role: discord.Role = None,
                           unverified_role: discord.Role = None,
                           message: str = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await interaction.response.defer()
    gid = interaction.guild.id
    msg_text = message or "Click the button below to verify and access the server!"
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO verification_config"
                "(guild_id, channel_id, verified_role_id, unverified_role_id, message) "
                "VALUES(?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET "
                "channel_id=excluded.channel_id, verified_role_id=excluded.verified_role_id, "
                "unverified_role_id=excluded.unverified_role_id, message=excluded.message",
                (gid, channel.id,
                 verified_role.id if verified_role else 0,
                 unverified_role.id if unverified_role else 0,
                 msg_text))
            await db.commit()
    await _post_verification_embed(interaction.guild, channel)
    lines = [f"✅ Verification embed posted in {channel.mention}."]
    if verified_role:
        lines.append(f"Gives **{verified_role.name}** on verify.")
    if unverified_role:
        lines.append(f"Removes **{unverified_role.name}** on verify — assign it to new members "
                     f"and restrict channel access via Discord permissions.")
    await interaction.followup.send("\n".join(lines))

@bot.tree.command(name="disableverification", description="Disable the verification system")
@command_enabled()
async def disableverification(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM verification_config WHERE guild_id=?", (interaction.guild.id,))
            await db.commit()
    await interaction.response.send_message("🗑 Verification system disabled.")

# ═══════════════════════════════════════════════════════
# AUTO-ENTRY GIVEAWAYS
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="addautoentryrole",
                  description="Add/update a role that allows auto-entry for giveaways")
@app_commands.describe(
    role="Role to allow",
    message_requirement="Messages the user must send today to qualify (0 = no requirement)"
)
@command_enabled()
async def addautoentryrole(interaction: discord.Interaction,
                            role: discord.Role,
                            message_requirement: int = 0):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO auto_entry_roles(guild_id, role_id, message_requirement) "
                "VALUES(?,?,?) ON CONFLICT(guild_id, role_id) DO UPDATE SET "
                "message_requirement=excluded.message_requirement",
                (interaction.guild.id, role.id, message_requirement))
            await db.commit()
    req_str = f" — requires **{message_requirement}** messages today" if message_requirement else ""
    await interaction.response.send_message(
        f"✅ {role.mention} can use auto-entry{req_str}.")


@bot.tree.command(name="listautoentryroles",
                  description="List roles that allow auto-entry and their requirements")
@command_enabled()
async def listautoentryroles(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute(
            "SELECT role_id, message_requirement FROM auto_entry_roles WHERE guild_id=?",
            (interaction.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message(
            "❌ No auto-entry roles configured.", ephemeral=True); return
    lines = []
    for rid, req in rows:
        r = interaction.guild.get_role(rid)
        name = r.mention if r else f"<@&{rid}>"
        lines.append(f"• {name}" + (f" — **{req}** messages/day required" if req else " — no requirement"))
    embed = discord.Embed(title="🎉 Auto-Entry Roles",
                          description="\n".join(lines),
                          color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="autoentry",
                  description="Toggle automatic entry into all server giveaways")
@command_enabled()
async def autoentry(interaction: discord.Interaction):
    gid = interaction.guild.id
    uid = interaction.user.id

    async with get_db() as db:
        async with db.execute(
            "SELECT role_id, message_requirement FROM auto_entry_roles WHERE guild_id=?",
            (gid,)) as cur:
            all_roles = await cur.fetchall()

    if not all_roles:
        await interaction.response.send_message(
            "❌ Auto-entry is not configured for this server.", ephemeral=True); return

    member    = interaction.guild.get_member(uid)
    user_rids = {r.id for r in member.roles} if member else set()

    eligible     = False
    no_role      = True
    unmet_reqs   = []  # (Role | None, required, actual)

    for rid, req in all_roles:
        if rid not in user_rids:
            continue
        no_role = False
        if req > 0:
            today_count = await _get_today_msg_count(gid, uid)
            if today_count < req:
                unmet_reqs.append((interaction.guild.get_role(rid), req, today_count))
                continue
        eligible = True
        break

    if no_role:
        mentions = []
        for rid, req in all_roles:
            r = interaction.guild.get_role(rid)
            if r:
                suffix = f" *(needs {req} msgs/day)*" if req else ""
                mentions.append(f"{r.mention}{suffix}")
        await interaction.response.send_message(
            f"❌ You need one of these roles to be able to automatically enter giveaways: "
            f"{', '.join(mentions)}", ephemeral=True); return

    if not eligible:
        lines = ["❌ You don't meet the daily message requirements for any of your eligible roles:"]
        for role, req, got in unmet_reqs:
            name = role.mention if role else "?"
            lines.append(f"• {name}: **{got}/{req}** messages sent today")
        await interaction.response.send_message("\n".join(lines), ephemeral=True); return

    # Toggle
    async with get_db() as db:
        async with db.execute(
            "SELECT enabled FROM auto_entry_users WHERE guild_id=? AND user_id=?",
            (gid, uid)) as cur:
            existing = await cur.fetchone()

    if existing:
        new_val = 0 if existing[0] else 1
        async with db_lock:
            async with get_db() as db:
                await db.execute(
                    "UPDATE auto_entry_users SET enabled=? WHERE guild_id=? AND user_id=?",
                    (new_val, gid, uid))
                await db.commit()
    else:
        new_val = 1
        async with db_lock:
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO auto_entry_users(guild_id,user_id,enabled) VALUES(?,?,1)",
                    (gid, uid))
                await db.commit()

    if new_val:
        await interaction.response.send_message(
            "✅ Auto-entry **enabled** — you'll be automatically entered into all giveaways.",
            ephemeral=True)
    else:
        await interaction.response.send_message(
            "🔒 Auto-entry **disabled**.", ephemeral=True)

@bot.tree.command(name="removeautoentryrole",
                  description="Remove a role from auto-entry eligibility")
@app_commands.describe(role="Role to remove")
@command_enabled()
async def removeautoentryrole(interaction: discord.Interaction, role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM auto_entry_roles WHERE guild_id=? AND role_id=?",
                (interaction.guild.id, role.id))
            await db.commit()
    await interaction.response.send_message(
        f"🗑 {role.mention} removed from auto-entry eligibility.")

#  --- STAT CHANNEl

async def _build_stats_embed(guild: discord.Guild) -> discord.Embed:
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(tickets),0) FROM raffle "
            "WHERE guild_id=? AND tickets>0", (guild.id,)) as cur:
            members, pool = await cur.fetchone()
    embed = discord.Embed(
        title="📊 Stats",
        description=(
            "Click a button below to check your personal stats.\n"
            "All responses are **private** — only you can see them."
        ),
        color=discord.Color.blurple())
    embed.add_field(
        name="🎟 Current Raffle Pool",
        value=f"{pool:,} tickets across {members:,} member(s)",
        inline=False)
    embed.set_footer(text="Results are only visible to you")
    return embed


async def _refresh_stats_channel(guild: discord.Guild):
    async with get_db() as db:
        async with db.execute(
            "SELECT channel_id, message_id FROM stats_channel_config WHERE guild_id=?",
            (guild.id,)) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return
    ch = bot.get_channel(row[0])
    if not ch:
        return
    embed = await _build_stats_embed(guild)
    view  = StatsChannelView()
    if row[1]:
        try:
            msg = await ch.fetch_message(row[1])
            await msg.edit(embed=embed, view=view)
            return
        except discord.NotFound:
            pass
    new_msg = await ch.send(embed=embed, view=view)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "UPDATE stats_channel_config SET message_id=? WHERE guild_id=?",
                (new_msg.id, guild.id))
            await db.commit()


class StatsChannelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="💰 Balance", style=discord.ButtonStyle.secondary,
                       custom_id="stats_panel:balance", row=0)
    async def check_balance(self, interaction: discord.Interaction, btn: discord.ui.Button):
        bal = await get_balance(interaction.guild.id, interaction.user.id)
        embed = discord.Embed(
            title=f"💰 {interaction.user.display_name}'s Balance",
            description=f"**{bal:,}** coins",
            color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="⭐ Activity Rank", style=discord.ButtonStyle.secondary,
                       custom_id="stats_panel:rank", row=0)
    async def check_rank(self, interaction: discord.Interaction, btn: discord.ui.Button):
        gid    = interaction.guild.id
        uid    = interaction.user.id
        exp    = await get_level_exp(gid, uid)
        usable = await get_exp(gid, uid)
        lvl    = await get_level(gid, uid)
        embed  = discord.Embed(
            title=f"⭐ {interaction.user.display_name}'s Activity Rank",
            color=discord.Color.gold())
        embed.add_field(name="Activity Rank",  value=str(lvl),      inline=True)
        embed.add_field(name="Total EXP (7d)", value=f"{exp:,}",    inline=True)
        embed.add_field(name="Usable EXP",     value=f"{usable:,}", inline=True)
        embed.add_field(name="Chests Available",
                        value=f"{usable // CHEST_COST}", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🎟 Raffle Tickets", style=discord.ButtonStyle.secondary,
                       custom_id="stats_panel:tickets", row=0)
    async def check_tickets(self, interaction: discord.Interaction, btn: discord.ui.Button):
        gid     = interaction.guild.id
        uid     = interaction.user.id
        tickets = await get_tickets(gid, uid)
        async with get_db() as db:
            async with db.execute(
                "SELECT COALESCE(SUM(tickets),0) FROM raffle WHERE guild_id=?",
                (gid,)) as cur:
                total = (await cur.fetchone())[0]
        chance = (tickets / total * 100) if total else 0
        embed = discord.Embed(title="🎟 Your Raffle Stats", color=discord.Color.gold())
        embed.add_field(name="Your Tickets",  value=f"{tickets:,}",    inline=True)
        embed.add_field(name="Total Pool",    value=f"{total:,}",      inline=True)
        embed.add_field(name="Win Chance",    value=f"{chance:.2f}%",  inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🎒 Inventory", style=discord.ButtonStyle.secondary,
                       custom_id="stats_panel:inventory", row=0)
    async def check_inventory(self, interaction: discord.Interaction, btn: discord.ui.Button):
        gid = interaction.guild.id
        uid = interaction.user.id
        inv = await inventory_get(gid, uid)
        embed = discord.Embed(
            title=f"🎒 {interaction.user.display_name}'s Inventory",
            color=discord.Color.blurple())
        if not inv:
            embed.description = "Inventory is empty."
        else:
            lines = []
            for item_name, qty in inv:
                if item_name == VIP_CHEST_KEY:
                    lines.append(f"• 🔑 **{item_name}** ×{qty}")
                elif item_name == GAMBLE_TOKEN:
                    lines.append(f"• 🎲 **{item_name}** ×{qty}")
                else:
                    si = await get_item(gid, item_name)
                    if si:
                        r = interaction.guild.get_role(si[3])
                        lines.append(f"• **{item_name}** ×{qty}" + (f" → {r.mention}" if r else ""))
                    else:
                        lines.append(f"• 📦 **{item_name}** ×{qty}")
            embed.description = "\n".join(lines[:30])
            if len(inv) > 30:
                embed.set_footer(text=f"Showing 30 of {len(inv)} items")
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setstatchannel",
                  description="Post the stats panel embed in a channel")
@app_commands.describe(channel="Channel to post the panel in")
@command_enabled()
async def setstatchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await interaction.response.defer()
    gid = interaction.guild.id

    # Remove old panel if in a different channel
    async with get_db() as db:
        async with db.execute(
            "SELECT channel_id, message_id FROM stats_channel_config WHERE guild_id=?",
            (gid,)) as cur:
            old = await cur.fetchone()
    if old and old[0] and old[0] != channel.id and old[1]:
        old_ch = bot.get_channel(old[0])
        if old_ch:
            try: await (await old_ch.fetch_message(old[1])).delete()
            except Exception: pass

    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO stats_channel_config(guild_id, channel_id, message_id) VALUES(?,?,0) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=0",
                (gid, channel.id))
            await db.commit()

    await _refresh_stats_channel(interaction.guild)
    await interaction.followup.send(f"✅ Stats panel posted in {channel.mention}.")

@bot.command(name="setstatchannel")
async def pfx_setstatchannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setstatchannel._callback(FakeInteraction(ctx), channel)

# ═══════════════════════════════════════════════════════
# LEADERBOARD
# ═══════════════════════════════════════════════════════

_LB_PER_PAGE = 10

class LeaderboardView(discord.ui.View):
    def __init__(self, all_data: list, guild: discord.Guild, caller_id: int,
                 caller_rank: int | None, caller_amt: int | None,
                 title: str, current_page: int, total_pages: int):
        super().__init__(timeout=120)
        self.all_data    = all_data
        self.guild       = guild
        self.caller_id   = caller_id
        self.caller_rank = caller_rank
        self.caller_amt  = caller_amt
        self.title       = title
        self.current     = current_page   # 1-indexed
        self.total       = total_pages
        self._sync()

    def _sync(self):
        for btn in self.children:
            if isinstance(btn, discord.ui.Button):
                if btn.label == "◀": btn.disabled = self.current <= 1
                elif btn.label == "▶": btn.disabled = self.current >= self.total

    def build_embed(self, page: int) -> discord.Embed:
        medals = ["🥇", "🥈", "🥉"]
        start  = (page - 1) * _LB_PER_PAGE
        chunk  = self.all_data[start:start + _LB_PER_PAGE]
        embed  = discord.Embed(title=self.title, color=discord.Color.gold())
        lines  = []
        for i, (uid, amt) in enumerate(chunk):
            rank   = start + i + 1
            m      = self.guild.get_member(uid)
            name   = m.display_name if m else "*[Left Server]*"
            star   = " ★" if uid == self.caller_id else ""
            prefix = medals[rank - 1] if rank <= 3 else f"**#{rank}**"
            lines.append(f"{prefix} {name}{star} — {amt:,}")
        embed.description = "\n".join(lines) if lines else "*No entries on this page.*"
        # Always show caller's rank in the footer
        page_info = f"Page {page}/{self.total} · {len(self.all_data)} entries"
        if self.caller_rank is not None:
            embed.set_footer(text=f"{page_info} · Your rank: #{self.caller_rank} ({self.caller_amt:,})")
        else:
            embed.set_footer(text=f"{page_info} · You have no entry yet")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, disabled=True)
    async def prev_page(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.caller_id:
            await interaction.response.send_message("❌ Not your leaderboard.", ephemeral=True); return
        self.current -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(self.current), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.caller_id:
            await interaction.response.send_message("❌ Not your leaderboard.", ephemeral=True); return
        self.current += 1
        self._sync()
        await interaction.response.edit_message(embed=self.build_embed(self.current), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

@bot.tree.command(name="leaderboard", description="View leaderboards")
@app_commands.choices(category=[
    app_commands.Choice(name="Total EXP",              value="total_exp"),
    app_commands.Choice(name="Usable EXP",             value="current_exp"),
    app_commands.Choice(name="Balance",                value="balance"),
    app_commands.Choice(name="Lifetime Tickets",       value="raffle_tickets_bought"),
    app_commands.Choice(name="Current Raffle Tickets", value="current_tickets"),
    app_commands.Choice(name="Chests Opened",          value="chests_opened"),
    app_commands.Choice(name="Gifted Balance",         value="gifted_balance"),
])
@app_commands.describe(
    category="Which leaderboard to view",
    page="Jump directly to this page number (default: 1)"
)
@command_enabled()
async def leaderboard(interaction: discord.Interaction,
                      category: app_commands.Choice[str],
                      page: int = 1):
    value    = category.value
    gid      = interaction.guild.id
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    await interaction.response.defer()

    all_data: list[tuple[int, int]] = []
    async with get_db() as db:
        if value == "current_exp":
            # One efficient query instead of per-user calls
            async with db.execute(
                "SELECT user_id, SUM(amount) FROM exp_history "
                "WHERE guild_id=? AND timestamp>=? GROUP BY user_id "
                "HAVING SUM(amount)>0 ORDER BY SUM(amount) DESC",
                (gid, week_ago)) as cur:
                all_data = [(uid, int(amt)) for uid, amt in await cur.fetchall()]
        elif value == "current_tickets":
            async with db.execute(
                "SELECT user_id, tickets FROM raffle "
                "WHERE guild_id=? AND tickets>0 ORDER BY tickets DESC",
                (gid,)) as cur:
                all_data = list(await cur.fetchall())
        elif value == "balance":
            async with db.execute(
                "SELECT user_id, balance FROM balances "
                "WHERE guild_id=? AND balance>0 ORDER BY balance DESC",
                (gid,)) as cur:
                all_data = list(await cur.fetchall())
        else:
            async with db.execute(
                f"SELECT user_id, {value} FROM user_stats "
                f"WHERE guild_id=? AND {value}>0 ORDER BY {value} DESC",
                (gid,)) as cur:
                all_data = list(await cur.fetchall())

    if not all_data:
        await interaction.followup.send("❌ No data found."); return

    # Find the caller's rank (None if they have no entry)
    caller_rank = caller_amt = None
    for rank, (uid, amt) in enumerate(all_data, 1):
        if uid == interaction.user.id:
            caller_rank, caller_amt = rank, amt
            break

    title_map = {
        "total_exp":             "🏆 Total EXP",
        "current_exp":           "⭐ Usable EXP",
        "balance":               "💰 Balance",
        "raffle_tickets_bought": "🎟 Lifetime Tickets",
        "current_tickets":       "🎫 Current Tickets",
        "chests_opened":         "📦 Chests Opened",
        "gifted_balance":        "💸 Gifted Balance",
    }
    title       = title_map[value] + " Leaderboard"
    total_pages = max(1, (len(all_data) + _LB_PER_PAGE - 1) // _LB_PER_PAGE)
    page        = max(1, min(page, total_pages))

    view  = LeaderboardView(all_data, interaction.guild, interaction.user.id,
                            caller_rank, caller_amt, title, page, total_pages)
    await interaction.followup.send(
        embed=view.build_embed(page),
        view=view if total_pages > 1 else None)

# ═══════════════════════════════════════════════════════
# RARE DROP CHANNEL
# ═══════════════════════════════════════════════════════

async def get_rare_drop_channel(guild_id: int):
    async with get_db() as db:
        async with db.execute("SELECT channel_id FROM rare_drop_config WHERE guild_id=?",
                              (guild_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None

async def get_rare_chest_names(guild_id: int, chest_type: str) -> set[str]:
    """Returns custom rare-drop names if configured, else falls back to hardcoded defaults."""
    async with get_db() as db:
        async with db.execute(
            "SELECT prize_name FROM rare_chest_config WHERE guild_id=? AND chest_type=?",
            (guild_id, chest_type)) as cur:
            rows = await cur.fetchall()
    if rows:
        return {r[0] for r in rows}
    return RARE_CHEST_PRIZES if chest_type == "chest" else RARE_VIP_PRIZES

async def get_rare_box_ids(guild_id: int, box_name: str) -> set[int]:
    """Returns the prize IDs that trigger a rare-drop announcement for this box."""
    async with get_db() as db:
        async with db.execute(
            "SELECT prize_id FROM rare_box_config WHERE guild_id=? AND box_name=?",
            (guild_id, box_name)) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}

@bot.tree.command(name="setraredropchannel", description="Set channel for rare chest drop announcements")
@command_enabled()
async def setraredropchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO rare_drop_config VALUES(?,?)",
                             (interaction.guild.id, channel.id))
            await db.commit()
    await interaction.response.send_message(f"✅ Rare drop announcements → {channel.mention}")

# ═══════════════════════════════════════════════════════
# RAFFLE INFO CHANNEL
# ═══════════════════════════════════════════════════════

def build_raffle_info_embed(guild, total_tickets, top_entries, prev=None):
    now    = datetime.now(UTC)
    target = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= target: target += timedelta(days=1)
    end_ts = int(target.timestamp())
    embed  = discord.Embed(title="🎟 Live Raffle Status", color=discord.Color.gold())
    embed.add_field(name="⏰ Next Draw",
                    value=f"<t:{end_ts}:R> (<t:{end_ts}:F>)", inline=False)
    embed.add_field(name="🎫 Total Tickets", value=f"{total_tickets:,}", inline=False)

    if top_entries:
        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, (uid, t) in enumerate(top_entries[:5]):
            medal  = medals[i] if i < 3 else f"#{i+1}"
            member = guild.get_member(uid)
            name   = member.display_name if member else f"<@{uid}>"
            chance = (t / total_tickets * 100) if total_tickets > 0 else 0
            lines.append(f"{medal} **{name}** — {t:,} tickets ({chance:.1f}%)")
        embed.add_field(name="🏆 Top Participants", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🏆 Top Participants", value="No tickets yet.", inline=False)

    # ── Previous raffle ──────────────────────────────────────────────
    if prev:
        draw_dt   = datetime.fromtimestamp(prev["ts"], UTC)
        date_str  = draw_dt.strftime("%Y-%m-%d %H:%M UTC")
        winner    = guild.get_member(prev["winner_id"])
        wname     = winner.display_name if winner else f"<@{prev['winner_id']}>"
        wpct      = (prev["winner_tickets"] / prev["total"] * 100) if prev["total"] else 0
        lines2    = [
            f"🏆 **{wname}** — {prev['winner_tickets']:,} tickets ({wpct:.1f}%)",
            f"📊 Pool: {prev['total']:,} tickets",
        ]
        for i, (uid, t) in enumerate(prev.get("top", [])[:3]):
            m    = guild.get_member(uid)
            mn   = m.display_name if m else f"<@{uid}>"
            pct2 = (t / prev["total"] * 100) if prev["total"] else 0
            lines2.append(f"{'🥇🥈🥉'[i]} {mn} — {t:,} ({pct2:.1f}%)")
        embed.add_field(name=f"📜 Previous Draw ({date_str})",
                        value="\n".join(lines2), inline=False)

    embed.set_footer(text=f"Updated: {datetime.now(UTC).strftime('%H:%M:%S UTC')}")
    return embed

@bot.tree.command(name="setraffleinfochannel", description="Set channel for the live raffle status board")
@command_enabled()
async def setraffleinfochannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT user_id,tickets FROM raffle WHERE guild_id=? ORDER BY tickets DESC LIMIT 5",
                              (interaction.guild.id,)) as cur:
            top = await cur.fetchall()
        async with db.execute("SELECT SUM(tickets) FROM raffle WHERE guild_id=?",
                              (interaction.guild.id,)) as cur:
            total = (await cur.fetchone())[0] or 0
    embed = build_raffle_info_embed(interaction.guild, total, top)
    info_msg = await channel.send(embed=embed)
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO raffle_info_config VALUES(?,?,?)",
                             (interaction.guild.id, channel.id, info_msg.id))
            await db.commit()
    await interaction.response.send_message(f"✅ Live raffle board posted in {channel.mention}.")

async def raffle_info_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        async with get_db() as db:
            async with db.execute(
                "SELECT guild_id,channel_id,message_id FROM raffle_info_config") as cur:
                configs = await cur.fetchall()
        for guild_id, channel_id, message_id in configs:
            try:
                guild   = bot.get_guild(guild_id)
                channel = bot.get_channel(channel_id)
                if not guild or not channel: continue

                async with get_db() as db:
                    async with db.execute(
                        "SELECT user_id,tickets FROM raffle WHERE guild_id=? "
                        "ORDER BY tickets DESC LIMIT 5", (guild_id,)) as cur:
                        top = await cur.fetchall()
                    async with db.execute(
                        "SELECT SUM(tickets) FROM raffle WHERE guild_id=?",
                        (guild_id,)) as cur:
                        total = (await cur.fetchone())[0] or 0
                    # Fetch most recent history entry
                    async with db.execute(
                        "SELECT draw_timestamp,winner_id,winner_tickets,total_tickets,top_json "
                        "FROM raffle_history WHERE guild_id=? "
                        "ORDER BY draw_timestamp DESC LIMIT 1",
                        (guild_id,)) as cur:
                        h = await cur.fetchone()

                prev = None
                if h:
                    ts, wid, wt, tot, tj = h
                    prev = {"ts": ts, "winner_id": wid, "winner_tickets": wt,
                            "total": tot, "top": json.loads(tj) if tj else []}

                embed = build_raffle_info_embed(guild, total, top, prev)
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.edit(embed=embed)
                except discord.NotFound:
                    new_msg = await channel.send(embed=embed)
                    async with db_lock:
                        async with get_db() as db:
                            await db.execute(
                                "UPDATE raffle_info_config SET message_id=? WHERE guild_id=?",
                                (new_msg.id, guild_id))
                            await db.commit()
            except Exception as e:
                print(f"[RaffleInfoLoop] {guild_id}: {e}")
        await asyncio.sleep(60)

# ═══════════════════════════════════════════════════════
# EXP BOOSTS
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="expboost",
                  description="Set an EXP boost for a role — optionally limit it to a channel or category")
@app_commands.describe(
    role="Role to boost",
    boost="e.g. 1.5 = +1.5%, -25 = penalty. All matching boosts are summed.",
    channel="Only apply in this channel (omit for global or category-wide scope)",
    category="Only apply in this category (omit for global or single-channel scope)"
)
@command_enabled()
async def expboost(interaction: discord.Interaction, role: discord.Role, boost: float,
                   channel:  discord.TextChannel    = None,
                   category: discord.CategoryChannel = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if boost == 0:
        await interaction.response.send_message("❌ Boost cannot be 0%.", ephemeral=True); return
    if channel and category:
        await interaction.response.send_message(
            "❌ Specify a channel OR a category, not both.", ephemeral=True); return
    channel_id  = channel.id  if channel  else 0
    category_id = category.id if category else 0
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO exp_boosts VALUES(?,?,?,?,?)",
                (interaction.guild.id, role.id, boost, channel_id, category_id))
            await db.commit()
    sign  = "+" if boost > 0 else ""
    scope = "globally"
    if channel:   scope = f"in {channel.mention} only"
    elif category: scope = f"in the **{category.name}** category only"
    await interaction.response.send_message(
        f"✅ {role.mention} now earns **{sign}{boost}% EXP** per message {scope}.")

@bot.tree.command(name="removeexpboost",
                  description="Remove an EXP boost — specify the same scope used when it was set")
@app_commands.describe(
    role="Role to remove boost from",
    channel="Remove the boost specific to this channel (omit for global/category boost)",
    category="Remove the boost specific to this category (omit for global/channel boost)"
)
@command_enabled()
async def removeexpboost(interaction: discord.Interaction, role: discord.Role,
                          channel:  discord.TextChannel    = None,
                          category: discord.CategoryChannel = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if channel and category:
        await interaction.response.send_message(
            "❌ Specify a channel OR a category, not both.", ephemeral=True); return
    channel_id  = channel.id  if channel  else 0
    category_id = category.id if category else 0
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM exp_boosts "
                "WHERE guild_id=? AND role_id=? AND channel_id=? AND category_id=?",
                (interaction.guild.id, role.id, channel_id, category_id))
            await db.commit()
    scope = "global"
    if channel:   scope = f"channel {channel.mention}"
    elif category: scope = f"category **{category.name}**"
    await interaction.response.send_message(
        f"🗑 Removed {scope} EXP boost from {role.mention}.")
    
# ═══════════════════════════════════════════════════════
# TRADE SYSTEM
# ═══════════════════════════════════════════════════════

trade_sessions: dict = {}

class TradeOffer:
    def __init__(self):
        self.balance: int = 0
        self.exp:     int = 0
        self.tickets: int = 0
        self.items:   list[tuple[str, int]] = []

    def display(self) -> str:
        lines = []
        if self.balance > 0: lines.append(f"💰 {self.balance:,} coins")
        if self.exp > 0:     lines.append(f"⭐ {self.exp:,} EXP")
        if self.tickets > 0: lines.append(f"🎟 {self.tickets:,} ticket(s)")
        for n, q in self.items: lines.append(f"🎒 {q}x {n}")
        return "\n".join(lines) if lines else "*Nothing*"

class TradeSession:
    def __init__(self, guild_id, initiator_id, target_id):
        self.guild_id     = guild_id
        self.initiator_id = initiator_id
        self.target_id    = target_id
        self.offers: dict = {initiator_id: None, target_id: None}
        self.confirmed: dict = {initiator_id: False, target_id: False}
        self.message      = None
        self.done         = False
        self.lock         = asyncio.Lock()

    def session_key(self):
        return (self.guild_id, frozenset({self.initiator_id, self.target_id}))

    def build_embed(self, guild):
        init = guild.get_member(self.initiator_id)
        tgt  = guild.get_member(self.target_id)
        embed = discord.Embed(title="🤝 Trade Offer", color=discord.Color.blurple())
        io = self.offers[self.initiator_id]
        to = self.offers[self.target_id]
        is_ = "✅" if self.confirmed[self.initiator_id] else ("📋" if io else "❓")
        ts_ = "✅" if self.confirmed[self.target_id] else ("📋" if to else "❓")
        embed.add_field(name=f"{init.display_name if init else 'User'}'s offer {is_}",
                        value=io.display() if io else "*Not set yet*", inline=True)
        embed.add_field(name=f"{tgt.display_name if tgt else 'User'}'s offer {ts_}",
                        value=to.display() if to else "*Not set yet*", inline=True)
        return embed

class TradeOfferModal(discord.ui.Modal, title="Set Your Trade Offer"):
    balance_input = discord.ui.TextInput(label="Balance to offer (0 for none)", default="0", max_length=20)
    exp_input     = discord.ui.TextInput(label="EXP to offer (0 for none)",     default="0", max_length=20)
    tickets_input = discord.ui.TextInput(label="Raffle tickets (0 for none)",   default="0", max_length=20)
    items_input   = discord.ui.TextInput(label="Items/boxes (blank for none)",
                                         placeholder="Name:qty, Name2:qty2",
                                         required=False, max_length=300)

    def __init__(self, session):
        super().__init__()
        self.session = session

    async def on_submit(self, interaction: discord.Interaction):
        uid = interaction.user.id
        session = self.session
        try: balance = max(0, int(self.balance_input.value.strip()))
        except ValueError:
            await interaction.response.send_message("❌ Invalid balance.", ephemeral=True); return
        try: exp = max(0, int(self.exp_input.value.strip()))
        except ValueError:
            await interaction.response.send_message("❌ Invalid EXP.", ephemeral=True); return
        try: tickets = max(0, int(self.tickets_input.value.strip()))
        except ValueError:
            await interaction.response.send_message("❌ Invalid tickets.", ephemeral=True); return
        items = []
        for part in (self.items_input.value or "").split(","):
            part = part.strip()
            if not part: continue
            if ":" not in part:
                await interaction.response.send_message(f"❌ Bad format `{part}` — use Name:qty",
                                                        ephemeral=True); return
            iname, qty_str = part.rsplit(":", 1)
            try:
                qty = int(qty_str.strip()); assert qty > 0
            except:
                await interaction.response.send_message(f"❌ Invalid qty for {iname}", ephemeral=True); return
            items.append((iname.strip(), qty))
        if balance > 0 and await get_balance(interaction.guild.id, uid) < balance:
            await interaction.response.send_message("❌ Not enough coins.", ephemeral=True); return
        if exp > 0 and await get_exp(interaction.guild.id, uid) < exp:
            await interaction.response.send_message("❌ Not enough EXP.", ephemeral=True); return
        if tickets > 0 and await get_tickets(session.guild_id, uid) < tickets:
            await interaction.response.send_message("❌ Not enough tickets.", ephemeral=True); return
        if items:
            inv = {n.lower(): q for n, q in await inventory_get(self.session.guild_id, uid)}
            for n, q in items:
                if inv.get(n.lower(), 0) < q:
                    await interaction.response.send_message(f"❌ Not enough {n}.", ephemeral=True); return
        offer = TradeOffer()
        offer.balance = balance; offer.exp = exp; offer.tickets = tickets; offer.items = items
        session.offers[uid] = offer
        session.confirmed[uid] = False
        await session.message.edit(embed=session.build_embed(interaction.guild), view=TradeView(session))
        await interaction.response.send_message("✅ Offer updated!", ephemeral=True)

class TradeView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=300)
        self.session = session

    async def interaction_check(self, interaction):
        if interaction.user.id not in (self.session.initiator_id, self.session.target_id):
            await interaction.response.send_message("❌ Not your trade.", ephemeral=True); return False
        if self.session.done:
            await interaction.response.send_message("❌ Trade already finished.", ephemeral=True); return False
        return True

    @discord.ui.button(label="Set Offer", style=discord.ButtonStyle.primary, emoji="📋")
    async def set_offer(self, interaction, button):
        await interaction.response.send_modal(TradeOfferModal(self.session))

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction, button):
        session = self.session
        if session.offers[interaction.user.id] is None:
            await interaction.response.send_message("❌ Set your offer first.", ephemeral=True); return
        async with session.lock:
            session.confirmed[interaction.user.id] = True
            if not all(session.confirmed.values()):
                await session.message.edit(embed=session.build_embed(interaction.guild), view=self)
                await interaction.response.send_message("✅ Confirmed! Waiting for other party.",
                                                        ephemeral=True); return
            session.done = True
            success, err = await execute_trade(session)
            trade_sessions.pop(session.session_key(), None)
        if success:
            await session.message.edit(
                embed=discord.Embed(title="✅ Trade Complete!", color=discord.Color.green()), view=None)
            await interaction.response.send_message("✅ Trade executed!", ephemeral=True)
        else:
            session.confirmed[session.initiator_id] = session.confirmed[session.target_id] = False
            session.done = False
            await session.message.edit(embed=session.build_embed(interaction.guild), view=TradeView(session))
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction, button):
        session = self.session; session.done = True
        trade_sessions.pop(session.session_key(), None)
        await session.message.edit(
            embed=discord.Embed(title="❌ Trade Cancelled", color=discord.Color.red()), view=None)
        await interaction.response.send_message("Trade cancelled.", ephemeral=True)

    async def on_timeout(self):
        if not self.session.done:
            self.session.done = True
            trade_sessions.pop(self.session.session_key(), None)
            if self.session.message:
                try:
                    await self.session.message.edit(
                        embed=discord.Embed(title="⏰ Trade Expired",
                                            color=discord.Color.light_grey()), view=None)
                except: pass

async def execute_trade(session) -> tuple[bool, str]:
    iid, tid = session.initiator_id, session.target_id
    gid = session.guild_id
    for uid, offer in [(iid, session.offers[iid]), (tid, session.offers[tid])]:
        if offer.balance > 0 and await get_balance(gid, uid) < offer.balance:
            return False, f"<@{uid}> no longer has enough coins."
        if offer.exp > 0 and await get_exp(gid, uid) < offer.exp:
            return False, f"<@{uid}> no longer has enough EXP."
        if offer.tickets > 0 and await get_tickets(gid, uid) < offer.tickets:
            return False, f"<@{uid}> no longer has enough tickets."
        inv = {n.lower(): q for n, q in await inventory_get(gid, uid)}
        for n, q in offer.items:
            if inv.get(n.lower(), 0) < q:
                return False, f"<@{uid}> no longer has {q}x {n}."
    io, to = session.offers[iid], session.offers[tid]
    if io.balance > 0: await add_balance(gid, iid, -io.balance); await add_balance(gid, tid, io.balance)
    if to.balance > 0: await add_balance(gid, tid, -to.balance); await add_balance(gid, iid, to.balance)
    if io.exp > 0: await add_exp(gid, iid, -io.exp); await add_exp(gid, tid, io.exp)
    if to.exp > 0: await add_exp(gid, tid, -to.exp); await add_exp(gid, iid, to.exp)
    if io.tickets > 0:
        await add_tickets(gid, iid, -io.tickets); await add_tickets(gid, tid, io.tickets)
    if to.tickets > 0:
        await add_tickets(gid, tid, -to.tickets); await add_tickets(gid, iid, to.tickets)
    for n, q in io.items: await inventory_remove(gid, iid, n, q); await inventory_add(gid, tid, n, q)
    for n, q in to.items: await inventory_remove(gid, tid, n, q); await inventory_add(gid, iid, n, q)
    return True, ""

@bot.tree.command(name="trade", description="Initiate a trade with another user")
@command_enabled()
async def trade(interaction: discord.Interaction, user: discord.Member):
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ Can't trade with yourself.", ephemeral=True); return
    if user.bot:
        await interaction.response.send_message("❌ Can't trade with a bot.", ephemeral=True); return
    key = (interaction.guild.id, frozenset({interaction.user.id, user.id}))
    if key in trade_sessions:
        await interaction.response.send_message("❌ A trade is already in progress.", ephemeral=True); return
    session = TradeSession(interaction.guild.id, interaction.user.id, user.id)
    trade_sessions[key] = session
    await interaction.response.send_message(
        f"🤝 {interaction.user.mention} wants to trade with {user.mention}!\n"
        f"Click **Set Offer** to enter what you're offering (coins, EXP, tickets, items/boxes), "
        f"then **Confirm**.",
        embed=session.build_embed(interaction.guild), view=TradeView(session))
    session.message = await interaction.original_response()

# ═══════════════════════════════════════════════════════
# WELCOME DM SYSTEM
# ═══════════════════════════════════════════════════════

class _WelcomeView(discord.ui.View):
    """Non-interactive footer button that shows which server sent the DM."""
    def __init__(self, guild_name: str):
        super().__init__(timeout=None)
        label = f"Sent from {guild_name}"
        if len(label) > 80:
            label = label[:77] + "..."
        self.add_item(discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            disabled=True))


class WelcomeMessageModal(discord.ui.Modal, title="Set Welcome DM Message"):
    message_input = discord.ui.TextInput(
        label="Welcome message",
        style=discord.TextStyle.long,
        placeholder="Welcome to {server}, {member}! 🎉  — use {member} and {server} as placeholders",
        max_length=1800,
        required=True)

    def __init__(self, existing: str = ""):
        super().__init__()
        if existing:
            self.message_input.default = existing

    async def on_submit(self, interaction: discord.Interaction):
        msg = self.message_input.value.strip()
        async with db_lock:
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO welcome_config(guild_id, enabled, message) VALUES(?,?,?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET enabled=1, message=excluded.message",
                    (interaction.guild.id, 1, msg))
                await db.commit()
        await interaction.response.send_message(
            "✅ Welcome message saved and **enabled**!\n"
            "Use `/previewwelcome` to see how it looks, or `/disablewelcome` to turn it off.",
            ephemeral=True)

@bot.tree.command(name="setwelcome",
                  description="Set or edit the DM new members receive — opens a text editor")
@command_enabled()
async def setwelcome(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT message FROM welcome_config WHERE guild_id=?",
                              (interaction.guild.id,)) as cur:
            row = await cur.fetchone()
    await interaction.response.send_modal(
        WelcomeMessageModal(existing=row[0] if row and row[0] else ""))

# ═══════════════════════════════════════════════════════
# CHANNEL WELCOME SYSTEM
# ═══════════════════════════════════════════════════════

class WelcomeChannelModal(discord.ui.Modal, title="Set Channel Welcome Message"):
    message_input = discord.ui.TextInput(
        label="Welcome message (blank = DM message/default)",
        style=discord.TextStyle.long,
        placeholder="Welcome to {server}, {member}! 🎉  — {member} and {server} are placeholders",
        max_length=1800,
        required=False)

    def __init__(self, guild_id: int, channel_id: int, existing: str = ""):
        super().__init__()
        self.guild_id   = guild_id
        self.channel_id = channel_id
        if existing:
            self.message_input.default = existing

    async def on_submit(self, interaction: discord.Interaction):
        msg = self.message_input.value.strip() or None
        async with db_lock:
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO welcome_config"
                    "(guild_id, enabled, message, channel_id, channel_enabled, channel_message) "
                    "VALUES(?,0,NULL,?,1,?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET "
                    "channel_id=excluded.channel_id, "
                    "channel_enabled=1, "
                    "channel_message=excluded.channel_message",
                    (self.guild_id, self.channel_id, msg))
                await db.commit()
        ch = interaction.guild.get_channel(self.channel_id)
        await interaction.response.send_message(
            f"✅ Channel welcome **enabled** in {ch.mention if ch else f'<#{self.channel_id}>'}!\n"
            + ("Custom message saved." if msg else
               "No custom message set — will fall back to the DM message, or a default greeting.") +
            "\nUse `/disablewelcomechannel` to turn it off, or `/previewwelcomechannel` to preview.",
            ephemeral=True)


@bot.tree.command(name="setwelcomechannel",
                  description="Set the channel for join announcements — opens a message editor")
@app_commands.describe(channel="Channel to post welcome pings in")
@command_enabled()
async def setwelcomechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT channel_message FROM welcome_config WHERE guild_id=?",
                              (interaction.guild.id,)) as cur:
            row = await cur.fetchone()
    existing = row[0] if row and row[0] else ""
    await interaction.response.send_modal(
        WelcomeChannelModal(interaction.guild.id, channel.id, existing))


@bot.tree.command(name="disablewelcomechannel",
                  description="Disable channel welcome pings (the channel setting is kept)")
@command_enabled()
async def disablewelcomechannel(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "UPDATE welcome_config SET channel_enabled=0 WHERE guild_id=?",
                (interaction.guild.id,))
            await db.commit()
    await interaction.response.send_message(
        "🔕 Channel welcome pings disabled. Use `/setwelcomechannel` or `/enablewelcomechannel` to re-enable.")


@bot.tree.command(name="enablewelcomechannel",
                  description="Re-enable channel welcome pings (channel must already be set)")
@command_enabled()
async def enablewelcomechannel(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT channel_id FROM welcome_config WHERE guild_id=?",
                              (interaction.guild.id,)) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        await interaction.response.send_message(
            "❌ No channel configured yet. Use `/setwelcomechannel` first.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "UPDATE welcome_config SET channel_enabled=1 WHERE guild_id=?",
                (interaction.guild.id,))
            await db.commit()
    ch = interaction.guild.get_channel(row[0])
    await interaction.response.send_message(
        f"✅ Channel welcome pings re-enabled in {ch.mention if ch else '?'}.")


@bot.tree.command(name="previewwelcomechannel",
                  description="Preview how the channel welcome ping will look")
@command_enabled()
async def previewwelcomechannel(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute(
            "SELECT channel_id, channel_enabled, channel_message, message "
            "FROM welcome_config WHERE guild_id=?",
            (interaction.guild.id,)) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        await interaction.response.send_message(
            "❌ No channel welcome configured. Use `/setwelcomechannel` first.",
            ephemeral=True); return
    ch_id, ch_enabled, ch_message, dm_message = row
    fallback = "Welcome {member} to **{server}**! 🎉"
    msg_text = (ch_message or dm_message or fallback)
    msg_text  = msg_text.replace("{member}", interaction.user.mention).replace("{server}", interaction.guild.name)
    embed = discord.Embed(description=msg_text, color=discord.Color.green())
    embed.set_author(
        name=interaction.guild.name,
        icon_url=interaction.guild.icon.url if interaction.guild.icon else None)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text=f"Member #{interaction.guild.member_count}")
    ch      = interaction.guild.get_channel(ch_id)
    status  = "✅ Enabled" if ch_enabled else "🔒 Disabled"
    source  = ("Custom message" if ch_message
                else "Falling back to DM message" if dm_message
                else "Default greeting")
    await interaction.response.send_message(
        f"📬 **Channel Welcome Preview** — Status: {status} | {source}\n"
        f"Channel: {ch.mention if ch else '?'}\n"
        "*(your mention is used as the example member)*",
        embed=embed, ephemeral=True)

# ═══════════════════════════════════════════════════════
# ADMIN ABUSE BOX SYSTEM
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="addboxprize", description="Add a prize to an abuse box")
@app_commands.describe(box="Box name", prize_type="Type of prize", chance="Weight (e.g. 50)",
                       amount="Amount for balance/exp prizes",
                       item_name="Item name for 'item' prizes",
                       custom_label="Label for 'nothing'/'custom'")
@app_commands.choices(prize_type=[
    app_commands.Choice(name="Balance", value="balance"),
    app_commands.Choice(name="EXP",     value="exp"),
    app_commands.Choice(name="Item",    value="item"),
    app_commands.Choice(name="Nothing", value="nothing"),
    app_commands.Choice(name="Custom",  value="custom"),
])
@command_enabled()
async def addboxprize(interaction: discord.Interaction, box: str, prize_type: str, chance: int,
                      amount: int = 0, item_name: str = None, custom_label: str = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if chance <= 0:
        await interaction.response.send_message("❌ Chance must be > 0.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (interaction.guild.id, box)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message(f"❌ Box **{box}** not found.", ephemeral=True); return
    if prize_type in ("balance", "exp"):
        if amount <= 0:
            await interaction.response.send_message("❌ Provide amount > 0.", ephemeral=True); return
        prize_value = str(amount)
    elif prize_type == "item":
        if not item_name:
            await interaction.response.send_message("❌ Provide item_name.", ephemeral=True); return
        item = await get_item(interaction.guild.id, item_name)
        if not item:
            await interaction.response.send_message(f"❌ Item **{item_name}** not found.", ephemeral=True); return
        prize_value = item[1]   # [1] = item_name (was [0] before guild_id column was added)
    elif prize_type == "nothing":
        prize_value = custom_label or "Nothing"
    else:
        if not custom_label:
            await interaction.response.send_message("❌ Provide custom_label.", ephemeral=True); return
        prize_value = custom_label
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO abuse_box_prizes(guild_id,box_name,prize_type,prize_value,prize_amount,chance) "
                "VALUES(?,?,?,?,?,?)",
                (interaction.guild.id, box, prize_type, prize_value, amount, chance))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Added to **{box}**: `{prize_type}` — **{prize_value}** (weight: {chance})")

@bot.tree.command(name="openbox", description="Open one or more abuse boxes from your inventory")
@app_commands.describe(box="Box name", amount="How many to open (default 1, max 20)")
@command_enabled()
async def openbox(interaction: discord.Interaction, box: str, amount: int = 1):
    await interaction.response.defer()
    if amount <= 0:  await interaction.followup.send("❌ Amount must be ≥ 1."); return
    if amount > 20:  await interaction.followup.send("❌ Max 20 boxes at once."); return

    inv   = await inventory_get(interaction.guild.id, interaction.user.id)
    owned = {n.lower(): (n, q) for n, q in inv}
    if box.lower() not in owned or owned[box.lower()][1] < amount:
        have = owned.get(box.lower(), (box, 0))[1]
        await interaction.followup.send(f"❌ Need {amount}x **{box}** but you only have {have}."); return

    canonical_box = owned[box.lower()][0]
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (interaction.guild.id, canonical_box)) as cur:
            if not await cur.fetchone():
                await interaction.followup.send(f"❌ Box **{canonical_box}** no longer exists on this server."); return
        # Include the prize ID so we can check rare_box_config
        async with db.execute(
            "SELECT id, prize_type, prize_value, prize_amount, chance FROM abuse_box_prizes "
            "WHERE guild_id=? AND box_name=?",
            (interaction.guild.id, canonical_box)) as cur:
            prizes = await cur.fetchall()

    if not prizes:
        await interaction.followup.send(f"❌ **{canonical_box}** has no prizes configured."); return
    if not await inventory_remove(interaction.guild.id, interaction.user.id, canonical_box, amount):
        await interaction.followup.send("❌ Failed to remove boxes."); return

    rare_ids = await get_rare_box_ids(interaction.guild.id, canonical_box)
    results:     dict[str, int] = {}
    rare_wins:   dict[str, int] = {}
    total_balance = 0
    total_exp     = 0
    item_grants:  dict[str, int] = {}

    for _ in range(amount):
        p_id, p_type, p_value, p_amount, _ = random.choices(
            prizes, weights=[p[4] for p in prizes], k=1)[0]

        if p_type == "balance":
            amt = int(p_value); total_balance += amt; label = f"💰 {amt:,} coins"
        elif p_type == "exp":
            amt = int(p_value); total_exp += amt;    label = f"⭐ {amt:,} EXP"
        elif p_type == "item":
            item_grants[p_value] = item_grants.get(p_value, 0) + 1; label = f"🎒 {p_value}"
        elif p_type == "nothing": label = f"😔 {p_value}"
        else:                     label = f"✨ {p_value}"

        results[label] = results.get(label, 0) + 1
        if p_id in rare_ids:
            rare_wins[label] = rare_wins.get(label, 0) + 1

    if total_balance > 0: await add_balance(interaction.guild.id, interaction.user.id, total_balance)
    gid = interaction.guild.id
    if total_exp > 0:     await add_exp(gid, interaction.user.id, total_exp)
    for iname, qty in item_grants.items():
        si = await get_item(gid, iname)
        await inventory_add(gid, interaction.user.id, si[1] if si else iname, qty)

    result_text = "\n".join(f"• {count}x {desc}" for desc, count in results.items())
    embed = discord.Embed(title=f"📦 {canonical_box} × {amount}",
                          description=result_text, color=discord.Color.orange())
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

    if rare_wins:
        rcid = await get_rare_drop_channel(interaction.guild.id)
        if rcid:
            rc = bot.get_channel(rcid)
            if rc:
                text = " and ".join(f"**{c}x {n}**" for n, c in rare_wins.items())
                re   = discord.Embed(title="🎁 Rare Box Drop!",
                    description=f"{interaction.user.mention} pulled {text} from a **{canonical_box}**! 🎉",
                    color=discord.Color.orange())
                re.set_thumbnail(url=interaction.user.display_avatar.url)
                await rc.send(embed=re)

# ═══════════════════════════════════════════════════════
# CODE SYSTEM
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="redeem", description="Redeem a code for prizes")
@app_commands.describe(code="The code to redeem")
@command_enabled()
async def redeem(interaction: discord.Interaction, code: str):
    code     = code.upper().strip()
    guild_id = interaction.guild.id
    user_id  = interaction.user.id

    # ── 1. Try guild-specific code ────────────────────────────────────────────
    async with get_db() as db:
        async with db.execute(
            "SELECT prize_json,uses_left,min_level,min_balance,required_role_id "
            "FROM redeem_codes WHERE guild_id=? AND code=?", (guild_id, code)) as cur:
            guild_row = await cur.fetchone()

    # ── 2. Try global code ────────────────────────────────────────────────────
    global_row = None
    if not guild_row:
        async with get_db() as db:
            async with db.execute(
                "SELECT prize_json,uses_left,min_level,min_balance "
                "FROM global_redeem_codes WHERE code=?", (code,)) as cur:
                global_row = await cur.fetchone()

    if not guild_row and not global_row:
        await interaction.response.send_message(
            "❌ Code not found or already expired.", ephemeral=True); return

    is_global = (global_row is not None and guild_row is None)

    if is_global:
        prize_json, uses_left, min_level, min_balance = global_row
        req_role_id = 0
    else:
        prize_json, uses_left, min_level, min_balance, req_role_id = guild_row

    if uses_left == 0:
        await interaction.response.send_message(
            "❌ This code has no uses left.", ephemeral=True); return

    # ── 3. Check if already used ──────────────────────────────────────────────
    if is_global:
        async with get_db() as db:
            async with db.execute(
                "SELECT 1 FROM global_code_uses WHERE code=? AND user_id=?",
                (code, user_id)) as cur:
                already = await cur.fetchone()
    else:
        async with get_db() as db:
            async with db.execute(
                "SELECT 1 FROM code_uses WHERE guild_id=? AND code=? AND user_id=?",
                (guild_id, code, user_id)) as cur:
                already = await cur.fetchone()
    if already:
        await interaction.response.send_message(
            "❌ You've already used this code.", ephemeral=True); return

    # ── 4. Requirements ───────────────────────────────────────────────────────
    if await get_level(guild_id, user_id) < min_level:
        await interaction.response.send_message(
            f"❌ You need Activity Rank {min_level}.", ephemeral=True); return
    if await get_balance(guild_id, user_id) < min_balance:
        await interaction.response.send_message(
            f"❌ You need {min_balance:,} coins.", ephemeral=True); return
    if req_role_id:
        role = interaction.guild.get_role(req_role_id)
        if role and role not in interaction.user.roles:
            await interaction.response.send_message(
                f"❌ You need the {role.mention} role.", ephemeral=True); return

    try:
        prize = json.loads(prize_json)
    except json.JSONDecodeError:
        await interaction.response.send_message(
            "❌ Code has invalid prize data.", ephemeral=True); return

    # ── 5. Mark as used ───────────────────────────────────────────────────────
    async with db_lock:
        async with get_db() as db:
            if is_global:
                await db.execute(
                    "INSERT INTO global_code_uses(code,user_id) VALUES(?,?)", (code, user_id))
                if uses_left > 0:
                    await db.execute(
                        "UPDATE global_redeem_codes SET uses_left=uses_left-1 WHERE code=?",
                        (code,))
            else:
                await db.execute(
                    "INSERT INTO code_uses(guild_id,code,user_id) VALUES(?,?,?)",
                    (guild_id, code, user_id))
                if uses_left > 0:
                    await db.execute(
                        "UPDATE redeem_codes SET uses_left=uses_left-1 WHERE guild_id=? AND code=?",
                        (guild_id, code))
            await db.commit()

    # ── 6. Distribute prizes ──────────────────────────────────────────────────
    parts = []
    if prize.get("balance", 0) > 0:
        await add_balance(guild_id, user_id, prize["balance"])
        parts.append(f"💰 {prize['balance']:,} coins")
    if prize.get("exp", 0) > 0:
        await add_exp(guild_id, user_id, prize["exp"])
        parts.append(f"⭐ {prize['exp']:,} EXP")
    if prize.get("tickets", 0) > 0:
        await add_tickets(guild_id, user_id, prize["tickets"])
        parts.append(f"🎟 {prize['tickets']} ticket(s)")
    if prize.get("gamble_tokens", 0) > 0:
        await inventory_add(guild_id, user_id, GAMBLE_TOKEN, prize["gamble_tokens"])
        parts.append(f"🎲 {prize['gamble_tokens']} gamble token(s)")
    if prize.get("vip_keys", 0) > 0:
        await inventory_add(guild_id, user_id, VIP_CHEST_KEY, prize["vip_keys"])
        parts.append(f"🔑 {prize['vip_keys']} VIP key(s)")
    if prize.get("item"):
        qty = prize.get("item_qty", 1)
        await inventory_add(guild_id, user_id, prize["item"], qty)
        parts.append(f"🎒 {qty}x {prize['item']}")

    badge = "🌐 " if is_global else ""
    embed = discord.Embed(
        title=f"{badge}🎫 Code Redeemed!",
        description=f"You redeemed **{code}** and received:\n" +
                    "\n".join(f"• {p}" for p in parts),
        color=discord.Color.green())
    if is_global:
        embed.set_footer(text="This was a global code — valid across all servers.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ═══════════════════════════════════════════════════════
# GAMBLING SYSTEM
# ═══════════════════════════════════════════════════════

async def daily_gamble_loop():
    """1 Gamble Token/day to all members; +1 extra for Nitro Boosters."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now    = datetime.now(UTC)
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= target: target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        for guild in bot.guilds:
            if not await is_system_enabled(guild.id, "gamble"): continue
            for member in guild.members:
                if member.bot: continue
                try:
                    async with db_lock:
                        async with get_db() as db:
                            try:
                                await db.execute(
                                    "INSERT INTO daily_gamble_log(guild_id,user_id,date) VALUES(?,?,?)",
                                    (guild.id, member.id, today))
                                await db.commit()
                            except aiosqlite.IntegrityError:
                                continue
                    tokens = 1 + (1 if member.premium_since else 0)
                    await inventory_add(guild.id, member.id, GAMBLE_TOKEN, tokens)
                except Exception as e:
                    print(f"[DailyGamble] {member} / {guild.name}: {e}")

# ─── BLACKJACK ────────────────────────────────────────────────────────────────

_BJ_RANKS = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
_BJ_SUITS = ["♠","♥","♦","♣"]

def _bj_deck():
    d = [(r, s) for r in _BJ_RANKS for s in _BJ_SUITS]
    random.shuffle(d)
    return d

def _bj_val(hand: list) -> int:
    total, aces = 0, 0
    for r, _ in hand:
        if r in ("J","Q","K"): total += 10
        elif r == "A":         total += 11; aces += 1
        else:                  total += int(r)
    while total > 21 and aces:
        total -= 10; aces -= 1
    return total

def _bj_fmt(hand: list) -> str:
    return " ".join(f"{r}{s}" for r, s in hand)

def _bj_rank_int(card) -> int:
    r = card[0]
    if r in ("J","Q","K"): return 10
    if r == "A":           return 11
    return int(r)


class _BJState:
    def __init__(self, deck, hand, dealer, bet, user_id, guild_id, tokens):
        self.deck   = deck
        self.hands  = [list(hand)]   # grows on split
        self.dealer = list(dealer)
        self.bets   = [bet]          # parallel to hands; doubled on double-down
        self.active = 0              # index of the hand currently being played
        self.first  = True           # False after first action (disables double/split)
        self.uid    = user_id
        self.guild_id = guild_id
        self.tokens = tokens


class _BJView(discord.ui.View):
    def __init__(self, state: _BJState):
        super().__init__(timeout=60)
        self.state = state
        self._refresh()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _refresh(self):
        s    = self.state
        hand = s.hands[s.active]
        can_dd    = s.first and len(hand) == 2
        can_split = can_dd and _bj_rank_int(hand[0]) == _bj_rank_int(hand[1])
        for btn in self.children:
            if btn.label == "Double": btn.disabled = not can_dd
            elif btn.label == "Split": btn.disabled = not can_split

    def _embed(self, note: str = "") -> discord.Embed:
        s  = self.state
        h  = s.hands[s.active]
        v  = _bj_val(h)
        n  = len(s.hands)
        title = f"🃏 Blackjack — Hand {s.active+1}/{n}" if n > 1 else "🃏 Blackjack"
        rows  = [f"**{_bj_fmt(h)}** ({v})",
                 f"Dealer: **{s.dealer[0][0]}{s.dealer[0][1]}** + ??"]
        if n > 1:
            for i, hh in enumerate(s.hands):
                if i != s.active:
                    rows.append(f"Hand {i+1}: {_bj_fmt(hh)} ({_bj_val(hh)})")
        if note: rows.append(f"\n{note}")
        bets_str = (
            " | ".join(f"H{i+1}: {b:,}" for i, b in enumerate(s.bets))
            if n > 1 else f"Bet: {s.bets[0]:,}"
        )
        embed = discord.Embed(title=title, description="\n".join(rows), color=discord.Color.blue())
        embed.set_footer(text=bets_str)
        return embed

    async def _resolve(self, inter: discord.Interaction):
        s = self.state
        while _bj_val(s.dealer) < 17:
            s.dealer.append(s.deck.pop())
        dv = _bj_val(s.dealer)
        lines, delta = [], 0
        for i, (h, bet) in enumerate(zip(s.hands, s.bets)):
            pv  = _bj_val(h)
            lbl = f"H{i+1}" if len(s.hands) > 1 else "You"
            if pv > 21:
                lines.append(f"{lbl}: 💸 Bust ({pv}) −{bet:,}");       delta -= bet
            elif dv > 21 or pv > dv:
                lines.append(f"{lbl}: 🏆 Win ({pv} vs {dv}) +{bet:,}"); delta += bet
            elif pv == dv:
                lines.append(f"{lbl}: 🤝 Push ({pv})")
            else:
                lines.append(f"{lbl}: 💸 Loss ({pv} vs {dv}) −{bet:,}"); delta -= bet
        if delta: await add_balance(s.guild_id, s.uid, delta)
        color = (discord.Color.green() if delta > 0
                 else discord.Color.red() if delta < 0
                 else discord.Color.greyple())
        hs    = "\n".join(f"{_bj_fmt(h)} ({_bj_val(h)})" for h in s.hands)
        embed = discord.Embed(title="🃏 Blackjack — Result", color=color,
                              description=(f"Your hand(s):\n{hs}\n"
                                           f"Dealer: {_bj_fmt(s.dealer)} ({dv})\n\n"
                                           + "\n".join(lines)))
        await inter.response.edit_message(embed=embed, view=None)
        self.stop()
        if inter.guild:
            outcome = (f"+{delta:,}" if delta > 0 else
                       (f"{delta:,}" if delta < 0 else "Push ±0"))
            await log_event(inter.guild.id, "gamble", _log_embed(
                "🃏 Blackjack", color,
                User=inter.user.mention,
                Total_Bet=f"{sum(s.bets):,}",
                Outcome=outcome))

    async def _next(self, inter: discord.Interaction):
        """Advance to the next hand, or resolve if all hands are done."""
        s = self.state
        s.active += 1
        s.first   = True
        if s.active >= len(s.hands):
            await self._resolve(inter)
        else:
            if _bj_val(s.hands[s.active]) == 21:   # auto-stand on 21
                await self._next(inter)
            else:
                self._refresh()
                await inter.response.edit_message(
                    embed=self._embed(f"Now playing Hand {s.active+1} of {len(s.hands)}."),
                    view=self)

    # ── interaction guard ────────────────────────────────────────────────────

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.state.uid:
            await inter.response.send_message("Not your game.", ephemeral=True)
            return False
        return True

    # ── buttons ──────────────────────────────────────────────────────────────

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="➕")
    async def hit(self, inter: discord.Interaction, btn: discord.ui.Button):
        s = self.state
        s.first = False
        s.hands[s.active].append(s.deck.pop())
        v = _bj_val(s.hands[s.active])
        if v >= 21:
            await self._next(inter)
        else:
            self._refresh()
            await inter.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="✋")
    async def stand(self, inter: discord.Interaction, btn: discord.ui.Button):
        self.state.first = False
        await self._next(inter)

    @discord.ui.button(label="Double", style=discord.ButtonStyle.success, emoji="⬆️")
    async def double(self, inter: discord.Interaction, btn: discord.ui.Button):
        s = self.state
        if not s.first:
            await inter.response.send_message("❌ Too late to double.", ephemeral=True); return
        extra = s.bets[s.active]
        if await get_balance(s.guild_id, s.uid) < extra:
            await inter.response.send_message(
                f"❌ You need {extra:,} extra coins to double.", ephemeral=True); return
        s.bets[s.active] *= 2   # bet doubles; resolved at the end, no upfront deduction
        s.first = False
        s.hands[s.active].append(s.deck.pop())
        await self._next(inter)

    @discord.ui.button(label="Split", style=discord.ButtonStyle.danger, emoji="✂️", disabled=True)
    async def split_btn(self, inter: discord.Interaction, btn: discord.ui.Button):
        s = self.state
        if not s.first:
            await inter.response.send_message("❌ Too late to split.", ephemeral=True); return
        hand  = s.hands[s.active]
        extra = s.bets[s.active]
        if await get_balance(s.guild_id, s.uid) < extra:
            await inter.response.send_message(
                f"❌ You need {extra:,} extra coins to split.", ephemeral=True); return
        c1, c2 = hand[0], hand[1]
        s.hands[s.active]         = [c1, s.deck.pop()]
        s.hands.insert(s.active+1,  [c2, s.deck.pop()])
        s.bets.insert(s.active+1, extra)
        s.first = True
        self._refresh()
        await inter.response.edit_message(
            embed=self._embed(f"✂️ Split into {len(s.hands)} hands! Playing Hand 1."),
            view=self)

    async def on_timeout(self):
        # Forfeit all outstanding bets since game was abandoned
        await add_balance(self.state.guild_id, self.state.uid, -sum(self.state.bets))


@bot.tree.command(name="blackjack", description="Play blackjack — costs 1 Gamble Token")
@app_commands.describe(bet="Amount of coins to bet")
@command_enabled()
async def blackjack(interaction: discord.Interaction, bet: int):
    if not await is_system_enabled(interaction.guild.id, "gamble"):
        await interaction.response.send_message("❌ Gambling system is disabled.", ephemeral=True); return
    if bet <= 0:
        await interaction.response.send_message("❌ Bet must be > 0.", ephemeral=True); return
    bal = await get_balance(interaction.guild.id, interaction.user.id)
    if bal < bet:
        await interaction.response.send_message(f"❌ Not enough balance ({bal:,}).", ephemeral=True); return
    tokens = await get_gamble_tokens(interaction.guild.id, interaction.user.id)
    if tokens < 1:
        await interaction.response.send_message(
            f"❌ You need 1 {GAMBLE_TOKEN} to play. You receive one daily (Nitro Boosters get 2)!",
            ephemeral=True); return
    await inventory_remove(interaction.guild.id, interaction.user.id, GAMBLE_TOKEN, 1)

    deck  = _bj_deck()
    phand = [deck.pop(), deck.pop()]
    dhand = [deck.pop(), deck.pop()]

    # Natural blackjack (21 on first two cards)
    if _bj_val(phand) == 21:
        win = int(bet * 1.5)
        await add_balance(interaction.guild.id, interaction.user.id, win)
        embed = discord.Embed(
            title="🃏 Blackjack — Natural 21! 🎉", color=discord.Color.gold(),
            description=(f"**{_bj_fmt(phand)}** (21)\n"
                         f"Dealer: **{_bj_fmt(dhand)}** ({_bj_val(dhand)})\n\n"
                         f"🏆 **Blackjack! +{win:,} coins** (1.5× payout)"))
        await interaction.response.send_message(embed=embed); return

    state = _BJState(deck, phand, dhand, bet, interaction.user.id, interaction.guild.id, tokens)
    view  = _BJView(state)
    await interaction.response.send_message(embed=view._embed(), view=view)

# ─── ROULETTE ─────────────────────────────────────────────────────────────────

_R_RED   = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
_R_BLACK = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}
_R_EVEN  = {n for n in range(2, 37, 2)}
_R_ODD   = {n for n in range(1, 37, 2)}
_R_LOW   = set(range(1, 19))
_R_HIGH  = set(range(19, 37))
_R_COL1  = {n for n in range(1, 37) if n % 3 == 1}   # 1st column: 3n+1
_R_COL2  = {n for n in range(1, 37) if n % 3 == 2}   # 2nd column: 3n+2
_R_COL3  = {n for n in range(1, 37) if n % 3 == 0}   # 3rd column: divisible by 3
_R_DOZ1  = set(range(1, 13))
_R_DOZ2  = set(range(13, 25))
_R_DOZ3  = set(range(25, 37))

_R_BETS: dict[str, tuple[str, set[int], int]] = {
    # ×2 even-money bets (0 not included in any)
    "red":    ("🔴 Red",          _R_RED,   2),
    "black":  ("⚫ Black",        _R_BLACK, 2),
    "even":   ("Even",            _R_EVEN,  2),
    "odd":    ("Odd",             _R_ODD,   2),
    "low":    ("1–18 (Low)",      _R_LOW,   2),
    "1-18":   ("1–18 (Low)",      _R_LOW,   2),
    "high":   ("19–36 (High)",    _R_HIGH,  2),
    "19-36":  ("19–36 (High)",    _R_HIGH,  2),
    # ×3 dozens
    "1-12":   ("1st Dozen (1–12)",   _R_DOZ1, 3),
    "dozen1": ("1st Dozen (1–12)",   _R_DOZ1, 3),
    "13-24":  ("2nd Dozen (13–24)", _R_DOZ2, 3),
    "dozen2": ("2nd Dozen (13–24)", _R_DOZ2, 3),
    "25-36":  ("3rd Dozen (25–36)", _R_DOZ3, 3),
    "dozen3": ("3rd Dozen (25–36)", _R_DOZ3, 3),
    # ×3 columns
    "col1":   ("1st Column (3n+1)", _R_COL1, 3),
    "1st":    ("1st Column (3n+1)", _R_COL1, 3),
    "col2":   ("2nd Column (3n+2)", _R_COL2, 3),
    "2nd":    ("2nd Column (3n+2)", _R_COL2, 3),
    "col3":   ("3rd Column (÷3)",   _R_COL3, 3),
    "3rd":    ("3rd Column (÷3)",   _R_COL3, 3),
}

def _r_parse(choice: str) -> tuple[str, set[int], int] | None:
    """Return (label, winning_numbers, multiplier) or None if invalid."""
    c = choice.lower().strip()
    if c in _R_BETS:
        return _R_BETS[c]
    try:
        n = int(c)
        if 0 <= n <= 36:
            return (f"Number {n}", {n}, 36)
    except ValueError:
        pass
    return None


@bot.tree.command(name="roulette", description="Play roulette — costs 1 Gamble Token")
@app_commands.describe(
    bet="Coins to bet",
    choice=(
        "×36: 0–36  |  ×2: red black even odd 1-18 19-36  |  "
        "×3 dozen: 1-12 13-24 25-36  |  ×3 col: col1/1st  col2/2nd  col3/3rd"
    ),
)
@command_enabled()
async def roulette(interaction: discord.Interaction, bet: int, choice: str):
    if not await is_system_enabled(interaction.guild.id, "gamble"):
        await interaction.response.send_message("❌ Gambling system is disabled.", ephemeral=True); return
    if bet <= 0:
        await interaction.response.send_message("❌ Bet must be > 0.", ephemeral=True); return
    bal = await get_balance(interaction.guild.id, interaction.user.id)
    if bal < bet:
        await interaction.response.send_message(f"❌ Not enough balance ({bal:,}).", ephemeral=True); return
    tokens = await get_gamble_tokens(interaction.guild.id, interaction.user.id)
    if tokens < 1:
        await interaction.response.send_message(f"❌ You need 1 {GAMBLE_TOKEN} to play.", ephemeral=True); return

    parsed = _r_parse(choice)
    if parsed is None:
        await interaction.response.send_message(
            f"❌ Invalid choice `{choice}`.\n\n"
            "**×36** — single number `0`–`36`\n"
            "**×2** — `red` `black` `even` `odd` `1-18` `19-36`\n"
            "**×3 dozens** — `1-12` `13-24` `25-36`  *(aliases: dozen1 / dozen2 / dozen3)*\n"
            "**×3 columns** — `col1`/`1st` (3n+1) · `col2`/`2nd` (3n+2) · `col3`/`3rd` (3n)\n\n"
            "*0 only wins on a direct single-number bet.*",
            ephemeral=True)
        return

    bet_label, winning_set, multiplier = parsed
    await inventory_remove(interaction.guild.id, interaction.user.id, GAMBLE_TOKEN, 1)
    result = random.randint(0, 36)

    # Build a human-readable description of the result
    if result == 0:
        result_str = "🟢 **0** (Green)"
    else:
        col  = "🔴 Red" if result in _R_RED else "⚫ Black"
        par  = "Even" if result % 2 == 0 else "Odd"
        half = "1–18" if result <= 18 else "19–36"
        if result in _R_COL1:    col_lbl = "Col 1"
        elif result in _R_COL2:  col_lbl = "Col 2"
        else:                    col_lbl = "Col 3"
        if result in _R_DOZ1:    doz_lbl = "Doz 1"
        elif result in _R_DOZ2:  doz_lbl = "Doz 2"
        else:                    doz_lbl = "Doz 3"
        result_str = f"{col} **{result}** ({par}, {half}, {col_lbl}, {doz_lbl})"

    if result in winning_set:
        winnings    = bet * (multiplier - 1)
        await add_balance(interaction.guild.id, interaction.user.id, winnings)
        outcome     = f"🏆 **You win {winnings:,} coins!** ({multiplier}×)"
        color       = discord.Color.green()
        log_outcome = f"WIN +{winnings:,}"
    else:
        await add_balance(interaction.guild.id, interaction.user.id, -bet)
        outcome     = f"💸 **You lose {bet:,} coins.**"
        color       = discord.Color.red()
        log_outcome = f"LOSS −{bet:,}"

    embed = discord.Embed(title="🎰 Roulette", color=color,
        description=(
            f"Ball landed on: {result_str}\n"
            f"Your bet: **{bet_label}** for **{bet:,} coins** ({multiplier}×)\n\n"
            f"{outcome}"
        ))
    embed.set_footer(text=f"1 {GAMBLE_TOKEN} consumed | {tokens - 1} remaining")
    await interaction.response.send_message(embed=embed)
    await log_event(interaction.guild.id, "gamble", _log_embed(
        "🎰 Roulette", color,
        User=interaction.user.mention, Bet=f"{bet:,}", Choice=choice,
        Number=str(result), Outcome=log_outcome))

# ═══════════════════════════════════════════════════════
# GAME PRESETS
# ═══════════════════════════════════════════════════════

def _ch(continent: str, cap_letter: str, extra: str = None) -> list[str]:
    h = [f"This country is in {continent}",
         f"Its capital city starts with the letter '{cap_letter}'"]
    if extra:
        h.append(extra)
    return h

_PRESET_DATA: dict[str, dict] = {
    # ── Colors ────────────────────────────────────────────────────────────
    "colors": {
        "description": "Common colors",
        "answers_hints": {
            "Red":        ["It is a primary color",          "It is a warm color",           "Associated with fire and blood",    "Opposite of green on a color wheel"],
            "Orange":     ["It is a warm color",             "Secondary color (red + yellow)","Color of pumpkins",                 "Named after the fruit"],
            "Yellow":     ["It is a primary color",          "It is a warm color",           "Color of the sun and bananas",      "Brightest color to the human eye"],
            "Green":      ["It is a secondary color",        "It is a cool color",           "Color of grass and leaves",         "Made by mixing blue and yellow"],
            "Blue":       ["It is a primary color",          "It is a cool color",           "Color of the sky and oceans",       "Most popular favorite color worldwide"],
            "Purple":     ["It is a secondary color",        "It is a cool color",           "Historically associated with royalty","Made by mixing red and blue"],
            "Pink":       ["It is a warm color",             "Made by mixing red and white", "Color of flamingos and roses"],
            "Brown":      ["It is a neutral/warm color",     "Color of wood and chocolate",  "Made by mixing all three primary colors"],
            "Black":      ["It is an achromatic color",      "Absorbs all visible light",    "The darkest possible color"],
            "White":      ["It is an achromatic color",      "Reflects all visible light",   "The lightest possible color"],
            "Gray":       ["Between black and white",        "Color of storm clouds",        "An achromatic/neutral color"],
            "Grey":       ["Between black and white",        "British spelling variant",     "An achromatic/neutral color"],
            "Cyan":       ["Cool color",                     "Made by mixing blue and green","Used in CMYK printing",             "Color of tropical ocean water"],
            "Magenta":    ["Warm color",                     "Used in CMYK printing",        "Mix of red and violet"],
            "Turquoise":  ["Cool color",                     "Mix of blue and green",        "Color of the gemstone turquoise"],
            "Violet":     ["Cool color",                     "Shortest visible wavelength",  "Part of the rainbow (ROY G BIV)"],
            "Indigo":     ["Cool color",                     "Between blue and violet",      "One of Newton's seven rainbow colors"],
            "Maroon":     ["Warm color",                     "Dark brownish-red",            "Named after the French word for chestnut"],
            "Navy":       ["Cool color",                     "Very dark blue",               "Named after naval uniform color"],
            "Olive":      ["Warm/neutral color",             "Dark yellowish-green",         "Color of unripe olives"],
            "Teal":       ["Cool color",                     "Combination of blue and green","Named after the common teal duck"],
            "Coral":      ["Warm color",                     "Mix of orange, pink, and red", "Named after coral reef organisms"],
            "Gold":       ["Warm color",                     "Shiny metallic yellow",        "Color of the precious metal gold"],
            "Silver":     ["Cool/neutral color",             "Shiny metallic gray",          "Color of the precious metal silver"],
            "Beige":      ["Neutral color",                  "Pale sandy color",             "Commonly used in interior design"],
            "Lavender":   ["Cool color",                     "Light purple/violet",          "Named after the lavender flower"],
            "Crimson":    ["Warm color",                     "Strong, deep red",             "Associated with passion and urgency"],
            "Scarlet":    ["Warm color",                     "Bright red with orange tint",  "Associated with warning and danger"],
            "Azure":      ["Cool color",                     "Bright cerulean blue",         "Color of a clear midday sky"],
        }
    },

    # ── Africa ────────────────────────────────────────────────────────────
    "countries_africa": {
        "description": "Countries in Africa",
        "answers_hints": {
            "Algeria":                          _ch("Africa", "A", "Largest country in Africa by area"),
            "Angola":                           _ch("Africa", "L"),
            "Benin":                            _ch("Africa", "P", "Historically a center of the Voodoo religion"),
            "Botswana":                         _ch("Africa", "G", "Home to the Okavango Delta"),
            "Burkina Faso":                     _ch("Africa", "O"),
            "Burundi":                          _ch("Africa", "G", "One of the smallest countries in Africa"),
            "Cameroon":                         _ch("Africa", "Y", "Called 'Africa in miniature' for its diversity"),
            "Cape Verde":                       _ch("Africa", "P", "Atlantic island nation off West Africa"),
            "Central African Republic":         _ch("Africa", "B"),
            "Chad":                             _ch("Africa", "N", "Named after Lake Chad"),
            "Comoros":                          _ch("Africa", "M", "Island nation in the Indian Ocean"),
            "Democratic Republic of the Congo": _ch("Africa", "K", "Second largest country in Africa"),
            "Republic of the Congo":            _ch("Africa", "B"),
            "Djibouti":                         _ch("Africa", "D", "One of the smallest countries in mainland Africa"),
            "Egypt":                            _ch("Africa", "C", "Home to the ancient pyramids and Sphinx"),
            "Equatorial Guinea":                _ch("Africa", "M", "Only African country with Spanish as official language"),
            "Eritrea":                          _ch("Africa", "A"),
            "Eswatini":                         _ch("Africa", "M", "Formerly known as Swaziland"),
            "Ethiopia":                         _ch("Africa", "A", "Never colonized; one of the oldest civilizations"),
            "Gabon":                            _ch("Africa", "L"),
            "Gambia":                           _ch("Africa", "B", "Smallest country in mainland Africa"),
            "Ghana":                            _ch("Africa", "A", "First sub-Saharan country to gain independence"),
            "Guinea":                           _ch("Africa", "C"),
            "Guinea-Bissau":                    _ch("Africa", "B"),
            "Ivory Coast":                      _ch("Africa", "Y", "World's largest cocoa producer; also called Côte d'Ivoire"),
            "Kenya":                            _ch("Africa", "N", "Famous for the Maasai Mara wildlife reserve"),
            "Lesotho":                          _ch("Africa", "M", "Landlocked country entirely surrounded by South Africa"),
            "Liberia":                          _ch("Africa", "M", "Founded by freed American slaves"),
            "Libya":                            _ch("Africa", "T"),
            "Madagascar":                       _ch("Africa", "A", "Fourth largest island in the world"),
            "Malawi":                           _ch("Africa", "L"),
            "Mali":                             _ch("Africa", "B", "Home to the ancient city of Timbuktu"),
            "Mauritania":                       _ch("Africa", "N"),
            "Mauritius":                        _ch("Africa", "P", "Island nation in the Indian Ocean"),
            "Morocco":                          _ch("Africa", "R", "Northernmost country in Africa"),
            "Mozambique":                       _ch("Africa", "M"),
            "Namibia":                          _ch("Africa", "W", "Home to the Namib Desert, one of Earth's oldest"),
            "Niger":                            _ch("Africa", "N"),
            "Nigeria":                          _ch("Africa", "A", "Most populous country in Africa"),
            "Rwanda":                           _ch("Africa", "K", "Known as the 'Land of a Thousand Hills'"),
            "São Tomé and Príncipe":             _ch("Africa", "S", "Smallest country in Africa by population"),
            "Senegal":                          _ch("Africa", "D"),
            "Seychelles":                       _ch("Africa", "V", "Island nation; smallest population in Africa"),
            "Sierra Leone":                     _ch("Africa", "F"),
            "Somalia":                          _ch("Africa", "M", "Easternmost country in Africa"),
            "South Africa":                     _ch("Africa", "P", "Has three capital cities"),
            "South Sudan":                      _ch("Africa", "J", "World's youngest country, independent since 2011"),
            "Sudan":                            _ch("Africa", "K"),
            "Tanzania":                         _ch("Africa", "D", "Home to Mount Kilimanjaro, Africa's highest peak"),
            "Togo":                             _ch("Africa", "L"),
            "Tunisia":                          _ch("Africa", "T", "Northeasternmost country in Africa"),
            "Uganda":                           _ch("Africa", "K", "Home to mountain gorillas"),
            "Zambia":                           _ch("Africa", "L", "Home to Victoria Falls"),
            "Zimbabwe":                         _ch("Africa", "H", "Shares Victoria Falls with Zambia"),
        }
    },

    # ── Americas ─────────────────────────────────────────────────────────
    "countries_americas": {
        "description": "Countries in the Americas",
        "answers_hints": {
            "Antigua and Barbuda":              _ch("the Caribbean", "S", "Island nation said to have 365 beaches"),
            "Argentina":                        _ch("South America", "B", "Second largest country in South America"),
            "Bahamas":                          _ch("the Caribbean",  "N", "Archipelago of over 700 islands"),
            "Barbados":                         _ch("the Caribbean",  "B", "Birthplace of rum production"),
            "Belize":                           _ch("Central America","B", "Only Central American country with English as official language"),
            "Bolivia":                          _ch("South America",  "S", "Home to one of the world's highest capital cities"),
            "Brazil":                           _ch("South America",  "B", "Largest country in South America"),
            "Canada":                           _ch("North America",  "O", "Second largest country in the world by area"),
            "Chile":                            _ch("South America",  "S", "Longest country in the world from north to south"),
            "Colombia":                         _ch("South America",  "B", "Only South American country with coastline on both Pacific and Caribbean"),
            "Costa Rica":                       _ch("Central America","S", "Has no standing army; known for biodiversity"),
            "Cuba":                             _ch("the Caribbean",  "H", "Largest island in the Caribbean"),
            "Dominica":                         _ch("the Caribbean",  "R", "Known as the 'Nature Isle of the Caribbean'"),
            "Dominican Republic":               _ch("the Caribbean",  "S", "Shares the island of Hispaniola with Haiti"),
            "Ecuador":                          _ch("South America",  "Q", "Named after the equator that crosses it"),
            "El Salvador":                      _ch("Central America","S", "Smallest and most densely populated in Central America"),
            "Grenada":                          _ch("the Caribbean",  "S", "Known as the 'Spice Isle'"),
            "Guatemala":                        _ch("Central America","G", "Most populous country in Central America"),
            "Guyana":                           _ch("South America",  "G", "Only English-speaking country in South America"),
            "Haiti":                            _ch("the Caribbean",  "P", "First Black republic in the world"),
            "Honduras":                         _ch("Central America","T"),
            "Jamaica":                          _ch("the Caribbean",  "K", "Birthplace of reggae music and Bob Marley"),
            "Mexico":                           _ch("North America",  "M", "Largest Spanish-speaking country in the world"),
            "Nicaragua":                        _ch("Central America","M", "Largest country in Central America"),
            "Panama":                           _ch("Central America","P", "Home to the famous canal connecting two oceans"),
            "Paraguay":                         _ch("South America",  "A", "Doubly landlocked country in South America"),
            "Peru":                             _ch("South America",  "L", "Home to Machu Picchu and the Amazon River source"),
            "Saint Kitts and Nevis":            _ch("the Caribbean",  "B", "Smallest country in the Americas"),
            "Saint Lucia":                      _ch("the Caribbean",  "C"),
            "Saint Vincent and the Grenadines": _ch("the Caribbean",  "K"),
            "Suriname":                         _ch("South America",  "P", "Smallest country in South America"),
            "Trinidad and Tobago":              _ch("the Caribbean",  "P", "Southernmost Caribbean island nation"),
            "United States":                    _ch("North America",  "W", "Third largest country by area"),
            "Uruguay":                          _ch("South America",  "M", "Smallest Spanish-speaking country in South America"),
            "Venezuela":                        _ch("South America",  "C", "Home to Angel Falls, world's highest waterfall"),
        }
    },

    # ── Asia ─────────────────────────────────────────────────────────────
    "countries_asia": {
        "description": "Countries in Asia",
        "answers_hints": {
            "Afghanistan":          _ch("Asia", "K"),
            "Armenia":              _ch("Asia", "Y", "First country in history to adopt Christianity as state religion"),
            "Azerbaijan":           _ch("Asia", "B", "Known as the 'Land of Fire'"),
            "Bahrain":              _ch("Asia", "M", "Smallest country in Asia"),
            "Bangladesh":           _ch("Asia", "D", "One of the most densely populated countries"),
            "Bhutan":               _ch("Asia", "T", "Measures Gross National Happiness instead of GDP"),
            "Brunei":               _ch("Asia", "B", "Oil-rich sultanate on the island of Borneo"),
            "Cambodia":             _ch("Asia", "P", "Home to Angkor Wat, the largest religious monument"),
            "China":                _ch("Asia", "B", "Most populous country in the world"),
            "Cyprus":               _ch("Asia", "N", "Island country in the Mediterranean Sea"),
            "Georgia":              _ch("Asia", "T", "Claims to be the birthplace of wine"),
            "India":                _ch("Asia", "N", "Second most populous country in the world"),
            "Indonesia":            _ch("Asia", "J", "Largest archipelago nation in the world"),
            "Iran":                 _ch("Asia", "T", "Formerly known as Persia"),
            "Iraq":                 _ch("Asia", "B", "Location of ancient Mesopotamia"),
            "Israel":               _ch("Asia", "J", "Country in the Middle East"),
            "Japan":                _ch("Asia", "T", "Island nation known for Mount Fuji"),
            "Jordan":               _ch("Asia", "A", "Home to the ancient rose-red city of Petra"),
            "Kazakhstan":           _ch("Asia", "N", "Largest landlocked country in the world"),
            "Kuwait":               _ch("Asia", "K", "One of the wealthiest countries per capita"),
            "Kyrgyzstan":           _ch("Asia", "B"),
            "Laos":                 _ch("Asia", "V", "Only landlocked country in Southeast Asia"),
            "Lebanon":              _ch("Asia", "B", "Known as the 'Switzerland of the Middle East'"),
            "Malaysia":             _ch("Asia", "K", "Home to the Petronas Twin Towers"),
            "Maldives":             _ch("Asia", "M", "Lowest-lying country in the world"),
            "Mongolia":             _ch("Asia", "U", "Most sparsely populated country in the world"),
            "Myanmar":              _ch("Asia", "N", "Formerly known as Burma"),
            "Nepal":                _ch("Asia", "K", "Home to Mount Everest, the world's highest peak"),
            "North Korea":          _ch("Asia", "P"),
            "Oman":                 _ch("Asia", "M"),
            "Pakistan":             _ch("Asia", "I"),
            "Palestine":            _ch("Asia", "R"),
            "Philippines":          _ch("Asia", "M", "Archipelago of over 7,600 islands"),
            "Qatar":                _ch("Asia", "D", "Host of the 2022 FIFA World Cup"),
            "Saudi Arabia":         _ch("Asia", "R", "Largest country in the Middle East"),
            "Singapore":            _ch("Asia", "S", "City-state and one of the world's leading financial hubs"),
            "South Korea":          _ch("Asia", "S"),
            "Sri Lanka":            _ch("Asia", "C", "Island nation called the 'Pearl of the Indian Ocean'"),
            "Syria":                _ch("Asia", "D"),
            "Taiwan":               _ch("Asia", "T"),
            "Tajikistan":           _ch("Asia", "D"),
            "Thailand":             _ch("Asia", "B", "Known as the 'Land of Smiles'"),
            "Timor-Leste":          _ch("Asia", "D", "Also known as East Timor"),
            "Turkey":               _ch("Asia", "A", "Bridges Europe and Asia; formerly the Ottoman Empire"),
            "Turkmenistan":         _ch("Asia", "A"),
            "United Arab Emirates": _ch("Asia", "A", "Home to the Burj Khalifa, world's tallest building"),
            "Uzbekistan":           _ch("Asia", "T", "Doubly landlocked country"),
            "Vietnam":              _ch("Asia", "H", "S-shaped country in Southeast Asia"),
            "Yemen":                _ch("Asia", "S"),
        }
    },

    # ── Europe ───────────────────────────────────────────────────────────
    "countries_europe": {
        "description": "Countries in Europe",
        "answers_hints": {
            "Albania":                  _ch("Europe", "T"),
            "Andorra":                  _ch("Europe", "A", "Microstate in the Pyrenees between France and Spain"),
            "Austria":                  _ch("Europe", "V", "Home of Mozart; former center of the Habsburg Empire"),
            "Belarus":                  _ch("Europe", "M"),
            "Belgium":                  _ch("Europe", "B", "Home of the European Union headquarters"),
            "Bosnia and Herzegovina":   _ch("Europe", "S"),
            "Bulgaria":                 _ch("Europe", "S"),
            "Croatia":                  _ch("Europe", "Z", "Famous for Dubrovnik and Plitvice Lakes"),
            "Czech Republic":           _ch("Europe", "P", "Also called Czechia; home to medieval Prague"),
            "Denmark":                  _ch("Europe", "C", "Birthplace of Hans Christian Andersen"),
            "Estonia":                  _ch("Europe", "T", "One of the most digitally advanced countries"),
            "Finland":                  _ch("Europe", "H", "Home of Santa Claus (Rovaniemi)"),
            "France":                   _ch("Europe", "P", "Home to the Eiffel Tower; most visited country"),
            "Germany":                  _ch("Europe", "B", "Most populous country in the European Union"),
            "Greece":                   _ch("Europe", "A", "Birthplace of democracy and the Olympic Games"),
            "Hungary":                  _ch("Europe", "B", "Known for thermal baths and paprika"),
            "Iceland":                  _ch("Europe", "R", "Most sparsely populated country in Europe"),
            "Ireland":                  _ch("Europe", "D", "Known as the 'Emerald Isle'"),
            "Italy":                    _ch("Europe", "R", "Home to the Roman Colosseum and pizza"),
            "Kosovo":                   _ch("Europe", "P", "One of the youngest countries in Europe (2008)"),
            "Latvia":                   _ch("Europe", "R"),
            "Liechtenstein":            _ch("Europe", "V", "Doubly landlocked microstate"),
            "Lithuania":                _ch("Europe", "V", "Largest of the three Baltic states"),
            "Luxembourg":               _ch("Europe", "L", "One of the world's wealthiest countries"),
            "Malta":                    _ch("Europe", "V", "Smallest EU member state"),
            "Moldova":                  _ch("Europe", "C"),
            "Monaco":                   _ch("Europe", "M", "Second smallest country in the world"),
            "Montenegro":               _ch("Europe", "P"),
            "Netherlands":              _ch("Europe", "A", "Famous for windmills, tulips, and canals"),
            "North Macedonia":          _ch("Europe", "S"),
            "Norway":                   _ch("Europe", "O", "Famous for fjords and the Northern Lights"),
            "Poland":                   _ch("Europe", "W"),
            "Portugal":                 _ch("Europe", "L", "Westernmost country in continental Europe"),
            "Romania":                  _ch("Europe", "B", "Home to Transylvania and the Dracula legend"),
            "Russia":                   _ch("Europe", "M", "Largest country in the world by area"),
            "San Marino":               _ch("Europe", "S", "World's oldest republic; surrounded by Italy"),
            "Serbia":                   _ch("Europe", "B"),
            "Slovakia":                 _ch("Europe", "B"),
            "Slovenia":                 _ch("Europe", "L"),
            "Spain":                    _ch("Europe", "M", "Famous for flamenco and paella"),
            "Sweden":                   _ch("Europe", "S", "Home of IKEA, ABBA, and Volvo"),
            "Switzerland":              _ch("Europe", "B", "Famous for chocolate, cheese, and watches"),
            "Ukraine":                  _ch("Europe", "K", "Largest country lying entirely within Europe"),
            "United Kingdom":           _ch("Europe", "L", "Made up of England, Scotland, Wales, and Northern Ireland"),
            "Vatican City":             _ch("Europe", "V", "Smallest country in the world; home of the Pope"),
        }
    },

    # ── Oceania ──────────────────────────────────────────────────────────
    "countries_oceania": {
        "description": "Countries in Oceania",
        "answers_hints": {
            "Australia":         _ch("Oceania", "C", "Largest country in Oceania; home to kangaroos"),
            "Fiji":              _ch("Oceania", "S", "Island nation in the South Pacific"),
            "Kiribati":          _ch("Oceania", "T", "One of the first countries threatened by rising sea levels"),
            "Marshall Islands":  _ch("Oceania", "M"),
            "Micronesia":        _ch("Oceania", "P", "Full name: Federated States of Micronesia"),
            "Nauru":             _ch("Oceania", "Y", "Smallest island country in the world"),
            "New Zealand":       _ch("Oceania", "W", "Filming location for the Lord of the Rings trilogy"),
            "Palau":             _ch("Oceania", "N"),
            "Papua New Guinea":  _ch("Oceania", "P", "One of the most biodiverse countries on Earth"),
            "Samoa":             _ch("Oceania", "A"),
            "Solomon Islands":   _ch("Oceania", "H"),
            "Tonga":             _ch("Oceania", "N", "Only monarchy in the Pacific"),
            "Tuvalu":            _ch("Oceania", "F", "Second smallest country by area"),
            "Vanuatu":           _ch("Oceania", "P"),
        }
    },

    # ── Food ─────────────────────────────────────────────────────────────
    "food": {
        "description": "Common foods",
        "answers_hints": {
            "Pizza":          ["Italian dish", "Often has cheese and tomato sauce", "Baked in an oven", "Can be topped with pepperoni or vegetables"],
            "Sushi":          ["Japanese dish", "Usually involves rice and seafood", "Often served with soy sauce and wasabi"],
            "Burger":         ["American fast food", "Usually beef patty between two buns", "Often served with fries and ketchup"],
            "Pasta":          ["Italian dish", "Made from flour and water", "Comes in shapes like spaghetti and penne"],
            "Tacos":          ["Mexican dish", "Served in a folded tortilla", "Filled with meat, beans, or vegetables"],
            "Ramen":          ["Japanese noodle soup", "Often topped with pork, egg, and nori", "Has a rich broth"],
            "Curry":          ["Common in South and Southeast Asia", "Made with spices", "Often served with rice or bread"],
            "Steak":          ["Cut of beef", "Grilled or pan-fried", "Can be ordered rare, medium, or well-done"],
            "Fried rice":     ["Asian dish", "Made with leftover rice, vegetables, and often egg", "Often cooked in a wok"],
            "Sandwich":       ["Food between two slices of bread", "Named after the Earl of Sandwich"],
            "Soup":           ["Liquid dish", "Can be hot or cold", "Made by cooking ingredients in liquid or broth"],
            "Salad":          ["Dish of raw vegetables", "Often dressed with oil and vinegar", "Can include greens, tomatoes, and cucumber"],
            "Bread":          ["Baked food made from flour and water", "One of the oldest prepared foods", "Staple in many cultures"],
            "Cheese":         ["Dairy product made from milk", "Comes in hundreds of varieties", "Can be soft or hard"],
            "Chocolate":      ["Sweet food made from cacao beans", "Can be dark, milk, or white", "Popular in desserts worldwide"],
            "Ice cream":      ["Frozen dessert", "Made with cream and sugar", "Comes in many flavors"],
            "Pancakes":       ["Flat cake cooked on a griddle", "Often served with syrup or fruit", "Popular breakfast food"],
            "Waffles":        ["Grid-patterned baked food", "Often served with syrup or whipped cream", "Crispy on the outside"],
            "Dumplings":      ["Dough filled with meat or vegetables", "Common in many Asian cuisines", "Can be steamed or fried"],
            "Falafel":        ["Middle Eastern dish", "Deep-fried chickpea balls", "Often served in pita bread"],
            "Hummus":         ["Middle Eastern dip", "Made from chickpeas and tahini", "Often served with pita or vegetables"],
            "Croissant":      ["French pastry", "Flaky and buttery", "Crescent-shaped baked good"],
            "Paella":         ["Spanish rice dish", "Often made with seafood or chicken", "Named after the pan it is cooked in"],
            "Fish and chips": ["British dish", "Battered fried fish served with thick-cut fries", "Often seasoned with malt vinegar"],
            "Hot dog":        ["American fast food", "Sausage in a long bun", "Common at sporting events"],
            "Pho":            ["Vietnamese noodle soup", "Made with broth, rice noodles, and herbs", "Often served with beef or chicken"],
            "Churros":        ["Spanish/Latin American dessert", "Fried dough pastry", "Often dipped in chocolate sauce"],
            "Biryani":        ["South Asian dish", "Fragrant rice cooked with spices and meat", "Common in India and Pakistan"],
            "Dim sum":        ["Chinese dish", "Small bite-sized portions served in steamer baskets", "Usually enjoyed with tea"],
            "Gyoza":          ["Japanese dumplings", "Usually filled with pork and cabbage", "Pan-fried or steamed"],
        }
    },

    # ── Fruits ───────────────────────────────────────────────────────────
    "fruits": {
        "description": "Common fruits",
        "answers_hints": {
            "Apple":          ["Common red or green fruit", "Used to make cider", "Grows on trees", "'An apple a day keeps the doctor away'"],
            "Banana":         ["Yellow tropical fruit", "High in potassium", "Monkeys love this fruit", "Grows in large bunches"],
            "Orange":         ["Citrus fruit", "High in vitamin C", "Named after its color", "Florida and Spain are famous for them"],
            "Grape":          ["Grows in clusters on vines", "Used to make wine and raisins", "Can be red, green, or purple"],
            "Strawberry":     ["Red fruit with seeds on the outside", "Heart-shaped", "Popular in desserts and jam"],
            "Watermelon":     ["Large green fruit, red inside", "About 92% water", "Very popular in summer"],
            "Mango":          ["Tropical fruit", "Yellow or orange flesh", "National fruit of India", "Called the 'king of fruits'"],
            "Pineapple":      ["Tropical fruit with spiky exterior", "Yellow sweet flesh inside", "Grows from a plant on the ground, not a tree"],
            "Peach":          ["Fuzzy skin, orange-yellow flesh", "Georgia (USA) is known for growing this", "Related to plums and cherries"],
            "Pear":           ["Light green or yellow fruit", "Similar to an apple but pear-shaped", "Has a gritty, sweet flesh"],
            "Cherry":         ["Small round red fruit", "Grows on trees", "Has a hard stone/pit inside"],
            "Kiwi":           ["Brown fuzzy exterior, bright green inside", "Named after the New Zealand bird", "High in vitamin C"],
            "Lemon":          ["Yellow citrus fruit", "Very sour taste", "Used in cooking, baking, and cleaning"],
            "Lime":           ["Small green citrus fruit", "Used in cocktails and cooking", "Related to lemons and oranges"],
            "Coconut":        ["Large brown tropical fruit", "White flesh and coconut water inside", "Grows on palm trees"],
            "Avocado":        ["Technically a fruit (berry)", "Green creamy flesh", "Used to make guacamole", "High in healthy fats"],
            "Blueberry":      ["Small blue/purple berry", "Very high in antioxidants", "Often used in muffins and pancakes"],
            "Raspberry":      ["Small red or black berry", "Made up of small drupelets", "Tart and sweet flavor"],
            "Pomegranate":    ["Red fruit full of seeds", "High in antioxidants", "Ancient fruit; originated in the Middle East"],
            "Papaya":         ["Tropical fruit with orange flesh", "Contains the enzyme papain", "Common in tropical regions"],
            "Plum":           ["Purple or red smooth-skinned fruit", "Dried plums are called prunes", "Has a single hard stone inside"],
            "Apricot":        ["Small orange fruit", "Related to peaches and plums", "Often dried or made into jam"],
            "Fig":            ["Soft fruit with many tiny seeds inside", "Ancient fruit mentioned in the Bible", "Can be fresh or dried"],
            "Passion fruit":  ["Round tropical fruit, yellow or purple", "Intensely fragrant and flavorful", "Seeds are edible"],
            "Guava":          ["Tropical fruit, pink or white flesh", "Very high in vitamin C", "Common in Latin America and Asia"],
            "Lychee":         ["Small tropical fruit", "White translucent flesh", "Sweet and floral flavor"],
            "Dragon fruit":   ["Cactus fruit", "Pink or yellow exterior", "White or red flesh with black seeds"],
            "Jackfruit":      ["Very large tropical fruit", "Can weigh up to 80 pounds (36 kg)", "Used as a meat substitute"],
            "Durian":         ["Southeast Asian fruit", "Known for its extremely strong smell", "Called the 'king of fruits' in Southeast Asia"],
            "Mandarin":       ["Small citrus fruit", "Easy to peel", "Sweeter and less acidic than an orange"],
        }
    },

    # ── Vegetables ───────────────────────────────────────────────────────
    "vegetables": {
        "description": "Common vegetables",
        "answers_hints": {
            "Carrot":           ["Orange root vegetable", "Rich in vitamin A; good for eyesight", "Rabbits famously love this vegetable"],
            "Broccoli":         ["Green vegetable that looks like a tiny tree", "Member of the cabbage family", "High in vitamins C and K"],
            "Potato":           ["Starchy root vegetable", "Used for chips, fries, and mash", "Originated in South America"],
            "Tomato":           ["Technically a fruit but used as a vegetable", "Key ingredient in pizza sauce and ketchup", "Can be red, yellow, or green"],
            "Onion":            ["Makes your eyes water when cutting it", "Pungent smell and flavor", "Used as a base in almost every cuisine"],
            "Garlic":           ["Related to the onion family", "Strong flavor and smell used in cooking", "Folklore says it repels vampires"],
            "Spinach":          ["Dark green leafy vegetable", "High in iron and folate", "Popeye the Sailor Man eats this for strength"],
            "Lettuce":          ["Leafy green vegetable", "Main ingredient in salads", "Mostly water by weight"],
            "Cucumber":         ["Long green vegetable", "Very high water content", "Often pickled to make pickles/gherkins"],
            "Bell pepper":      ["Can be red, green, or yellow", "Sweet and crunchy texture", "High in vitamin C"],
            "Corn":             ["Also called maize", "Yellow kernels on a cob", "Can be popped into popcorn"],
            "Peas":             ["Small green spheres that grow in pods", "High in protein and fiber", "Often frozen and sold year-round"],
            "Beans":            ["Legume vegetable", "Comes in many varieties (kidney, black, green)", "High in protein and fiber"],
            "Cauliflower":      ["White vegetable", "Related to broccoli and cabbage", "Can be used as a low-carb pizza base"],
            "Cabbage":          ["Round leafy vegetable", "Used to make sauerkraut and coleslaw", "Related to broccoli and kale"],
            "Eggplant":         ["Purple vegetable", "Also called aubergine in British English", "Used in moussaka and ratatouille"],
            "Zucchini":         ["Long green vegetable", "Also called courgette", "A type of summer squash"],
            "Asparagus":        ["Long green stalks", "A spring vegetable", "Famous for causing a distinctive urine smell"],
            "Celery":           ["Long, crunchy green stalks", "Very low in calories", "Often used in soups, stews, and stocks"],
            "Mushroom":         ["Technically a fungus, not a plant", "Comes in many edible varieties", "Has a savory umami flavor"],
            "Kale":             ["Dark leafy green", "Considered a 'superfood'", "High in vitamins K, A, and C"],
            "Sweet potato":     ["Orange root vegetable", "Sweeter than a regular potato", "High in beta-carotene and fiber"],
            "Beetroot":         ["Round red root vegetable", "Used in borscht soup", "Can stain your hands and urine red"],
            "Artichoke":        ["Edible flower bud", "The heart is the most prized edible part", "Often used in dips"],
            "Brussels sprouts": ["Look like tiny cabbages", "Member of the cabbage family", "Often roasted or steamed as a side dish"],
            "Leek":             ["Related to onions and garlic", "Milder flavor than onion", "National vegetable of Wales"],
            "Radish":           ["Small red root vegetable", "Spicy and peppery flavor", "Eaten raw in salads"],
            "Turnip":           ["White and purple root vegetable", "Used in soups and stews", "Has a slightly bitter taste"],
            "Parsnip":          ["Pale white root vegetable", "Similar in shape to a carrot but white", "Sweeter when roasted"],
            "Pumpkin":          ["Large orange squash", "Used in pies and carved for Halloween", "Very high in beta-carotene"],
        }
    },
}

# Build the world countries preset from all five continents
_PRESET_DATA["countries_world"] = {
    "description": "All countries in the world",
    "answers_hints": {
        **_PRESET_DATA["countries_africa"]["answers_hints"],
        **_PRESET_DATA["countries_americas"]["answers_hints"],
        **_PRESET_DATA["countries_asia"]["answers_hints"],
        **_PRESET_DATA["countries_europe"]["answers_hints"],
        **_PRESET_DATA["countries_oceania"]["answers_hints"],
    },
}

_PRESET_CHOICES = [app_commands.Choice(name=f"{k} ({len(v['answers_hints'])} entries)", value=k)
                   for k, v in _PRESET_DATA.items()]

# ═══════════════════════════════════════════════════════
# RANDOM GAMES SYSTEM
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="addgame", description="Add a random game to the pool")
@app_commands.describe(
    name="The question/prompt shown to players",
    reward_balance="Coin reward for winner",
    reward_exp="EXP reward for winner",
    reward_tickets="Raffle ticket reward",
    reward_gamble_tokens="Gamble token reward",
    reward_vip_keys="VIP Chest Key reward",
    reward_item="Item or box name reward (optional)",
    reward_item_qty="Quantity of item reward (default 1)",
    reward_role="Role to give the winner (optional)",
    chance="Selection weight — higher = chosen more often (default 1.0)",
    answer_time="Seconds players have to answer this specific game (default 30)"
)
@command_enabled()
async def addgame(interaction: discord.Interaction, name: str,
                  reward_balance: int = 0, reward_exp: int = 0,
                  reward_tickets: int = 0, reward_gamble_tokens: int = 0,
                  reward_vip_keys: int = 0, reward_item: str = None,
                  reward_item_qty: int = 1, reward_role: discord.Role = None,
                  chance: float = 1.0, answer_time: int = 30):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if chance <= 0:
        await interaction.response.send_message("❌ Chance must be > 0.", ephemeral=True); return
    if answer_time < 5:
        await interaction.response.send_message("❌ Answer time must be ≥ 5 seconds.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute(
                    "INSERT INTO games(guild_id,game_name,reward_balance,reward_exp,"
                    "reward_tickets,reward_gamble_tokens,reward_vip_keys,"
                    "reward_item,reward_item_qty,reward_role_id,chance,answer_time) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (interaction.guild.id, name, reward_balance, reward_exp,
                     reward_tickets, reward_gamble_tokens, reward_vip_keys,
                     reward_item, reward_item_qty,
                     reward_role.id if reward_role else 0, chance, answer_time))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(f"❌ Game **{name}** already exists.",
                                                        ephemeral=True); return
    parts = []
    if reward_balance > 0:        parts.append(f"💰 {reward_balance:,}")
    if reward_exp > 0:            parts.append(f"⭐ {reward_exp:,} EXP")
    if reward_tickets > 0:        parts.append(f"🎟 {reward_tickets}")
    if reward_gamble_tokens > 0:  parts.append(f"🎲 {reward_gamble_tokens}")
    if reward_vip_keys > 0:       parts.append(f"🔑 {reward_vip_keys}")
    if reward_item:               parts.append(f"🎒 {reward_item_qty}x {reward_item}")
    if reward_role:               parts.append(f"👑 {reward_role.mention}")
    await interaction.response.send_message(
        f"✅ Added game **{name}**\n"
        f"Reward: {' + '.join(parts) or 'None'} | Chance weight: {chance} | Answer time: {answer_time}s\n"
        f"Use `/addgameanswer` or `/addgamepreset` to add answers.")


@bot.tree.command(name="editgame", description="Edit a game's rewards, chance weight, or answer time")
@app_commands.describe(
    name="Game to edit",
    reward_balance="New coin reward",
    reward_exp="New EXP reward",
    reward_tickets="New ticket reward",
    reward_gamble_tokens="New gamble token reward",
    reward_vip_keys="New VIP key reward",
    reward_item="New item/box reward (type 'none' to clear)",
    reward_item_qty="New item quantity",
    reward_role="New role reward (leave empty to keep current)",
    clear_role="Set True to remove the role reward",
    chance="New selection weight",
    answer_time="New answer time in seconds"
)
@command_enabled()
async def editgame(interaction: discord.Interaction, name: str,
                   reward_balance:        Optional[int]          = None,
                   reward_exp:            Optional[int]          = None,
                   reward_tickets:        Optional[int]          = None,
                   reward_gamble_tokens:  Optional[int]          = None,
                   reward_vip_keys:       Optional[int]          = None,
                   reward_item:           Optional[str]          = None,
                   reward_item_qty:       Optional[int]          = None,
                   reward_role:           Optional[discord.Role] = None,
                   clear_role:            Optional[bool]         = None,
                   chance:                Optional[float]        = None,
                   answer_time:           Optional[int]          = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return

    async with get_db() as db:
        async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                              (interaction.guild.id, name)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message(f"❌ Game **{name}** not found.",
                                                        ephemeral=True); return

    updates: dict = {}
    if reward_balance is not None:       updates["reward_balance"]        = max(0, reward_balance)
    if reward_exp is not None:           updates["reward_exp"]            = max(0, reward_exp)
    if reward_tickets is not None:       updates["reward_tickets"]        = max(0, reward_tickets)
    if reward_gamble_tokens is not None: updates["reward_gamble_tokens"]  = max(0, reward_gamble_tokens)
    if reward_vip_keys is not None:      updates["reward_vip_keys"]       = max(0, reward_vip_keys)
    if reward_item is not None:
        updates["reward_item"] = None if reward_item.strip().lower() == "none" else reward_item.strip()
    if reward_item_qty is not None:      updates["reward_item_qty"]       = max(1, reward_item_qty)
    if reward_role is not None:          updates["reward_role_id"]        = reward_role.id
    if clear_role:                       updates["reward_role_id"]        = 0
    if chance is not None:
        if chance <= 0:
            await interaction.response.send_message("❌ Chance must be > 0.", ephemeral=True); return
        updates["chance"] = chance
    if answer_time is not None:
        if answer_time < 5:
            await interaction.response.send_message("❌ Answer time must be ≥ 5 seconds.", ephemeral=True); return
        updates["answer_time"] = answer_time

    if not updates:
        await interaction.response.send_message("❌ No changes provided.", ephemeral=True); return

    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [interaction.guild.id, name]
    async with db_lock:
        async with get_db() as db:
            await db.execute(f"UPDATE games SET {set_clause} WHERE guild_id=? AND game_name=?", values)
            await db.commit()

    changed = ", ".join(f"**{k}** → `{v}`" for k, v in updates.items())
    await interaction.response.send_message(f"✅ Updated game **{name}**: {changed}")
                       
# ── List games (paginated) ────────────────────────────────────────────────────

class GameListView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], user_id: int, initial_page: int = 0):
        super().__init__(timeout=120)
        self.pages   = pages
        self.current = max(0, min(initial_page, len(pages) - 1))
        self.user_id = user_id
        self._sync()

    def _sync(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.label == "◀":
                    item.disabled = (self.current == 0)
                elif item.label == "▶":
                    item.disabled = (self.current >= len(self.pages) - 1)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, disabled=True)
    async def prev_page(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your menu.", ephemeral=True); return
        self.current -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your menu.", ephemeral=True); return
        self.current += 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

# ── Channel setup ─────────────────────────────────────────────────────────────

@bot.tree.command(name="setgamechannel",
                  description="Set the channel for random games, interval, and hint timing")
@app_commands.describe(
    channel="Channel for games",
    interval_seconds="Seconds between one game ENDING and the next one STARTING (default 60)",
    hint1_delay="Seconds after question to reveal hint 1 (omit to disable hints)",
    hint2_delay="Seconds after question to reveal hint 2",
    hint3_delay="Seconds after question to reveal hint 3",
    hint4_delay="Seconds after question to reveal hint 4",
    hint5_delay="Seconds after question to reveal hint 5"
)
@command_enabled()
async def setgamechannel(interaction: discord.Interaction, channel: discord.TextChannel,
                         interval_seconds: int = 60,
                         hint1_delay: Optional[int] = None,
                         hint2_delay: Optional[int] = None,
                         hint3_delay: Optional[int] = None,
                         hint4_delay: Optional[int] = None,
                         hint5_delay: Optional[int] = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if interval_seconds < 5:
        await interaction.response.send_message("❌ Interval must be ≥ 5 seconds.", ephemeral=True); return

    delays = [d for d in [hint1_delay, hint2_delay, hint3_delay, hint4_delay, hint5_delay]
              if d is not None]
    hint_delays_json = json.dumps(delays) if delays else None

    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO game_config(guild_id,channel_id,interval_seconds,hint_delays) "
                "VALUES(?,?,?,?)",
                (interaction.guild.id, channel.id, interval_seconds, hint_delays_json))
            await db.commit()

    hint_info = (f" | Hints at: {', '.join(str(d)+'s' for d in delays)}" if delays
                 else " | No hints configured")
    await interaction.response.send_message(
        f"✅ Game channel: {channel.mention} | Interval: **{interval_seconds}s** (after game ends){hint_info}")


@bot.tree.command(name="startgames", description="Start automatic random games")
@command_enabled()
async def startgames(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid = interaction.guild.id
    if gid in game_tasks and not game_tasks[gid].done():
        await interaction.response.send_message("❌ Games already running.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT channel_id FROM game_config WHERE guild_id=?", (gid,)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message("❌ Use `/setgamechannel` first.",
                                                        ephemeral=True); return
        async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND enabled=1", (gid,)) as cur:
            if not await cur.fetchall():
                await interaction.response.send_message("❌ No enabled games. Use `/addgame`.",
                                                        ephemeral=True); return
    game_tasks[gid] = asyncio.create_task(guild_game_loop(gid))
    await interaction.response.send_message("🎮 Random games started!")

# ── Game presets ──────────────────────────────────────────────────────────────

@bot.tree.command(name="addgamepreset",
                  description="Bulk-add a preset of answers (and hints) to a game")
@app_commands.describe(
    game_name="Game to add answers to",
    preset="Which preset to load"
)
@app_commands.choices(preset=_PRESET_CHOICES)
@command_enabled()
async def addgamepreset(interaction: discord.Interaction, game_name: str, preset: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await interaction.response.defer()

    async with get_db() as db:
        async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                              (interaction.guild.id, game_name)) as cur:
            if not await cur.fetchone():
                await interaction.followup.send(f"❌ Game **{game_name}** not found."); return

    preset_data = _PRESET_DATA[preset]
    added = skipped = hints_added = 0

    async with db_lock:
        async with get_db() as db:
            for answer, hints in preset_data["answers_hints"].items():
                # Check if answer already exists
                async with db.execute(
                    "SELECT id FROM game_answers WHERE guild_id=? AND game_name=? AND answer=?",
                    (interaction.guild.id, game_name, answer)) as cur:
                    existing = await cur.fetchone()

                if existing:
                    ans_id = existing[0]
                    skipped += 1
                else:
                    cur = await db.execute(
                        "INSERT INTO game_answers(guild_id,game_name,answer) VALUES(?,?,?)",
                        (interaction.guild.id, game_name, answer))
                    ans_id = cur.lastrowid
                    added += 1

                # Replace all hints for this answer with the preset hints
                await db.execute(
                    "DELETE FROM game_hints WHERE guild_id=? AND game_name=? AND answer_id=?",
                    (interaction.guild.id, game_name, ans_id))
                for order, hint_text in enumerate(hints[:5], 1):
                    await db.execute(
                        "INSERT INTO game_hints(guild_id,game_name,answer_id,hint_text,hint_order) "
                        "VALUES(?,?,?,?,?)",
                        (interaction.guild.id, game_name, ans_id, hint_text, order))
                    hints_added += 1

            await db.commit()

    total = len(preset_data["answers_hints"])
    await interaction.followup.send(
        f"✅ Preset **{preset}** loaded into **{game_name}**!\n"
        f"Added: **{added}** answers | Skipped (already existed): **{skipped}** | "
        f"Hints written: **{hints_added}** (across {total} total entries)")


# ── Game loop ─────────────────────────────────────────────────────────────────

async def guild_game_loop(guild_id: int):
    await bot.wait_until_ready()
    while not bot.is_closed():
        # Fetch config
        async with get_db() as db:
            async with db.execute(
                "SELECT channel_id, interval_seconds, hint_delays "
                "FROM game_config WHERE guild_id=?", (guild_id,)) as cur:
                config = await cur.fetchone()
        if not config: break
        channel_id, interval_seconds, hint_delays_json = config
        channel = bot.get_channel(channel_id)
        if not channel: await asyncio.sleep(30); continue

        hint_delays: list[int] = []
        if hint_delays_json:
            try:
                hint_delays = json.loads(hint_delays_json)
            except Exception:
                hint_delays = []

        # Fetch enabled games with all reward columns
        async with get_db() as db:
            async with db.execute(
                "SELECT game_name, reward_balance, reward_exp, reward_tickets, "
                "reward_gamble_tokens, reward_vip_keys, reward_item, reward_item_qty, "
                "reward_role_id, chance, answer_time "
                "FROM games WHERE guild_id=? AND enabled=1",
                (guild_id,)) as cur:
                game_rows = await cur.fetchall()

        eligible: list[dict] = []
        for row in game_rows:
            (gname, rb, re, rt, rgt, rvk, ri, riq, rrole, chance, atime) = row
            async with get_db() as db:
                async with db.execute(
                    "SELECT id, answer FROM game_answers WHERE guild_id=? AND game_name=?",
                    (guild_id, gname)) as cur:
                    answers = await cur.fetchall()
            if answers:
                eligible.append({
                    "name":                 gname,
                    "reward_balance":       rb   or 0,
                    "reward_exp":           re   or 0,
                    "reward_tickets":       rt   or 0,
                    "reward_gamble_tokens": rgt  or 0,
                    "reward_vip_keys":      rvk  or 0,
                    "reward_item":          ri,
                    "reward_item_qty":      riq  or 1,
                    "reward_role_id":       rrole or 0,
                    "chance":               chance or 1.0,
                    "answer_time":          atime  or 30,
                    "answers":              answers,
                })

        if not eligible:
            await asyncio.sleep(interval_seconds)
            continue

        # Weighted random game selection
        game = random.choices(eligible, weights=[g["chance"] for g in eligible], k=1)[0]
        correct_id, correct_ans = random.choice(game["answers"])
        answer_time = game["answer_time"]

        # Build reward summary
        guild_obj = bot.get_guild(guild_id)
        reward_parts = []
        if game["reward_balance"] > 0:        reward_parts.append(f"💰 {game['reward_balance']:,} coins")
        if game["reward_exp"] > 0:            reward_parts.append(f"⭐ {game['reward_exp']:,} EXP")
        if game["reward_tickets"] > 0:        reward_parts.append(f"🎟 {game['reward_tickets']} ticket(s)")
        if game["reward_gamble_tokens"] > 0:  reward_parts.append(f"🎲 {game['reward_gamble_tokens']} token(s)")
        if game["reward_vip_keys"] > 0:       reward_parts.append(f"🔑 {game['reward_vip_keys']} key(s)")
        if game["reward_item"]:               reward_parts.append(f"🎒 {game['reward_item_qty']}x {game['reward_item']}")
        if game["reward_role_id"] and guild_obj:
            role = guild_obj.get_role(game["reward_role_id"])
            if role: reward_parts.append(f"👑 {role.mention}")

        embed = discord.Embed(
            title="🎮 Random Game!",
            color=discord.Color.teal(),
            description=f"**{game['name']}**\n\nType your answer in chat!\n⏰ You have **{answer_time} seconds**.")
        if reward_parts:
            embed.add_field(name="🏆 Winner gets", value=" + ".join(reward_parts), inline=False)
        embed.set_footer(text=f"Answer within {answer_time} seconds!")
        await channel.send(embed=embed)

        answered_event = asyncio.Event()
        active_game_sessions[guild_id] = {
            "game_name":  game["name"],
            "answer":     correct_ans,
            "channel_id": channel_id,
            "answered":   False,
            "winner":     None,
            "event":      answered_event,
        }

        # Fetch hints for the selected answer and schedule them
        hints: list[str] = []
        if hint_delays:
            async with get_db() as db:
                async with db.execute(
                    "SELECT hint_text FROM game_hints "
                    "WHERE guild_id=? AND game_name=? AND answer_id=? ORDER BY hint_order",
                    (guild_id, game["name"], correct_id)) as cur:
                    hints = [r[0] for r in await cur.fetchall()]

        hint_tasks = []
        for i, delay in enumerate(hint_delays):
            if i < len(hints) and 0 < delay < answer_time:
                task = asyncio.create_task(
                    _send_hint_at(channel, f"{i+1}: {hints[i]}", delay, answered_event))
                hint_tasks.append(task)

        # Wait for a correct answer or timeout
        try:
            await asyncio.wait_for(answered_event.wait(), timeout=answer_time)
        except asyncio.TimeoutError:
            pass

        # Cancel any hints that haven't fired yet
        for task in hint_tasks:
            task.cancel()

        session = active_game_sessions.pop(guild_id, None)
        if not session:
            await asyncio.sleep(interval_seconds)
            continue

        if session.get("answered") and session.get("winner"):
            winner = session["winner"]
            if game["reward_balance"] > 0:
                await add_balance(guild_id, winner.id, game["reward_balance"])
            if game["reward_exp"] > 0:
                await add_exp(guild_id, winner.id, game["reward_exp"])
            if game["reward_tickets"] > 0:
                await add_tickets(guild_id, winner.id, game["reward_tickets"])
            if game["reward_gamble_tokens"] > 0:
                await inventory_add(guild_id, winner.id, GAMBLE_TOKEN, game["reward_gamble_tokens"])
            if game["reward_vip_keys"] > 0:
                await inventory_add(guild_id, winner.id, VIP_CHEST_KEY, game["reward_vip_keys"])
            if game["reward_item"]:
                await inventory_add(guild_id, winner.id, game["reward_item"], game["reward_item_qty"])
            if game["reward_role_id"] and guild_obj:
                role   = guild_obj.get_role(game["reward_role_id"])
                member = guild_obj.get_member(winner.id)
                if role and member:
                    try: await member.add_roles(role)
                    except Exception: pass
            result_embed = discord.Embed(
                title="🎉 Correct!", color=discord.Color.green(),
                description=f"{winner.mention} got it! The answer was **{correct_ans}**.")
            if reward_parts:
                result_embed.add_field(name="Reward given", value=" + ".join(reward_parts), inline=False)
        else:
            result_embed = discord.Embed(
                title="⏰ Time's Up!", color=discord.Color.red(),
                description=f"Nobody got it. The answer was **{correct_ans}**.")

        await channel.send(embed=result_embed)

        # Interval is the gap between THIS game ENDING and the NEXT one STARTING
        await asyncio.sleep(interval_seconds)


async def game_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for gid, task in list(game_tasks.items()):
            if task.done():
                try:
                    if exc := task.exception():
                        print(f"[GameLoop] Guild {gid} crashed: {exc}")
                except Exception:
                    pass
                game_tasks[gid] = asyncio.create_task(guild_game_loop(gid))
        await asyncio.sleep(30)

# ─── LEADERBOARD STAT MANAGEMENT ─────────────────────────────────────────────

_STAT_CHOICES = [
    app_commands.Choice(name="Total EXP",        value="total_exp"),
    app_commands.Choice(name="Gifted Balance",    value="gifted_balance"),
    app_commands.Choice(name="Chests Opened",     value="chests_opened"),
    app_commands.Choice(name="Lifetime Tickets",  value="raffle_tickets_bought"),
]
        
# ─── HELP SYSTEM ─────────────────────────────────────────────────────────────

# (emoji, display title, [(command_name, description), ...])
_HELP_CATS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "giveaway": ("🎉", "Giveaway Commands", [
        ("giveaway",          "Create a giveaway. Duration in **seconds**. Supports coin, EXP, ticket, gamble token, VIP key, role, and item/box rewards."),
        ("reroll",            "Reroll a finished giveaway by its message ID. The old winner's coins are refunded."),
        ("addautogiveaway",   "Add a prize/reward/winners preset to the auto-rotation pool."),
        ("removeautogiveaway","Remove a preset from the auto pool by prize name."),
        ("startgiveaways",    "Start posting giveaways automatically at a fixed interval using the presets in the pool."),
        ("stopgiveaways",     "Stop the automatic giveaway loop."),
        ("addgiveawayrole",   "Give a role permission to manage giveaways and use admin commands."),
        ("removegiveawayrole","Remove a role's management permissions."),
        ("giveawayroles",     "List all roles with giveaway/admin permissions."),
    ]),
    "economy": ("💰", "Economy & EXP", [
        ("balance",             "Check a user's coin balance."),
        ("gift",                "Send coins to another user (deducted from your own balance)."),
        ("addbalance",          "Admin: add coins to a user."),
        ("removebalance",       "Admin: remove coins from a user."),
        ("level",               "Show Level, **Total EXP (7d)** (used for level), and **Usable EXP** (spent on chests)."),
        ("addexp",              "Admin: add **usable EXP only** — does **not** change level or Total EXP (7d)."),
        ("removeexp",           "Admin: deduct EXP from a user."),
        ("addtotalexp",         "Admin: add to **Total EXP (7d) and level only** — usable EXP stays the same."),
        ("removetotalexp",      "Admin: remove from **Total EXP (7d) and level only** — usable EXP stays the same."),
        ("expboost",            "Set a chat-EXP multiplier for a role, e.g. +50% or -25%. Multiple roles are summed."),
        ("removeexpboost",      "Remove a role's EXP multiplier."),
        ("listexpboosts",       "List all active EXP boosts."),
        ("leaderboard",         "Top-10 across: Balance, Total EXP, Current EXP, Tickets, Chests Opened, Gifted Balance."),
        ("addleaderboardstat",  "Admin: directly add to a user's leaderboard stat."),
        ("removeleaderboardstat","Admin: directly subtract from a user's leaderboard stat."),
    ]),
    "raffle": ("🎟", "Raffle", [
        ("buytickets",          "Buy tickets for 100 coins each. More tickets → higher win chance (weighted draw)."),
        ("rafflechance",        "Check a user's ticket count and current win probability."),
        ("addtickets",          "Admin: add tickets to a user."),
        ("removetickets",       "Admin: remove tickets from a user."),
        ("setrafflechannel",    "Set the channel for daily winner announcements."),
        ("setraffleinfochannel","Post a live status board showing the pool and top participants (auto-updates every 60 s)."),
    ]),
    "chests": ("📦", "Chests", [
        ("chest",               "Open EXP chest(s). Costs **1 000 EXP** each. Bulk-open when you have ≥1 400 EXP."),
        ("vipchest",            "Open VIP Chest(s) — costs 1 **VIP Chest Key** each (max 10). Better prizes than normal chests."),
        ("givekey",             "Admin: give VIP Chest Keys to a user."),
        ("takekey",             "Admin: take VIP Chest Keys from a user."),
        ("addchestprize",       "Admin: add a custom prize to the EXP or VIP chest loot table (overrides defaults for this server)."),
        ("removechestprize",    "Admin: remove a custom chest prize by ID (see /listchestprizes)."),
        ("listchestprizes",     "List all prizes and drop percentages for a chest type."),
        ("addrarechestdrop",    "Admin: mark a prize name **or ID** as a rare drop for announcements. Custom list replaces defaults once any entry is added."),
        ("removerarechestdrop", "Admin: unmark a prize as a rare drop."),
        ("setraredropchannel",  "Set the channel for all rare-drop announcements (chests, VIP chests, and boxes)."),
    ]),
    "items": ("🛒", "Item Store & Inventory", [
        ("item store",  "Browse items available to purchase."),
        ("item buy",    "Buy an item for coins (goes to your inventory)."),
        ("item use",    "Redeem a store item to receive its Discord role."),
        ("item inv",    "View a user's inventory — items, boxes, VIP keys, and gamble tokens."),
        ("item info",   "Show details for an item **or** box (includes all prizes and drop chances)."),
        ("item give",   "Admin: give any item, box, VIP Chest Key, or Gamble Token to a user."),
        ("item take",   "Admin: take any item, box, VIP Chest Key, or Gamble Token from a user."),
        ("item add",    "Admin: add a new purchasable item to the store (linked to a Discord role)."),
        ("item remove", "Admin: remove an item from the store."),
    ]),
    "boxes": ("🎁", "Admin Abuse Boxes", [
        ("addbox",          "Create a new box."),
        ("removebox",       "Delete a box and all its prizes permanently."),
        ("addboxprize",     "Add a prize to a box — balance, EXP, item, nothing, or a custom label."),
        ("removeboxprize",  "Remove a specific prize from a box by ID (see /listboxes)."),
        ("listboxes",       "List every box with its prizes, weights, and percentage chances."),
        ("givebox",         "Give boxes to every member that has a specific role."),
        ("openbox",         "Open one or more boxes from your inventory (max 20 at once)."),
        ("addrarebox",      "Admin: mark a box prize by ID as a rare drop → triggers an announcement."),
        ("removerarebox",   "Admin: unmark a box prize as a rare drop."),
    ]),
    "gambling": ("🎲", "Gambling", [
        ("blackjack",       (
            "Play blackjack vs the dealer — costs 1 **Gamble Token**.\n"
            "**Hit** ➕ draw a card\n"
            "**Stand** ✋ end your turn\n"
            "**Double** ⬆️ first action only: double your bet, receive exactly one more card, then auto-stand\n"
            "**Split** ✂️ first action only, same-value cards: split into two independent hands each with the original bet\n"
            "Natural 21 pays **1.5×**. Dealer stands on soft 17."
        )),
        ("roulette",        (
            "Spin the wheel — costs 1 **Gamble Token**.\n"
            "**×36** — single number `0`–`36`\n"
            "**×2** — `red` · `black` · `even` · `odd` · `1-18` / `low` · `19-36` / `high`\n"
            "**×3 dozens** — `1-12` · `13-24` · `25-36` (aliases: `dozen1/2/3`)\n"
            "**×3 columns** — `col1` / `1st` (3n+1) · `col2` / `2nd` (3n+2) · `col3` / `3rd` (3n)\n"
            "0 only wins on a single-number bet."
        )),
        ("givegambletoken", "Admin: give Gamble Tokens to a user."),
        ("takegambletoken", "Admin: take Gamble Tokens from a user."),
    ]),
    "games": ("🎮", "Random Games", [
        ("addgame",         "Add a trivia/guessing game question with optional coin + EXP rewards for the winner."),
        ("removegame",      "Delete a game and all its answers."),
        ("enablegame",      "Enable a disabled game so it appears in automatic rotation."),
        ("disablegame",     "Disable a game without deleting it (excluded from rotation)."),
        ("addgameanswer",   "Add a valid answer to a game (case-insensitive matching)."),
        ("removegameanswer","Remove an answer by its ID (see /listgames)."),
        ("listgames",       "List all games with answers, rewards, and enabled/disabled status."),
        ("setgamechannel",  "Set the posting channel, the answer window in seconds, and the interval between games."),
        ("startgames",      "Start the automatic game loop in the configured channel."),
        ("stopgames",       "Stop the automatic game loop."),
    ]),
    "trade": ("🤝", "Trading", [
        ("trade", (
            "Open an interactive trade session with another user.\n"
            "Both parties click **Set Offer** to enter what they're offering "
            "(coins, EXP, raffle tickets, and any inventory items/boxes), "
            "then both click **Confirm** to execute. Either party can cancel at any time. "
            "Times out after 5 minutes."
        )),
    ]),
    "codes": ("🎫", "Redeemable Codes", [
        ("createcode", "Admin: create a code with any prize mix. Supports limited or unlimited uses, min level/balance, and a required role."),
        ("deletecode", "Admin: delete a code and its usage history."),
        ("listcodes",  "Admin: list all active codes with prizes, uses remaining, and requirements."),
        ("redeem",     "Redeem a code to claim its prizes (each code can only be used once per user)."),
    ]),
    "admin": ("⚙️", "Admin & System", [
        ("disablecmd",          "Temporarily disable any bot command by name."),
        ("enablecmd",           "Re-enable a previously disabled command."),
        ("enablesystem",        "Enable a major system: **raffle**, **vipkey**, or **gamble**."),
        ("disablesystem",       "Disable a major system (blocks related commands for all users)."),
        ("systemstatus",        "Check which major systems are currently enabled or disabled."),
        ("setraredropchannel",  "Set the announcement channel for rare drops from chests and boxes."),
        ("addgiveawayrole",     "Give a role giveaway/admin permissions."),
        ("removegiveawayrole",  "Remove a role's permissions."),
        ("giveawayroles",       "List all privileged roles."),
    ]),
}

# Flat lookup: command_name → (category_title, description)
_HELP_LOOKUP: dict[str, tuple[str, str]] = {}
for _ck, (_ce, _ct, _cc) in _HELP_CATS.items():
    for _cn, _cd in _cc:
        _HELP_LOOKUP[_cn.lower()] = (_ct, _cd)


@bot.tree.command(name="help", description="Overview of the bot or detailed info on a specific command")
@app_commands.describe(command="Command name or category (blank for full overview)")
@command_enabled()
async def help_cmd(interaction: discord.Interaction, command: str = None):
    if command is None:
        embed = discord.Embed(
            title="📖 Bot Help",
            description=(
                "A giveaway, economy, gambling, and games bot.\n"
                "Use `/help <command>` or `/help <category>` for details.\n\u200b"
            ),
            color=discord.Color.blurple(),
        )
        for ck, (ce, ct, cc) in _HELP_CATS.items():
            sample = ", ".join(f"`{n}`" for n, _ in cc[:4])
            more   = f" *+{len(cc)-4} more*" if len(cc) > 4 else ""
            embed.add_field(name=f"{ce} {ct}", value=sample + more, inline=False)
        embed.set_footer(text="/help <command name>  or  /help <category key, e.g. gambling>")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    key = command.lower().strip().lstrip("/")

    # Category match
    if key in _HELP_CATS:
        ce, ct, cc = _HELP_CATS[key]
        embed = discord.Embed(title=f"{ce} {ct}", color=discord.Color.blurple())
        for cn, cd in cc:
            embed.add_field(name=f"`/{cn}`", value=cd, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Command match
    if key in _HELP_LOOKUP:
        ct, cd = _HELP_LOOKUP[key]
        embed  = discord.Embed(title=f"📖 /{key}", description=cd, color=discord.Color.blurple())
        embed.set_footer(text=f"Category: {ct}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.send_message(
        f"❌ No command or category **{command}** found.\n"
        "Valid categories: " + ", ".join(f"`{k}`" for k in _HELP_CATS),
        ephemeral=True,
    )

# ═══════════════════════════════════════════════════════
# PREFIX COMMANDS  (replaces removed slash commands)
# ═══════════════════════════════════════════════════════

# ── Balance ───────────────────────────────────────────────────────────────────

@bot.command(name="gift")
async def cmd_gift(ctx, user: discord.Member, amount: int):
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    if user.id == ctx.author.id: await ctx.send("❌ You cannot gift yourself."); return
    bal = await get_balance(ctx.guild.id, ctx.author.id)
    if bal < amount: await ctx.send("❌ Not enough balance."); return
    await add_balance(ctx.guild.id, ctx.author.id, -amount)
    await add_balance(ctx.guild.id, user.id, amount)
    await add_stat(ctx.guild.id, ctx.author.id, "gifted_balance", amount)
    await ctx.send(f"💸 You gifted **{amount:,}** coins to {user.mention}!")
    await log_event(ctx.guild.id, "balance", _log_embed("🎁 Gift Sent", discord.Color.green(),
        From=ctx.author.mention, To=user.mention, Amount=f"{amount:,}"))

@bot.command(name="addbalance")
async def cmd_addbalance(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await add_balance(ctx.guild.id, user.id, amount)
    await ctx.send(f"✅ Added {amount:,} coins to {user.mention}.")
    await log_event(ctx.guild.id, "balance", _log_embed("💰 Balance Added", discord.Color.green(),
        Admin=ctx.author.mention, User=user.mention, Amount=f"+{amount:,}"))
    await log_event(ctx.guild.id, "admin", _log_embed("⚙️ addbalance", discord.Color.orange(),
        By=ctx.author.mention, User=user.mention, Amount=f"+{amount:,}"))

@bot.command(name="removebalance")
async def cmd_removebalance(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await add_balance(ctx.guild.id, user.id, -amount)
    await ctx.send(f"❌ Removed {amount:,} coins from {user.mention}.")
    await log_event(ctx.guild.id, "balance", _log_embed("💸 Balance Removed", discord.Color.red(),
        Admin=ctx.author.mention, User=user.mention, Amount=f"-{amount:,}"))
    await log_event(ctx.guild.id, "admin", _log_embed("⚙️ removebalance", discord.Color.orange(),
        By=ctx.author.mention, User=user.mention, Amount=f"-{amount:,}"))

# ── EXP ───────────────────────────────────────────────────────────────────────

@bot.command(name="addexp")
async def cmd_addexp(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    await add_exp(ctx.guild.id, user.id, amount, is_bonus=True)
    await ctx.send(f"✅ Added **{amount:,}** usable EXP to {user.mention}.")
    await log_event(ctx.guild.id, "exp", _log_embed("⭐ Usable EXP Added", discord.Color.green(),
        Admin=ctx.author.mention, User=user.mention, Amount=f"+{amount:,}"))

@bot.command(name="removeexp")
async def cmd_removeexp(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await add_exp(ctx.guild.id, user.id, -amount)
    await ctx.send(f"❌ Removed {amount:,} EXP from {user.mention}.")
    await log_event(ctx.guild.id, "exp", _log_embed("📉 EXP Removed", discord.Color.red(),
        Admin=ctx.author.mention, User=user.mention, Amount=f"-{amount:,}"))

@bot.command(name="addtotalexp")
async def cmd_addtotalexp(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    now = int(datetime.now(UTC).timestamp())
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                             (ctx.guild.id, user.id, amount, now, 0))
            await db.execute("INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                             (ctx.guild.id, user.id, -amount, now, 0))
            await db.commit()
    await ctx.send(f"✅ Added **{amount:,}** to {user.mention}'s Total EXP (7d) / Activity Rank. Usable EXP unchanged.")

@bot.command(name="removetotalexp")
async def cmd_removetotalexp(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    remaining = amount; actually_removed = 0
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT rowid, amount FROM exp_history "
                "WHERE guild_id=? AND user_id=? AND timestamp>=? AND amount>0 AND is_bonus=0 "
                "ORDER BY timestamp ASC", (ctx.guild.id, user.id, week_ago)) as cur:
                entries = await cur.fetchall()
            for rowid, entry_amount in entries:
                if remaining <= 0: break
                if entry_amount <= remaining:
                    await db.execute("DELETE FROM exp_history WHERE rowid=?", (rowid,))
                    remaining -= entry_amount
                else:
                    await db.execute("UPDATE exp_history SET amount=? WHERE rowid=?",
                                     (entry_amount - remaining, rowid)); remaining = 0
            actually_removed = amount - remaining
            if actually_removed > 0:
                await db.execute(
                    "INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                    (ctx.guild.id, user.id, actually_removed, int(datetime.now(UTC).timestamp()), 1))
            await db.commit()
    if actually_removed == 0:
        await ctx.send(f"❌ {user.mention} has no Total EXP (7d) to remove.")
    else:
        await ctx.send(f"✅ Removed **{actually_removed:,}** from {user.mention}'s Total EXP (7d). Usable EXP unchanged.")
    await log_event(ctx.guild.id, "exp", _log_embed("📉 Total EXP Removed", discord.Color.orange(),
        Admin=ctx.author.mention, User=user.mention,
        Removed=f"-{actually_removed:,}", Requested=f"-{amount:,}"))

# ── Giveaway ──────────────────────────────────────────────────────────────────

@bot.command(name="reroll")
async def cmd_reroll(ctx, message_id: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    try: mid = int(message_id)
    except ValueError: await ctx.send("❌ Invalid message ID."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT * FROM giveaways WHERE message_id=?", (mid,)) as cur:
                data = await cur.fetchone()
    if not data: await ctx.send("❌ Giveaway not found."); return
    (_mid, channel_id, prize_raw, winner_count, legacy_reward,
     end_time, required_role, template, ended) = data
    channel = bot.get_channel(channel_id)
    if not channel: await ctx.send("❌ Channel not found."); return
    try: message = await channel.fetch_message(mid)
    except discord.NotFound: await ctx.send("❌ Message not found."); return
    reaction = discord.utils.get(message.reactions, emoji="🎉")
    if not reaction: await ctx.send("❌ Reaction not found."); return
    users = []
    async for user in reaction.users():
        if user.bot: continue
        member = channel.guild.get_member(user.id)
        if not member: continue
        if required_role and required_role not in [r.id for r in member.roles]: continue
        users.append(user)
    if not users: await ctx.send("❌ No participants."); return
    weighted = []
    for user in users:
        lvl = await get_level(ctx.guild.id, user.id)
        weighted.extend([user] * random.randint(1, max(1, min(10, lvl))))
    new_winner = random.choice(weighted)
    try:
        _parsed = json.loads(prize_raw)
        meta = _parsed if isinstance(_parsed, dict) else {"label": str(prize_raw), "balance": legacy_reward}
        prize_label = meta.get("label", prize_raw)
    except Exception:
        meta = {"label": str(prize_raw), "balance": legacy_reward}; prize_label = str(prize_raw)
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO giveaway_winners VALUES(?,?,?)",
                             (mid, new_winner.id, int(meta.get("balance", 0))))
            await db.commit()
    await distribute_prizes(channel.guild, [new_winner], meta)
    embed = discord.Embed(title="🔄 Giveaway Rerolled",
        description=f"**Prize:** {prize_label}\n**Reward:** {build_reward_summary(meta, channel.guild)}\n**New Winner:** {new_winner.mention}",
        color=discord.Color.orange())
    await channel.send(embed=embed)
    await ctx.send("✅ Giveaway rerolled.")

@bot.command(name="giveawayroles")
async def cmd_giveawayroles(ctx):
    async with get_db() as db:
        async with db.execute("SELECT role_id FROM giveaway_roles WHERE guild_id=?", (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows: await ctx.send("❌ No giveaway roles configured."); return
    mentions = [r.mention for row in rows if (r := ctx.guild.get_role(row[0]))]
    await ctx.send("🎉 Giveaway Roles:\n" + ("\n".join(mentions) if mentions else "None found."))

# ── Raffle ────────────────────────────────────────────────────────────────────

@bot.command(name="addtickets")
async def cmd_addtickets(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await add_tickets(ctx.guild.id, user.id, amount)
    await ctx.send(f"✅ Added {amount} tickets to {user.mention}.")

@bot.command(name="removetickets")
async def cmd_removetickets(ctx, user: discord.Member, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await add_tickets(ctx.guild.id, user.id, -amount)
    await ctx.send(f"❌ Removed {amount} tickets from {user.mention}.")

@bot.command(name="checkrafflehistory")
async def cmd_checkrafflehistory(ctx):
    async with get_db() as db:
        async with db.execute(
            "SELECT draw_timestamp,winner_id,winner_tickets,total_tickets,top_json "
            "FROM raffle_history WHERE guild_id=? ORDER BY draw_timestamp DESC LIMIT 10",
            (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows: await ctx.send("❌ No raffle history yet."); return
    embed = discord.Embed(title="📜 Recent Raffle History (last 10)", color=discord.Color.gold())
    for ts, wid, wt, tot, tj in rows:
        draw_dt = datetime.fromtimestamp(ts, UTC)
        date_str = draw_dt.strftime("%Y-%m-%d %H:%M UTC")
        winner = ctx.guild.get_member(wid)
        wname = winner.display_name if winner else "*[Left Server]*"
        wpct = (wt / tot * 100) if tot else 0
        embed.add_field(name=f"🗓 {date_str}",
                        value=f"🏆 **{wname}** — {wt:,} tickets ({wpct:.1f}%) | Pool: {tot:,}",
                        inline=False)
    await ctx.send(embed=embed)

# ── VIP Keys ──────────────────────────────────────────────────────────────────

@bot.command(name="givekey")
async def cmd_givekey(ctx, user: discord.Member, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    await inventory_add(ctx.guild.id, user.id, VIP_CHEST_KEY, amount)
    await ctx.send(f"🔑 Gave **{amount}x {VIP_CHEST_KEY}** to {user.mention}.")
    await log_event(ctx.guild.id, "item", _log_embed("🔑 VIP Key Given", discord.Color.green(),
        Admin=ctx.author.mention, User=user.mention, Keys=str(amount)))

@bot.command(name="takekey")
async def cmd_takekey(ctx, user: discord.Member, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    if not await inventory_remove(ctx.guild.id, user.id, VIP_CHEST_KEY, amount):
        await ctx.send(f"❌ {user.mention} doesn't have {amount}x {VIP_CHEST_KEY}."); return
    await ctx.send(f"🗑 Took **{amount}x {VIP_CHEST_KEY}** from {user.mention}.")
    await log_event(ctx.guild.id, "item", _log_embed("🔑 VIP Key Taken", discord.Color.red(),
        Admin=ctx.author.mention, User=user.mention, Keys=str(amount)))

@bot.command(name="givekeyrole")
async def cmd_givekeyrole(ctx, role: discord.Role, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    members = [m for m in ctx.guild.members if role in m.roles and not m.bot]
    if not members: await ctx.send(f"❌ No non-bot members with {role.mention}."); return
    async with ctx.typing():
        for m in members:
            await inventory_add(ctx.guild.id, m.id, VIP_CHEST_KEY, amount)
    await ctx.send(f"🔑 Gave **{amount}x {VIP_CHEST_KEY}** to **{len(members)}** member(s) with {role.mention}.")
    await log_event(ctx.guild.id, "item", _log_embed("🔑 VIP Keys Given (Role)", discord.Color.green(),
        Admin=ctx.author.mention, Role=role.name, Members=str(len(members)), Keys_Each=str(amount)))

@bot.command(name="takekeyrole")
async def cmd_takekeyrole(ctx, role: discord.Role, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    members = [m for m in ctx.guild.members if role in m.roles and not m.bot]
    if not members: await ctx.send(f"❌ No non-bot members with {role.mention}."); return
    full_taken = partial_taken = skipped = 0
    async with ctx.typing():
        for m in members:
            inv = await inventory_get(ctx.guild.id, m.id)
            owned = {n.lower(): q for n, q in inv}
            current = owned.get(VIP_CHEST_KEY.lower(), 0)
            if current == 0: skipped += 1; continue
            to_take = min(amount, current)
            await inventory_remove(ctx.guild.id, m.id, VIP_CHEST_KEY, to_take)
            if to_take == amount: full_taken += 1
            else: partial_taken += 1
    lines = [f"🗑 Processed **{len(members)}** member(s) with {role.mention}:"]
    if full_taken: lines.append(f"• **{full_taken}** lost the full **{amount}x** key(s)")
    if partial_taken: lines.append(f"• **{partial_taken}** had fewer — lost all their keys")
    if skipped: lines.append(f"• **{skipped}** had no keys (skipped)")
    await ctx.send("\n".join(lines))

# ── Gamble Tokens ─────────────────────────────────────────────────────────────

@bot.command(name="givegambletoken")
async def cmd_givegambletoken(ctx, user: discord.Member, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    await inventory_add(ctx.guild.id, user.id, GAMBLE_TOKEN, amount)
    await ctx.send(f"🎲 Gave **{amount}x {GAMBLE_TOKEN}** to {user.mention}.")

@bot.command(name="takegambletoken")
async def cmd_takegambletoken(ctx, user: discord.Member, amount: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    if not await inventory_remove(ctx.guild.id, user.id, GAMBLE_TOKEN, amount):
        await ctx.send(f"❌ {user.mention} doesn't have {amount}x {GAMBLE_TOKEN}."); return
    await ctx.send(f"🗑 Took **{amount}x {GAMBLE_TOKEN}** from {user.mention}.")

# ── Auto Giveaway ─────────────────────────────────────────────────────────────

@bot.command(name="removeautogiveaway")
async def cmd_removeautogiveaway(ctx, entry_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT prize FROM auto_giveaway_pool WHERE id=? AND guild_id=?",
                                  (entry_id, ctx.guild.id)) as cur:
                row = await cur.fetchone()
            if not row: await ctx.send(f"❌ No auto giveaway with ID `#{entry_id}`."); return
            await db.execute("DELETE FROM auto_giveaway_pool WHERE id=?", (entry_id,))
            await db.commit()
    await ctx.send(f"🗑 Removed **{row[0]}** (`#{entry_id}`) from the auto pool.")

@bot.command(name="listautogiveaways")
async def cmd_listautogiveaways(ctx):
    async with get_db() as db:
        async with db.execute(
            "SELECT id,prize,winners,chance,reward_balance,reward_exp,"
            "reward_tickets,reward_gamble_tokens,reward_vip_keys,reward_role_id,reward_item,reward_item_qty "
            "FROM auto_giveaway_pool WHERE guild_id=? ORDER BY id", (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows: await ctx.send("❌ Auto giveaway pool is empty."); return
    total_weight = sum(r[3] for r in rows)
    embed = discord.Embed(title="🎉 Auto Giveaway Pool", color=discord.Color.gold())
    for (row_id, prize, winners, chance, rb, re, rt, rgt, rvk, rrole, ri, riq) in rows:
        pct = (chance / total_weight * 100) if total_weight > 0 else 0
        parts = []
        if rb:  parts.append(f"💰{rb:,}")
        if re:  parts.append(f"⭐{re:,}")
        if rt:  parts.append(f"🎟{rt}")
        if rgt: parts.append(f"🎲{rgt}")
        if rvk: parts.append(f"🔑{rvk}")
        if rrole:
            role = ctx.guild.get_role(rrole)
            if role: parts.append(f"👑{role.name}")
        if ri:  parts.append(f"🎒{riq}x {ri}")
        embed.add_field(name=f"`#{row_id}` {prize}",
                        value=f"Winners: {winners} | **{pct:.1f}%** (w:{chance})\n{' + '.join(parts) or 'None'}",
                        inline=False)
    embed.set_footer(text=f"{len(rows)} item(s) | total weight: {total_weight}")
    await ctx.send(embed=embed)

@bot.command(name="stopgiveaways")
async def cmd_stopgiveaways(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    gid = ctx.guild.id
    task = auto_giveaway_tasks.pop(gid, None)
    if task: task.cancel()
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE auto_giveaway_config SET running=0 WHERE guild_id=?", (gid,))
            await db.commit()
    await ctx.send("🛑 Automatic giveaways stopped.")

# ── Games ─────────────────────────────────────────────────────────────────────

@bot.command(name="removegame")
async def cmd_removegame(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                                  (ctx.guild.id, name)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Game **{name}** not found."); return
            await db.execute("DELETE FROM games WHERE guild_id=? AND game_name=?", (ctx.guild.id, name))
            await db.execute("DELETE FROM game_answers WHERE guild_id=? AND game_name=?", (ctx.guild.id, name))
            await db.execute("DELETE FROM game_hints WHERE guild_id=? AND game_name=?", (ctx.guild.id, name))
            await db.commit()
    await ctx.send(f"🗑 Removed game **{name}** and all its answers and hints.")

@bot.command(name="enablegame")
async def cmd_enablegame(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                                  (ctx.guild.id, name)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Game **{name}** not found."); return
            await db.execute("UPDATE games SET enabled=1 WHERE guild_id=? AND game_name=?", (ctx.guild.id, name))
            await db.commit()
    await ctx.send(f"✅ Game **{name}** enabled.")

@bot.command(name="disablegame")
async def cmd_disablegame(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                                  (ctx.guild.id, name)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Game **{name}** not found."); return
            await db.execute("UPDATE games SET enabled=0 WHERE guild_id=? AND game_name=?", (ctx.guild.id, name))
            await db.commit()
    await ctx.send(f"🔒 Game **{name}** disabled.")

@bot.command(name="addgameanswer")
async def cmd_addgameanswer(ctx, game_name: str, *, answer: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with get_db() as db:
        async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                              (ctx.guild.id, game_name)) as cur:
            if not await cur.fetchone(): await ctx.send(f"❌ Game **{game_name}** not found."); return
    async with db_lock:
        async with get_db() as db:
            cur = await db.execute("INSERT INTO game_answers(guild_id,game_name,answer) VALUES(?,?,?)",
                                   (ctx.guild.id, game_name, answer))
            new_id = cur.lastrowid
            await db.commit()
    await ctx.send(f"✅ Added answer `{answer}` to **{game_name}** (ID: #{new_id}).\n"
                   f"Use `addhint {game_name} {new_id} <order 1-5> <hint text>` to add hints.")

@bot.command(name="removegameanswer")
async def cmd_removegameanswer(ctx, game_name: str, answer_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT id FROM game_answers WHERE id=? AND guild_id=? AND game_name=?",
                                  (answer_id, ctx.guild.id, game_name)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Answer #{answer_id} not found."); return
            await db.execute("DELETE FROM game_answers WHERE id=?", (answer_id,))
            await db.execute("DELETE FROM game_hints WHERE guild_id=? AND game_name=? AND answer_id=?",
                             (ctx.guild.id, game_name, answer_id))
            await db.commit()
    await ctx.send(f"🗑 Removed answer #{answer_id} and its hints from **{game_name}**.")

@bot.command(name="listgames")
async def cmd_listgames(ctx, *, game_name: str = None):
    gid = ctx.guild.id
    if game_name is None:
        async with get_db() as db:
            async with db.execute(
                "SELECT game_name,enabled,reward_balance,reward_exp,chance,answer_time "
                "FROM games WHERE guild_id=?", (gid,)) as cur:
                games = await cur.fetchall()
        if not games: await ctx.send("❌ No games configured."); return
        lines = []
        for (gname, enabled, rb, re, chance, atime) in games:
            status = "✅" if enabled else "🔒"
            lines.append(f"{status} **{gname}** | 💰{rb:,} ⭐{re:,} | ⚖️{chance} ⏱{atime}s")
        embed = discord.Embed(title="🎮 Random Games", description="\n".join(lines), color=discord.Color.teal())
        embed.set_footer(text=f"Use `{_BOT_PREFIX}listgames <name>` to see a game's answers")
        await ctx.send(embed=embed)
    else:
        async with get_db() as db:
            async with db.execute(
                "SELECT a.id, a.answer, COUNT(h.id) FROM game_answers a "
                "LEFT JOIN game_hints h ON h.answer_id=a.id AND h.guild_id=a.guild_id "
                "WHERE a.guild_id=? AND a.game_name=? GROUP BY a.id ORDER BY a.id",
                (gid, game_name)) as cur:
                answers = await cur.fetchall()
        if not answers: await ctx.send(f"❌ No answers for **{game_name}** (or game not found)."); return
        lines = [f"`#{aid}` {'🔔' * hc if hc else '·'} {ans}" for aid, ans, hc in answers]
        # Split into chunks of 1800 chars
        chunks, buf = [], []
        for line in lines:
            buf.append(line)
            if len("\n".join(buf)) > 1800:
                chunks.append("\n".join(buf[:-1]))
                buf = [buf[-1]]
        if buf: chunks.append("\n".join(buf))
        for i, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=f"🎯 {game_name}" + (f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else ""),
                description=chunk, color=discord.Color.teal())
            await ctx.send(embed=embed)

@bot.command(name="addhint")
async def cmd_addhint(ctx, game_name: str, answer_id: int, order: int, *, hint: str):
    """Usage: !addhint <game_name> <answer_id> <order 1-5> <hint text>"""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if not (1 <= order <= 5): await ctx.send("❌ Order must be 1–5."); return
    async with get_db() as db:
        async with db.execute("SELECT answer FROM game_answers WHERE id=? AND guild_id=? AND game_name=?",
                              (answer_id, ctx.guild.id, game_name)) as cur:
            ans_row = await cur.fetchone()
    if not ans_row: await ctx.send(f"❌ Answer #{answer_id} not found in **{game_name}**."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM game_hints WHERE guild_id=? AND game_name=? AND answer_id=? AND hint_order=?",
                             (ctx.guild.id, game_name, answer_id, order))
            await db.execute("INSERT INTO game_hints(guild_id,game_name,answer_id,hint_text,hint_order) VALUES(?,?,?,?,?)",
                             (ctx.guild.id, game_name, answer_id, hint, order))
            await db.commit()
    await ctx.send(f"✅ Hint #{order} set for answer **{ans_row[0]}** (#{answer_id}) in **{game_name}**.")

@bot.command(name="removehint")
async def cmd_removehint(ctx, hint_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT hint_text FROM game_hints WHERE id=? AND guild_id=?",
                                  (hint_id, ctx.guild.id)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Hint #{hint_id} not found."); return
            await db.execute("DELETE FROM game_hints WHERE id=?", (hint_id,))
            await db.commit()
    await ctx.send(f"🗑 Removed hint #{hint_id}.")

@bot.command(name="listhints")
async def cmd_listhints(ctx, game_name: str, answer_id: int = None):
    async with get_db() as db:
        if answer_id is not None:
            async with db.execute(
                "SELECT h.id, a.id, a.answer, h.hint_order, h.hint_text "
                "FROM game_hints h JOIN game_answers a ON h.answer_id=a.id "
                "WHERE h.guild_id=? AND h.game_name=? AND h.answer_id=? ORDER BY h.hint_order",
                (ctx.guild.id, game_name, answer_id)) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT h.id, a.id, a.answer, h.hint_order, h.hint_text "
                "FROM game_hints h JOIN game_answers a ON h.answer_id=a.id "
                "WHERE h.guild_id=? AND h.game_name=? ORDER BY a.id, h.hint_order",
                (ctx.guild.id, game_name)) as cur:
                rows = await cur.fetchall()
    if not rows: await ctx.send(f"❌ No hints found for **{game_name}**."); return
    lines = []; last_aid = None
    for h_id, a_id, answer, h_order, h_text in rows:
        if a_id != last_aid:
            lines.append(f"**`#{a_id}` {answer}**"); last_aid = a_id
        lines.append(f"  `[#{h_id}]` Hint {h_order}: {h_text}")
    text = "\n".join(lines)
    if len(text) > 1900: text = text[:1900] + "..."
    embed = discord.Embed(title=f"💡 Hints — {game_name}", description=text, color=discord.Color.teal())
    await ctx.send(embed=embed)

@bot.command(name="stopgames")
async def cmd_stopgames(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    gid = ctx.guild.id
    task = game_tasks.pop(gid, None)
    if task: task.cancel()
    active_game_sessions.pop(gid, None)
    await ctx.send("🛑 Random games stopped.")

# ── Admin Abuse Boxes ─────────────────────────────────────────────────────────

@bot.command(name="addbox")
async def cmd_addbox(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO abuse_boxes VALUES(?,?)", (ctx.guild.id, name))
                await db.commit()
            except aiosqlite.IntegrityError:
                await ctx.send(f"❌ Box **{name}** already exists."); return
    await ctx.send(f"✅ Created box **{name}**.")

@bot.command(name="removebox")
async def cmd_removebox(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                                  (ctx.guild.id, name)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Box **{name}** not found."); return
            await db.execute("DELETE FROM abuse_boxes WHERE guild_id=? AND box_name=?", (ctx.guild.id, name))
            await db.execute("DELETE FROM abuse_box_prizes WHERE guild_id=? AND box_name=?", (ctx.guild.id, name))
            await db.commit()
    await ctx.send(f"🗑 Removed box **{name}** and all its prizes.")

@bot.command(name="removeboxprize")
async def cmd_removeboxprize(ctx, box: str, prize_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT id FROM abuse_box_prizes WHERE id=? AND guild_id=? AND box_name=?",
                                  (prize_id, ctx.guild.id, box)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Prize #{prize_id} not found in **{box}**."); return
            await db.execute("DELETE FROM abuse_box_prizes WHERE id=?", (prize_id,))
            await db.commit()
    await ctx.send(f"🗑 Removed prize #{prize_id} from **{box}**.")

@bot.command(name="listboxes")
async def cmd_listboxes(ctx, *, box: str = None):
    async with get_db() as db:
        query = "SELECT box_name FROM abuse_boxes WHERE guild_id=?" + (" AND box_name=?" if box else "")
        async with db.execute(query, (ctx.guild.id, box) if box else (ctx.guild.id,)) as cur:
            boxes = await cur.fetchall()
    if not boxes: await ctx.send("❌ No boxes found."); return
    embed = discord.Embed(title="📦 Admin Abuse Boxes", color=discord.Color.orange())
    for (box_name,) in boxes:
        async with get_db() as db:
            async with db.execute(
                "SELECT id,prize_type,prize_value,chance FROM abuse_box_prizes "
                "WHERE guild_id=? AND box_name=? ORDER BY id", (ctx.guild.id, box_name)) as cur:
                prizes = await cur.fetchall()
        if not prizes: embed.add_field(name=f"📦 {box_name}", value="*No prizes yet*", inline=False); continue
        total_w = sum(p[3] for p in prizes)
        lines = []
        for p_id, p_type, p_value, p_chance in prizes:
            pct = (p_chance / total_w * 100) if total_w > 0 else 0
            desc = (f"💰 {int(p_value):,} coins" if p_type == "balance" else
                    f"⭐ {int(p_value):,} EXP"   if p_type == "exp" else
                    f"🎒 {p_value}"               if p_type == "item" else f"✨ {p_value}")
            lines.append(f"`#{p_id}` {desc} — **{pct:.1f}%**")
        embed.add_field(name=f"📦 {box_name}", value="\n".join(lines), inline=False)
    await ctx.send(embed=embed)

@bot.command(name="givebox")
async def cmd_givebox(ctx, role: discord.Role, amount: int, *, box: str):
    """Usage: !givebox @role <amount> <box name>"""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if amount <= 0: await ctx.send("❌ Amount must be ≥ 1."); return
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (ctx.guild.id, box)) as cur:
            if not await cur.fetchone(): await ctx.send(f"❌ Box **{box}** not found."); return
    members = [m for m in ctx.guild.members if role in m.roles and not m.bot]
    if not members: await ctx.send(f"❌ No non-bot members with {role.mention}."); return
    async with ctx.typing():
        for m in members:
            await inventory_add(ctx.guild.id, m.id, box, amount)
    await ctx.send(f"✅ Gave **{amount}x {box}** to **{len(members)}** member(s) with {role.mention}.")

@bot.command(name="addrarebox")
async def cmd_addrarebox(ctx, box: str, prize_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (ctx.guild.id, box)) as cur:
            if not await cur.fetchone(): await ctx.send(f"❌ Box **{box}** not found."); return
        async with db.execute("SELECT prize_type, prize_value FROM abuse_box_prizes "
                              "WHERE id=? AND guild_id=? AND box_name=?",
                              (prize_id, ctx.guild.id, box)) as cur:
            row = await cur.fetchone()
    if not row: await ctx.send(f"❌ Prize #{prize_id} not found in **{box}**."); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO rare_box_config(guild_id,box_name,prize_id) VALUES(?,?,?)",
                                 (ctx.guild.id, box, prize_id))
                await db.commit()
            except aiosqlite.IntegrityError:
                await ctx.send(f"❌ Prize #{prize_id} already marked as rare."); return
    await ctx.send(f"✅ Prize `#{prize_id}` ({row[0]}: **{row[1]}**) in **{box}** is now a rare drop.")

@bot.command(name="removerarebox")
async def cmd_removerarebox(ctx, box: str, prize_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM rare_box_config WHERE guild_id=? AND box_name=? AND prize_id=?",
                             (ctx.guild.id, box, prize_id))
            await db.commit()
    await ctx.send(f"🗑 Prize #{prize_id} in **{box}** is no longer a rare drop.")

# ── Codes ─────────────────────────────────────────────────────────────────────

@bot.command(name="createcode")
async def cmd_createcode(ctx, code: str, prize_json: str, uses: int = -1,
                          min_rank: int = 0, min_balance: int = 0):
    """Usage: !createcode <CODE> '<json>' [uses] [min_rank] [min_balance]"""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    code = code.upper().strip()
    try: prize = json.loads(prize_json)
    except json.JSONDecodeError:
        await ctx.send('❌ Invalid JSON. Example: `{"balance":500,"exp":1000}`'); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute(
                    "INSERT INTO redeem_codes(guild_id,code,prize_json,uses_left,min_level,min_balance,required_role_id) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (ctx.guild.id, code, json.dumps(prize), uses, min_rank, min_balance, 0))
                await db.commit()
            except aiosqlite.IntegrityError:
                await ctx.send(f"❌ Code **{code}** already exists."); return
    uses_str = "unlimited" if uses == -1 else str(uses)
    await ctx.send(f"✅ Code **{code}** created! Uses: {uses_str} | Min rank: {min_rank} | Min balance: {min_balance:,}")
    await log_event(ctx.guild.id, "code", _log_embed("🎫 Code Created", discord.Color.green(),
        By=ctx.author.mention, Code=code, Uses=uses_str, MinRank=str(min_rank)))

@bot.command(name="deletecode")
async def cmd_deletecode(ctx, code: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    code = code.upper().strip()
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT code FROM redeem_codes WHERE guild_id=? AND code=?",
                                  (ctx.guild.id, code)) as cur:
                if not await cur.fetchone(): await ctx.send(f"❌ Code **{code}** not found."); return
            await db.execute("DELETE FROM redeem_codes WHERE guild_id=? AND code=?", (ctx.guild.id, code))
            await db.execute("DELETE FROM code_uses WHERE guild_id=? AND code=?", (ctx.guild.id, code))
            await db.commit()
    await ctx.send(f"🗑 Code **{code}** deleted.")

@bot.command(name="listcodes")
async def cmd_listcodes(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with get_db() as db:
        async with db.execute(
            "SELECT code,prize_json,uses_left,min_level,min_balance,required_role_id "
            "FROM redeem_codes WHERE guild_id=?", (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows: await ctx.send("❌ No codes configured."); return
    embed = discord.Embed(title="🎫 Redeemable Codes", color=discord.Color.green())
    for code, prize_json, uses_left, min_level, min_balance, req_role_id in rows:
        try: prize = json.loads(prize_json)
        except: prize = {}
        parts = []
        if prize.get("balance",0) > 0: parts.append(f"💰{prize['balance']:,}")
        if prize.get("exp",0) > 0: parts.append(f"⭐{prize['exp']:,}")
        if prize.get("tickets",0) > 0: parts.append(f"🎟{prize['tickets']}")
        if prize.get("gamble_tokens",0) > 0: parts.append(f"🎲{prize['gamble_tokens']}")
        if prize.get("vip_keys",0) > 0: parts.append(f"🔑{prize['vip_keys']}")
        if prize.get("item"): parts.append(f"🎒{prize['item']}")
        uses_str = "∞" if uses_left == -1 else str(uses_left)
        embed.add_field(name=f"`{code}`",
                        value=f"{' + '.join(parts) or 'No prize'} | Uses: {uses_str} | Rank≥{min_level} | Bal≥{min_balance:,}",
                        inline=False)
    await ctx.send(embed=embed)

# ── Leaderboard stats ─────────────────────────────────────────────────────────

_VALID_STATS = {"total_exp", "gifted_balance", "chests_opened", "raffle_tickets_bought"}

@bot.command(name="addleaderboardstat")
async def cmd_addleaderboardstat(ctx, user: discord.Member, stat: str, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if stat not in _VALID_STATS:
        await ctx.send(f"❌ Valid stats: {', '.join(_VALID_STATS)}"); return
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    await ensure_stats(ctx.guild.id, user.id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(f"UPDATE user_stats SET {stat}={stat}+? WHERE guild_id=? AND user_id=?",
                             (amount, ctx.guild.id, user.id))
            await db.commit()
    await ctx.send(f"✅ Added **{amount:,}** to {user.mention}'s **{stat}**.")

@bot.command(name="removeleaderboardstat")
async def cmd_removeleaderboardstat(ctx, user: discord.Member, stat: str, amount: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if stat not in _VALID_STATS:
        await ctx.send(f"❌ Valid stats: {', '.join(_VALID_STATS)}"); return
    if amount <= 0: await ctx.send("❌ Amount must be > 0."); return
    await ensure_stats(ctx.guild.id, user.id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(f"UPDATE user_stats SET {stat}=MAX(0,{stat}-?) WHERE guild_id=? AND user_id=?",
                             (amount, ctx.guild.id, user.id))
            await db.commit()
    await ctx.send(f"❌ Removed **{amount:,}** from {user.mention}'s **{stat}**.")

# ── System ────────────────────────────────────────────────────────────────────

@bot.command(name="systemstatus")
async def cmd_systemstatus(ctx):
    embed = discord.Embed(title="⚙️ System Status", color=discord.Color.blurple())
    for flag, label in _SYSTEM_LABELS.items():
        on = await is_system_enabled(ctx.guild.id, flag)
        embed.add_field(name=label, value="✅ Enabled" if on else "🔒 Disabled", inline=True)
    await ctx.send(embed=embed)

# ── Welcome ───────────────────────────────────────────────────────────────────

@bot.command(name="enablewelcome")
async def cmd_enablewelcome(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with get_db() as db:
        async with db.execute("SELECT message FROM welcome_config WHERE guild_id=?", (ctx.guild.id,)) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        await ctx.send("❌ No welcome message set. Use `/setwelcome` to write one first."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE welcome_config SET enabled=1 WHERE guild_id=?", (ctx.guild.id,))
            await db.commit()
    await ctx.send("✅ Welcome DMs enabled.")

@bot.command(name="disablewelcome")
async def cmd_disablewelcome(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("UPDATE welcome_config SET enabled=0 WHERE guild_id=?", (ctx.guild.id,))
            await db.commit()
    await ctx.send("🔕 Welcome DMs disabled. Use `enablewelcome` to re-enable.")

@bot.command(name="previewwelcome")
async def cmd_previewwelcome(ctx):
    async with get_db() as db:
        async with db.execute("SELECT enabled, message FROM welcome_config WHERE guild_id=?",
                              (ctx.guild.id,)) as cur:
            row = await cur.fetchone()
    if not row or not row[1]:
        await ctx.send("❌ No welcome message configured. Use `/setwelcome` to create one."); return
    enabled, message = row
    text = message.replace("{member}", ctx.author.mention).replace("{server}", ctx.guild.name)
    embed = discord.Embed(description=text, color=discord.Color.blurple())
    embed.set_author(name=ctx.guild.name, icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
    status = "✅ Enabled" if enabled else "🔒 Disabled"
    await ctx.send(f"📬 **Welcome DM Preview** — Status: {status}\n*(your mention is used as example)*",
                   embed=embed, view=_WelcomeView(ctx.guild.name))

@bot.command(name="setwelcomechannel")
async def cmd_setwelcomechannel(ctx, channel: discord.TextChannel, *, message: str = None):
    """Usage: !setwelcomechannel #channel [optional message — use {member} and {server}]"""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO welcome_config"
                "(guild_id, enabled, message, channel_id, channel_enabled, channel_message) "
                "VALUES(?,0,NULL,?,1,?) "
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "channel_id=excluded.channel_id, "
                "channel_enabled=1, "
                "channel_message=excluded.channel_message",
                (ctx.guild.id, channel.id, message))
            await db.commit()
    await ctx.send(f"✅ Channel welcome enabled in {channel.mention}."
                   + (f" Custom message saved." if message else " Using fallback message."))

@bot.command(name="disablewelcomechannel")
async def cmd_disablewelcomechannel(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await disablewelcomechannel._callback(FakeInteraction(ctx))

@bot.command(name="enablewelcomechannel")
async def cmd_enablewelcomechannel(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await enablewelcomechannel._callback(FakeInteraction(ctx))

@bot.command(name="previewwelcomechannel")
async def cmd_previewwelcomechannel(ctx):
    await previewwelcomechannel._callback(FakeInteraction(ctx))

# ── Counting ──────────────────────────────────────────────────────────────────

@bot.command(name="enablecounting")
async def cmd_enablecounting(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO counting_config(guild_id,enabled) VALUES(?,1) "
                             "ON CONFLICT(guild_id) DO UPDATE SET enabled=1", (ctx.guild.id,))
            await db.commit()
    await ctx.send("✅ Counting rewards enabled.")

@bot.command(name="disablecounting")
async def cmd_disablecounting(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO counting_config(guild_id,enabled) VALUES(?,0) "
                             "ON CONFLICT(guild_id) DO UPDATE SET enabled=0", (ctx.guild.id,))
            await db.commit()
    await ctx.send("🔒 Counting rewards disabled.")

@bot.command(name="removecountingprize")
async def cmd_removecountingprize(ctx, prize_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT prize_type, prize_value FROM counting_prizes WHERE id=? AND guild_id=?",
                                  (prize_id, ctx.guild.id)) as cur:
                row = await cur.fetchone()
            if not row: await ctx.send(f"❌ Prize `#{prize_id}` not found."); return
            await db.execute("DELETE FROM counting_prizes WHERE id=?", (prize_id,))
            await db.commit()
    await ctx.send(f"🗑 Removed counting prize `#{prize_id}` ({row[0]}: {row[1]}).")

@bot.command(name="listcountingprizes")
async def cmd_listcountingprizes(ctx):
    async with get_db() as db:
        async with db.execute(
            "SELECT id,prize_type,prize_value,prize_amount,weight_formula "
            "FROM counting_prizes WHERE guild_id=? ORDER BY id", (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows: await ctx.send("❌ No counting prizes. Use `/addcountingprize`."); return
    embed = discord.Embed(title="🔢 Counting Prize Pool", color=discord.Color.teal())
    for (pid, ptype, pvalue, pamount, formula) in rows:
        label = (f"💰{pamount:,} coins" if ptype == "balance" else f"⭐{pamount:,} EXP" if ptype == "exp" else
                 f"🎒{pamount}x {pvalue}" if ptype == "item" else f"✨{pvalue}")
        embed.add_field(name=f"`#{pid}` {label}",
                        value=f"Formula: `{formula}` | n=100: **{_eval_weight(formula,100):.2f}**",
                        inline=False)
    await ctx.send(embed=embed)

@bot.command(name="removecountingspecial")
async def cmd_removecountingspecial(ctx, special_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT number, label FROM counting_special_prizes WHERE id=? AND guild_id=?",
                                  (special_id, ctx.guild.id)) as cur:
                row = await cur.fetchone()
            if not row: await ctx.send(f"❌ Special `#{special_id}` not found."); return
            await db.execute("DELETE FROM counting_special_prizes WHERE id=?", (special_id,))
            await db.commit()
    await ctx.send(f"🗑 Removed special `#{special_id}` (count {row[0]:,}: {row[1]}).")

@bot.command(name="listcountingspecials")
async def cmd_listcountingspecials(ctx):
    async with get_db() as db:
        async with db.execute(
            "SELECT id,number,prize_type,prize_value,prize_amount,label "
            "FROM counting_special_prizes WHERE guild_id=? ORDER BY number", (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows: await ctx.send("❌ No special prizes. Use `/addcountingspecial`."); return
    embed = discord.Embed(title="✨ Special Count Prizes", color=discord.Color.teal())
    for (sid, num, ptype, pvalue, pamount, lbl) in rows:
        embed.add_field(name=f"`#{sid}` Count **{num:,}**", value=lbl, inline=False)
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════
# BUILT-IN COUNTING COMMANDS
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="countingstats", description="Show current count, record, and your ban status")
@command_enabled()
async def countingstats(interaction: discord.Interaction):
    gid = interaction.guild.id
    async with get_db() as db:
        async with db.execute(
            "SELECT current_count, record FROM counting_state WHERE guild_id=?",
            (gid,)) as cur:
            state = await cur.fetchone()
        async with db.execute(
            "SELECT unban_time FROM counting_bans WHERE guild_id=? AND user_id=?",
            (gid, interaction.user.id)) as cur:
            ban = await cur.fetchone()

    now_ts = int(datetime.now(UTC).timestamp())
    embed = discord.Embed(title="🔢 Counting Stats", color=discord.Color.teal())
    if state:
        embed.add_field(name="Current Count", value=f"{state[0]:,}", inline=True)
        embed.add_field(name="Record", value=f"{state[1]:,}", inline=True)
    else:
        embed.description = "Counting not active in this server."
    if ban and ban[0] > now_ts:
        embed.add_field(
            name="Your Status",
            value=f"🔒 Banned — unbans <t:{ban[0]}:R>",
            inline=False)
    else:
        embed.add_field(name="Your Status", value="✅ Can count", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="resetcount", description="Admin: reset the count to 0")
@command_enabled()
async def resetcount(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "UPDATE counting_state SET current_count=0, last_user_id=0, "
                "last_message_id=0, notify_message_id=0 WHERE guild_id=?",
                (interaction.guild.id,))
            await db.commit()
    await interaction.response.send_message("✅ Count reset to 0. Next number is **1**.")

@bot.tree.command(name="unbancounter",
                  description="Admin: remove a user's counting ban immediately")
@app_commands.describe(user="User to unban")
@command_enabled()
async def unbancounter(interaction: discord.Interaction, user: discord.Member):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM counting_bans WHERE guild_id=? AND user_id=?",
                (interaction.guild.id, user.id))
            await db.commit()
    await interaction.response.send_message(f"✅ {user.mention} can count again.")

# ── List commands (read-only) ─────────────────────────────────────────────────

@bot.command(name="listchestprizes")
async def cmd_listchestprizes(ctx, chest_type: str = "chest"):
    if chest_type not in ("chest", "vipchest"):
        await ctx.send("❌ Use `chest` or `vipchest`."); return
    prizes = await get_chest_prizes(ctx.guild.id, chest_type)
    total_w = sum(p["chance"] for p in prizes)
    is_custom = any("id" in p for p in prizes)
    embed = discord.Embed(
        title=f"{'📦 EXP' if chest_type=='chest' else '💎 VIP'} Chest Prizes",
        color=discord.Color.purple())
    if not is_custom:
        embed.set_footer(text="Using default prizes. Use /addchestprize to customise.")
    lines = []
    for p in prizes:
        pct = (p["chance"] / total_w * 100) if total_w > 0 else 0
        desc = []
        if p["exp"] > 0: desc.append(f"⭐{p['exp']:,}")
        if p["balance"] > 0: desc.append(f"💰{p['balance']:,}")
        if not desc: desc.append("✨Special")
        id_str = f"`#{p['id']}` " if "id" in p else ""
        lines.append(f"{id_str}**{p['name']}** — {' + '.join(desc)} — **{pct:.1f}%** (w:{p['chance']})")
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)

@bot.command(name="listlogchannels")
async def cmd_listlogchannels(ctx):
    async with get_db() as db:
        async with db.execute("SELECT log_type,channel_id FROM log_channels WHERE guild_id=? ORDER BY log_type",
                              (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows: await ctx.send("❌ No log channels configured."); return
    embed = discord.Embed(title="📋 Log Channels", color=discord.Color.blurple())
    for log_type, channel_id in rows:
        label = next((c.name for c in _LOG_CHOICES if c.value == log_type), log_type)
        ch = bot.get_channel(channel_id)
        embed.add_field(name=label,
                        value=ch.mention if ch else f"<#{channel_id}> *(deleted)*", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="listexpboosts")
async def cmd_listexpboosts(ctx):
    async with get_db() as db:
        async with db.execute(
            "SELECT role_id,boost_percent,channel_id,category_id FROM exp_boosts "
            "WHERE guild_id=? ORDER BY boost_percent DESC", (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows: await ctx.send("❌ No EXP boosts configured."); return
    embed = discord.Embed(title="⚡ Active EXP Boosts", color=discord.Color.blurple())
    for role_id, boost, channel_id, category_id in rows:
        role = ctx.guild.get_role(role_id)
        name = role.mention if role else f"<deleted role {role_id}>"
        sign = "+" if boost > 0 else ""
        if channel_id:
            ch = ctx.guild.get_channel(channel_id); scope = ch.mention if ch else "deleted channel"
        elif category_id:
            cat = ctx.guild.get_channel(category_id); scope = f"📁 {cat.name}" if cat else "deleted category"
        else:
            scope = "🌐 Global"
        embed.add_field(name=name, value=f"{sign}{boost}% | {scope}", inline=False)
    await ctx.send(embed=embed)

# ═══════════════════════════════════════════════════════
# LOGGING SYSTEM
# ═══════════════════════════════════════════════════════

_LOG_CHOICES = [
    app_commands.Choice(name="💰 Balance  — add/remove/gift/wins",        value="balance"),
    app_commands.Choice(name="⭐ EXP      — add/remove/chest spend",      value="exp"),
    app_commands.Choice(name="🎒 Items    — buy/use/give/take/keys",      value="item"),
    app_commands.Choice(name="🎟 Raffle   — ticket purchases, daily draw",value="raffle"),
    app_commands.Choice(name="🎉 Giveaway — create/end/reroll",           value="giveaway"),
    app_commands.Choice(name="📦 Chests   — open results",                value="chest"),
    app_commands.Choice(name="🎁 Boxes    — open results",                value="box"),
    app_commands.Choice(name="🎲 Gamble   — blackjack/roulette results",  value="gamble"),
    app_commands.Choice(name="🎫 Codes    — create/redeem",               value="code"),
    app_commands.Choice(name="🤝 Trades   — executed trades",             value="trade"),
    app_commands.Choice(name="💬 Commands — every slash command used",    value="command"),
    app_commands.Choice(name="⚙️ Admin    — all admin-only actions",      value="admin"),
    app_commands.Choice(name="📥 Join    — member joined the server",    value="join"),
    app_commands.Choice(name="📤 Leave   — member left / was kicked",    value="leave"),
]

async def log_event(guild_id: int, log_type: str, embed: discord.Embed):
    """Send embed to the configured log channel, silently no-op if not configured."""
    try:
        async with get_db() as db:
            async with db.execute(
                "SELECT channel_id FROM log_channels WHERE guild_id=? AND log_type=?",
                (guild_id, log_type)) as cur:
                row = await cur.fetchone()
        if not row:
            return
        ch = bot.get_channel(row[0])
        if ch:
            await ch.send(embed=embed)
    except Exception as e:
        print(f"[Log:{log_type}] {e}")

def _log_embed(title: str, color: discord.Color, **fields) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(UTC))
    for name, value in fields.items():
        embed.add_field(name=name, value=str(value), inline=True)
    return embed

# ── /setlogchannel ────────────────────────────────────────────────────────────

@bot.tree.command(name="setlogchannel", description="Set a channel for a specific log type")
@app_commands.describe(log_type="Which events to send here", channel="Destination channel")
@app_commands.choices(log_type=_LOG_CHOICES)
@command_enabled()
async def setlogchannel(interaction: discord.Interaction,
                        log_type: str, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO log_channels(guild_id,log_type,channel_id) VALUES(?,?,?)",
                (interaction.guild.id, log_type, channel.id))
            await db.commit()
    label = next(c.name for c in _LOG_CHOICES if c.value == log_type)
    await interaction.response.send_message(f"✅ **{label}** logs → {channel.mention}")

@bot.tree.command(name="removelogchannel", description="Disable logging for a specific type")
@app_commands.describe(log_type="Which log type to disable")
@app_commands.choices(log_type=_LOG_CHOICES)
@command_enabled()
async def removelogchannel(interaction: discord.Interaction, log_type: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM log_channels WHERE guild_id=? AND log_type=?",
                             (interaction.guild.id, log_type))
            await db.commit()
    label = next(c.name for c in _LOG_CHOICES if c.value == log_type)
    await interaction.response.send_message(f"🗑 **{label}** logs disabled.")

# ── Command log via interaction listener ──────────────────────────────────────
# Uses bot.listen so it doesn't override on_message / on_ready.

@bot.listen("on_interaction")
async def _log_command_use(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.application_command:
        return
    if not interaction.guild:
        return
    cmd  = interaction.data.get("name", "?")
    opts = interaction.data.get("options", [])
    # Resolve sub-command name if present (type 1 = SUB_COMMAND, type 2 = SUB_COMMAND_GROUP)
    if opts and opts[0].get("type") in (1, 2):
        cmd += f" {opts[0]['name']}"
    embed = discord.Embed(
        description=f"{interaction.user.mention} used **`/{cmd}`**",
        color=discord.Color.light_grey(),
        timestamp=datetime.now(UTC))
    embed.set_author(name=str(interaction.user),
                     icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text=(f"#{interaction.channel.name}" if interaction.channel else "?")
                     + f" | UID: {interaction.user.id}")
    await log_event(interaction.guild.id, "command", embed)

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING PATCHES
# Rules:
#   • Admin-only commands: check is_allowed_to_giveaway BEFORE logging so
#     denied attempts don't appear as successful actions in the log.
#   • User commands (buy, use, redeem): pre-check all early-return conditions
#     before the original call so we know whether it will succeed.
#   • chest / vipchest / roulette / blackjack: logged inline inside the command
#     bodies (so the actual amount/outcome is used). No patches for those.
# ─────────────────────────────────────────────────────────────────────────────

# ── Items / keys / tokens ─────────────────────────────────────────────────────

_orig_item_give = item_give._callback
async def _item_give_logged(interaction: discord.Interaction,
                             user: discord.Member, name: str, quantity: int = 1):
    await _orig_item_give(interaction, user, name, quantity)
    if not await is_allowed_to_giveaway(interaction): return
    await log_event(interaction.guild.id, "item", _log_embed(
        "🎒 Item Given", discord.Color.green(),
        Admin=interaction.user.mention, User=user.mention, Item=name, Qty=str(quantity)))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ item give", discord.Color.orange(),
        By=interaction.user.mention, To=user.mention, Item=f"{quantity}x {name}"))
item_give._callback = _item_give_logged

_orig_item_take = item_take._callback
async def _item_take_logged(interaction: discord.Interaction,
                             user: discord.Member, name: str, quantity: int = 1):
    await _orig_item_take(interaction, user, name, quantity)
    if not await is_allowed_to_giveaway(interaction): return
    await log_event(interaction.guild.id, "item", _log_embed(
        "🎒 Item Taken", discord.Color.red(),
        Admin=interaction.user.mention, User=user.mention, Item=name, Qty=str(quantity)))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ item take", discord.Color.orange(),
        By=interaction.user.mention, From=user.mention, Item=f"{quantity}x {name}"))
item_take._callback = _item_take_logged

_orig_item_buy = item_buy._callback
async def _item_buy_logged(interaction: discord.Interaction, name: str):
    # Pre-check: item must exist, role must exist, user must afford it
    item = await get_item(interaction.guild.id, name)
    should_log = (
        item is not None and
        interaction.guild.get_role(item[3]) is not None and
        await get_balance(interaction.guild.id, interaction.user.id) >= item[2]
    )
    await _orig_item_buy(interaction, name)
    if should_log:
        await log_event(interaction.guild.id, "item", _log_embed(
            "🛒 Item Purchased", discord.Color.blue(),
            User=interaction.user.mention, Item=name))
item_buy._callback = _item_buy_logged

_orig_item_use = item_use._callback
async def _item_use_logged(interaction: discord.Interaction, name: str):
    # Pre-check every early-return condition inside item_use()
    item = await get_item(interaction.guild.id, name)
    if item:
        _, item_name, _, role_id, _ = item
        inv    = {n.lower(): q for n, q in await inventory_get(interaction.guild.id, interaction.user.id)}
        role   = interaction.guild.get_role(role_id)
        member = interaction.guild.get_member(interaction.user.id)
        should_log = (
            inv.get(item_name.lower(), 0) >= 1 and
            role is not None and
            member is not None and
            role not in member.roles
        )
    else:
        should_log = False
    await _orig_item_use(interaction, name)
    if should_log:
        await log_event(interaction.guild.id, "item", _log_embed(
            "✅ Item Used (Role Claimed)", discord.Color.blue(),
            User=interaction.user.mention, Item=name))
item_use._callback = _item_use_logged

# ── Raffle ────────────────────────────────────────────────────────────────────

_orig_buytickets = buytickets._callback
async def _buytickets_logged(interaction: discord.Interaction, amount: int):
    # Pre-check: system on, positive amount, sufficient balance
    price = amount * RAFFLE_TICKET_PRICE
    should_log = (
        amount > 0 and
        await is_system_enabled(interaction.guild.id, "raffle") and
        await get_balance(interaction.guild.id, interaction.user.id) >= price
    )
    await _orig_buytickets(interaction, amount)
    if should_log:
        await log_event(interaction.guild.id, "raffle", _log_embed(
            "🎟 Tickets Purchased", discord.Color.gold(),
            User=interaction.user.mention, Tickets=str(amount),
            Cost=f"{price:,} coins"))
buytickets._callback = _buytickets_logged

# ── Giveaway ──────────────────────────────────────────────────────────────────

_orig_giveaway_cmd = giveaway._callback
async def _giveaway_logged(
    interaction: discord.Interaction,
    prize: str, seconds: int, winners: int,
    reward_balance: int = 0, reward_exp: int = 0, reward_tickets: int = 0,
    reward_gamble_tokens: int = 0, reward_vip_keys: int = 0,
    reward_role: discord.Role = None, reward_item: str = None,
    reward_item_qty: int = 1, channel: discord.TextChannel = None,
    required_role: discord.Role = None, template: str = "gold"
):
    await _orig_giveaway_cmd(
        interaction, prize, seconds, winners, reward_balance, reward_exp,
        reward_tickets, reward_gamble_tokens, reward_vip_keys, reward_role,
        reward_item, reward_item_qty, channel, required_role, template)
    if not await is_allowed_to_giveaway(interaction): return
    ch = channel or interaction.channel
    embed = _log_embed("🎉 Giveaway Created", discord.Color.gold(),
        By=interaction.user.mention, Prize=prize,
        Duration=f"{seconds}s", Winners=str(winners),
        Channel=ch.mention if ch else "?")
    await log_event(interaction.guild.id, "giveaway", embed)
    await log_event(interaction.guild.id, "admin", embed)
giveaway._callback = _giveaway_logged

# ── Codes ─────────────────────────────────────────────────────────────────────

_orig_redeem = redeem._callback
async def _redeem_logged(interaction: discord.Interaction, code: str):
    code_upper = code.upper().strip()
    # Pre-check: code must exist and not yet used by this person
    async with get_db() as db:
        async with db.execute(
            "SELECT uses_left FROM redeem_codes WHERE guild_id=? AND code=?",
            (interaction.guild.id, code_upper)) as cur:
            guild_row = await cur.fetchone()
        async with db.execute(
            "SELECT uses_left FROM global_redeem_codes WHERE code=?",
            (code_upper,)) as cur:
            global_row = await cur.fetchone()
    # Determine if a successful redemption is possible
    should_log = False
    if guild_row and guild_row[0] != 0:
        async with get_db() as db:
            async with db.execute(
                "SELECT 1 FROM code_uses WHERE guild_id=? AND code=? AND user_id=?",
                (interaction.guild.id, code_upper, interaction.user.id)) as cur:
                should_log = (await cur.fetchone() is None)
    elif global_row and global_row[0] != 0:
        async with get_db() as db:
            async with db.execute(
                "SELECT 1 FROM global_code_uses WHERE code=? AND user_id=?",
                (code_upper, interaction.user.id)) as cur:
                should_log = (await cur.fetchone() is None)
    await _orig_redeem(interaction, code)
    if should_log:
        await log_event(interaction.guild.id, "code", _log_embed(
            "🎫 Code Redeemed", discord.Color.green(),
            User=interaction.user.mention, Code=code_upper))
redeem._callback = _redeem_logged

# ── Chests / boxes ───────────────────────────────────────────────────────────
# chest and vipchest: log_event moved inline into the command bodies (Fixes 2 & 3).
# openbox stays as a patch — amount can't change inside openbox.

_orig_openbox = openbox._callback
async def _openbox_logged(interaction: discord.Interaction, box: str, amount: int = 1):
    if amount <= 0:
        await _orig_openbox(interaction, box, amount); return
    inv   = await inventory_get(interaction.guild.id, interaction.user.id)
    owned = {n.lower(): q for n, q in inv}
    if owned.get(box.lower(), 0) < amount:
        await _orig_openbox(interaction, box, amount); return
    await _orig_openbox(interaction, box, amount)
    await log_event(interaction.guild.id, "box", _log_embed(
        "🎁 Box Opened", discord.Color.orange(),
        User=interaction.user.mention, Box=box, Amount=str(amount)))
openbox._callback = _openbox_logged

# ── Gambling ─────────────────────────────────────────────────────────────────
# roulette and blackjack: logged inline (Fixes 4 & 5). No patches here.

# ── Trade ─────────────────────────────────────────────────────────────────────

_orig_execute_trade = execute_trade
async def _execute_trade_logged(session) -> tuple[bool, str]:
    success, err = await _orig_execute_trade(session)
    if success:
        for guild in bot.guilds:
            if guild.id == session.guild_id:
                init = guild.get_member(session.initiator_id)
                tgt  = guild.get_member(session.target_id)
                io   = session.offers[session.initiator_id]
                to_  = session.offers[session.target_id]
                embed = discord.Embed(title="🤝 Trade Executed",
                                      color=discord.Color.blurple(),
                                      timestamp=datetime.now(UTC))
                embed.add_field(name=f"{init.display_name if init else '?'} gave",
                                value=io.display() if io else "*Nothing*", inline=True)
                embed.add_field(name=f"{tgt.display_name if tgt else '?'} gave",
                                value=to_.display() if to_ else "*Nothing*", inline=True)
                await log_event(guild.id, "trade", embed)
                break
    return success, err
execute_trade = _execute_trade_logged

_orig_end_giveaway = end_giveaway
async def _end_giveaway_logged(message_id, reroll=False):
    await _orig_end_giveaway(message_id, reroll)
    for guild in bot.guilds:
        async with get_db() as db:
            async with db.execute(
                "SELECT channel_id, prize FROM giveaways WHERE message_id=?",
                (message_id,)) as cur:
                row = await cur.fetchone()
        if row:
            channel_id, prize_raw = row
            ch = bot.get_channel(channel_id)
            if ch and ch.guild.id == guild.id:
                try:
                    label = json.loads(prize_raw).get("label", prize_raw)
                except Exception:
                    label = prize_raw
                event_title = "🔄 Giveaway Rerolled" if reroll else "🎊 Giveaway Ended"
                await log_event(guild.id, "giveaway", _log_embed(
                    event_title, discord.Color.green(),
                    Prize=label, Channel=ch.mention))
                break
end_giveaway = _end_giveaway_logged

# ═══════════════════════════════════════════════════════
# GLOBAL OWNER COMMANDS  (bot owner only — user 906291437895843901)
# ═══════════════════════════════════════════════════════

def _owner_only(interaction: discord.Interaction) -> bool:
    return interaction.user.id == BOT_OWNER_ID

# -- FIX EXP ------------------------------------------------------------------

@bot.tree.command(name="fixexp",
                  description="[Owner] Fix a user's hidden negative usable EXP balance")
@app_commands.describe(user="User to inspect and fix")
async def fixexp(interaction: discord.Interaction, user: discord.Member):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    gid      = interaction.guild.id
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    async with get_db() as db:
        async with db.execute(
            "SELECT SUM(amount) FROM exp_history "
            "WHERE guild_id=? AND user_id=? AND timestamp>=?",
            (gid, user.id, week_ago)) as cur:
            row = await cur.fetchone()
    raw = row[0] or 0
    if raw >= 0:
        await interaction.response.send_message(
            f"✅ {user.mention}'s underlying usable EXP is **{raw:,}** — no fix needed.",
            ephemeral=True); return
    # Compensate the negative hole with a bonus entry
    await add_exp(gid, user.id, -raw, is_bonus=True)
    await interaction.response.send_message(
        f"✅ Fixed {user.mention}: underlying balance was **{raw:,}** (shown as 0). "
        f"Added **{-raw:,}** bonus EXP to bring it to 0.",
        ephemeral=True)

# ── Global command disable / enable ──────────────────────────────────────────

@bot.tree.command(name="gdisablecmd",
                  description="[Owner] Globally disable a command in ALL servers")
@app_commands.describe(command_name="Command to disable globally")
async def gdisablecmd(interaction: discord.Interaction, command_name: str):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    if command_name in _UNDISABLEABLE:
        await interaction.response.send_message(
            "❌ That command cannot be disabled.", ephemeral=True); return
    global_disabled_commands.add(command_name)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO global_disabled_commands VALUES(?)", (command_name,))
            await db.commit()
    await interaction.response.send_message(
        f"🌐🔒 `/{command_name}` disabled in **all** servers.")

@bot.tree.command(name="genablecmd",
                  description="[Owner] Re-enable a globally disabled command")
@app_commands.describe(command_name="Command to re-enable globally")
async def genablecmd(interaction: discord.Interaction, command_name: str):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    if command_name not in global_disabled_commands:
        await interaction.response.send_message(
            f"ℹ️ `/{command_name}` is not globally disabled.", ephemeral=True); return
    global_disabled_commands.discard(command_name)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM global_disabled_commands WHERE command_name=?", (command_name,))
            await db.commit()
    await interaction.response.send_message(
        f"🌐🔓 `/{command_name}` re-enabled in **all** servers.")

# ── Global system disable / enable ───────────────────────────────────────────

@bot.tree.command(name="gdisablesystem",
                  description="[Owner] Globally disable a system in ALL servers")
@app_commands.describe(system="System to disable")
@app_commands.choices(system=_SYSTEM_CHOICES)
async def gdisablesystem(interaction: discord.Interaction, system: str):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO global_system_flags VALUES(?,?)", (system, 0))
            await db.commit()
    await interaction.response.send_message(
        f"🌐🔒 **{_SYSTEM_LABELS[system]}** disabled in **all** servers.")

@bot.tree.command(name="genablesystem",
                  description="[Owner] Re-enable a globally disabled system")
@app_commands.describe(system="System to re-enable")
@app_commands.choices(system=_SYSTEM_CHOICES)
async def genablesystem(interaction: discord.Interaction, system: str):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO global_system_flags VALUES(?,?)", (system, 1))
            await db.commit()
    await interaction.response.send_message(
        f"🌐✅ **{_SYSTEM_LABELS[system]}** re-enabled in **all** servers.")

# ── Global status ─────────────────────────────────────────────────────────────

@bot.tree.command(name="gstatus",
                  description="[Owner] Show all globally disabled commands and systems")
async def gstatus(interaction: discord.Interaction):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute(
            "SELECT flag_name, enabled FROM global_system_flags") as cur:
            sys_rows = await cur.fetchall()
    embed = discord.Embed(title="🌐 Global Overrides", color=discord.Color.dark_red())
    g_cmds = sorted(global_disabled_commands)
    embed.add_field(
        name="🔒 Globally disabled commands",
        value="\n".join(f"• `/{c}`" for c in g_cmds) if g_cmds else "None",
        inline=False)
    sys_lines = []
    for flag, enabled in sys_rows:
        label = _SYSTEM_LABELS.get(flag, flag)
        sys_lines.append(f"{'✅' if enabled else '🔒'} {label}")
    embed.add_field(
        name="⚙️ Global system overrides",
        value="\n".join(sys_lines) if sys_lines else "No overrides set",
        inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── Global codes ──────────────────────────────────────────────────────────────

@bot.tree.command(name="gcreate",
                  description="[Owner] Create a global redeemable code (works in all servers)")
@app_commands.describe(
    code="Code name (auto-uppercased)",
    prize_json='e.g. {"balance":500,"exp":1000,"vip_keys":1}',
    uses="Max uses total across all servers (-1 = unlimited)",
    min_level="Minimum Activity Rank required",
    min_balance="Minimum balance required (per-server balance)"
)
async def gcreate(interaction: discord.Interaction, code: str, prize_json: str,
                  uses: int = -1, min_level: int = 0, min_balance: int = 0):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    code = code.upper().strip()
    try:
        prize = json.loads(prize_json)
    except json.JSONDecodeError:
        await interaction.response.send_message(
            '❌ Invalid JSON. Example: `{"balance":500,"exp":1000}`', ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute(
                    "INSERT INTO global_redeem_codes(code,prize_json,uses_left,min_level,min_balance) "
                    "VALUES(?,?,?,?,?)",
                    (code, json.dumps(prize), uses, min_level, min_balance))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(
                    f"❌ Global code **{code}** already exists.", ephemeral=True); return
    parts = []
    if prize.get("balance",0) > 0:       parts.append(f"💰 {prize['balance']:,}")
    if prize.get("exp",0) > 0:           parts.append(f"⭐ {prize['exp']:,}")
    if prize.get("tickets",0) > 0:       parts.append(f"🎟 {prize['tickets']}")
    if prize.get("gamble_tokens",0) > 0: parts.append(f"🎲 {prize['gamble_tokens']}")
    if prize.get("vip_keys",0) > 0:      parts.append(f"🔑 {prize['vip_keys']}")
    if prize.get("item"):                 parts.append(f"🎒 {prize.get('item_qty',1)}x {prize['item']}")
    uses_str = "∞" if uses == -1 else str(uses)
    await interaction.response.send_message(
        f"🌐✅ Global code **{code}** created!\n"
        f"Prize: {' + '.join(parts) or 'None'} | Uses: {uses_str} | "
        f"Min rank: {min_level} | Min balance: {min_balance:,}",
        ephemeral=True)

@bot.tree.command(name="gdelete", description="[Owner] Delete a global code")
@app_commands.describe(code="Code to delete")
async def gdelete(interaction: discord.Interaction, code: str):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    code = code.upper().strip()
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT code FROM global_redeem_codes WHERE code=?", (code,)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(
                        f"❌ Global code **{code}** not found.", ephemeral=True); return
            await db.execute("DELETE FROM global_redeem_codes WHERE code=?", (code,))
            await db.execute("DELETE FROM global_code_uses WHERE code=?", (code,))
            await db.commit()
    await interaction.response.send_message(f"🗑 Global code **{code}** deleted.")

@bot.tree.command(name="gcodes", description="[Owner] List all global codes")
async def gcodes(interaction: discord.Interaction):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute(
            "SELECT code, prize_json, uses_left, min_level, min_balance "
            "FROM global_redeem_codes") as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No global codes.", ephemeral=True); return
    embed = discord.Embed(title="🌐 Global Codes", color=discord.Color.gold())
    for code, prize_json, uses_left, min_level, min_balance in rows:
        try: prize = json.loads(prize_json)
        except: prize = {}
        parts = []
        if prize.get("balance",0) > 0:       parts.append(f"💰 {prize['balance']:,}")
        if prize.get("exp",0) > 0:           parts.append(f"⭐ {prize['exp']:,}")
        if prize.get("tickets",0) > 0:       parts.append(f"🎟 {prize['tickets']}")
        if prize.get("gamble_tokens",0) > 0: parts.append(f"🎲 {prize['gamble_tokens']}")
        if prize.get("vip_keys",0) > 0:      parts.append(f"🔑 {prize['vip_keys']}")
        if prize.get("item"):                 parts.append(f"🎒 {prize.get('item_qty',1)}x {prize['item']}")
        async with get_db() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM global_code_uses WHERE code=?", (code,)) as cur:
                used = (await cur.fetchone())[0]
        uses_str = "∞" if uses_left == -1 else f"{uses_left - used if uses_left > 0 else uses_left} left"
        embed.add_field(
            name=f"🌐 `{code}`",
            value=f"{' + '.join(parts) or 'No prize'} | Uses: {uses_str} | "
                  f"Rank ≥ {min_level} | Bal ≥ {min_balance:,}",
            inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- PREFIX ----------------------------------------

@bot.tree.command(name="setprefix", description="[Owner] Set the bot's global command prefix")
@app_commands.describe(prefix="New prefix (e.g. ! or ? or .)")
async def setprefix(interaction: discord.Interaction, prefix: str):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    global _BOT_PREFIX
    prefix = prefix.strip()
    if not prefix or len(prefix) > 5:
        await interaction.response.send_message("❌ Prefix must be 1–5 characters.", ephemeral=True); return
    _BOT_PREFIX = prefix
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO bot_config VALUES('prefix',?)", (prefix,))
            await db.commit()
    await interaction.response.send_message(f"✅ Prefix set to `{prefix}` — all prefix commands now use `{prefix}<command>`")

# ═══════════════════════════════════════════════════════
# COUNTING SYSTEM
# ═══════════════════════════════════════════════════════

_CP_CHOICES = [
    app_commands.Choice(name="Balance",              value="balance"),
    app_commands.Choice(name="EXP",                  value="exp"),
    app_commands.Choice(name="Raffle Tickets",       value="tickets"),
    app_commands.Choice(name="Gamble Tokens",        value="gamble_tokens"),
    app_commands.Choice(name="VIP Keys",             value="vip_keys"),
    app_commands.Choice(name="Item / Box",           value="item"),
    app_commands.Choice(name="Nothing (filler slot)",value="nothing"),
    app_commands.Choice(name="Custom label only",    value="custom"),
]

@bot.tree.command(name="setcountingchannel",
                  description="Set which channel to watch and where to announce prizes")
@app_commands.describe(
    counting_channel="Channel to watch for counts (leave empty = all channels)",
    announce_channel="Where to post prize wins (leave empty = post in counting channel)"
)
@command_enabled()
async def setcountingchannel(interaction: discord.Interaction,
                               counting_channel:  discord.TextChannel = None,
                               announce_channel:  discord.TextChannel = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    ch_id  = counting_channel.id  if counting_channel  else 0
    ann_id = announce_channel.id  if announce_channel  else 0
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO counting_config(guild_id,enabled,channel_id,announce_channel_id) "
                "VALUES(?,1,?,?) ON CONFLICT(guild_id) DO UPDATE SET "
                "channel_id=excluded.channel_id, announce_channel_id=excluded.announce_channel_id",
                (interaction.guild.id, ch_id, ann_id))
            await db.execute(
                "INSERT OR IGNORE INTO counting_state(guild_id) VALUES(?)",
                (interaction.guild.id,))
            await db.commit()
    parts = []
    parts.append(f"Counting channel: {counting_channel.mention if counting_channel else '🌐 All channels'}")
    parts.append(f"Announce channel: {announce_channel.mention if announce_channel else '↩️ Same as counting channel'}")
    await interaction.response.send_message("✅ " + " | ".join(parts))


@bot.tree.command(name="addcountingprize",
                  description="Add a prize to the counting reward pool")
@app_commands.describe(
    prize_type="Type of prize",
    amount="Amount for balance/EXP/tickets/tokens/keys prizes",
    item_name="Item or box name (for 'item' type)",
    label="Display label for 'nothing' or 'custom' type",
    weight_formula=(
        "Weight formula — fixed number OR math with {n} = count. "
        "Examples: 1  |  {n}  |  sqrt({n})  |  max(1,100-{n})"
    )
)
@app_commands.choices(prize_type=_CP_CHOICES)
@command_enabled()
async def addcountingprize(interaction: discord.Interaction,
                            prize_type: str, amount: int = 0,
                            item_name: str = None, label: str = None,
                            weight_formula: str = "1"):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return

    # Validate inputs
    if prize_type in ("balance","exp","tickets","gamble_tokens","vip_keys") and amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    if prize_type == "item" and not item_name:
        await interaction.response.send_message("❌ Provide item_name.", ephemeral=True); return

    # Test formula
    test_w = _eval_weight(weight_formula, 100)
    if test_w <= 0:
        await interaction.response.send_message(
            "❌ Formula evaluates to ≤ 0 at n=100. Use a positive expression.",
            ephemeral=True); return

    p_value  = item_name.strip() if prize_type == "item" else (label or prize_type)
    p_amount = amount if prize_type != "item" else (amount or 1)

    async with db_lock:
        async with get_db() as db:
            cur = await db.execute(
                "INSERT INTO counting_prizes(guild_id,prize_type,prize_value,prize_amount,weight_formula) "
                "VALUES(?,?,?,?,?)",
                (interaction.guild.id, prize_type, p_value, p_amount, weight_formula))
            new_id = cur.lastrowid
            await db.commit()

    prize_str = {
        "balance":       f"💰 {amount:,} coins",
        "exp":           f"⭐ {amount:,} EXP",
        "tickets":       f"🎟 {amount} ticket(s)",
        "gamble_tokens": f"🎲 {amount} Gamble Token(s)",
        "vip_keys":      f"🔑 {amount} VIP Key(s)",
        "item":          f"🎒 {p_amount}x {item_name}",
        "nothing":       "nothing 😔",
        "custom":        label or "custom",
    }.get(prize_type, prize_type)

    await interaction.response.send_message(
        f"✅ Added counting prize `#{new_id}`: **{prize_str}**\n"
        f"Weight formula: `{weight_formula}` "
        f"*(evaluated at n=100: **{test_w:.2f}**)*")

@bot.tree.command(name="addcountingspecial",
                  description="Add a bonus prize given on top for a specific count number")
@app_commands.describe(
    number="The exact count that triggers this bonus",
    prize_type="Type of prize",
    amount="Amount for balance/EXP/tickets/tokens/keys",
    item_name="Item or box name (for item prizes)",
    label="How it's announced, e.g. '1k EXP milestone bonus'"
)
@app_commands.choices(prize_type=_CP_CHOICES)
@command_enabled()
async def addcountingspecial(interaction: discord.Interaction,
                              number: int, prize_type: str,
                              amount: int = 0, item_name: str = None,
                              label: str = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if number <= 0:
        await interaction.response.send_message("❌ Number must be > 0.", ephemeral=True); return
    if prize_type in ("balance","exp","tickets","gamble_tokens","vip_keys") and amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    if prize_type == "item" and not item_name:
        await interaction.response.send_message("❌ Provide item_name.", ephemeral=True); return

    p_value  = item_name.strip() if prize_type == "item" else (label or prize_type)
    p_amount = amount if prize_type != "item" else (amount or 1)
    p_label  = label or p_value

    async with db_lock:
        async with get_db() as db:
            cur = await db.execute(
                "INSERT INTO counting_special_prizes"
                "(guild_id,number,prize_type,prize_value,prize_amount,label) "
                "VALUES(?,?,?,?,?,?)",
                (interaction.guild.id, number, prize_type, p_value, p_amount, p_label))
            new_id = cur.lastrowid
            await db.commit()
    await interaction.response.send_message(
        f"✅ Special prize `#{new_id}` added: counting **{number:,}** gives bonus **{p_label}**.")

# ═══════════════════════════════════════════════════════
# FAKE INTERACTION  — wraps ctx so slash callbacks work as prefix
# ═══════════════════════════════════════════════════════

class FakeInteraction:
    """
    Wraps a commands.Context so that slash command ._callback() can be
    called directly from a prefix command without any modification.
    ephemeral=True is silently ignored (message is sent publicly).
    """

    class _Resp:
        def __init__(self, ctx):
            self._ctx = ctx

        async def send_message(self, content=None, *, embed=None, embeds=None,
                                ephemeral=False, view=None):
            kw = {}
            if content  is not None: kw['content'] = content
            if embed    is not None: kw['embed']   = embed
            if embeds   is not None: kw['embeds']  = embeds
            if view     is not None: kw['view']    = view
            await self._ctx.send(**kw)

        async def defer(self, ephemeral=False):
            await self._ctx.trigger_typing()

        async def send_modal(self, modal):
            await self._ctx.send(
                "❌ This command requires the slash version "
                "(it opens a pop-up form that prefix commands can't show).")

    class _Follow:
        def __init__(self, ctx):
            self._ctx = ctx

        async def send(self, content=None, *, embed=None, embeds=None,
                       ephemeral=False, view=None):
            kw = {}
            if content  is not None: kw['content'] = content
            if embed    is not None: kw['embed']   = embed
            if embeds   is not None: kw['embeds']  = embeds
            if view     is not None: kw['view']    = view
            await self._ctx.send(**kw)

    def __init__(self, ctx: commands.Context):
        self._ctx     = ctx
        self.guild    = ctx.guild
        self.user     = ctx.author
        self.channel  = ctx.channel
        self.command  = ctx.command
        self.data     = {}
        self.type     = discord.InteractionType.application_command
        self.response = self._Resp(ctx)
        self.followup = self._Follow(ctx)

    async def original_response(self):
        """Used by trade — returns the last sent message."""
        async for msg in self._ctx.channel.history(limit=1):
            return msg


class _MC:
    """Mock app_commands.Choice — used for leaderboard and addgamepreset."""
    def __init__(self, value: str):
        self.value = value
        self.name  = value


# ═══════════════════════════════════════════════════════
# PREFIX WRAPPERS FOR ALL REMAINING SLASH COMMANDS
# ═══════════════════════════════════════════════════════

# ── Public user commands ──────────────────────────────────────────────────────

@bot.command(name="balance")
async def pfx_balance(ctx, user: discord.Member = None):
    await balance._callback(FakeInteraction(ctx), user)

@bot.command(name="activityrank")
async def pfx_activityrank(ctx, user: discord.Member = None):
    await level._callback(FakeInteraction(ctx), user)

@bot.command(name="leaderboard")
async def pfx_leaderboard(ctx, category: str = "balance", page: int = 1):
    _valid = {"total_exp","current_exp","balance","raffle_tickets_bought",
              "current_tickets","chests_opened","gifted_balance"}
    if category not in _valid:
        await ctx.send(f"❌ Valid categories: {', '.join(sorted(_valid))}"); return
    await leaderboard._callback(FakeInteraction(ctx), _MC(category), page)

@bot.command(name="buytickets")
async def pfx_buytickets(ctx, amount: int):
    await buytickets._callback(FakeInteraction(ctx), amount)

@bot.command(name="rafflechance")
async def pfx_rafflechance(ctx, user: discord.Member = None):
    await rafflechance._callback(FakeInteraction(ctx), user)

@bot.command(name="chest")
async def pfx_chest(ctx, amount: int = 1):
    await chest._callback(FakeInteraction(ctx), amount)

@bot.command(name="vipchest")
async def pfx_vipchest(ctx, amount: int = 1):
    await vipchest._callback(FakeInteraction(ctx), amount)

@bot.command(name="openbox")
async def pfx_openbox(ctx, box: str, amount: int = 1):
    await openbox._callback(FakeInteraction(ctx), box, amount)

@bot.command(name="blackjack")
async def pfx_blackjack(ctx, bet: int):
    await blackjack._callback(FakeInteraction(ctx), bet)

@bot.command(name="roulette")
async def pfx_roulette(ctx, bet: int, choice: str):
    await roulette._callback(FakeInteraction(ctx), bet, choice)

@bot.command(name="redeem")
async def pfx_redeem(ctx, code: str):
    await redeem._callback(FakeInteraction(ctx), code)

@bot.command(name="trade")
async def pfx_trade(ctx, user: discord.Member):
    if user.id == ctx.author.id: await ctx.send("❌ Can't trade with yourself."); return
    if user.bot:                  await ctx.send("❌ Can't trade with a bot."); return
    key = (ctx.guild.id, frozenset({ctx.author.id, user.id}))
    if key in trade_sessions:     await ctx.send("❌ A trade is already in progress."); return
    session = TradeSession(ctx.guild.id, ctx.author.id, user.id)
    trade_sessions[key] = session
    msg = await ctx.send(
        f"🤝 {ctx.author.mention} wants to trade with {user.mention}!\n"
        "Click **Set Offer**, then **Confirm**.",
        embed=session.build_embed(ctx.guild), view=TradeView(session))
    session.message = msg

@bot.command(name="help")
async def pfx_help(ctx, *, command: str = None):
    await help_cmd._callback(FakeInteraction(ctx), command)

# ── Item group as prefix group ─────────────────────────────────────────────────

@bot.group(name="item", invoke_without_command=True)
async def pfx_item(ctx):
    p = _BOT_PREFIX
    await ctx.send(
        f"**Item commands:** `{p}item store` · `{p}item buy <name>` · `{p}item use <name>` · "
        f"`{p}item inv [@user]` · `{p}item info <name>` · `{p}item give @user <name> [qty]` · "
        f"`{p}item take @user <name> [qty]` · `{p}item add <name> <price> @role <desc>` · "
        f"`{p}item remove <name>`")

@pfx_item.command(name="store")
async def pfx_item_store(ctx):
    await item_store_cmd._callback(FakeInteraction(ctx))

@pfx_item.command(name="buy")
async def pfx_item_buy_cmd(ctx, *, name: str):
    await item_buy._callback(FakeInteraction(ctx), name)

@pfx_item.command(name="use")
async def pfx_item_use_cmd(ctx, *, name: str):
    await item_use._callback(FakeInteraction(ctx), name)

@pfx_item.command(name="inv")
async def pfx_item_inv_cmd(ctx, user: discord.Member = None):
    await item_inv._callback(FakeInteraction(ctx), user)

@pfx_item.command(name="info")
async def pfx_item_info_cmd(ctx, *, name: str):
    await item_info._callback(FakeInteraction(ctx), name)

@pfx_item.command(name="give")
async def pfx_item_give_cmd(ctx, user: discord.Member, name: str, quantity: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await item_give._callback(FakeInteraction(ctx), user, name, quantity)

@pfx_item.command(name="take")
async def pfx_item_take_cmd(ctx, user: discord.Member, name: str, quantity: int = 1):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await item_take._callback(FakeInteraction(ctx), user, name, quantity)

@pfx_item.command(name="add")
async def pfx_item_add_cmd(ctx, name: str, price: int, role: discord.Role, *, description: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await item_add._callback(FakeInteraction(ctx), name, price, role, description)

@pfx_item.command(name="remove")
async def pfx_item_remove_cmd(ctx, *, name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await item_remove._callback(FakeInteraction(ctx), name)

# ── Giveaway admin ────────────────────────────────────────────────────────────

@bot.command(name="addgiveawayrole")
async def pfx_addgiveawayrole(ctx, role: discord.Role):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await addgiveawayrole._callback(FakeInteraction(ctx), role)

@bot.command(name="removegiveawayrole")
async def pfx_removegiveawayrole(ctx, role: discord.Role):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await removegiveawayrole._callback(FakeInteraction(ctx), role)

@bot.command(name="giveaway")
async def pfx_giveaway(ctx, prize: str, seconds: int, winners: int = 1,
                        reward_balance: int = 0, reward_exp: int = 0):
    """Usage: !giveaway <prize> <seconds> [winners] [reward_balance] [reward_exp]"""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await giveaway._callback(FakeInteraction(ctx), prize, seconds, winners,
                              reward_balance, reward_exp, 0, 0, 0,
                              None, None, 1, None, None, "gold")

@bot.command(name="addautogiveaway")
async def pfx_addautogiveaway(ctx, prize: str, winners: int = 1, chance: float = 1.0,
                               reward_balance: int = 0, reward_exp: int = 0):
    """Usage: !addautogiveaway <prize> [winners] [chance] [reward_balance] [reward_exp]"""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await addautogiveaway._callback(FakeInteraction(ctx), prize, winners, chance,
                                    reward_balance, reward_exp, 0, 0, 0, None, None, 1)

@bot.command(name="startgiveaways")
async def pfx_startgiveaways(ctx, interval_seconds: int, giveaway_duration_seconds: int,
                               channel: discord.TextChannel = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await startgiveaways._callback(FakeInteraction(ctx), interval_seconds,
                                   giveaway_duration_seconds, channel)

# ── Channel / config setup ───────────────────────────────────────────────────

@bot.command(name="setrafflechannel")
async def pfx_setrafflechannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setrafflechannel._callback(FakeInteraction(ctx), channel)

@bot.command(name="setraffleinfochannel")
async def pfx_setraffleinfochannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setraffleinfochannel._callback(FakeInteraction(ctx), channel)

@bot.command(name="setraredropchannel")
async def pfx_setraredropchannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setraredropchannel._callback(FakeInteraction(ctx), channel)

@bot.command(name="setlogchannel")
async def pfx_setlogchannel(ctx, log_type: str, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if log_type not in {c.value for c in _LOG_CHOICES}:
        await ctx.send(f"❌ Valid types: {', '.join(c.value for c in _LOG_CHOICES)}"); return
    await setlogchannel._callback(FakeInteraction(ctx), log_type, channel)

@bot.command(name="removelogchannel")
async def pfx_removelogchannel(ctx, log_type: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if log_type not in {c.value for c in _LOG_CHOICES}:
        await ctx.send(f"❌ Valid types: {', '.join(c.value for c in _LOG_CHOICES)}"); return
    await removelogchannel._callback(FakeInteraction(ctx), log_type)

@bot.command(name="setcountingchannel")
async def pfx_setcountingchannel(ctx, counting_channel: discord.TextChannel = None,
                                   announce_channel: discord.TextChannel = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setcountingchannel._callback(FakeInteraction(ctx), counting_channel, announce_channel)

@bot.command(name="countingstats")
async def pfx_countingstats(ctx):
    await countingstats._callback(FakeInteraction(ctx))

@bot.command(name="resetcount")
async def pfx_resetcount(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await resetcount._callback(FakeInteraction(ctx))

@bot.command(name="unbancounter")
async def pfx_unbancounter(ctx, user: discord.Member):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await unbancounter._callback(FakeInteraction(ctx), user)

@bot.command(name="addautoentryrole")
async def pfx_addautoentryrole(ctx, role: discord.Role, message_requirement: int = 0):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await addautoentryrole._callback(FakeInteraction(ctx), role, message_requirement)

@bot.command(name="removeautoentryrole")
async def pfx_removeautoentryrole(ctx, role: discord.Role):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await removeautoentryrole._callback(FakeInteraction(ctx), role)

@bot.command(name="listautoentryroles")
async def pfx_listautoentryroles(ctx):
    await listautoentryroles._callback(FakeInteraction(ctx))

@bot.command(name="autoentry")
async def pfx_autoentry(ctx):
    await autoentry._callback(FakeInteraction(ctx))

@bot.command(name="setstatchannel")
async def pfx_setstatchannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setstatchannel._callback(FakeInteraction(ctx), channel)

@bot.command(name="expboost")
async def pfx_expboost(ctx, role: discord.Role, boost: float,
                        channel: discord.TextChannel = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await expboost._callback(FakeInteraction(ctx), role, boost, channel, None)

@bot.command(name="removeexpboost")
async def pfx_removeexpboost(ctx, role: discord.Role,
                               channel: discord.TextChannel = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await removeexpboost._callback(FakeInteraction(ctx), role, channel, None)

@bot.command(name="enablesystem")
async def pfx_enablesystem(ctx, system: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if system not in _SYSTEM_LABELS:
        await ctx.send(f"❌ Valid: {', '.join(_SYSTEM_LABELS)}"); return
    await enablesystem._callback(FakeInteraction(ctx), system)

@bot.command(name="disablesystem")
async def pfx_disablesystem(ctx, system: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if system not in _SYSTEM_LABELS:
        await ctx.send(f"❌ Valid: {', '.join(_SYSTEM_LABELS)}"); return
    await disablesystem._callback(FakeInteraction(ctx), system)

@bot.command(name="disablecmd")
async def pfx_disablecmd(ctx, command_name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await disablecmd._callback(FakeInteraction(ctx), command_name)

@bot.command(name="enablecmd")
async def pfx_enablecmd(ctx, command_name: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await enablecmd._callback(FakeInteraction(ctx), command_name)

@bot.command(name="listcmds")
async def pfx_listcmds(ctx):
    await listcmds._callback(FakeInteraction(ctx))

# ── Chest management ─────────────────────────────────────────────────────────

@bot.command(name="addchestprize")
async def pfx_addchestprize(ctx, chest_type: str, name: str,
                              exp: int = 0, balance: int = 0, chance: float = 10.0):
    """Usage: !addchestprize <chest|vipchest> <name> [exp] [balance] [chance]"""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if chest_type not in ("chest","vipchest"):
        await ctx.send("❌ Use `chest` or `vipchest`."); return
    await addchestprize._callback(FakeInteraction(ctx), chest_type, name, exp, balance, chance)

@bot.command(name="removechestprize")
async def pfx_removechestprize(ctx, chest_type: str, prize_id: int):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if chest_type not in ("chest","vipchest"):
        await ctx.send("❌ Use `chest` or `vipchest`."); return
    await removechestprize._callback(FakeInteraction(ctx), chest_type, prize_id)

@bot.command(name="addrarechestdrop")
async def pfx_addrarechestdrop(ctx, chest_type: str, *, prize: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if chest_type not in ("chest","vipchest"):
        await ctx.send("❌ Use `chest` or `vipchest`."); return
    await addrarechestdrop._callback(FakeInteraction(ctx), chest_type, prize)

@bot.command(name="removerarechestdrop")
async def pfx_removerarechestdrop(ctx, chest_type: str, *, prize: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if chest_type not in ("chest","vipchest"):
        await ctx.send("❌ Use `chest` or `vipchest`."); return
    await removerarechestdrop._callback(FakeInteraction(ctx), chest_type, prize)

# ── Box management ────────────────────────────────────────────────────────────

@bot.command(name="addboxprize")
async def pfx_addboxprize(ctx, box: str, prize_type: str, chance: int,
                           amount: int = 0, item_name: str = None, *, custom_label: str = None):
    """Usage: !addboxprize <box> <balance|exp|item|nothing|custom> <chance> [amount] [item_name] [custom_label]"""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if prize_type not in ("balance","exp","item","nothing","custom"):
        await ctx.send("❌ Valid types: balance, exp, item, nothing, custom"); return
    await addboxprize._callback(FakeInteraction(ctx), box, prize_type, chance,
                                amount, item_name, custom_label)

# ── Game management ───────────────────────────────────────────────────────────

@bot.command(name="addgame")
async def pfx_addgame(ctx, name: str, reward_balance: int = 0, reward_exp: int = 0,
                       chance: float = 1.0, answer_time: int = 30):
    """Usage: !addgame <name> [reward_balance] [reward_exp] [chance] [answer_time]"""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await addgame._callback(FakeInteraction(ctx), name, reward_balance, reward_exp,
                            0, 0, 0, None, 1, None, chance, answer_time)

@bot.command(name="editgame")
async def pfx_editgame(ctx, name: str, reward_balance: int = None, reward_exp: int = None,
                        chance: float = None, answer_time: int = None):
    """Usage: !editgame <name> [reward_balance] [reward_exp] [chance] [answer_time]"""
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await editgame._callback(FakeInteraction(ctx), name, reward_balance, reward_exp,
                              None, None, None, None, None, None, None, chance, answer_time)

@bot.command(name="setgamechannel")
async def pfx_setgamechannel(ctx, channel: discord.TextChannel, interval_seconds: int = 60):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await setgamechannel._callback(FakeInteraction(ctx), channel, interval_seconds)

@bot.command(name="startgames")
async def pfx_startgames(ctx):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await startgames._callback(FakeInteraction(ctx))

@bot.command(name="addgamepreset")
async def pfx_addgamepreset(ctx, game_name: str, preset: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if preset not in _PRESET_DATA:
        await ctx.send(f"❌ Valid presets: {', '.join(_PRESET_DATA.keys())}"); return
    await addgamepreset._callback(FakeInteraction(ctx), game_name, preset)

# ── Welcome (modal command — prefix not supported) ────────────────────────────

@bot.command(name="setwelcome")
async def pfx_setwelcome(ctx):
    await ctx.send("❌ `/setwelcome` opens a pop-up text editor that only works as a slash command. "
                   "Please use `/setwelcome` instead.")


# ═══════════════════════════════════════════════════════
# /transfer  — owner command to copy all data between guilds
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="transfer",
                  description="[Owner] Copy ALL data from one guild to another (replaces destination data)")
@app_commands.describe(
    guild_id_from="Source guild ID",
    guild_id_to="Destination guild ID — ALL existing data there will be replaced!"
)
async def transfer(interaction: discord.Interaction,
                   guild_id_from: str, guild_id_to: str):
    if not _owner_only(interaction):
        await interaction.response.send_message("❌ Owner only.", ephemeral=True); return
    try:
        gid_from = int(guild_id_from.strip())
        gid_to   = int(guild_id_to.strip())
    except ValueError:
        await interaction.response.send_message("❌ Invalid guild IDs.", ephemeral=True); return
    if gid_from == gid_to:
        await interaction.response.send_message("❌ Source and destination are the same.", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)
    copied: dict[str, int] = {}

    # ── helper: delete target rows, copy source rows with guild_id swapped ──

    async def _copy(db, table: str, cols: list[str]):
        """cols[0] must be 'guild_id'."""
        await db.execute(f"DELETE FROM {table} WHERE guild_id=?", (gid_to,))
        async with db.execute(
            f"SELECT {','.join(cols)} FROM {table} WHERE guild_id=?", (gid_from,)) as cur:
            rows = await cur.fetchall()
        if not rows:
            copied[table] = 0; return
        ph  = ','.join('?' * len(cols))
        sql = f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})"
        n   = 0
        for row in rows:
            vals    = list(row)
            vals[0] = gid_to       # replace guild_id (always index 0)
            try:   await db.execute(sql, vals); n += 1
            except Exception: pass
        copied[table] = n

    async with db_lock:
        async with get_db() as db:

            # ── 1. Pure guild-scoped tables — no FK complications ─────────────
            await _copy(db, "balances",
                        ["guild_id","user_id","balance"])
            await _copy(db, "exp_history",
                        ["guild_id","user_id","amount","timestamp","is_bonus"])
            await _copy(db, "user_stats",
                        ["guild_id","user_id","total_exp","gifted_balance",
                         "chests_opened","raffle_tickets_bought"])
            await _copy(db, "inventory",
                        ["guild_id","user_id","item_name","quantity"])
            await _copy(db, "item_store",
                        ["guild_id","item_name","price","role_id","description"])
            await _copy(db, "raffle",
                        ["guild_id","user_id","tickets"])
            await _copy(db, "giveaway_roles",
                        ["guild_id","role_id"])
            await _copy(db, "system_flags",
                        ["guild_id","flag_name","enabled"])
            await _copy(db, "rare_drop_config",
                        ["guild_id","channel_id"])
            await _copy(db, "raffle_config",
                        ["guild_id","channel_id"])
            await _copy(db, "raffle_info_config",
                        ["guild_id","channel_id","message_id"])
            await _copy(db, "game_config",
                        ["guild_id","channel_id","answer_time","interval_seconds","hint_delays"])
            await _copy(db, "rare_chest_config",
                        ["guild_id","chest_type","prize_name"])
            await _copy(db, "log_channels",
                        ["guild_id","log_type","channel_id"])
            await _copy(db, "disabled_commands_persist",
                        ["guild_id","command_name"])
            await _copy(db, "welcome_config",
                        ["guild_id","enabled","message"])
            await _copy(db, "auto_giveaway_config",
                        ["guild_id","channel_id","interval_seconds","duration_seconds","running"])
            await _copy(db, "counting_config",
                        ["guild_id","enabled","channel_id","announce_channel_id"])
            await _copy(db, "daily_key_log",
                        ["guild_id","user_id","date"])
            await _copy(db, "daily_gamble_log",
                        ["guild_id","user_id","date"])
            await _copy(db, "redeem_codes",
                        ["guild_id","code","prize_json","uses_left",
                         "min_level","min_balance","required_role_id"])
            await _copy(db, "code_uses",
                        ["guild_id","code","user_id"])
            await _copy(db, "abuse_boxes",
                        ["guild_id","box_name"])

            # exp_boosts has a composite PK — handle separately
            await db.execute("DELETE FROM exp_boosts WHERE guild_id=?", (gid_to,))
            async with db.execute(
                "SELECT guild_id,role_id,boost_percent,channel_id,category_id "
                "FROM exp_boosts WHERE guild_id=?", (gid_from,)) as cur:
                rows = await cur.fetchall()
            n = 0
            for _, rid, bp, cid, catid in rows:
                try:
                    await db.execute("INSERT OR IGNORE INTO exp_boosts VALUES(?,?,?,?,?)",
                                     (gid_to, rid, bp, cid, catid))
                    n += 1
                except Exception: pass
            copied['exp_boosts'] = n

            # ── 2. Auto-increment tables with NO external FK references ────────
            # auto_giveaway_pool (id not referenced elsewhere)
            await db.execute("DELETE FROM auto_giveaway_pool WHERE guild_id=?", (gid_to,))
            async with db.execute(
                "SELECT guild_id,prize,winners,chance,reward_balance,reward_exp,"
                "reward_tickets,reward_gamble_tokens,reward_vip_keys,"
                "reward_role_id,reward_item,reward_item_qty "
                "FROM auto_giveaway_pool WHERE guild_id=?", (gid_from,)) as cur:
                rows = await cur.fetchall()
            for r in rows:
                await db.execute(
                    "INSERT INTO auto_giveaway_pool(guild_id,prize,winners,chance,"
                    "reward_balance,reward_exp,reward_tickets,reward_gamble_tokens,"
                    "reward_vip_keys,reward_role_id,reward_item,reward_item_qty) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (gid_to, *r[1:]))
            copied['auto_giveaway_pool'] = len(rows)

            # counting_prizes
            await db.execute("DELETE FROM counting_prizes WHERE guild_id=?", (gid_to,))
            async with db.execute(
                "SELECT guild_id,prize_type,prize_value,prize_amount,weight_formula "
                "FROM counting_prizes WHERE guild_id=?", (gid_from,)) as cur:
                rows = await cur.fetchall()
            for r in rows:
                await db.execute(
                    "INSERT INTO counting_prizes(guild_id,prize_type,prize_value,prize_amount,weight_formula) "
                    "VALUES(?,?,?,?,?)", (gid_to, *r[1:]))
            copied['counting_prizes'] = len(rows)

            # counting_special_prizes
            await db.execute("DELETE FROM counting_special_prizes WHERE guild_id=?", (gid_to,))
            async with db.execute(
                "SELECT guild_id,number,prize_type,prize_value,prize_amount,label "
                "FROM counting_special_prizes WHERE guild_id=?", (gid_from,)) as cur:
                rows = await cur.fetchall()
            for r in rows:
                await db.execute(
                    "INSERT INTO counting_special_prizes"
                    "(guild_id,number,prize_type,prize_value,prize_amount,label) "
                    "VALUES(?,?,?,?,?,?)", (gid_to, *r[1:]))
            copied['counting_special_prizes'] = len(rows)

            # raffle_history
            await db.execute("DELETE FROM raffle_history WHERE guild_id=?", (gid_to,))
            async with db.execute(
                "SELECT guild_id,draw_timestamp,winner_id,winner_tickets,total_tickets,top_json "
                "FROM raffle_history WHERE guild_id=?", (gid_from,)) as cur:
                rows = await cur.fetchall()
            for r in rows:
                await db.execute(
                    "INSERT INTO raffle_history(guild_id,draw_timestamp,winner_id,"
                    "winner_tickets,total_tickets,top_json) VALUES(?,?,?,?,?,?)",
                    (gid_to, *r[1:]))
            copied['raffle_history'] = len(rows)

            # chest_prizes (referenced by rare_chest_config by NAME, not ID — no mapping needed)
            await db.execute("DELETE FROM chest_prizes WHERE guild_id=?", (gid_to,))
            async with db.execute(
                "SELECT guild_id,chest_type,name,exp,balance,chance "
                "FROM chest_prizes WHERE guild_id=?", (gid_from,)) as cur:
                rows = await cur.fetchall()
            for r in rows:
                await db.execute(
                    "INSERT INTO chest_prizes(guild_id,chest_type,name,exp,balance,chance) "
                    "VALUES(?,?,?,?,?,?)", (gid_to, *r[1:]))
            copied['chest_prizes'] = len(rows)

            # games
            await db.execute("DELETE FROM games WHERE guild_id=?", (gid_to,))
            async with db.execute(
                "SELECT guild_id,game_name,enabled,reward_balance,reward_exp,"
                "reward_tickets,reward_gamble_tokens,reward_vip_keys,reward_item,"
                "reward_item_qty,reward_role_id,chance,answer_time "
                "FROM games WHERE guild_id=?", (gid_from,)) as cur:
                rows = await cur.fetchall()
            for r in rows:
                try:
                    await db.execute(
                        "INSERT OR IGNORE INTO games(guild_id,game_name,enabled,reward_balance,"
                        "reward_exp,reward_tickets,reward_gamble_tokens,reward_vip_keys,"
                        "reward_item,reward_item_qty,reward_role_id,chance,answer_time) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (gid_to, *r[1:]))
                except Exception: pass
            copied['games'] = len(rows)

            # ── 3. Tables with ID-based FK chains (need old→new ID mapping) ───

            # game_answers → game_hints
            await db.execute("DELETE FROM game_hints   WHERE guild_id=?", (gid_to,))
            await db.execute("DELETE FROM game_answers WHERE guild_id=?", (gid_to,))

            async with db.execute(
                "SELECT id,guild_id,game_name,answer "
                "FROM game_answers WHERE guild_id=?", (gid_from,)) as cur:
                ans_rows = await cur.fetchall()
            ans_map: dict[int, int] = {}
            for old_id, _, gname, ans in ans_rows:
                c = await db.execute(
                    "INSERT INTO game_answers(guild_id,game_name,answer) VALUES(?,?,?)",
                    (gid_to, gname, ans))
                ans_map[old_id] = c.lastrowid
            copied['game_answers'] = len(ans_rows)

            async with db.execute(
                "SELECT guild_id,game_name,answer_id,hint_text,hint_order "
                "FROM game_hints WHERE guild_id=?", (gid_from,)) as cur:
                hint_rows = await cur.fetchall()
            for _, gname, old_aid, ht, ho in hint_rows:
                await db.execute(
                    "INSERT INTO game_hints(guild_id,game_name,answer_id,hint_text,hint_order) "
                    "VALUES(?,?,?,?,?)",
                    (gid_to, gname, ans_map.get(old_aid, old_aid), ht, ho))
            copied['game_hints'] = len(hint_rows)

            # abuse_box_prizes → rare_box_config
            await db.execute("DELETE FROM rare_box_config  WHERE guild_id=?", (gid_to,))
            await db.execute("DELETE FROM abuse_box_prizes WHERE guild_id=?", (gid_to,))

            async with db.execute(
                "SELECT id,guild_id,box_name,prize_type,prize_value,prize_amount,chance "
                "FROM abuse_box_prizes WHERE guild_id=?", (gid_from,)) as cur:
                bp_rows = await cur.fetchall()
            bp_map: dict[int, int] = {}
            for old_id, _, bname, pt, pv, pa, pc in bp_rows:
                c = await db.execute(
                    "INSERT INTO abuse_box_prizes"
                    "(guild_id,box_name,prize_type,prize_value,prize_amount,chance) "
                    "VALUES(?,?,?,?,?,?)", (gid_to, bname, pt, pv, pa, pc))
                bp_map[old_id] = c.lastrowid
            copied['abuse_box_prizes'] = len(bp_rows)

            async with db.execute(
                "SELECT guild_id,box_name,prize_id "
                "FROM rare_box_config WHERE guild_id=?", (gid_from,)) as cur:
                rbc_rows = await cur.fetchall()
            for _, bname, old_pid in rbc_rows:
                try:
                    await db.execute(
                        "INSERT INTO rare_box_config(guild_id,box_name,prize_id) VALUES(?,?,?)",
                        (gid_to, bname, bp_map.get(old_pid, old_pid)))
                except Exception: pass
            copied['rare_box_config'] = len(rbc_rows)

            await db.commit()

    # ── Build summary ────────────────────────────────────────────────────────
    total = sum(copied.values())
    lines = [f"✅ **Transfer complete:** `{gid_from}` → `{gid_to}`",
             f"📊 **Total rows copied: {total:,}**", ""]
    for tbl, cnt in sorted(copied.items()):
        if cnt > 0:
            lines.append(f"• `{tbl}`: {cnt:,}")
    if total == 0:
        lines.append("*(no data found in source guild)*")

    await interaction.followup.send("\n".join(lines), ephemeral=True)

# ═══════════════════════════════════════════════════════
# RESET COMMANDS
# ═══════════════════════════════════════════════════════

_RESET_TYPE_CHOICES = [
    app_commands.Choice(name="Balance",                                      value="balance"),
    app_commands.Choice(name="EXP (all history — rank + usable)",            value="exp"),
    app_commands.Choice(name="Inventory (items, boxes, keys, tokens)",        value="inventory"),
    app_commands.Choice(name="Raffle Tickets",                               value="tickets"),
    app_commands.Choice(name="Leaderboard Stats (total EXP stat, chests…)",  value="stats"),
    app_commands.Choice(name="Everything (all of the above)",                value="all"),
]

@bot.tree.command(name="resetuser", description="Wipe a user's data in this server")
@app_commands.describe(user="User to reset", reset_type="What to wipe")
@app_commands.choices(reset_type=_RESET_TYPE_CHOICES)
@command_enabled()
async def resetuser(interaction: discord.Interaction,
                    user: discord.Member, reset_type: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await _do_reset(interaction.guild.id, user.id, reset_type)
    label = next(c.name for c in _RESET_TYPE_CHOICES if c.value == reset_type)
    await interaction.response.send_message(f"🗑 Reset **{label}** for {user.mention}.")
    await log_event(interaction.guild.id, "admin", _log_embed(
        "🗑 User Reset", discord.Color.red(),
        By=interaction.user.mention, User=user.mention, Type=reset_type))


@bot.tree.command(name="resetrole", description="Wipe data for every member with a role")
@app_commands.describe(role="Role whose members get reset", reset_type="What to wipe")
@app_commands.choices(reset_type=_RESET_TYPE_CHOICES)
@command_enabled()
async def resetrole(interaction: discord.Interaction,
                    role: discord.Role, reset_type: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await interaction.response.defer()
    members = [m for m in interaction.guild.members if role in m.roles and not m.bot]
    if not members:
        await interaction.followup.send(f"❌ No non-bot members with {role.mention}."); return
    for m in members:
        await _do_reset(interaction.guild.id, m.id, reset_type)
    label = next(c.name for c in _RESET_TYPE_CHOICES if c.value == reset_type)
    await interaction.followup.send(
        f"🗑 Reset **{label}** for **{len(members)}** member(s) with {role.mention}.")
    await log_event(interaction.guild.id, "admin", _log_embed(
        "🗑 Role Reset", discord.Color.red(),
        By=interaction.user.mention, Role=role.name,
        Members=str(len(members)), Type=reset_type))


# ═══════════════════════════════════════════════════════
# PREFIX CHANNEL RESTRICTIONS
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="disableprefixchannel",
                  description="Block prefix commands in a channel for everyone or a specific role")
@app_commands.describe(
    channel="Channel to restrict",
    role="Role to block (leave empty to block everyone in this channel)"
)
@command_enabled()
async def disableprefixchannel(interaction: discord.Interaction,
                                channel: discord.TextChannel,
                                role: discord.Role = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid, cid, rid = interaction.guild.id, channel.id, (role.id if role else 0)
    key = (gid, cid)
    prefix_channel_rules.setdefault(key, {})[rid] = False
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO prefix_restrictions VALUES(?,?,?,?)",
                (gid, cid, rid, 0))
            await db.commit()
    target = role.mention if role else "everyone"
    await interaction.response.send_message(
        f"🔒 Prefix commands disabled in {channel.mention} for **{target}**.\n"
        f"Use `/enableprefixchannel` to grant specific roles a bypass.")


@bot.tree.command(name="enableprefixchannel",
                  description="Let a role use prefix commands in a channel, even if it's blocked for others")
@app_commands.describe(
    channel="Channel to allow prefix in",
    role="Role to allow (their allowed status overrides any block)"
)
@command_enabled()
async def enableprefixchannel(interaction: discord.Interaction,
                               channel: discord.TextChannel,
                               role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid, cid, rid = interaction.guild.id, channel.id, role.id
    key = (gid, cid)
    prefix_channel_rules.setdefault(key, {})[rid] = True
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO prefix_restrictions VALUES(?,?,?,?)",
                (gid, cid, rid, 1))
            await db.commit()
    await interaction.response.send_message(
        f"✅ {role.mention} can use prefix commands in {channel.mention}, "
        f"even if the channel is blocked for everyone else.")


@bot.tree.command(name="resetprefixchannel",
                  description="Remove all prefix-command restrictions from a channel")
@app_commands.describe(channel="Channel whose restrictions to clear")
@command_enabled()
async def resetprefixchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    key = (interaction.guild.id, channel.id)
    prefix_channel_rules.pop(key, None)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM prefix_restrictions WHERE guild_id=? AND channel_id=?",
                (interaction.guild.id, channel.id))
            await db.commit()
    await interaction.response.send_message(
        f"🔓 All prefix restrictions removed from {channel.mention}.")


@bot.tree.command(name="listprefixchannels",
                  description="Show all prefix-command channel restrictions in this server")
@command_enabled()
async def listprefixchannels(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute(
            "SELECT channel_id,role_id,allowed FROM prefix_restrictions "
            "WHERE guild_id=? ORDER BY channel_id,role_id",
            (interaction.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("✅ No prefix restrictions configured."); return
    embed = discord.Embed(title="🔒 Prefix Command Restrictions", color=discord.Color.orange())
    channels: dict[int, list] = {}
    for cid, rid, allowed in rows:
        channels.setdefault(cid, []).append((rid, bool(allowed)))
    for cid, rules in channels.items():
        ch    = interaction.guild.get_channel(cid)
        lines = []
        for rid, allowed in sorted(rules):
            icon   = "✅" if allowed else "🔒"
            if rid == 0:
                target = "@everyone"
            else:
                r      = interaction.guild.get_role(rid)
                target = r.mention if r else f"<@&{rid}>"
            lines.append(f"{icon} {target}")
        embed.add_field(
            name=ch.mention if ch else f"<#{cid}>",
            value="\n".join(lines),
            inline=False)
    await interaction.response.send_message(embed=embed)

# ── Reset ─────────────────────────────────────────────────────────────────────

@bot.command(name="resetuser")
async def pfx_resetuser(ctx, user: discord.Member, reset_type: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if reset_type not in {c.value for c in _RESET_TYPE_CHOICES}:
        await ctx.send(f"❌ Valid types: {', '.join(c.value for c in _RESET_TYPE_CHOICES)}"); return
    await resetuser._callback(FakeInteraction(ctx), user, reset_type)

@bot.command(name="resetrole")
async def pfx_resetrole(ctx, role: discord.Role, reset_type: str):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    if reset_type not in {c.value for c in _RESET_TYPE_CHOICES}:
        await ctx.send(f"❌ Valid types: {', '.join(c.value for c in _RESET_TYPE_CHOICES)}"); return
    await resetrole._callback(FakeInteraction(ctx), role, reset_type)

# ── Prefix channel restrictions ───────────────────────────────────────────────

@bot.command(name="disableprefixchannel")
async def pfx_disableprefixchannel(ctx, channel: discord.TextChannel, role: discord.Role = None):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await disableprefixchannel._callback(FakeInteraction(ctx), channel, role)

@bot.command(name="enableprefixchannel")
async def pfx_enableprefixchannel(ctx, channel: discord.TextChannel, role: discord.Role):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await enableprefixchannel._callback(FakeInteraction(ctx), channel, role)

@bot.command(name="resetprefixchannel")
async def pfx_resetprefixchannel(ctx, channel: discord.TextChannel):
    if not await _is_allowed_ctx(ctx): await ctx.send("❌ No permission."); return
    await resetprefixchannel._callback(FakeInteraction(ctx), channel)

@bot.command(name="listprefixchannels")
async def pfx_listprefixchannels(ctx):
    await listprefixchannels._callback(FakeInteraction(ctx))

# ═══════════════════════════════════════════════════════
# RUN BOT
# ═══════════════════════════════════════════════════════

bot.run(TOKEN)

import os
import json
import random
import asyncio
import aiosqlite
from datetime import datetime, timedelta, UTC
from contextlib import asynccontextmanager

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

bot = commands.Bot(command_prefix="!", intents=intents)

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
            # EXP bug fix: zero spent_exp so new negative-entry system takes over
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

# ═══════════════════════════════════════════════════════
# ON_MESSAGE  (game guesses + EXP with boosts)
# ═══════════════════════════════════════════════════════

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.guild:
        session = active_game_sessions.get(message.guild.id)
        if session and not session.get("answered") and message.channel.id == session.get("channel_id"):
            if message.content.strip().lower() == session["answer"].lower():
                session["answered"] = True
                session["winner"] = message.author
                if "event" in session:
                    session["event"].set()
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
                    async with db.execute(
                        f"SELECT boost_percent FROM exp_boosts WHERE guild_id=? AND role_id IN ({placeholders})",
                        (message.guild.id, *member_role_ids)) as cur:
                        boost_rows = await cur.fetchall()
                if boost_rows:
                    total_boost = sum(r[0] for r in boost_rows)
                    gained = max(0, int(gained * (1 + total_boost / 100)))
        await add_exp(message.guild.id, message.author.id, gained)
        last_message_exp[key] = now
    await bot.process_commands(message)

# ═══════════════════════════════════════════════════════
# READY EVENT
# ═══════════════════════════════════════════════════════

@bot.event
async def on_ready():
    await setup_database()
    await load_disabled_commands()   # ← restore persisted disabled commands

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

@bot.tree.command(name="giveawayroles", description="View giveaway manager roles")
@command_enabled()
async def giveawayroles(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute("SELECT role_id FROM giveaway_roles WHERE guild_id=?",
                              (interaction.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No giveaway roles configured."); return
    mentions = [r.mention for row in rows if (r := interaction.guild.get_role(row[0]))]
    await interaction.response.send_message("🎉 Giveaway Roles:\n" + "\n".join(mentions))

# ═══════════════════════════════════════════════════════
# BALANCE COMMANDS
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="gift", description="Gift balance to another user")
@command_enabled()
async def gift(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You cannot gift yourself.", ephemeral=True); return
    gid = interaction.guild.id
    bal = await get_balance(gid, interaction.user.id)
    if bal < amount:
        await interaction.response.send_message("❌ Not enough balance.", ephemeral=True); return
    await add_balance(gid, interaction.user.id, -amount)
    await add_balance(gid, user.id, amount)
    await add_stat(gid, interaction.user.id, "gifted_balance", amount)
    await interaction.response.send_message(f"💸 You gifted {amount:,} coins to {user.mention}!")

@bot.tree.command(name="balance", description="Check a balance")
@command_enabled()
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    bal = await get_balance(interaction.guild.id, user.id)
    embed = discord.Embed(title=f"💰 {user.display_name}'s Balance",
                          description=f"{bal:,} coins", color=discord.Color.green())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="addbalance", description="Add balance to a user")
@command_enabled()
async def addbalance(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_balance(interaction.guild.id, user.id, amount)
    await interaction.response.send_message(f"✅ Added {amount:,} coins to {user.mention}")

@bot.tree.command(name="removebalance", description="Remove balance from a user")
@command_enabled()
async def removebalance(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_balance(interaction.guild.id, user.id, -amount)
    await interaction.response.send_message(f"❌ Removed {amount:,} coins from {user.mention}")

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

@bot.tree.command(name="addexp", description="Add usable EXP to a user — does NOT affect Total EXP (7d) or Activity Rank")
@command_enabled()
async def addexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    await add_exp(interaction.guild.id, user.id, amount, is_bonus=True)
    await interaction.response.send_message(
        f"✅ Added **{amount:,}** usable EXP to {user.mention} (Total EXP / Activity Rank unchanged).")

@bot.tree.command(name="removeexp", description="Remove EXP from a user")
@command_enabled()
async def removeexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_exp(interaction.guild.id, user.id, -amount)
    await interaction.response.send_message(f"❌ Removed {amount:,} EXP from {user.mention}")

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
        meta        = json.loads(prize_raw)
        prize_label = meta.get("label", prize_raw)
    except (json.JSONDecodeError, TypeError):
        meta        = {"label": prize_raw, "balance": legacy_reward}
        prize_label = prize_raw

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
    if not users:
        await channel.send("No valid participants."); return

    weighted = []
    for user in users:
        lvl = await get_level(channel.guild.id, user.id)
        weighted.extend([user] * random.randint(1, min(100, max(1, lvl))))

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
# REROLL  (supports ALL prize types)
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="reroll", description="Reroll a giveaway — distributes all prize types")
@command_enabled()
async def reroll(interaction: discord.Interaction, message_id: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    mid = int(message_id)
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT * FROM giveaways WHERE message_id=?", (mid,)) as cur:
                data = await cur.fetchone()
            if not data:
                await interaction.response.send_message("❌ Giveaway not found."); return
            (_mid, channel_id, prize_raw, winner_count, legacy_reward,
             end_time, required_role, template, ended) = data
            async with db.execute("SELECT winner_id, reward FROM giveaway_winners WHERE message_id=?",
                                  (mid,)) as cur:
                old_data = await cur.fetchone()

    channel = bot.get_channel(channel_id)
    if not channel: await interaction.response.send_message("❌ Channel not found."); return
    try:
        message = await channel.fetch_message(mid)
    except discord.NotFound:
        await interaction.response.send_message("❌ Message not found."); return

    reaction = discord.utils.get(message.reactions, emoji="🎉")
    if not reaction: await interaction.response.send_message("❌ Reaction not found."); return

    users = []
    async for user in reaction.users():
        if user.bot: continue
        member = channel.guild.get_member(user.id)
        if not member: continue
        if required_role and required_role not in [r.id for r in member.roles]: continue
        users.append(user)
    if not users:
        await interaction.response.send_message("❌ No participants."); return

    weighted = []
    for user in users:
        inv   = await inventory_get(interaction.guild.id, interaction.user.id)
        weighted.extend([user] * random.randint(1, min(100, max(1, level))))
    new_winner = random.choice(weighted)

    try:
        meta        = json.loads(prize_raw)
        prize_label = meta.get("label", prize_raw)
    except (json.JSONDecodeError, TypeError):
        meta        = {"label": prize_raw, "balance": legacy_reward}
        prize_label = prize_raw

    if old_data:
        await add_balance(channel.guild.id, old_data[0], -old_data[1])

    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO giveaway_winners VALUES(?,?,?)",
                             (mid, new_winner.id, int(meta.get("balance", 0))))
            await db.commit()

    await distribute_prizes(channel.guild, [new_winner], meta)

    reward_summary = build_reward_summary(meta, channel.guild)
    embed = discord.Embed(
        title="🔄 Giveaway Rerolled",
        description=f"**Prize:** {prize_label}\n**Reward:** {reward_summary}\n**New Winner:** {new_winner.mention}",
        color=discord.Color.orange())
    await channel.send(embed=embed)
    await interaction.response.send_message("✅ Giveaway rerolled.")

# ═══════════════════════════════════════════════════════
# AUTO GIVEAWAY
# ═══════════════════════════════════════════════════════

AUTO_GIVEAWAY_ENABLED = False
AUTO_GIVEAWAY_POOL    = []
auto_giveaway_task    = None

@bot.tree.command(name="addautogiveaway", description="Add a giveaway to the auto pool")
@command_enabled()
async def addautogiveaway(interaction: discord.Interaction, prize: str, reward: int, winners: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    AUTO_GIVEAWAY_POOL.append({"prize": prize, "reward": reward, "winners": winners})
    await interaction.response.send_message(f"✅ Added: {prize} | Reward: {reward} | Winners: {winners}")

@bot.tree.command(name="removeautogiveaway", description="Remove auto giveaway by prize name")
@command_enabled()
async def removeautogiveaway(interaction: discord.Interaction, prize: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    global AUTO_GIVEAWAY_POOL
    before = len(AUTO_GIVEAWAY_POOL)
    AUTO_GIVEAWAY_POOL = [g for g in AUTO_GIVEAWAY_POOL if g["prize"].lower() != prize.lower()]
    if len(AUTO_GIVEAWAY_POOL) == before:
        await interaction.response.send_message("❌ Not found.")
    else:
        await interaction.response.send_message(f"🗑 Removed: {prize}")

@bot.tree.command(name="startgiveaways", description="Start automatic giveaways")
@app_commands.describe(interval_seconds="Seconds between giveaways",
                       giveaway_duration_seconds="How long each lasts",
                       channel="Channel (default current)")
@command_enabled()
async def startgiveaways(interaction: discord.Interaction, interval_seconds: int,
                         giveaway_duration_seconds: int, channel: discord.TextChannel = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    global AUTO_GIVEAWAY_ENABLED, auto_giveaway_task
    if auto_giveaway_task and not auto_giveaway_task.done():
        await interaction.response.send_message("Already running.", ephemeral=True); return
    if not AUTO_GIVEAWAY_POOL:
        await interaction.response.send_message("❌ No auto giveaways added.", ephemeral=True); return
    target_channel = channel or interaction.channel
    AUTO_GIVEAWAY_ENABLED = True
    async def auto_loop():
        global AUTO_GIVEAWAY_ENABLED
        while AUTO_GIVEAWAY_ENABLED:
            gd = random.choice(AUTO_GIVEAWAY_POOL)
            end_time = datetime.now(UTC) + timedelta(seconds=giveaway_duration_seconds)
            embed = discord.Embed(title="🎉 AUTOMATIC GIVEAWAY 🎉",
                description=(f"React with 🎉 to enter\n\nPrize: **{gd['prize']}**\n"
                             f"Winners: **{gd['winners']}**\nReward: **{gd['reward']} coins**\n"
                             f"Ends: <t:{int(end_time.timestamp())}:R>"),
                color=discord.Color.gold())
            msg = await target_channel.send(embed=embed)
            await msg.add_reaction("🎉")
            async with get_db() as db:
                await db.execute(
                    "INSERT INTO giveaways(message_id,channel_id,prize,winners,reward,end_time,required_role,template,ended) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (msg.id, target_channel.id, gd["prize"], gd["winners"], gd["reward"],
                     int(end_time.timestamp()), 0, "gold", 0))
                await db.commit()
            asyncio.create_task(giveaway_timer(msg.id, giveaway_duration_seconds))
            await asyncio.sleep(interval_seconds)
    auto_giveaway_task = asyncio.create_task(auto_loop())
    await interaction.response.send_message("✅ Automatic giveaways started.")

@bot.tree.command(name="stopgiveaways", description="Stop automatic giveaways")
@command_enabled()
async def stopgiveaways(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    global AUTO_GIVEAWAY_ENABLED, auto_giveaway_task
    AUTO_GIVEAWAY_ENABLED = False
    if auto_giveaway_task:
        auto_giveaway_task.cancel(); auto_giveaway_task = None
    await interaction.response.send_message("🛑 Automatic giveaways stopped.")

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

@bot.tree.command(name="addtickets", description="Add raffle tickets to a user")
@command_enabled()
async def addtickets(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_tickets(interaction.guild.id, user.id, amount)
    await interaction.response.send_message(f"✅ Added {amount} tickets to {user.mention}")

@bot.tree.command(name="removetickets", description="Remove raffle tickets from a user")
@command_enabled()
async def removetickets(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_tickets(interaction.guild.id, user.id, -amount)
    await interaction.response.send_message(f"❌ Removed {amount} tickets from {user.mention}")

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
                async with db.execute("SELECT user_id,tickets FROM raffle WHERE guild_id=?",
                                      (guild.id,)) as cur:
                    entries = await cur.fetchall()
            pool = []
            for uid, t in entries: pool.extend([uid] * t)
            if not pool: continue
            winner_id = random.choice(pool)
            await add_balance(guild.id, winner_id, RAFFLE_PRIZE)
            async with get_db() as db:
                async with db.execute("SELECT channel_id FROM raffle_config WHERE guild_id=?",
                                      (guild.id,)) as cur:
                    row = await cur.fetchone()
            ann = bot.get_channel(row[0]) if row else guild.system_channel
            if ann:
                await ann.send(f"🎉 <@{winner_id}> won the daily raffle and will receive a huge pet!")
                # ▼ ADD THIS LINE:
                await log_event(guild.id, "raffle", _log_embed(
                    "🎟 Daily Raffle Draw", discord.Color.gold(),
                    Winner=f"<@{winner_id}>", Guild=guild.name))
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

@bot.tree.command(name="listchestprizes", description="List all prizes in the chest loot table")
@app_commands.choices(chest_type=[
    app_commands.Choice(name="EXP Chest",  value="chest"),
    app_commands.Choice(name="VIP Chest",  value="vipchest")])
@command_enabled()
async def listchestprizes(interaction: discord.Interaction, chest_type: str):
    prizes     = await get_chest_prizes(interaction.guild.id, chest_type)
    is_custom  = any("id" in p for p in prizes)
    total_w    = sum(p["chance"] for p in prizes)
    title      = "📦 EXP Chest Prizes" if chest_type == "chest" else "💎 VIP Chest Prizes"
    embed      = discord.Embed(title=title, color=discord.Color.purple())
    if not is_custom:
        embed.set_footer(text="Using default prizes. Use /addchestprize to customise.")
    lines = []
    for p in prizes:
        pct  = (p["chance"] / total_w * 100) if total_w > 0 else 0
        desc = []
        if p["exp"] > 0:     desc.append(f"⭐ {p['exp']:,} EXP")
        if p["balance"] > 0: desc.append(f"💰 {p['balance']:,} coins")
        if not desc:         desc.append("✨ Special")
        id_str = f"`#{p['id']}` " if "id" in p else ""
        lines.append(f"{id_str}**{p['name']}** — {' + '.join(desc)} — **{pct:.1f}%** (w: {p['chance']})")
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)

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
    await add_exp(gid, interaction.user.id, -total_cost)
    if total_balance > 0: await add_balance(gid, interaction.user.id, total_balance)
    if total_exp_won > 0: await add_exp(gid, interaction.user.id, total_exp_won)
    await add_stat(gid, interaction.user.id, "chests_opened", amount)

    result_text = "\n".join(f"• {count}x {name}" for name, count in results.items())
    embed = discord.Embed(title="📦 Chest Results", description=result_text, color=discord.Color.purple())
    embed.set_footer(text=f"Opened {amount} chest(s) | Cost: {total_cost:,} EXP")
    await interaction.followup.send(embed=embed)

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

@bot.tree.command(name="givekey", description="Give VIP Chest Key(s) to a user")
@app_commands.describe(user="Target user", amount="Number of keys (default 1)")
@command_enabled()
async def givekey(interaction: discord.Interaction, user: discord.Member, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    await inventory_add(interaction.guild.id, user.id, VIP_CHEST_KEY, amount)
    await interaction.response.send_message(f"🔑 Gave **{amount}x {VIP_CHEST_KEY}** to {user.mention}.")

@bot.tree.command(name="takekey", description="Take VIP Chest Key(s) from a user")
@app_commands.describe(user="Target user", amount="Number of keys (default 1)")
@command_enabled()
async def takekey(interaction: discord.Interaction, user: discord.Member, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    if not await inventory_remove(interaction.guild.id, user.id, VIP_CHEST_KEY, amount):
        await interaction.response.send_message(f"❌ {user.mention} doesn't have {amount}x {VIP_CHEST_KEY}.",
                                                ephemeral=True); return
    await interaction.response.send_message(f"🗑 Took **{amount}x {VIP_CHEST_KEY}** from {user.mention}.")

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

# ─── RARE BOX DROP CONFIG ─────────────────────────────────────────────────────

@bot.tree.command(name="addrarebox",
                  description="Mark a box prize as a rare drop (triggers announcement in rare drop channel)")
@app_commands.describe(box="Box name", prize_id="Prize ID from /listboxes")
@command_enabled()
async def addrarebox(interaction: discord.Interaction, box: str, prize_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (interaction.guild.id, box)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message(f"❌ Box **{box}** not found.", ephemeral=True); return
        async with db.execute(
            "SELECT prize_type, prize_value FROM abuse_box_prizes "
            "WHERE id=? AND guild_id=? AND box_name=?",
            (prize_id, interaction.guild.id, box)) as cur:
            row = await cur.fetchone()
    if not row:
        await interaction.response.send_message(
            f"❌ Prize #{prize_id} not found in **{box}**. Use `/listboxes` to see IDs.",
            ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute(
                    "INSERT INTO rare_box_config(guild_id, box_name, prize_id) VALUES(?,?,?)",
                    (interaction.guild.id, box, prize_id))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(
                    f"❌ Prize #{prize_id} in **{box}** is already marked as rare.", ephemeral=True); return
    p_type, p_value = row
    await interaction.response.send_message(
        f"✅ Prize `#{prize_id}` ({p_type}: **{p_value}**) in **{box}** is now a rare drop.")

@bot.tree.command(name="removerarebox",
                  description="Unmark a box prize as a rare drop")
@app_commands.describe(box="Box name", prize_id="Prize ID from /listboxes")
@command_enabled()
async def removerarebox(interaction: discord.Interaction, box: str, prize_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM rare_box_config WHERE guild_id=? AND box_name=? AND prize_id=?",
                (interaction.guild.id, box, prize_id))
            await db.commit()
    await interaction.response.send_message(f"🗑 Prize #{prize_id} in **{box}** is no longer a rare drop.")

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

@bot.tree.command(name="systemstatus", description="Check which systems are enabled or disabled")
@command_enabled()
async def systemstatus(interaction: discord.Interaction):
    embed = discord.Embed(title="⚙️ System Status", color=discord.Color.blurple())
    for flag, label in _SYSTEM_LABELS.items():
        on = await is_system_enabled(interaction.guild.id, flag)
        embed.add_field(name=label, value="✅ Enabled" if on else "🔒 Disabled", inline=True)
    await interaction.response.send_message(embed=embed)

# ═══════════════════════════════════════════════════════
# LEADERBOARD
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="leaderboard", description="View leaderboards")
@app_commands.choices(category=[
    app_commands.Choice(name="Total EXP",              value="total_exp"),
    app_commands.Choice(name="Current EXP",            value="current_exp"),
    app_commands.Choice(name="Balance",                value="balance"),
    app_commands.Choice(name="Lifetime Tickets",       value="raffle_tickets_bought"),
    app_commands.Choice(name="Current Raffle Tickets", value="current_tickets"),
    app_commands.Choice(name="Chests Opened",          value="chests_opened"),
    app_commands.Choice(name="Gifted Balance",         value="gifted_balance"),
])
@command_enabled()
async def leaderboard(interaction: discord.Interaction, category: app_commands.Choice[str]):
    value = category.value
    leaderboard_data = []
    async with get_db() as db:
        if value == "current_exp":
            async with db.execute(
                "SELECT DISTINCT user_id FROM exp_history WHERE guild_id=?",
                (interaction.guild.id,)) as cur:
                users = await cur.fetchall()
            for (uid,) in users:
                leaderboard_data.append((uid, await get_exp(interaction.guild.id, uid)))
        elif value == "current_tickets":
            async with db.execute(
                "SELECT user_id,tickets FROM raffle WHERE guild_id=? ORDER BY tickets DESC LIMIT 10",
                (interaction.guild.id,)) as cur:
                leaderboard_data = await cur.fetchall()
        elif value == "balance":
            async with db.execute(
                "SELECT user_id,balance FROM balances WHERE guild_id=? ORDER BY balance DESC LIMIT 10",
                (interaction.guild.id,)) as cur:
                leaderboard_data = await cur.fetchall()
        else:
            async with db.execute(
                f"SELECT user_id,{value} FROM user_stats WHERE guild_id=? ORDER BY {value} DESC LIMIT 10",
                (interaction.guild.id,)) as cur:
                leaderboard_data = await cur.fetchall()
    if value == "current_exp":
        leaderboard_data.sort(key=lambda x: x[1], reverse=True)
        leaderboard_data = leaderboard_data[:10]
    if not leaderboard_data:
        await interaction.response.send_message("❌ No data found."); return
    title_map = {
        "total_exp": "🏆 Total EXP", "current_exp": "⭐ Current EXP",
        "balance": "💰 Balance", "raffle_tickets_bought": "🎟 Lifetime Tickets",
        "current_tickets": "🎫 Current Tickets", "chests_opened": "📦 Chests Opened",
        "gifted_balance": "💸 Gifted Balance"
    }
    embed  = discord.Embed(title=title_map[value] + " Leaderboard", color=discord.Color.gold())
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, amt) in enumerate(leaderboard_data, 1):
        u = interaction.guild.get_member(uid)
        if not u: continue
        embed.add_field(name=f"{medals[i-1] if i<=3 else '#'+str(i)} {u.display_name}",
                        value=f"{amt:,}", inline=False)
    await interaction.response.send_message(embed=embed)

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

def build_raffle_info_embed(guild, total_tickets, top_entries):
    now    = datetime.now(UTC)
    target = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= target: target += timedelta(days=1)
    end_ts = int(target.timestamp())
    embed  = discord.Embed(title="🎟 Live Raffle Status", color=discord.Color.gold())
    embed.add_field(name="⏰ Next Draw",          value=f"<t:{end_ts}:R> (<t:{end_ts}:F>)",
                    inline=False)
    embed.add_field(name="🎫 Total Tickets",       value=f"{total_tickets:,}", inline=False)
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
            async with db.execute("SELECT guild_id,channel_id,message_id FROM raffle_info_config") as cur:
                configs = await cur.fetchall()
        for guild_id, channel_id, message_id in configs:
            try:
                guild   = bot.get_guild(guild_id)
                channel = bot.get_channel(channel_id)
                if not guild or not channel: continue
                async with get_db() as db:
                    async with db.execute(
                        "SELECT user_id,tickets FROM raffle WHERE guild_id=? ORDER BY tickets DESC LIMIT 5",
                        (guild_id,)) as cur:
                        top = await cur.fetchall()
                    async with db.execute("SELECT SUM(tickets) FROM raffle WHERE guild_id=?",
                                          (guild_id,)) as cur:
                        total = (await cur.fetchone())[0] or 0
                embed = build_raffle_info_embed(guild, total, top)
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.edit(embed=embed)
                except discord.NotFound:
                    new_msg = await channel.send(embed=embed)
                    async with db_lock:
                        async with get_db() as db:
                            await db.execute("UPDATE raffle_info_config SET message_id=? WHERE guild_id=?",
                                             (new_msg.id, guild_id))
                            await db.commit()
            except Exception as e:
                print(f"[RaffleInfoLoop] {guild_id}: {e}")
        await asyncio.sleep(60)

# ═══════════════════════════════════════════════════════
# EXP BOOSTS
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="expboost", description="Set an EXP boost for a role (decimals and negatives supported)")
@app_commands.describe(role="Role to boost",
                       boost="e.g. 1.5 = +1.5%, -25 = penalty. All matching roles are summed.")
@command_enabled()
async def expboost(interaction: discord.Interaction, role: discord.Role, boost: float):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if boost == 0:
        await interaction.response.send_message("❌ Boost cannot be 0%.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO exp_boosts VALUES(?,?,?)",
                             (interaction.guild.id, role.id, boost))
            await db.commit()
    sign = "+" if boost > 0 else ""
    await interaction.response.send_message(f"✅ {role.mention} now earns **{sign}{boost}% EXP** per message.")

@bot.tree.command(name="removeexpboost", description="Remove an EXP boost from a role")
@command_enabled()
async def removeexpboost(interaction: discord.Interaction, role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM exp_boosts WHERE guild_id=? AND role_id=?",
                             (interaction.guild.id, role.id))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed EXP boost from {role.mention}.")

@bot.tree.command(name="listexpboosts", description="List all active EXP boosts")
@command_enabled()
async def listexpboosts(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute(
            "SELECT role_id,boost_percent FROM exp_boosts WHERE guild_id=? ORDER BY boost_percent DESC",
            (interaction.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No EXP boosts configured."); return
    embed = discord.Embed(title="⚡ Active EXP Boosts", color=discord.Color.blurple())
    for role_id, boost in rows:
        role = interaction.guild.get_role(role_id)
        name = role.mention if role else f"<deleted role {role_id}>"
        sign = "+" if boost > 0 else ""
        embed.add_field(name=name, value=f"{sign}{boost}%", inline=False)
    await interaction.response.send_message(embed=embed)

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
# ADMIN ABUSE BOX SYSTEM
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="addbox", description="Create a new admin abuse box")
@command_enabled()
async def addbox(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO abuse_boxes VALUES(?,?)", (interaction.guild.id, name))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(f"❌ Box **{name}** already exists.",
                                                        ephemeral=True); return
    await interaction.response.send_message(f"✅ Created box **{name}**.")

@bot.tree.command(name="removebox", description="Delete an admin abuse box and all its prizes")
@command_enabled()
async def removebox(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                                  (interaction.guild.id, name)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Box **{name}** not found.",
                                                            ephemeral=True); return
            await db.execute("DELETE FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                             (interaction.guild.id, name))
            await db.execute("DELETE FROM abuse_box_prizes WHERE guild_id=? AND box_name=?",
                             (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed box **{name}** and all its prizes.")

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

@bot.tree.command(name="removeboxprize", description="Remove a prize from a box by ID (see /listboxes)")
@command_enabled()
async def removeboxprize(interaction: discord.Interaction, box: str, prize_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT id FROM abuse_box_prizes WHERE id=? AND guild_id=? AND box_name=?",
                (prize_id, interaction.guild.id, box)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Prize #{prize_id} not found.",
                                                            ephemeral=True); return
            await db.execute("DELETE FROM abuse_box_prizes WHERE id=?", (prize_id,))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed prize #{prize_id} from **{box}**.")

@bot.tree.command(name="listboxes", description="List all abuse boxes and their prizes")
@command_enabled()
async def listboxes(interaction: discord.Interaction, box: str = None):
    async with get_db() as db:
        query  = "SELECT box_name FROM abuse_boxes WHERE guild_id=?" + (" AND box_name=?" if box else "")
        params = (interaction.guild.id, box) if box else (interaction.guild.id,)
        async with db.execute(query, params) as cur:
            boxes = await cur.fetchall()
    if not boxes:
        await interaction.response.send_message("❌ No boxes found."); return
    embed = discord.Embed(title="📦 Admin Abuse Boxes", color=discord.Color.orange())
    for (box_name,) in boxes:
        async with get_db() as db:
            async with db.execute(
                "SELECT id,prize_type,prize_value,chance FROM abuse_box_prizes "
                "WHERE guild_id=? AND box_name=? ORDER BY id",
                (interaction.guild.id, box_name)) as cur:
                prizes = await cur.fetchall()
        if not prizes:
            embed.add_field(name=f"📦 {box_name}", value="*No prizes yet*", inline=False); continue
        total_w = sum(p[3] for p in prizes)
        lines   = []
        for p_id, p_type, p_value, p_chance in prizes:
            pct = (p_chance / total_w * 100) if total_w > 0 else 0
            if p_type == "balance": desc = f"💰 {int(p_value):,} coins"
            elif p_type == "exp":   desc = f"⭐ {int(p_value):,} EXP"
            elif p_type == "item":  desc = f"🎒 {p_value}"
            else:                   desc = f"✨ {p_value}"
            lines.append(f"`#{p_id}` {desc} — **{pct:.1f}%** (weight: {p_chance})")
        embed.add_field(name=f"📦 {box_name}", value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="givebox", description="Give an abuse box to all members with a specific role")
@app_commands.describe(box="Box name", role="Role whose members receive the box",
                       amount="How many each (default 1)")
@command_enabled()
async def givebox(interaction: discord.Interaction, box: str, role: discord.Role, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (interaction.guild.id, box)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message(f"❌ Box **{box}** not found.",
                                                        ephemeral=True); return
    members = [m for m in interaction.guild.members if role in m.roles and not m.bot]
    if not members:
        await interaction.response.send_message(f"❌ No members with {role.mention}.",
                                                ephemeral=True); return
    await interaction.response.defer()
    for m in members: await inventory_add(interaction.guild.id, m.id, box, amount)
    await interaction.followup.send(
        f"✅ Gave **{amount}x {box}** to **{len(members)}** member(s) with {role.mention}.")

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

@bot.tree.command(name="createcode", description="Create a redeemable code with prizes")
@app_commands.describe(
    code="Code players type (e.g. SUMMER2025)",
    prize_json='JSON prize: {"balance":500,"exp":1000,"tickets":2,"gamble_tokens":1,"vip_keys":1,"item":"BoxName","item_qty":1}',
    uses="How many times (−1 for unlimited, default -1)",
    min_activity_rank="Minimum Activity Rank required (default 0)",
    min_balance="Minimum balance required (default 0)",
    required_role="Required role to redeem (optional)"
)
@command_enabled()
async def createcode(interaction: discord.Interaction, code: str, prize_json: str,
                     uses: int = -1, min_activity_rank: int = 0, min_balance: int = 0,
                     required_role: discord.Role = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
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
                    "INSERT INTO redeem_codes(guild_id,code,prize_json,uses_left,min_level,min_balance,required_role_id) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (interaction.guild.id, code, json.dumps(prize), uses, min_activity_rank, min_balance,
                     required_role.id if required_role else 0))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(f"❌ Code **{code}** already exists.",
                                                        ephemeral=True); return
    parts = []
    if prize.get("balance", 0) > 0:       parts.append(f"💰 {prize['balance']:,}")
    if prize.get("exp", 0) > 0:           parts.append(f"⭐ {prize['exp']:,} EXP")
    if prize.get("tickets", 0) > 0:       parts.append(f"🎟 {prize['tickets']} ticket(s)")
    if prize.get("gamble_tokens", 0) > 0: parts.append(f"🎲 {prize['gamble_tokens']} token(s)")
    if prize.get("vip_keys", 0) > 0:      parts.append(f"🔑 {prize['vip_keys']} key(s)")
    if prize.get("item"):                  parts.append(f"🎒 {prize.get('item_qty',1)}x {prize['item']}")
    uses_str = "unlimited" if uses == -1 else str(uses)
    await interaction.response.send_message(
        f"✅ Code **{code}** created!\nPrize: {' + '.join(parts) or 'None'}\n"
        f"Uses: {uses_str} | Min activity rank: {min_activity_rank} | Min balance: {min_balance:,}"
        + (f" | Required role: {required_role.mention}" if required_role else ""),
        ephemeral=True)

@bot.tree.command(name="deletecode", description="Delete a redeemable code")
@command_enabled()
async def deletecode(interaction: discord.Interaction, code: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    code = code.upper().strip()
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT code FROM redeem_codes WHERE guild_id=? AND code=?",
                                  (interaction.guild.id, code)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Code **{code}** not found.",
                                                            ephemeral=True); return
            await db.execute("DELETE FROM redeem_codes WHERE guild_id=? AND code=?",
                             (interaction.guild.id, code))
            await db.execute("DELETE FROM code_uses WHERE guild_id=? AND code=?",
                             (interaction.guild.id, code))
            await db.commit()
    await interaction.response.send_message(f"🗑 Code **{code}** deleted.")

@bot.tree.command(name="listcodes", description="List all active redeemable codes (admin only)")
@command_enabled()
async def listcodes(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute(
            "SELECT code,prize_json,uses_left,min_level,min_balance,required_role_id "
            "FROM redeem_codes WHERE guild_id=?", (interaction.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No codes configured.", ephemeral=True); return
    embed = discord.Embed(title="🎫 Redeemable Codes", color=discord.Color.green())
    for code, prize_json, uses_left, min_level, min_balance, req_role_id in rows:
        try: prize = json.loads(prize_json)
        except: prize = {}
        parts = []
        if prize.get("balance", 0) > 0:       parts.append(f"💰 {prize['balance']:,}")
        if prize.get("exp", 0) > 0:           parts.append(f"⭐ {prize['exp']:,}")
        if prize.get("tickets", 0) > 0:       parts.append(f"🎟 {prize['tickets']}")
        if prize.get("gamble_tokens", 0) > 0: parts.append(f"🎲 {prize['gamble_tokens']}")
        if prize.get("vip_keys", 0) > 0:      parts.append(f"🔑 {prize['vip_keys']}")
        if prize.get("item"):                  parts.append(f"🎒 {prize.get('item_qty',1)}x {prize['item']}")
        uses_str = "∞" if uses_left == -1 else str(uses_left)
        req = ""
        if req_role_id:
            role = interaction.guild.get_role(req_role_id)
            req  = f" | Role: {role.mention if role else '?'}"
        embed.add_field(name=f"🎫 `{code}`",
                        value=f"{' + '.join(parts) or 'No prize'}\n"
                              f"Uses: {uses_str} | Activity Rank ≥ {min_level} | Bal ≥ {min_balance:,}{req}",
                        inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

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

@bot.tree.command(name="givegambletoken", description="Give Gamble Token(s) to a user")
@app_commands.describe(user="Target user", amount="Number of tokens (default 1)")
@command_enabled()
async def givegambletoken(interaction: discord.Interaction, user: discord.Member, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    await inventory_add(interaction.guild.id, user.id, GAMBLE_TOKEN, amount)
    await interaction.response.send_message(f"🎲 Gave **{amount}x {GAMBLE_TOKEN}** to {user.mention}.")

@bot.tree.command(name="takegambletoken", description="Take Gamble Token(s) from a user")
@app_commands.describe(user="Target user", amount="Number of tokens (default 1)")
@command_enabled()
async def takegambletoken(interaction: discord.Interaction, user: discord.Member, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    if not await inventory_remove(interaction.guild.id, user.id, GAMBLE_TOKEN, amount):
        await interaction.response.send_message(
            f"❌ {user.mention} doesn't have {amount}x {GAMBLE_TOKEN}.", ephemeral=True); return
    await interaction.response.send_message(f"🗑 Took **{amount}x {GAMBLE_TOKEN}** from {user.mention}.")

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
        winnings = bet * (multiplier - 1)
        await add_balance(interaction.guild.id, interaction.user.id, winnings)
        outcome = f"🏆 **You win {winnings:,} coins!** ({multiplier}×)"
        color   = discord.Color.green()
    else:
        await add_balance(interaction.guild.id, interaction.user.id, -bet)
        outcome = f"💸 **You lose {bet:,} coins.**"
        color   = discord.Color.red()

    embed = discord.Embed(title="🎰 Roulette", color=color,
        description=(
            f"Ball landed on: {result_str}\n"
            f"Your bet: **{bet_label}** for **{bet:,} coins** ({multiplier}×)\n\n"
            f"{outcome}"
        ))
    embed.set_footer(text=f"1 {GAMBLE_TOKEN} consumed | {tokens - 1} remaining")
    await interaction.response.send_message(embed=embed)

# ═══════════════════════════════════════════════════════
# RANDOM GAMES SYSTEM
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="addgame", description="Add a random game to the pool")
@app_commands.describe(name="The question shown to players",
                       reward_balance="Coin reward for winner",
                       reward_exp="EXP reward for winner")
@command_enabled()
async def addgame(interaction: discord.Interaction, name: str,
                  reward_balance: int = 0, reward_exp: int = 0):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute(
                    "INSERT INTO games(guild_id,game_name,reward_balance,reward_exp) VALUES(?,?,?,?)",
                    (interaction.guild.id, name, reward_balance, reward_exp))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(f"❌ Game **{name}** already exists.",
                                                        ephemeral=True); return
    await interaction.response.send_message(
        f"✅ Added game **{name}** (💰 {reward_balance:,} + ⭐ {reward_exp:,} EXP).\n"
        f"Use `/addgameanswer` to add valid answers.")

@bot.tree.command(name="removegame", description="Remove a game and all its answers")
@command_enabled()
async def removegame(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                                  (interaction.guild.id, name)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Game **{name}** not found.",
                                                            ephemeral=True); return
            await db.execute("DELETE FROM games WHERE guild_id=? AND game_name=?",
                             (interaction.guild.id, name))
            await db.execute("DELETE FROM game_answers WHERE guild_id=? AND game_name=?",
                             (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed game **{name}**.")

@bot.tree.command(name="enablegame", description="Enable a game so it appears in automatic games")
@command_enabled()
async def enablegame(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                                  (interaction.guild.id, name)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Game **{name}** not found.",
                                                            ephemeral=True); return
            await db.execute("UPDATE games SET enabled=1 WHERE guild_id=? AND game_name=?",
                             (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"✅ Game **{name}** enabled.")

@bot.tree.command(name="disablegame", description="Disable a game without deleting it")
@command_enabled()
async def disablegame(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                                  (interaction.guild.id, name)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Game **{name}** not found.",
                                                            ephemeral=True); return
            await db.execute("UPDATE games SET enabled=0 WHERE guild_id=? AND game_name=?",
                             (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"🔒 Game **{name}** disabled.")

@bot.tree.command(name="addgameanswer", description="Add a valid answer to a game")
@command_enabled()
async def addgameanswer(interaction: discord.Interaction, game_name: str, answer: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND game_name=?",
                              (interaction.guild.id, game_name)) as cur:
            if not await cur.fetchone():
                await interaction.response.send_message(f"❌ Game **{game_name}** not found.",
                                                        ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO game_answers(guild_id,game_name,answer) VALUES(?,?,?)",
                             (interaction.guild.id, game_name, answer))
            await db.commit()
    await interaction.response.send_message(f"✅ Added answer `{answer}` to **{game_name}**.")

@bot.tree.command(name="removegameanswer", description="Remove an answer from a game by ID (see /listgames)")
@command_enabled()
async def removegameanswer(interaction: discord.Interaction, game_name: str, answer_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT id FROM game_answers WHERE id=? AND guild_id=? AND game_name=?",
                (answer_id, interaction.guild.id, game_name)) as cur:
                if not await cur.fetchone():
                    await interaction.response.send_message(f"❌ Answer #{answer_id} not found.",
                                                            ephemeral=True); return
            await db.execute("DELETE FROM game_answers WHERE id=?", (answer_id,))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed answer #{answer_id} from **{game_name}**.")

@bot.tree.command(name="listgames", description="List all games and their answers")
@command_enabled()
async def listgames(interaction: discord.Interaction, game_name: str = None):
    await interaction.response.defer()
    async with get_db() as db:
        if game_name:
            async with db.execute(
                "SELECT game_name,enabled,reward_balance,reward_exp FROM games WHERE guild_id=? AND game_name=?",
                (interaction.guild.id, game_name)) as cur:
                games = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT game_name,enabled,reward_balance,reward_exp FROM games WHERE guild_id=?",
                (interaction.guild.id,)) as cur:
                games = await cur.fetchall()
    if not games:
        await interaction.followup.send("❌ No games configured."); return

    embed = discord.Embed(title="🎮 Random Games", color=discord.Color.teal())

    for (gname, enabled, reward_bal, reward_exp) in games:
        async with get_db() as db:
            async with db.execute(
                "SELECT id,answer FROM game_answers WHERE guild_id=? AND game_name=? ORDER BY id",
                (interaction.guild.id, gname)) as cur:
                answers = await cur.fetchall()

        status = "✅ Enabled" if enabled else "🔒 Disabled"
        parts  = []
        if reward_bal > 0: parts.append(f"💰 {reward_bal:,}")
        if reward_exp > 0: parts.append(f"⭐ {reward_exp:,}")

        header = f"Reward: {' + '.join(parts) or 'None'}\nAnswers ({len(answers)} total):\n"

        if not answers:
            ans_block = "  *No answers yet*"
        else:
            all_lines = [f"  `#{aid}` {ans}" for aid, ans in answers]
            shown = []
            truncated = False

            for i, line in enumerate(all_lines):
                remaining = len(all_lines) - i - 1
                # When more lines follow, reserve space for the truncation note
                if remaining > 0:
                    test = header + "\n".join(shown + [line]) + f"\n  *... and {remaining} more*"
                else:
                    test = header + "\n".join(shown + [line])

                if len(test) > 1024:
                    truncated = True
                    break
                shown.append(line)

            if truncated:
                left = len(all_lines) - len(shown)
                if shown:
                    ans_block = "\n".join(shown) + f"\n  *... and {left} more*"
                else:
                    # Even the first answer is too long (extremely unlikely)
                    ans_block = f"  *{len(all_lines)} answers — use `/listgames {gname}` to browse*"
            else:
                ans_block = "\n".join(shown)

        embed.add_field(
            name=f"🎯 {gname} [{status}]",
            value=header + ans_block,
            inline=False)

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="setgamechannel", description="Set the channel for random games and configure timing")
@app_commands.describe(channel="Channel for games",
                       answer_time="Seconds to answer (default 30)",
                       interval_seconds="Seconds between games (default 60)")
@command_enabled()
async def setgamechannel(interaction: discord.Interaction, channel: discord.TextChannel,
                         answer_time: int = 30, interval_seconds: int = 60):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if answer_time < 5:
        await interaction.response.send_message("❌ Answer time ≥ 5s.", ephemeral=True); return
    if interval_seconds < 10:
        await interaction.response.send_message("❌ Interval ≥ 10s.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO game_config(guild_id,channel_id,answer_time,interval_seconds) "
                "VALUES(?,?,?,?)",
                (interaction.guild.id, channel.id, answer_time, interval_seconds))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Game channel: {channel.mention} | Answer time: **{answer_time}s** | "
        f"Interval: **{interval_seconds}s**")

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
        async with db.execute("SELECT game_name FROM games WHERE guild_id=? AND enabled=1",
                              (gid,)) as cur:
            if not await cur.fetchall():
                await interaction.response.send_message("❌ No enabled games. Use `/addgame`.",
                                                        ephemeral=True); return
    game_tasks[gid] = asyncio.create_task(guild_game_loop(gid))
    await interaction.response.send_message("🎮 Random games started!")

@bot.tree.command(name="stopgames", description="Stop automatic random games")
@command_enabled()
async def stopgames(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    gid  = interaction.guild.id
    task = game_tasks.pop(gid, None)
    if task: task.cancel()
    active_game_sessions.pop(gid, None)
    await interaction.response.send_message("🛑 Random games stopped.")

async def guild_game_loop(guild_id: int):
    await bot.wait_until_ready()
    while not bot.is_closed():
        async with get_db() as db:
            async with db.execute(
                "SELECT channel_id,answer_time,interval_seconds FROM game_config WHERE guild_id=?",
                (guild_id,)) as cur:
                config = await cur.fetchone()
        if not config: break
        channel_id, answer_time, interval_seconds = config
        channel = bot.get_channel(channel_id)
        if not channel: await asyncio.sleep(30); continue

        async with get_db() as db:
            async with db.execute(
                "SELECT game_name,reward_balance,reward_exp FROM games WHERE guild_id=? AND enabled=1",
                (guild_id,)) as cur:
                games = await cur.fetchall()
        eligible = []
        for gname, rb, re in games:
            async with get_db() as db:
                async with db.execute(
                    "SELECT answer FROM game_answers WHERE guild_id=? AND game_name=?",
                    (guild_id, gname)) as cur:
                    answers = [r[0] for r in await cur.fetchall()]
            if answers: eligible.append((gname, rb, re, answers))

        if not eligible: await asyncio.sleep(interval_seconds); continue

        gname, rb, re, answers = random.choice(eligible)
        correct = random.choice(answers)

        reward_parts = []
        if rb > 0: reward_parts.append(f"💰 {rb:,} coins")
        if re > 0: reward_parts.append(f"⭐ {re:,} EXP")

        embed = discord.Embed(title="🎮 Random Game!", color=discord.Color.teal(),
            description=f"**{gname}**\n\nType your answer in chat!\n⏰ You have **{answer_time} seconds**.")
        if reward_parts: embed.add_field(name="🏆 Winner gets", value=" + ".join(reward_parts), inline=False)
        embed.set_footer(text=f"Answer within {answer_time} seconds!")
        await channel.send(embed=embed)

        answered_event = asyncio.Event()
        active_game_sessions[guild_id] = {
            "game_name": gname, "answer": correct,
            "channel_id": channel_id, "answered": False, "winner": None,
            "event": answered_event
        }

        try:
            await asyncio.wait_for(answered_event.wait(), timeout=answer_time)
        except asyncio.TimeoutError:
            pass
        session = active_game_sessions.pop(guild_id, None)
        if not session: await asyncio.sleep(max(0, interval_seconds - answer_time)); continue

        if session.get("answered") and session.get("winner"):
            winner = session["winner"]
            if rb > 0: await add_balance(guild_id, winner.id, rb)
            if re > 0: await add_exp(guild_id, winner.id, re)
            result_embed = discord.Embed(title="🎉 Correct!", color=discord.Color.green(),
                description=f"{winner.mention} got it right! The answer was **{correct}**.")
            if reward_parts: result_embed.add_field(name="Reward given",
                                                    value=" + ".join(reward_parts), inline=False)
        else:
            result_embed = discord.Embed(title="⏰ Time's Up!", color=discord.Color.red(),
                description=f"Nobody got it. The answer was **{correct}**.")
        await channel.send(embed=result_embed)
        await asyncio.sleep(max(0, interval_seconds - answer_time))

async def game_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for gid, task in list(game_tasks.items()):
            if task.done():
                try:
                    if exc := task.exception(): print(f"[GameLoop] Guild {gid} died: {exc}")
                except Exception: pass
                game_tasks[gid] = asyncio.create_task(guild_game_loop(gid))
        await asyncio.sleep(30)

# ─── LEADERBOARD STAT MANAGEMENT ─────────────────────────────────────────────

_STAT_CHOICES = [
    app_commands.Choice(name="Total EXP",        value="total_exp"),
    app_commands.Choice(name="Gifted Balance",    value="gifted_balance"),
    app_commands.Choice(name="Chests Opened",     value="chests_opened"),
    app_commands.Choice(name="Lifetime Tickets",  value="raffle_tickets_bought"),
]

@bot.tree.command(name="addtotalexp", description="Add to Total EXP (7d) and Activity Rank only — usable EXP stays the same")
@app_commands.describe(user="Target user", amount="Amount to add")
@command_enabled()
async def addtotalexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    now = int(datetime.now(UTC).timestamp())
    async with db_lock:
        async with get_db() as db:
            # Non-bonus positive entry → raises Total EXP (7d) and usable EXP
            gid = interaction.guild.id
            await db.execute(
                "INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                (gid, user.id, amount, now, 0))
            await db.execute(
                "INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                (gid, user.id, -amount, now, 0))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Added **{amount:,}** to {user.mention}'s **Total EXP (7d)** and Activity Rank. Usable EXP unchanged.")


@bot.tree.command(name="removetotalexp",
                  description="Remove from Total EXP (7d) and Activity Rank only — usable EXP stays the same")
@app_commands.describe(user="Target user", amount="Amount to remove")
@command_enabled()
async def removetotalexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return

    week_ago        = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    remaining       = amount
    actually_removed = 0

    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT rowid, amount FROM exp_history "
                "WHERE guild_id=? AND user_id=? AND timestamp>=? AND amount>0 AND is_bonus=0 "
                "ORDER BY timestamp ASC",
                (interaction.guild.id, user.id, week_ago)) as cur:
                entries = await cur.fetchall()

            for rowid, entry_amount in entries:
                if remaining <= 0:
                    break
                if entry_amount <= remaining:
                    await db.execute("DELETE FROM exp_history WHERE rowid=?", (rowid,))
                    remaining -= entry_amount
                else:
                    await db.execute("UPDATE exp_history SET amount=? WHERE rowid=?",
                                     (entry_amount - remaining, rowid))
                    remaining = 0

            actually_removed = amount - remaining
            if actually_removed > 0:
                # Bonus entry so usable EXP is not affected
                await db.execute(
                    "INSERT INTO exp_history(guild_id,user_id,amount,timestamp,is_bonus) VALUES(?,?,?,?,?)",
                    (interaction.guild.id, user.id, actually_removed, int(datetime.now(UTC).timestamp()), 1))
            await db.commit()

    if actually_removed == 0:
        await interaction.response.send_message(f"❌ {user.mention} has no Total EXP (7d) to remove.")
    elif remaining > 0:
        await interaction.response.send_message(
            f"⚠️ Only removed **{actually_removed:,}** from {user.mention}'s **Total EXP (7d)** "
            f"— they didn't have the full {amount:,}. Usable EXP unchanged.")
    else:
        await interaction.response.send_message(
            f"✅ Removed **{amount:,}** from {user.mention}'s **Total EXP (7d)** and Activity Rank. "
            f"Usable EXP unchanged.")
    await log_event(interaction.guild.id, "exp", _log_embed(
        "📉 Total EXP Removed", discord.Color.orange(),
        Admin=interaction.user.mention, User=user.mention,
        Removed=f"-{actually_removed:,}", Requested=f"-{amount:,}"))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ Remove Total EXP", discord.Color.orange(),
        By=interaction.user.mention, User=user.mention, Amount=f"-{actually_removed:,}"))
        
@bot.tree.command(name="addleaderboardstat", description="Add to a user's leaderboard stat")
@app_commands.describe(user="Target user", stat="Which stat to modify", amount="Amount to add")
@app_commands.choices(stat=_STAT_CHOICES)
@command_enabled()
async def addleaderboardstat(interaction: discord.Interaction, user: discord.Member,
                              stat: str, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    await ensure_stats(interaction.guild.id, user.id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                f"UPDATE user_stats SET {stat}={stat}+? WHERE guild_id=? AND user_id=?",
                (amount, interaction.guild.id, user.id))
            await db.commit()
    label = next(c.name for c in _STAT_CHOICES if c.value == stat)
    await interaction.response.send_message(
        f"✅ Added **{amount:,}** to {user.mention}'s **{label}**.")

@bot.tree.command(name="removeleaderboardstat", description="Remove from a user's leaderboard stat")
@app_commands.describe(user="Target user", stat="Which stat to modify", amount="Amount to remove")
@app_commands.choices(stat=_STAT_CHOICES)
@command_enabled()
async def removeleaderboardstat(interaction: discord.Interaction, user: discord.Member,
                                 stat: str, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return
    await ensure_stats(interaction.guild.id, user.id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                f"UPDATE user_stats SET {stat}=MAX(0,{stat}-?) WHERE guild_id=? AND user_id=?",
                (amount, interaction.guild.id, user.id))
            await db.commit()
    label = next(c.name for c in _STAT_CHOICES if c.value == stat)
    await interaction.response.send_message(
        f"❌ Removed **{amount:,}** from {user.mention}'s **{label}**.")

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

@bot.tree.command(name="listlogchannels", description="Show all configured log channels")
@command_enabled()
async def listlogchannels(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute(
            "SELECT log_type,channel_id FROM log_channels WHERE guild_id=? ORDER BY log_type",
            (interaction.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No log channels configured."); return
    embed = discord.Embed(title="📋 Log Channels", color=discord.Color.blurple())
    for log_type, channel_id in rows:
        label = next((c.name for c in _LOG_CHOICES if c.value == log_type), log_type)
        ch    = bot.get_channel(channel_id)
        embed.add_field(name=label,
                        value=ch.mention if ch else f"<#{channel_id}> *(channel deleted)*",
                        inline=False)
    await interaction.response.send_message(embed=embed)

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
# LOGGING PATCHES  ── use ._callback (private attr) because the .callback
# property has no setter in this version of discord.py
# ─────────────────────────────────────────────────────────────────────────────

# ── Balance ───────────────────────────────────────────────────────────────────

_orig_addbalance = addbalance._callback
async def _addbalance_logged(interaction: discord.Interaction, user: discord.Member, amount: int):
    await _orig_addbalance(interaction, user, amount)
    await log_event(interaction.guild.id, "balance", _log_embed(
        "💰 Balance Added", discord.Color.green(),
        Admin=interaction.user.mention, User=user.mention, Amount=f"+{amount:,}"))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ addbalance", discord.Color.orange(),
        By=interaction.user.mention, User=user.mention, Amount=f"+{amount:,}"))
addbalance._callback = _addbalance_logged

_orig_removebalance = removebalance._callback
async def _removebalance_logged(interaction: discord.Interaction, user: discord.Member, amount: int):
    await _orig_removebalance(interaction, user, amount)
    await log_event(interaction.guild.id, "balance", _log_embed(
        "💸 Balance Removed", discord.Color.red(),
        Admin=interaction.user.mention, User=user.mention, Amount=f"-{amount:,}"))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ removebalance", discord.Color.orange(),
        By=interaction.user.mention, User=user.mention, Amount=f"-{amount:,}"))
removebalance._callback = _removebalance_logged

_orig_gift = gift._callback
async def _gift_logged(interaction: discord.Interaction, user: discord.Member, amount: int):
    await _orig_gift(interaction, user, amount)
    await log_event(interaction.guild.id, "balance", _log_embed(
        "🎁 Gift Sent", discord.Color.green(),
        From=interaction.user.mention, To=user.mention, Amount=f"{amount:,}"))
gift._callback = _gift_logged

# ── EXP ───────────────────────────────────────────────────────────────────────

_orig_addexp = addexp._callback
async def _addexp_logged(interaction: discord.Interaction, user: discord.Member, amount: int):
    await _orig_addexp(interaction, user, amount)
    await log_event(interaction.guild.id, "exp", _log_embed(
        "⭐ Usable EXP Added", discord.Color.green(),
        Admin=interaction.user.mention, User=user.mention, Amount=f"+{amount:,}"))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ addexp (usable)", discord.Color.orange(),
        By=interaction.user.mention, User=user.mention, Amount=f"+{amount:,}"))
addexp._callback = _addexp_logged

_orig_removeexp = removeexp._callback
async def _removeexp_logged(interaction: discord.Interaction, user: discord.Member, amount: int):
    await _orig_removeexp(interaction, user, amount)
    await log_event(interaction.guild.id, "exp", _log_embed(
        "📉 EXP Removed", discord.Color.red(),
        Admin=interaction.user.mention, User=user.mention, Amount=f"-{amount:,}"))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ removeexp", discord.Color.orange(),
        By=interaction.user.mention, User=user.mention, Amount=f"-{amount:,}"))
removeexp._callback = _removeexp_logged

# NOTE: addtotalexp and removetotalexp already call log_event inline — no patch needed.

# ── Items / keys / tokens ─────────────────────────────────────────────────────

_orig_item_give = item_give._callback
async def _item_give_logged(interaction: discord.Interaction,
                             user: discord.Member, name: str, quantity: int = 1):
    await _orig_item_give(interaction, user, name, quantity)
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
    await log_event(interaction.guild.id, "item", _log_embed(
        "🎒 Item Taken", discord.Color.red(),
        Admin=interaction.user.mention, User=user.mention, Item=name, Qty=str(quantity)))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ item take", discord.Color.orange(),
        By=interaction.user.mention, From=user.mention, Item=f"{quantity}x {name}"))
item_take._callback = _item_take_logged

_orig_item_buy = item_buy._callback
async def _item_buy_logged(interaction: discord.Interaction, name: str):
    await _orig_item_buy(interaction, name)
    await log_event(interaction.guild.id, "item", _log_embed(
        "🛒 Item Purchased", discord.Color.blue(),
        User=interaction.user.mention, Item=name))
item_buy._callback = _item_buy_logged

_orig_item_use = item_use._callback
async def _item_use_logged(interaction: discord.Interaction, name: str):
    await _orig_item_use(interaction, name)
    await log_event(interaction.guild.id, "item", _log_embed(
        "✅ Item Used (Role Claimed)", discord.Color.blue(),
        User=interaction.user.mention, Item=name))
item_use._callback = _item_use_logged

_orig_givekey = givekey._callback
async def _givekey_logged(interaction: discord.Interaction,
                           user: discord.Member, amount: int = 1):
    await _orig_givekey(interaction, user, amount)
    await log_event(interaction.guild.id, "item", _log_embed(
        "🔑 VIP Key Given", discord.Color.green(),
        Admin=interaction.user.mention, User=user.mention, Keys=str(amount)))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ givekey", discord.Color.orange(),
        By=interaction.user.mention, To=user.mention, Amount=str(amount)))
givekey._callback = _givekey_logged

_orig_takekey = takekey._callback
async def _takekey_logged(interaction: discord.Interaction,
                           user: discord.Member, amount: int = 1):
    await _orig_takekey(interaction, user, amount)
    await log_event(interaction.guild.id, "item", _log_embed(
        "🔑 VIP Key Taken", discord.Color.red(),
        Admin=interaction.user.mention, User=user.mention, Keys=str(amount)))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ takekey", discord.Color.orange(),
        By=interaction.user.mention, From=user.mention, Amount=str(amount)))
takekey._callback = _takekey_logged

_orig_givegambletoken = givegambletoken._callback
async def _givegambletoken_logged(interaction: discord.Interaction,
                                   user: discord.Member, amount: int = 1):
    await _orig_givegambletoken(interaction, user, amount)
    await log_event(interaction.guild.id, "item", _log_embed(
        "🎲 Gamble Token Given", discord.Color.green(),
        Admin=interaction.user.mention, User=user.mention, Tokens=str(amount)))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ givegambletoken", discord.Color.orange(),
        By=interaction.user.mention, To=user.mention, Amount=str(amount)))
givegambletoken._callback = _givegambletoken_logged

_orig_takegambletoken = takegambletoken._callback
async def _takegambletoken_logged(interaction: discord.Interaction,
                                   user: discord.Member, amount: int = 1):
    await _orig_takegambletoken(interaction, user, amount)
    await log_event(interaction.guild.id, "item", _log_embed(
        "🎲 Gamble Token Taken", discord.Color.red(),
        Admin=interaction.user.mention, User=user.mention, Tokens=str(amount)))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ takegambletoken", discord.Color.orange(),
        By=interaction.user.mention, From=user.mention, Amount=str(amount)))
takegambletoken._callback = _takegambletoken_logged

# ── Raffle ────────────────────────────────────────────────────────────────────

_orig_buytickets = buytickets._callback
async def _buytickets_logged(interaction: discord.Interaction, amount: int):
    await _orig_buytickets(interaction, amount)
    await log_event(interaction.guild.id, "raffle", _log_embed(
        "🎟 Tickets Purchased", discord.Color.gold(),
        User=interaction.user.mention, Tickets=str(amount),
        Cost=f"{amount * RAFFLE_TICKET_PRICE:,} coins"))
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
    ch = channel or interaction.channel
    embed = _log_embed("🎉 Giveaway Created", discord.Color.gold(),
        By=interaction.user.mention, Prize=prize,
        Duration=f"{seconds}s", Winners=str(winners),
        Channel=ch.mention if ch else "?")
    await log_event(interaction.guild.id, "giveaway", embed)
    await log_event(interaction.guild.id, "admin", embed)
giveaway._callback = _giveaway_logged

# ── Codes ─────────────────────────────────────────────────────────────────────

_orig_createcode = createcode._callback
async def _createcode_logged(
    interaction: discord.Interaction, code: str, prize_json: str,
    uses: int = -1, min_activity_rank: int = 0, min_balance: int = 0,
    required_role: discord.Role = None
):
    await _orig_createcode(interaction, code, prize_json, uses,
                            min_activity_rank, min_balance, required_role)
    await log_event(interaction.guild.id, "code", _log_embed(
        "🎫 Code Created", discord.Color.green(),
        By=interaction.user.mention, Code=code,
        Uses="∞" if uses == -1 else str(uses), MinRank=str(min_activity_rank)))
    await log_event(interaction.guild.id, "admin", _log_embed(
        "⚙️ createcode", discord.Color.orange(),
        By=interaction.user.mention, Code=code,
        Uses="∞" if uses == -1 else str(uses)))
createcode._callback = _createcode_logged

_orig_redeem = redeem._callback
async def _redeem_logged(interaction: discord.Interaction, code: str):
    await _orig_redeem(interaction, code)
    await log_event(interaction.guild.id, "code", _log_embed(
        "🎫 Code Redeemed", discord.Color.green(),
        User=interaction.user.mention, Code=code.upper().strip()))
redeem._callback = _redeem_logged

# ── Chests / boxes ────────────────────────────────────────────────────────────

_orig_chest = chest._callback
async def _chest_logged(interaction: discord.Interaction, amount: int = 1):
    await _orig_chest(interaction, amount)
    await log_event(interaction.guild.id, "chest", _log_embed(
        "📦 Chest Opened", discord.Color.purple(),
        User=interaction.user.mention, Amount=str(amount),
        Cost=f"{CHEST_COST * amount:,} EXP"))
chest._callback = _chest_logged

_orig_vipchest = vipchest._callback
async def _vipchest_logged(interaction: discord.Interaction, amount: int = 1):
    await _orig_vipchest(interaction, amount)
    await log_event(interaction.guild.id, "chest", _log_embed(
        "💎 VIP Chest Opened", discord.Color.from_rgb(148, 0, 211),
        User=interaction.user.mention, Keys_Used=str(amount)))
vipchest._callback = _vipchest_logged

_orig_openbox = openbox._callback
async def _openbox_logged(interaction: discord.Interaction, box: str, amount: int = 1):
    await _orig_openbox(interaction, box, amount)
    await log_event(interaction.guild.id, "box", _log_embed(
        "🎁 Box Opened", discord.Color.orange(),
        User=interaction.user.mention, Box=box, Amount=str(amount)))
openbox._callback = _openbox_logged

# ── Gambling ──────────────────────────────────────────────────────────────────

_orig_roulette = roulette._callback
async def _roulette_logged(interaction: discord.Interaction, bet: int, choice: str):
    await _orig_roulette(interaction, bet, choice)
    await log_event(interaction.guild.id, "gamble", _log_embed(
        "🎰 Roulette Played", discord.Color.gold(),
        User=interaction.user.mention, Bet=f"{bet:,}", Choice=choice))
roulette._callback = _roulette_logged

# _BJView._resolve is a plain class method — standard Python patching works fine.
_orig_bj_resolve = _BJView._resolve
async def _bj_resolve_logged(self, inter: discord.Interaction):
    await _orig_bj_resolve(self, inter)
    if inter.guild:
        await log_event(inter.guild.id, "gamble", _log_embed(
            "🃏 Blackjack Played", discord.Color.dark_green(),
            User=inter.user.mention, Total_Bet=f"{sum(self.state.bets):,}"))
_BJView._resolve = _bj_resolve_logged

# ── Trade ─────────────────────────────────────────────────────────────────────
# execute_trade is a plain async function — module-level rebinding works fine.
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

# end_giveaway is also a plain async function — same approach.
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

# ═══════════════════════════════════════════════════════
# RUN BOT
# ═══════════════════════════════════════════════════════

bot.run(TOKEN)

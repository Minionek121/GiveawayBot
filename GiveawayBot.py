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
GUILD_ID            = 1494356360241090661
TARGET_GUILD        = discord.Object(id=GUILD_ID)

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

disabled_commands: set[str] = set()

def command_enabled():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.command and interaction.command.name in disabled_commands:
            await interaction.response.send_message("❌ This command is currently disabled.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

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
            await db.execute("UPDATE spent_exp SET amount=0")
            await db.commit()

# ═══════════════════════════════════════════════════════
# SYSTEM FLAGS
# ═══════════════════════════════════════════════════════

async def is_system_enabled(guild_id: int, flag: str) -> bool:
    async with get_db() as db:
        async with db.execute("SELECT enabled FROM system_flags WHERE guild_id=? AND flag_name=?",
                              (guild_id, flag)) as cur:
            row = await cur.fetchone()
    return row[0] == 1 if row else True

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
# BALANCE
# ═══════════════════════════════════════════════════════

async def get_balance(user_id):
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                await db.execute("INSERT INTO balances VALUES(?,?)", (user_id, 0))
                await db.commit()
                return 0
            return row[0]

async def add_balance(user_id, amount):
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR IGNORE INTO balances VALUES(?,?)", (user_id, 0))
            await db.execute("UPDATE balances SET balance=balance+? WHERE user_id=?", (amount, user_id))
            await db.execute("UPDATE balances SET balance=0 WHERE user_id=? AND balance<0", (user_id,))
            await db.commit()

async def ensure_stats(user_id):
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR IGNORE INTO user_stats(user_id) VALUES(?)", (user_id,))
            await db.commit()

async def add_stat(user_id, column, amount):
    await ensure_stats(user_id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(f"UPDATE user_stats SET {column}={column}+? WHERE user_id=?", (amount, user_id))
            await db.commit()

# ═══════════════════════════════════════════════════════
# EXP SYSTEM  ── FIXED: spending recorded as negative entries so it expires in 7 days
# ═══════════════════════════════════════════════════════

last_message_exp: dict[int, float] = {}

async def add_exp(user_id, amount, is_bonus=False):
    if amount > 0 and not is_bonus:
        await add_stat(user_id, "total_exp", amount)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO exp_history(user_id, amount, timestamp, is_bonus) VALUES(?,?,?,?)",
                (user_id, amount, int(datetime.now(UTC).timestamp()), 1 if is_bonus else 0))
            await db.commit()

async def get_exp(user_id):
    """Usable EXP = net sum of ALL history entries in last 7 days (gains + negative spends)."""
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    async with get_db() as db:
        async with db.execute("SELECT SUM(amount) FROM exp_history WHERE user_id=? AND timestamp>=?",
                              (user_id, week_ago)) as cur:
            row = await cur.fetchone()
    return max(row[0] or 0, 0)

async def get_level_exp(user_id):
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    async with get_db() as db:
        async with db.execute(
            "SELECT SUM(amount) FROM exp_history "
            "WHERE user_id=? AND timestamp>=? AND amount>0 AND is_bonus=0",
            (user_id, week_ago)) as cur:
            row = await cur.fetchone()
    return max(row[0] or 0, 0)

async def get_level(user_id):
    return min((await get_level_exp(user_id)) // LEVEL_DIVISOR + 1, 100)

# ═══════════════════════════════════════════════════════
# INVENTORY HELPERS
# ═══════════════════════════════════════════════════════

async def inventory_add(user_id: int, item_name: str, quantity: int = 1):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO inventory(user_id,item_name,quantity) VALUES(?,?,?) "
                "ON CONFLICT(user_id,item_name) DO UPDATE SET quantity=quantity+excluded.quantity",
                (user_id, item_name, quantity))
            await db.commit()

async def inventory_remove(user_id: int, item_name: str, quantity: int = 1) -> bool:
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT quantity FROM inventory WHERE user_id=? AND item_name=?",
                                  (user_id, item_name)) as cur:
                row = await cur.fetchone()
            if not row or row[0] < quantity:
                return False
            new_qty = row[0] - quantity
            if new_qty == 0:
                await db.execute("DELETE FROM inventory WHERE user_id=? AND item_name=?", (user_id, item_name))
            else:
                await db.execute("UPDATE inventory SET quantity=? WHERE user_id=? AND item_name=?",
                                 (new_qty, user_id, item_name))
            await db.commit()
    return True

async def inventory_get(user_id: int) -> list[tuple[str, int]]:
    async with get_db() as db:
        async with db.execute("SELECT item_name,quantity FROM inventory WHERE user_id=? ORDER BY item_name",
                              (user_id,)) as cur:
            return await cur.fetchall()

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
    now = datetime.now().timestamp()
    last_time = last_message_exp.get(message.author.id, 0)
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
        await add_exp(message.author.id, gained)
        last_message_exp[message.author.id] = now
    await bot.process_commands(message)

# ═══════════════════════════════════════════════════════
# READY EVENT
# ═══════════════════════════════════════════════════════

@bot.event
async def on_ready():
    await setup_database()
    try:
        bot.tree.copy_global_to(guild=TARGET_GUILD)
        synced = await bot.tree.sync(guild=TARGET_GUILD)
        print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync(guild=None)
        print("Cleared global commands")
    except Exception as e:
        print(f"[Sync Error] {e}")
    print(f"Logged in as {bot.user}")
    for task_fn in [raffle_loop, giveaway_watcher, raffle_info_loop,
                    game_loop, daily_key_loop, daily_gamble_loop]:
        bot.loop.create_task(task_fn())

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
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT balance FROM balances WHERE user_id=?",
                                  (interaction.user.id,)) as cur:
                data = await cur.fetchone()
            if data is None or data[0] < amount:
                await interaction.response.send_message("❌ Not enough balance.", ephemeral=True); return
            await db.execute("UPDATE balances SET balance=balance-? WHERE user_id=?",
                             (amount, interaction.user.id))
            await db.execute("INSERT OR IGNORE INTO balances VALUES(?,0)", (user.id,))
            await db.execute("UPDATE balances SET balance=balance+? WHERE user_id=?", (amount, user.id))
            await db.execute("INSERT OR IGNORE INTO user_stats(user_id) VALUES(?)", (interaction.user.id,))
            await db.execute("UPDATE user_stats SET gifted_balance=gifted_balance+? WHERE user_id=?",
                             (amount, interaction.user.id))
            await db.commit()
    await interaction.response.send_message(f"💸 You gifted {amount:,} coins to {user.mention}!")

@bot.tree.command(name="balance", description="Check a balance")
@command_enabled()
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    bal = await get_balance(user.id)
    embed = discord.Embed(title=f"💰 {user.display_name}'s Balance",
                          description=f"{bal:,} coins", color=discord.Color.green())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="addbalance", description="Add balance to a user")
@command_enabled()
async def addbalance(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_balance(user.id, amount)
    await interaction.response.send_message(f"✅ Added {amount:,} coins to {user.mention}")

@bot.tree.command(name="removebalance", description="Remove balance from a user")
@command_enabled()
async def removebalance(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_balance(user.id, -amount)
    await interaction.response.send_message(f"❌ Removed {amount:,} coins from {user.mention}")

# ═══════════════════════════════════════════════════════
# EXP COMMANDS
# ═══════════════════════════════════════════════════════

@bot.tree.command(name="activityrank", description="Check a user's Activity Rank and EXP")
@command_enabled()
async def level(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    exp    = await get_level_exp(user.id)
    usable = await get_exp(user.id)
    lvl    = await get_level(user.id)
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
    await add_exp(user.id, amount, is_bonus=True)
    await interaction.response.send_message(
        f"✅ Added **{amount:,}** usable EXP to {user.mention} (Total EXP / Activity Rank unchanged).")

@bot.tree.command(name="removeexp", description="Remove EXP from a user")
@command_enabled()
async def removeexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_exp(user.id, -amount)
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
            await add_balance(winner.id, prize_balance)
        if prize_exp > 0:
            await add_exp(winner.id, prize_exp)
        if prize_tickets > 0:
            await add_tickets(guild.id, winner.id, prize_tickets)
        if prize_gamble > 0:
            await inventory_add(winner.id, GAMBLE_TOKEN, prize_gamble)
        if prize_vip_keys > 0:
            await inventory_add(winner.id, VIP_CHEST_KEY, prize_vip_keys)
        if prize_role_id:
            role   = guild.get_role(prize_role_id)
            member = guild.get_member(winner.id)
            if role and member:
                try: await member.add_roles(role)
                except Exception: pass
        if prize_item:
            await inventory_add(winner.id, prize_item, prize_item_qty)

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
        level = await get_level(user.id)
        weighted.extend([user] * min(100, max(1, level)))

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
                if old:
                    await db.execute("INSERT OR IGNORE INTO balances VALUES(?,0)", (old[0],))
                    await db.execute("UPDATE balances SET balance=MAX(0,balance-?) WHERE user_id=?",
                                     (old[1], old[0]))
                await db.commit()

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
        level = await get_level(user.id)
        weighted.extend([user] * min(100, max(1, level)))
    new_winner = random.choice(weighted)

    try:
        meta        = json.loads(prize_raw)
        prize_label = meta.get("label", prize_raw)
    except (json.JSONDecodeError, TypeError):
        meta        = {"label": prize_raw, "balance": legacy_reward}
        prize_label = prize_raw

    if old_data:
        async with db_lock:
            async with get_db() as db:
                await db.execute("INSERT OR IGNORE INTO balances VALUES(?,0)", (old_data[0],))
                await db.execute("UPDATE balances SET balance=MAX(0,balance-?) WHERE user_id=?",
                                 (old_data[1], old_data[0]))
                await db.commit()

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
    bal   = await get_balance(interaction.user.id)
    if bal < price:
        await interaction.response.send_message("❌ Not enough balance."); return
    await add_balance(interaction.user.id, -price)
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
    await add_stat(interaction.user.id, "raffle_tickets_bought", amount)

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
            await add_balance(winner_id, RAFFLE_PRIZE)
            async with get_db() as db:
                async with db.execute("SELECT channel_id FROM raffle_config WHERE guild_id=?",
                                      (guild.id,)) as cur:
                    row = await cur.fetchone()
            ann = bot.get_channel(row[0]) if row else guild.system_channel
            if ann:
                await ann.send(f"🎉 <@{winner_id}> won the daily raffle and will receive a huge pet!")
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
    exp = await get_exp(interaction.user.id)
    if exp >= 1400:
        amount = min(amount, exp // CHEST_COST)
    else:
        amount = 1
    total_cost = CHEST_COST * amount
    if exp < total_cost:
        await interaction.followup.send(f"❌ You need {total_cost:,} EXP (you have {exp:,})."); return

    prizes     = await get_chest_prizes(interaction.guild.id, "chest")
    rare_names = {p["name"] for p in prizes if p["name"] in RARE_CHEST_PRIZES} or RARE_CHEST_PRIZES
    results: dict = {}
    total_balance = 0
    total_exp_won = 0
    for _ in range(amount):
        prize = random.choices(prizes, weights=[p["chance"] for p in prizes], k=1)[0]
        results[prize["name"]] = results.get(prize["name"], 0) + 1
        total_balance += prize["balance"]
        total_exp_won += prize["exp"]

    await add_exp(interaction.user.id, -total_cost)   # spend (negative entry, expires in 7d)
    if total_balance > 0: await add_balance(interaction.user.id, total_balance)
    if total_exp_won > 0: await add_exp(interaction.user.id, total_exp_won)
    await add_stat(interaction.user.id, "chests_opened", amount)

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

    inv  = await inventory_get(interaction.user.id)
    owned = {n.lower(): q for n, q in inv}
    available_keys = owned.get(VIP_CHEST_KEY.lower(), 0)
    if available_keys < amount:
        await interaction.followup.send(
            f"❌ Need {amount}x **{VIP_CHEST_KEY}** but only have {available_keys}.\n"
            f"Keys are given by admins; Nitro Boosters get one daily!"); return
    if not await inventory_remove(interaction.user.id, VIP_CHEST_KEY, amount):
        await interaction.followup.send("❌ Failed to consume keys."); return

    prizes     = await get_chest_prizes(interaction.guild.id, "vipchest")
    rare_names = {p["name"] for p in prizes if p["name"] in RARE_VIP_PRIZES} or RARE_VIP_PRIZES
    results: dict = {}
    total_balance = 0
    total_exp_won = 0
    for _ in range(amount):
        prize = random.choices(prizes, weights=[p["chance"] for p in prizes], k=1)[0]
        results[prize["name"]] = results.get(prize["name"], 0) + 1
        total_balance += prize["balance"]
        total_exp_won += prize["exp"]

    if total_balance > 0: await add_balance(interaction.user.id, total_balance)
    if total_exp_won > 0: await add_exp(interaction.user.id, total_exp_won)

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
    await inventory_add(user.id, VIP_CHEST_KEY, amount)
    await interaction.response.send_message(f"🔑 Gave **{amount}x {VIP_CHEST_KEY}** to {user.mention}.")

@bot.tree.command(name="takekey", description="Take VIP Chest Key(s) from a user")
@app_commands.describe(user="Target user", amount="Number of keys (default 1)")
@command_enabled()
async def takekey(interaction: discord.Interaction, user: discord.Member, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    if not await inventory_remove(user.id, VIP_CHEST_KEY, amount):
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
                    await inventory_add(member.id, VIP_CHEST_KEY, 1)
                except Exception as e:
                    print(f"[DailyKey] {member} / {guild.name}: {e}")

# ═══════════════════════════════════════════════════════
# ITEM STORE
# ═══════════════════════════════════════════════════════

async def add_item(item_name, price, role_id, description):
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO item_store VALUES(?,?,?,?)",
                             (item_name, price, role_id, description))
            await db.commit()

async def remove_item(item_name):
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM item_store WHERE item_name=?", (item_name,))
            await db.commit()

async def get_item(item_name):
    async with get_db() as db:
        async with db.execute("SELECT * FROM item_store WHERE LOWER(item_name)=LOWER(?)",
                              (item_name,)) as cur:
            return await cur.fetchone()

async def get_all_items():
    async with get_db() as db:
        async with db.execute("SELECT * FROM item_store") as cur:
            return await cur.fetchall()

item_group = app_commands.Group(name="item", description="Item store commands")
bot.tree.add_command(item_group)

@item_group.command(name="add", description="Add item to store")
@command_enabled()
async def item_add(interaction: discord.Interaction, name: str, price: int,
                   role: discord.Role, description: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    await add_item(name, price, role.id, description)
    await interaction.response.send_message(f"✅ Added **{name}** to the store.")

@item_group.command(name="remove", description="Remove item from store")
@command_enabled()
async def item_remove(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if not await get_item(name):
        await interaction.response.send_message("❌ Item not found."); return
    await remove_item(name)
    await interaction.response.send_message(f"🗑 Removed **{name}** from the store.")

@item_group.command(name="info", description="View item or box info")
@command_enabled()
async def item_info(interaction: discord.Interaction, name: str):
    item = await get_item(name)
    if item:
        item_name, price, role_id, description = item
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
    items = await get_all_items()
    if not items:
        await interaction.response.send_message("❌ Store is empty."); return
    embed = discord.Embed(title="🛒 Item Store", color=discord.Color.green())
    for item_name, price, role_id, description in items:
        role = interaction.guild.get_role(role_id)
        embed.add_field(name=item_name,
                        value=f"💰 {price:,} coins\n🎭 {role.mention if role else '?'}",
                        inline=False)
    await interaction.response.send_message(embed=embed)

@item_group.command(name="buy", description="Buy an item — goes to your inventory")
@command_enabled()
async def item_buy(interaction: discord.Interaction, name: str):
    item = await get_item(name)
    if not item:
        await interaction.response.send_message("❌ Item not found."); return
    item_name, price, role_id, description = item
    bal = await get_balance(interaction.user.id)
    if bal < price:
        await interaction.response.send_message("❌ Not enough balance."); return
    if not interaction.guild.get_role(role_id):
        await interaction.response.send_message("❌ Role no longer exists."); return
    await add_balance(interaction.user.id, -price)
    await inventory_add(interaction.user.id, item_name, 1)
    await interaction.response.send_message(
        f"✅ Bought **{item_name}** for {price:,} coins. Use `/item use {item_name}` to redeem!")

@item_group.command(name="use", description="Use a store item to receive its role")
@command_enabled()
async def item_use(interaction: discord.Interaction, name: str):
    item = await get_item(name)
    if not item:
        await interaction.response.send_message("❌ Item not found."); return
    item_name, price, role_id, description = item
    inv   = await inventory_get(interaction.user.id)
    owned = {n.lower(): q for n, q in inv}
    if owned.get(item_name.lower(), 0) < 1:
        await interaction.response.send_message(f"❌ You don't have **{item_name}** in your inventory."); return
    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.response.send_message("❌ Role no longer exists."); return
    member = interaction.guild.get_member(interaction.user.id)
    if role in member.roles:
        await interaction.response.send_message(f"❌ You already have **{role.name}**."); return
    if not await inventory_remove(interaction.user.id, item_name, 1):
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
    store_item = await get_item(name)
    canonical  = store_item[0] if store_item else name.strip()
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
    await inventory_add(user.id, canonical, quantity)
    await interaction.response.send_message(f"✅ Gave **{quantity}x {canonical}** to {user.mention}.")

@item_group.command(name="take", description="Take an item, box, or key from a user (admin only)")
@app_commands.describe(user="Target user", name="Item or box name", quantity="How many (default 1)")
@command_enabled()
async def item_take(interaction: discord.Interaction, user: discord.Member, name: str, quantity: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if quantity <= 0:
        await interaction.response.send_message("❌ Quantity must be ≥ 1.", ephemeral=True); return
    store_item = await get_item(name)
    canonical  = store_item[0] if store_item else name.strip()
    if not store_item:
        async with get_db() as db:
            async with db.execute(
                "SELECT box_name FROM abuse_boxes WHERE guild_id=? AND LOWER(box_name)=LOWER(?)",
                (interaction.guild.id, name)) as cur:
                box_row = await cur.fetchone()
        if box_row: canonical = box_row[0]
    if not await inventory_remove(user.id, canonical, quantity):
        await interaction.response.send_message(f"❌ {user.mention} doesn't have {quantity}x **{canonical}**."); return
    await interaction.response.send_message(f"🗑 Took **{quantity}x {canonical}** from {user.mention}.")

@item_group.command(name="inv", description="Check a user's inventory")
@app_commands.describe(user="User to check (defaults to yourself)")
@command_enabled()
async def item_inv(interaction: discord.Interaction, user: discord.Member = None):
    user  = user or interaction.user
    inv   = await inventory_get(user.id)
    embed = discord.Embed(title=f"🎒 {user.display_name}'s Inventory", color=discord.Color.blurple())
    if not inv:
        embed.description = "Inventory is empty."
    else:
        lines = []
        for item_name, quantity in inv:
            si = await get_item(item_name)
            if si:
                role   = interaction.guild.get_role(si[2])
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

@bot.tree.command(name="disablecmd", description="Temporarily disable a command")
async def disablecmd(interaction: discord.Interaction, command_name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if command_name in ("disablecmd", "enablecmd"):
        await interaction.response.send_message("❌ Cannot disable these commands.", ephemeral=True); return
    disabled_commands.add(command_name)
    await interaction.response.send_message(f"🔒 `/{command_name}` disabled.")

@bot.tree.command(name="enablecmd", description="Re-enable a disabled command")
async def enablecmd(interaction: discord.Interaction, command_name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if command_name not in disabled_commands:
        await interaction.response.send_message(f"ℹ️ `/{command_name}` is not disabled.", ephemeral=True); return
    disabled_commands.discard(command_name)
    await interaction.response.send_message(f"🔓 `/{command_name}` re-enabled.")

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
            async with db.execute("SELECT DISTINCT user_id FROM exp_history") as cur:
                users = await cur.fetchall()
            for (uid,) in users:
                leaderboard_data.append((uid, await get_exp(uid)))
        elif value == "current_tickets":
            async with db.execute(
                "SELECT user_id,tickets FROM raffle WHERE guild_id=? ORDER BY tickets DESC LIMIT 10",
                (interaction.guild.id,)) as cur:
                leaderboard_data = await cur.fetchall()
        elif value == "balance":
            async with db.execute("SELECT user_id,balance FROM balances ORDER BY balance DESC LIMIT 10") as cur:
                leaderboard_data = await cur.fetchall()
        else:
            async with db.execute(
                f"SELECT user_id,{value} FROM user_stats ORDER BY {value} DESC LIMIT 10") as cur:
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
        if balance > 0 and await get_balance(uid) < balance:
            await interaction.response.send_message("❌ Not enough coins.", ephemeral=True); return
        if exp > 0 and await get_exp(uid) < exp:
            await interaction.response.send_message("❌ Not enough EXP.", ephemeral=True); return
        if tickets > 0 and await get_tickets(session.guild_id, uid) < tickets:
            await interaction.response.send_message("❌ Not enough tickets.", ephemeral=True); return
        if items:
            inv = {n.lower(): q for n, q in await inventory_get(uid)}
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
    for uid, offer in [(iid, session.offers[iid]), (tid, session.offers[tid])]:
        if offer.balance > 0 and await get_balance(uid) < offer.balance:
            return False, f"<@{uid}> no longer has enough coins."
        if offer.exp > 0 and await get_exp(uid) < offer.exp:
            return False, f"<@{uid}> no longer has enough EXP."
        if offer.tickets > 0 and await get_tickets(session.guild_id, uid) < offer.tickets:
            return False, f"<@{uid}> no longer has enough tickets."
        inv = {n.lower(): q for n, q in await inventory_get(uid)}
        for n, q in offer.items:
            if inv.get(n.lower(), 0) < q:
                return False, f"<@{uid}> no longer has {q}x {n}."
    io, to = session.offers[iid], session.offers[tid]
    if io.balance > 0: await add_balance(iid, -io.balance); await add_balance(tid, io.balance)
    if to.balance > 0: await add_balance(tid, -to.balance); await add_balance(iid, to.balance)
    if io.exp > 0: await add_exp(iid, -io.exp); await add_exp(tid, io.exp)
    if to.exp > 0: await add_exp(tid, -to.exp); await add_exp(iid, to.exp)
    if io.tickets > 0:
        await add_tickets(session.guild_id, iid, -io.tickets)
        await add_tickets(session.guild_id, tid, io.tickets)
    if to.tickets > 0:
        await add_tickets(session.guild_id, tid, -to.tickets)
        await add_tickets(session.guild_id, iid, to.tickets)
    for n, q in io.items: await inventory_remove(iid, n, q); await inventory_add(tid, n, q)
    for n, q in to.items: await inventory_remove(tid, n, q); await inventory_add(iid, n, q)
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
        item = await get_item(item_name)
        if not item:
            await interaction.response.send_message(f"❌ Item **{item_name}** not found.", ephemeral=True); return
        prize_value = item[0]
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
    for m in members: await inventory_add(m.id, box, amount)
    await interaction.followup.send(
        f"✅ Gave **{amount}x {box}** to **{len(members)}** member(s) with {role.mention}.")

@bot.tree.command(name="openbox", description="Open one or more abuse boxes from your inventory")
@app_commands.describe(box="Box name", amount="How many to open (default 1, max 20)")
@command_enabled()
async def openbox(interaction: discord.Interaction, box: str, amount: int = 1):
    await interaction.response.defer()
    if amount <= 0:  await interaction.followup.send("❌ Amount must be ≥ 1."); return
    if amount > 20:  await interaction.followup.send("❌ Max 20 boxes at once."); return
    inv   = await inventory_get(interaction.user.id)
    owned = {n.lower(): (n, q) for n, q in inv}
    if box.lower() not in owned or owned[box.lower()][1] < amount:
        have = owned.get(box.lower(), (box, 0))[1]
        await interaction.followup.send(f"❌ Need {amount}x **{box}** but only have {have}."); return
    canonical_box = owned[box.lower()][0]
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id=? AND box_name=?",
                              (interaction.guild.id, canonical_box)) as cur:
            if not await cur.fetchone():
                await interaction.followup.send(f"❌ Box **{canonical_box}** no longer exists."); return
        async with db.execute(
            "SELECT prize_type,prize_value,prize_amount,chance FROM abuse_box_prizes "
            "WHERE guild_id=? AND box_name=?",
            (interaction.guild.id, canonical_box)) as cur:
            prizes = await cur.fetchall()
    if not prizes:
        await interaction.followup.send(f"❌ **{canonical_box}** has no prizes."); return
    if not await inventory_remove(interaction.user.id, canonical_box, amount):
        await interaction.followup.send("❌ Failed to remove boxes."); return

    results: dict = {}
    total_balance = 0
    total_exp     = 0
    item_grants: dict = {}
    for _ in range(amount):
        p_type, p_value, p_amount, _ = random.choices(prizes, weights=[p[3] for p in prizes], k=1)[0]
        if p_type == "balance":
            amt = int(p_value); total_balance += amt; key_r = f"💰 {amt:,} coins"
        elif p_type == "exp":
            amt = int(p_value); total_exp += amt;    key_r = f"⭐ {amt:,} EXP"
        elif p_type == "item":
            item_grants[p_value] = item_grants.get(p_value, 0) + 1; key_r = f"🎒 {p_value}"
        elif p_type == "nothing": key_r = f"😔 {p_value}"
        else:                     key_r = f"✨ {p_value}"
        results[key_r] = results.get(key_r, 0) + 1

    if total_balance > 0: await add_balance(interaction.user.id, total_balance)
    if total_exp > 0:     await add_exp(interaction.user.id, total_exp)
    for iname, qty in item_grants.items():
        si = await get_item(iname)
        await inventory_add(interaction.user.id, si[0] if si else iname, qty)

    result_text = "\n".join(f"• {count}x {desc}" for desc, count in results.items())
    embed = discord.Embed(title=f"📦 {canonical_box} × {amount}", description=result_text,
                          color=discord.Color.orange())
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

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
    async with get_db() as db:
        async with db.execute(
            "SELECT prize_json,uses_left,min_level,min_balance,required_role_id "
            "FROM redeem_codes WHERE guild_id=? AND code=?", (guild_id, code)) as cur:
            row = await cur.fetchone()
    if not row:
        await interaction.response.send_message("❌ Code not found or already expired.",
                                                ephemeral=True); return
    prize_json, uses_left, min_level, min_balance, req_role_id = row
    if uses_left == 0:
        await interaction.response.send_message("❌ This code has no uses left.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT 1 FROM code_uses WHERE guild_id=? AND code=? AND user_id=?",
                              (guild_id, code, user_id)) as cur:
            if await cur.fetchone():
                await interaction.response.send_message("❌ You've already used this code.",
                                                        ephemeral=True); return
    if await get_level(user_id) < min_level:
        await interaction.response.send_message(
            f"❌ You need Activity Rank {min_level} (you're Activity Rank {await get_level(user_id)}).",
            ephemeral=True); return
    if await get_balance(user_id) < min_balance:
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
        await interaction.response.send_message("❌ Code has invalid prize data.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO code_uses(guild_id,code,user_id) VALUES(?,?,?)",
                             (guild_id, code, user_id))
            if uses_left > 0:
                new_uses = uses_left - 1
                await db.execute("UPDATE redeem_codes SET uses_left=? WHERE guild_id=? AND code=?",
                                 (new_uses, guild_id, code))
            await db.commit()
    parts = []
    if prize.get("balance", 0) > 0:
        await add_balance(user_id, prize["balance"]); parts.append(f"💰 {prize['balance']:,} coins")
    if prize.get("exp", 0) > 0:
        await add_exp(user_id, prize["exp"]);          parts.append(f"⭐ {prize['exp']:,} EXP")
    if prize.get("tickets", 0) > 0:
        await add_tickets(guild_id, user_id, prize["tickets"])
        parts.append(f"🎟 {prize['tickets']} ticket(s)")
    if prize.get("gamble_tokens", 0) > 0:
        await inventory_add(user_id, GAMBLE_TOKEN, prize["gamble_tokens"])
        parts.append(f"🎲 {prize['gamble_tokens']} gamble token(s)")
    if prize.get("vip_keys", 0) > 0:
        await inventory_add(user_id, VIP_CHEST_KEY, prize["vip_keys"])
        parts.append(f"🔑 {prize['vip_keys']} VIP key(s)")
    if prize.get("item"):
        qty = prize.get("item_qty", 1)
        await inventory_add(user_id, prize["item"], qty)
        parts.append(f"🎒 {qty}x {prize['item']}")
    embed = discord.Embed(title="🎫 Code Redeemed!",
        description=f"You redeemed **{code}** and received:\n" +
                    "\n".join(f"• {p}" for p in parts),
        color=discord.Color.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ═══════════════════════════════════════════════════════
# GAMBLING SYSTEM
# ═══════════════════════════════════════════════════════

async def get_gamble_tokens(user_id: int) -> int:
    inv   = await inventory_get(user_id)
    owned = {n.lower(): q for n, q in inv}
    return owned.get(GAMBLE_TOKEN.lower(), 0)

@bot.tree.command(name="givegambletoken", description="Give Gamble Token(s) to a user")
@app_commands.describe(user="Target user", amount="Number of tokens (default 1)")
@command_enabled()
async def givegambletoken(interaction: discord.Interaction, user: discord.Member, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    await inventory_add(user.id, GAMBLE_TOKEN, amount)
    await interaction.response.send_message(f"🎲 Gave **{amount}x {GAMBLE_TOKEN}** to {user.mention}.")

@bot.tree.command(name="takegambletoken", description="Take Gamble Token(s) from a user")
@app_commands.describe(user="Target user", amount="Number of tokens (default 1)")
@command_enabled()
async def takegambletoken(interaction: discord.Interaction, user: discord.Member, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    if not await inventory_remove(user.id, GAMBLE_TOKEN, amount):
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
                    await inventory_add(member.id, GAMBLE_TOKEN, tokens)
                except Exception as e:
                    print(f"[DailyGamble] {member} / {guild.name}: {e}")

@bot.tree.command(name="blackjack", description="Play blackjack — costs 1 Gamble Token")
@app_commands.describe(bet="Amount of coins to bet")
@command_enabled()
async def blackjack(interaction: discord.Interaction, bet: int):
    if not await is_system_enabled(interaction.guild.id, "gamble"):
        await interaction.response.send_message("❌ Gambling system is disabled.", ephemeral=True); return
    if bet <= 0:
        await interaction.response.send_message("❌ Bet must be > 0.", ephemeral=True); return
    bal    = await get_balance(interaction.user.id)
    if bal < bet:
        await interaction.response.send_message(f"❌ Not enough balance (you have {bal:,}).",
                                                ephemeral=True); return
    tokens = await get_gamble_tokens(interaction.user.id)
    if tokens < 1:
        await interaction.response.send_message(
            f"❌ You need 1 {GAMBLE_TOKEN} to play. You get one daily (Nitro Boosters get 2)!",
            ephemeral=True); return
    await inventory_remove(interaction.user.id, GAMBLE_TOKEN, 1)

    suits  = ["♠", "♥", "♦", "♣"]
    ranks  = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
    def new_deck():
        return [(r, s) for r in ranks for s in suits]
    def card_value(hand):
        total, aces = 0, 0
        for rank, _ in hand:
            if rank in ("J", "Q", "K"): total += 10
            elif rank == "A":            total += 11; aces += 1
            else:                        total += int(rank)
        while total > 21 and aces:
            total -= 10; aces -= 1
        return total
    def fmt(hand):
        return " ".join(f"{r}{s}" for r, s in hand)

    deck         = new_deck()
    random.shuffle(deck)
    player_hand  = [deck.pop(), deck.pop()]
    dealer_hand  = [deck.pop(), deck.pop()]
    player_total = card_value(player_hand)

    if player_total == 21:
        winnings = int(bet * 1.5)
        await add_balance(interaction.user.id, winnings)
        embed = discord.Embed(title="🃏 Blackjack — Natural 21!", color=discord.Color.gold(),
            description=(f"Your hand: **{fmt(player_hand)}** ({player_total})\n"
                         f"Dealer: **{fmt(dealer_hand)}** ({card_value(dealer_hand)})\n\n"
                         f"🏆 **Blackjack! You win {winnings:,} coins!**"))
        await interaction.response.send_message(embed=embed); return

    class BJView(discord.ui.View):
        def __init__(self_):
            super().__init__(timeout=60)

        @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="➕")
        async def hit(self_, inter: discord.Interaction, button: discord.ui.Button):
            if inter.user.id != interaction.user.id:
                await inter.response.send_message("Not your game.", ephemeral=True); return
            player_hand.append(deck.pop())
            total = card_value(player_hand)
            if total > 21:
                self_.stop()
                await add_balance(interaction.user.id, -bet)
                await inter.response.edit_message(
                    embed=discord.Embed(title="🃏 Blackjack — Bust!", color=discord.Color.red(),
                        description=(f"Your hand: **{fmt(player_hand)}** ({total})\n\n"
                                     f"💸 **Busted! Lost {bet:,} coins.**")), view=None)
            else:
                await inter.response.edit_message(
                    embed=discord.Embed(title="🃏 Blackjack", color=discord.Color.blue(),
                        description=(f"Your hand: **{fmt(player_hand)}** ({total})\n"
                                     f"Dealer: **{dealer_hand[0][0]}{dealer_hand[0][1]}** + ??\n\n"
                                     f"Bet: {bet:,} | Hit or Stand?")), view=self_)

        @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="✋")
        async def stand(self_, inter: discord.Interaction, button: discord.ui.Button):
            if inter.user.id != interaction.user.id:
                await inter.response.send_message("Not your game.", ephemeral=True); return
            self_.stop()
            while card_value(dealer_hand) < 17:
                dealer_hand.append(deck.pop())
            p = card_value(player_hand)
            d = card_value(dealer_hand)
            if d > 21 or p > d:
                await add_balance(interaction.user.id, bet)
                result = f"🏆 **You win {bet:,} coins!** (Your {p} vs Dealer {d})"
                color  = discord.Color.green()
            elif p == d:
                result = f"🤝 **Push!** (Both {p})"
                color  = discord.Color.greyple()
            else:
                await add_balance(interaction.user.id, -bet)
                result = f"💸 **Dealer wins. Lost {bet:,} coins.** (Your {p} vs Dealer {d})"
                color  = discord.Color.red()
            await inter.response.edit_message(
                embed=discord.Embed(title="🃏 Blackjack — Result", color=color,
                    description=(f"Your hand: **{fmt(player_hand)}** ({p})\n"
                                 f"Dealer: **{fmt(dealer_hand)}** ({d})\n\n{result}")),
                view=None)

        async def on_timeout(self_):
            await add_balance(interaction.user.id, -bet)

    embed = discord.Embed(title="🃏 Blackjack", color=discord.Color.blue(),
        description=(f"Your hand: **{fmt(player_hand)}** ({player_total})\n"
                     f"Dealer: **{dealer_hand[0][0]}{dealer_hand[0][1]}** + ??\n\n"
                     f"**Bet: {bet:,} coins** | Hit or Stand?"))
    embed.set_footer(text=f"1 {GAMBLE_TOKEN} consumed | {tokens - 1} remaining")
    await interaction.response.send_message(embed=embed, view=BJView())

@bot.tree.command(name="roulette", description="Play roulette — costs 1 Gamble Token")
@app_commands.describe(bet="Amount of coins to bet",
                       choice="red/black (2x), even/odd (2x), or a number 0–36 (35x)")
@command_enabled()
async def roulette(interaction: discord.Interaction, bet: int, choice: str):
    if not await is_system_enabled(interaction.guild.id, "gamble"):
        await interaction.response.send_message("❌ Gambling system is disabled.", ephemeral=True); return
    if bet <= 0:
        await interaction.response.send_message("❌ Bet must be > 0.", ephemeral=True); return
    bal = await get_balance(interaction.user.id)
    if bal < bet:
        await interaction.response.send_message(f"❌ Not enough balance ({bal:,}).", ephemeral=True); return
    tokens = await get_gamble_tokens(interaction.user.id)
    if tokens < 1:
        await interaction.response.send_message(f"❌ You need 1 {GAMBLE_TOKEN} to play.",
                                                ephemeral=True); return
    choice = choice.lower().strip()
    RED   = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    BLACK = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}
    is_number, chosen_number = False, -1
    if choice not in ("red", "black", "even", "odd"):
        try:
            chosen_number = int(choice)
            assert 0 <= chosen_number <= 36
            is_number = True
        except (ValueError, AssertionError):
            await interaction.response.send_message(
                "❌ Choice must be red, black, even, odd, or a number 0–36.", ephemeral=True); return
    await inventory_remove(interaction.user.id, GAMBLE_TOKEN, 1)
    result       = random.randint(0, 36)
    result_color = "🟢 Green" if result == 0 else ("🔴 Red" if result in RED else "⚫ Black")
    result_parity = "0" if result == 0 else ("Even" if result % 2 == 0 else "Odd")
    won, multiplier = False, 0
    if choice == "red"   and result in RED:             won = True; multiplier = 2
    elif choice == "black" and result in BLACK:         won = True; multiplier = 2
    elif choice == "even"  and result > 0 and result % 2 == 0: won = True; multiplier = 2
    elif choice == "odd"   and result % 2 == 1:         won = True; multiplier = 2
    elif is_number and chosen_number == result:         won = True; multiplier = 35
    if won:
        winnings = bet * (multiplier - 1)
        await add_balance(interaction.user.id, winnings)
        outcome = f"🏆 **You win {winnings:,} coins!** ({multiplier}x)"
        color   = discord.Color.green()
    else:
        await add_balance(interaction.user.id, -bet)
        outcome = f"💸 **You lose {bet:,} coins.**"
        color   = discord.Color.red()
    embed = discord.Embed(title="🎰 Roulette", color=color,
        description=(f"Ball landed on **{result}** ({result_color}, {result_parity})\n"
                     f"Your bet: **{choice}** for **{bet:,} coins**\n\n{outcome}"))
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
        await interaction.response.send_message("❌ No games configured."); return
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
        ans_lines = "\n".join(f"  `#{aid}` {ans}" for aid, ans in answers) or "  *No answers yet*"
        embed.add_field(name=f"🎯 {gname} [{status}]",
                        value=f"Reward: {' + '.join(parts) or 'None'}\nAnswers:\n{ans_lines}",
                        inline=False)
    await interaction.response.send_message(embed=embed)

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

        active_game_sessions[guild_id] = {
            "game_name": gname, "answer": correct,
            "channel_id": channel_id, "answered": False, "winner": None
        }

        await asyncio.sleep(answer_time)
        session = active_game_sessions.pop(guild_id, None)
        if not session: await asyncio.sleep(max(0, interval_seconds - answer_time)); continue

        if session.get("answered") and session.get("winner"):
            winner = session["winner"]
            if rb > 0: await add_balance(winner.id, rb)
            if re > 0: await add_exp(winner.id, re)
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
            await db.execute(
                "INSERT INTO exp_history(user_id, amount, timestamp, is_bonus) VALUES(?,?,?,?)",
                (user.id, amount, now, 0))
            # Matching negative entry → cancels out the usable EXP gain
            await db.execute(
                "INSERT INTO exp_history(user_id, amount, timestamp, is_bonus) VALUES(?,?,?,?)",
                (user.id, -amount, now, 0))
            await db.commit()
    await interaction.response.send_message(
        f"✅ Added **{amount:,}** to {user.mention}'s **Total EXP (7d)** and Activity Rank. Usable EXP unchanged.")


@bot.tree.command(name="removetotalexp", description="Remove from Total EXP (7d) and Activity Rank only — usable EXP stays the same")
@app_commands.describe(user="Target user", amount="Amount to remove")
@command_enabled()
async def removetotalexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be > 0.", ephemeral=True); return

    week_ago  = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    remaining = amount

    async with db_lock:
        async with get_db() as db:
            # Only target positive non-bonus entries — the ones get_level_exp counts
            async with db.execute(
                "SELECT rowid, amount FROM exp_history "
                "WHERE user_id=? AND timestamp>=? AND amount>0 AND is_bonus=0 "
                "ORDER BY timestamp ASC",
                (user.id, week_ago)
            ) as cur:
                entries = await cur.fetchall()

            for rowid, entry_amount in entries:
                if remaining <= 0:
                    break
                if entry_amount <= remaining:
                    await db.execute("DELETE FROM exp_history WHERE rowid=?", (rowid,))
                    remaining -= entry_amount
                else:
                    await db.execute(
                        "UPDATE exp_history SET amount=? WHERE rowid=?",
                        (entry_amount - remaining, rowid))
                    remaining = 0

            actually_removed = amount - remaining
            if actually_removed > 0:
                # Bonus entry restores the usable EXP that was lost by deleting entries above
                await db.execute(
                    "INSERT INTO exp_history(user_id, amount, timestamp, is_bonus) VALUES(?,?,?,?)",
                    (user.id, actually_removed, int(datetime.now(UTC).timestamp()), 1))

            await db.commit()

    if actually_removed == 0:
        await interaction.response.send_message(
            f"❌ {user.mention} has no Total EXP (7d) to remove.")
    elif remaining > 0:
        await interaction.response.send_message(
            f"⚠️ Only removed **{actually_removed:,}** from {user.mention}'s **Total EXP (7d)** "
            f"— they didn't have the full {amount:,}. Usable EXP unchanged.")
    else:
        await interaction.response.send_message(
            f"✅ Removed **{amount:,}** from {user.mention}'s **Total EXP (7d)** and Activity Rank. Usable EXP unchanged.")

    async with db_lock:
        async with get_db() as db:
            # Fetch positive entries oldest-first so we eat old EXP first
            async with db.execute(
                "SELECT rowid, amount FROM exp_history "
                "WHERE user_id=? AND timestamp>=? AND amount>0 "
                "ORDER BY timestamp ASC",
                (user.id, week_ago)
            ) as cur:
                entries = await cur.fetchall()

            for rowid, entry_amount in entries:
                if remaining <= 0:
                    break
                if entry_amount <= remaining:
                    await db.execute("DELETE FROM exp_history WHERE rowid=?", (rowid,))
                    remaining -= entry_amount
                else:
                    await db.execute(
                        "UPDATE exp_history SET amount=? WHERE rowid=?",
                        (entry_amount - remaining, rowid))
                    remaining = 0

            await db.commit()

    actually_removed = amount - remaining
    if actually_removed == 0:
        await interaction.response.send_message(
            f"❌ {user.mention} has no Total EXP (7d) to remove.")
    elif remaining > 0:
        await interaction.response.send_message(
            f"⚠️ Only removed **{actually_removed:,}** EXP from {user.mention}'s Total EXP (7d) "
            f"— they didn't have the full {amount:,}.")
    else:
        await interaction.response.send_message(
            f"❌ Removed **{amount:,}** from {user.mention}'s Total EXP (7d).")
        
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
    await ensure_stats(user.id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                f"UPDATE user_stats SET {stat} = {stat} + ? WHERE user_id = ?",
                (amount, user.id))
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
    await ensure_stats(user.id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                f"UPDATE user_stats SET {stat} = MAX(0, {stat} - ?) WHERE user_id = ?",
                (amount, user.id))
            await db.commit()
    label = next(c.name for c in _STAT_CHOICES if c.value == stat)
    await interaction.response.send_message(
        f"❌ Removed **{amount:,}** from {user.mention}'s **{label}**.")

# ═══════════════════════════════════════════════════════
# RUN BOT
# ═══════════════════════════════════════════════════════

bot.run(TOKEN)

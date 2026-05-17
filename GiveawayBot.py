import os
import json
import random
import asyncio
import aiosqlite
from datetime import datetime, timedelta, UTC

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

DATABASE = "/app/data/giveaways.db"

db_lock = asyncio.Lock()

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- DISABLED COMMANDS ---------------- #

disabled_commands: set[str] = set()

def command_enabled():
    """App command check: blocks execution if the command is disabled."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.command and interaction.command.name in disabled_commands:
            await interaction.response.send_message(
                "❌ This command is currently disabled.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)

# ---------------- PERMISSION CHECK ---------------- #

async def is_allowed_to_giveaway(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if any(role.name.lower() == "bot developer" for role in member.roles):
        return True
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT role_id FROM giveaway_roles WHERE guild_id = ?",
                (interaction.guild.id,)
            ) as cursor:
                rows = await cursor.fetchall()
    allowed_roles = {row[0] for row in rows}
    return any(role.id in allowed_roles for role in member.roles)

# ---------------- GIVEAWAY WATCHER ---------------- #

async def giveaway_watcher():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = int(datetime.now(UTC).timestamp())
        async with get_db() as db:
            async with db.execute(
                "SELECT message_id FROM giveaways WHERE ended = 0 AND end_time <= ?",
                (now,)
            ) as cursor:
                rows = await cursor.fetchall()
        for (message_id,) in rows:
            try:
                await end_giveaway(message_id)
            except Exception as e:
                print(f"[Watcher Error] {message_id}: {e}")
        await asyncio.sleep(15)

# ---------------- DATABASE ---------------- #

from contextlib import asynccontextmanager

@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DATABASE)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout = 30000")
    try:
        yield db
    finally:
        await db.close()

async def setup_database():
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS giveaway_roles (
                    guild_id INTEGER,
                    role_id INTEGER
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS giveaways (
                    message_id INTEGER,
                    channel_id INTEGER,
                    prize TEXT,
                    winners INTEGER,
                    reward INTEGER,
                    end_time INTEGER,
                    required_role INTEGER,
                    template TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS balances (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS exp_history (
                    user_id INTEGER,
                    amount INTEGER,
                    timestamp INTEGER
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS raffle (
                    guild_id INTEGER,
                    user_id INTEGER,
                    tickets INTEGER,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS giveaway_winners (
                    message_id INTEGER PRIMARY KEY,
                    winner_id INTEGER,
                    reward INTEGER
                )
                """
            )
            # Change 4: raffle channel per guild
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS raffle_config (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER
                )
                """
            )
            try:
                await db.execute(
                    "ALTER TABLE giveaways ADD COLUMN ended INTEGER DEFAULT 0"
                )
            except aiosqlite.OperationalError:
                pass
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS spent_exp (
                    user_id INTEGER PRIMARY KEY,
                    amount INTEGER
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS item_store (
                    item_name TEXT PRIMARY KEY,
                    price INTEGER,
                    role_id INTEGER,
                    description TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id INTEGER PRIMARY KEY,
                    total_exp INTEGER DEFAULT 0,
                    gifted_balance INTEGER DEFAULT 0,
                    chests_opened INTEGER DEFAULT 0,
                    raffle_tickets_bought INTEGER DEFAULT 0
                )
                """
            )
            await db.execute("""
                CREATE TABLE IF NOT EXISTS inventory (
                    user_id INTEGER, item_name TEXT, quantity INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, item_name)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS exp_boosts (
                    guild_id INTEGER, role_id INTEGER, boost_percent REAL,
                    PRIMARY KEY (guild_id, role_id)
                )
            """)
            await db.execute("""CREATE TABLE IF NOT EXISTS rare_drop_config (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)""")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS raffle_info_config (
                    guild_id INTEGER PRIMARY KEY, channel_id INTEGER, message_id INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS abuse_boxes (
                    guild_id INTEGER, box_name TEXT, PRIMARY KEY (guild_id, box_name)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS abuse_box_prizes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, box_name TEXT,
                    prize_type TEXT, prize_value TEXT, prize_amount INTEGER DEFAULT 0, chance REAL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS games (
                    guild_id INTEGER, game_name TEXT, enabled INTEGER DEFAULT 1,
                    reward_balance INTEGER DEFAULT 0, reward_exp INTEGER DEFAULT 0,
                    PRIMARY KEY (guild_id, game_name)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS game_answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER,
                    game_name TEXT, answer TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS game_config (
                    guild_id INTEGER PRIMARY KEY, channel_id INTEGER,
                    answer_time INTEGER DEFAULT 30, interval_seconds INTEGER DEFAULT 60
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS daily_key_log (
                    guild_id INTEGER, user_id INTEGER, date TEXT,
                    PRIMARY KEY (guild_id, user_id, date)
                )
            """)
            # Migration: ensure giveaways has ended column
            try:
                await db.execute("ALTER TABLE giveaways ADD COLUMN ended INTEGER DEFAULT 0")
            except aiosqlite.OperationalError:
                pass
            await db.commit()

# ---------------- TEMPLATES ---------------- #

TEMPLATES = {
    "gold": discord.Color.gold(),
    "red": discord.Color.red(),
    "blue": discord.Color.blue(),
    "green": discord.Color.green(),
}

# ---------------- VIP CHEST PRIZES ---------------- #

VIP_CHEST_KEY = "VIP Chest Key"

VIP_CHEST_PRIZES = [
    {"name": "2k EXP",       "exp": 2000,  "balance": 0,      "chance": 28},
    {"name": "5k EXP",       "exp": 5000,  "balance": 0,      "chance": 18},
    {"name": "5k Balance",   "exp": 0,     "balance": 5000,   "chance": 18},
    {"name": "15k Balance",  "exp": 0,     "balance": 15000,  "chance": 12},
    {"name": "1 Huge",       "exp": 0,     "balance": 0,      "chance": 10},
    {"name": "25m Gems",     "exp": 0,     "balance": 0,      "chance": 9},
    {"name": "100k Balance", "exp": 0,     "balance": 100000, "chance": 5},
]

# ---------------- ACTIVE GAME SESSIONS ---------------- #

active_game_sessions: dict[int, dict] = {}  # guild_id -> session info
game_tasks: dict[int, asyncio.Task] = {}     # guild_id -> running game loop task

# ---------------- GIVEAWAY TIMER ---------------- #

async def giveaway_timer(message_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        await end_giveaway(message_id)
    except Exception as e:
        print(f"[Giveaway Timer Error] message_id={message_id} error={e}")

# ---------------- GIVEAWAY ROLES ---------------- #

@bot.tree.command(name="addgiveawayrole", description="Allow a role to manage giveaways")
@app_commands.check(is_allowed_to_giveaway)
@command_enabled()
async def addgiveawayrole(interaction: discord.Interaction, role: discord.Role):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO giveaway_roles VALUES (?, ?)",
                (interaction.guild.id, role.id)
            )
            await db.commit()
    await interaction.response.send_message(f"✅ {role.mention} can now manage giveaways.")

@bot.tree.command(name="removegiveawayrole", description="Remove giveaway permissions from a role")
@app_commands.check(is_allowed_to_giveaway)
@command_enabled()
async def removegiveawayrole(interaction: discord.Interaction, role: discord.Role):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM giveaway_roles WHERE guild_id = ? AND role_id = ?",
                (interaction.guild.id, role.id)
            )
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed giveaway permissions from {role.mention}")

@bot.tree.command(name="giveawayroles", description="View giveaway manager roles")
@command_enabled()
async def giveawayroles(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute(
            "SELECT role_id FROM giveaway_roles WHERE guild_id = ?",
            (interaction.guild.id,)
        ) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No giveaway roles configured.")
        return
    mentions = []
    for row in rows:
        role = interaction.guild.get_role(row[0])
        if role:
            mentions.append(role.mention)
    await interaction.response.send_message("🎉 Giveaway Roles:\n" + "\n".join(mentions))

# ---------------- BALANCE FUNCTIONS ---------------- #

async def get_balance(user_id):
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT balance FROM balances WHERE user_id = ?", (user_id,)
            ) as cursor:
                data = await cursor.fetchone()
            if data is None:
                await db.execute("INSERT INTO balances VALUES (?, ?)", (user_id, 0))
                await db.commit()
                return 0
            return data[0]

async def add_balance(user_id, amount):
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR IGNORE INTO balances VALUES (?, ?)", (user_id, 0))
            await db.execute(
                "UPDATE balances SET balance = balance + ? WHERE user_id = ?",
                (amount, user_id)
            )
            await db.execute(
                "UPDATE balances SET balance = 0 WHERE user_id = ? AND balance < 0",
                (user_id,)
            )
            await db.commit()

@bot.tree.command(name="gift", description="Gift balance to another user")
@command_enabled()
async def gift(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be greater than 0.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You cannot gift yourself.", ephemeral=True)
        return
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT balance FROM balances WHERE user_id = ?", (interaction.user.id,)
            ) as cursor:
                data = await cursor.fetchone()
            if data is None or data[0] < amount:
                await interaction.response.send_message("❌ You don't have enough balance.", ephemeral=True)
                return
            await db.execute(
                "UPDATE balances SET balance = balance - ? WHERE user_id = ?",
                (amount, interaction.user.id)
            )
            await db.execute("INSERT OR IGNORE INTO balances VALUES (?, 0)", (user.id,))
            await db.execute(
                "UPDATE balances SET balance = balance + ? WHERE user_id = ?",
                (amount, user.id)
            )
            await db.execute(
                "INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (interaction.user.id,)
            )
            await db.execute(
                "UPDATE user_stats SET gifted_balance = gifted_balance + ? WHERE user_id = ?",
                (amount, interaction.user.id)
            )
            await db.commit()
    await interaction.response.send_message(f"💸 You gifted {amount:,} coins to {user.mention}!")

async def ensure_stats(user_id):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (user_id,)
            )
            await db.commit()

async def add_stat(user_id, column, amount):
    await ensure_stats(user_id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                f"UPDATE user_stats SET {column} = {column} + ? WHERE user_id = ?",
                (amount, user_id)
            )
            await db.commit()

# ---------------- EXP SYSTEM ---------------- #

MESSAGE_EXP_MIN = 30
MESSAGE_EXP_MAX = 50
LEVEL_DIVISOR = 700

last_message_exp = {}

async def add_exp(user_id, amount):
    if amount > 0:
        await add_stat(user_id, "total_exp", amount)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO exp_history VALUES (?, ?, ?)",
                (user_id, amount, int(datetime.now(UTC).timestamp()))
            )
            await db.commit()

async def get_exp(user_id):
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    async with get_db() as db:
        async with db.execute(
            "SELECT SUM(amount) FROM exp_history WHERE user_id = ? AND timestamp >= ?",
            (user_id, week_ago)
        ) as cursor:
            data = await cursor.fetchone()
    gained_exp = max(data[0] or 0, 0)
    spent_exp = await get_spent_exp(user_id)
    return max(gained_exp - spent_exp, 0)

async def get_level_exp(user_id):
    week_ago = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    async with get_db() as db:
        async with db.execute(
            "SELECT SUM(amount) FROM exp_history WHERE user_id = ? AND timestamp >= ?",
            (user_id, week_ago)
        ) as cursor:
            data = await cursor.fetchone()
    return max(data[0] or 0, 0)

async def get_level(user_id):
    exp = await get_level_exp(user_id)
    level = (exp // LEVEL_DIVISOR) + 1
    return min(level, 100)

async def get_spent_exp(user_id):
    async with get_db() as db:
        async with db.execute(
            "SELECT amount FROM spent_exp WHERE user_id = ?", (user_id,)
        ) as cursor:
            data = await cursor.fetchone()
        if not data:
            await db.execute("INSERT INTO spent_exp VALUES (?, ?)", (user_id, 0))
            await db.commit()
            return 0
        return data[0]

async def add_spent_exp(user_id, amount):
    current = await get_spent_exp(user_id)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "UPDATE spent_exp SET amount = ? WHERE user_id = ?",
                (current + amount, user_id)
            )
            await db.commit()

# ---------------- AUTO GIVEAWAYS ---------------- #

AUTO_GIVEAWAY_ENABLED = False
AUTO_GIVEAWAY_INTERVAL_SECONDS = 60
auto_giveaway_task = None

AUTO_PRIZES = [
    ("500 bal", 500),
    ("300 bal", 300),
    ("200 bal", 200),
    ("100 bal", 100)
]

# ---------------- MESSAGE EXP ---------------- #

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # ---- Game guess check ----
    if message.guild:
        session = active_game_sessions.get(message.guild.id)
        if session and not session.get("answered") and message.channel.id == session.get("channel_id"):
            if message.content.strip().lower() == session["answer"].lower():
                session["answered"] = True
                session["winner"] = message.author

    # ---- EXP from chatting ----
    now = datetime.now().timestamp()
    last_time = last_message_exp.get(message.author.id, 0)
    if now - last_time >= 30:
        content_length = len(message.content.strip())
        gained = min(50, 30 + random.randint(0, max(1, min(20, content_length // 10))))

        # Sum all matching role boosts (supports floats and negatives)
        if message.guild and isinstance(message.author, discord.Member):
            member_role_ids = {role.id for role in message.author.roles}
            if member_role_ids:
                placeholders = ",".join("?" * len(member_role_ids))
                async with get_db() as db:
                    async with db.execute(
                        f"SELECT boost_percent FROM exp_boosts WHERE guild_id = ? AND role_id IN ({placeholders})",
                        (message.guild.id, *member_role_ids)
                    ) as cursor:
                        boost_rows = await cursor.fetchall()
                if boost_rows:
                    total_boost = sum(row[0] for row in boost_rows)
                    gained = max(0, int(gained * (1 + total_boost / 100)))

        await add_exp(message.author.id, gained)
        last_message_exp[message.author.id] = now

    await bot.process_commands(message)

# ---------------- READY EVENT ---------------- #

GUILD_ID = 1494356360241090661
TARGET_GUILD = discord.Object(id=GUILD_ID)

@bot.event
async def on_ready():
    await setup_database()
    try:
        # 1. Sync all commands to the guild first (instant, no CDN delay)
        bot.tree.copy_global_to(guild=TARGET_GUILD)
        synced = await bot.tree.sync(guild=TARGET_GUILD)
        print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
        # 2. Clear global commands to remove duplicates
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync(guild=None)
        print("Cleared global commands")
    except Exception as e:
        print(f"[Sync Error] {e}")
    print(f"Logged in as {bot.user}")
    bot.loop.create_task(raffle_loop())
    bot.loop.create_task(giveaway_watcher())
    bot.loop.create_task(raffle_info_loop())
    bot.loop.create_task(game_loop())
    bot.loop.create_task(daily_key_loop())

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        # The check itself already sent an ephemeral message; just swallow the error
        return
    # Re-raise anything else so it still shows in logs
    raise error

# ---------------- CREATE GIVEAWAY ---------------- #

@bot.tree.command(name="giveaway", description="Create a giveaway")
@app_commands.describe(
    prize="Prize description shown in the embed",
    seconds="Duration in seconds (e.g. 30, 90, 3600)",
    winners="Number of winners",
    reward_balance="Coin reward per winner (0 for none)",
    reward_exp="EXP reward per winner (0 for none)",
    reward_tickets="Raffle tickets per winner (0 for none)",
    reward_item="Item/box name to give each winner (leave blank for none)",
    reward_item_qty="How many of the item to give (default 1)",
    channel="Channel to post in (defaults to current)",
    required_role="Required role to enter",
    template="Template color"
)
@command_enabled()
async def giveaway(
    interaction: discord.Interaction,
    prize: str,
    seconds: int,
    winners: int,
    reward_balance: int = 0,
    reward_exp: int = 0,
    reward_tickets: int = 0,
    reward_item: str = None,
    reward_item_qty: int = 1,
    channel: discord.TextChannel = None,
    required_role: discord.Role = None,
    template: str = "gold"
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
        return
    if seconds <= 0:
        await interaction.response.send_message("❌ Duration must be > 0 seconds.", ephemeral=True)
        return

    # Validate item if provided
    resolved_item_name = None
    if reward_item:
        # Accept any inventory item name (store items or boxes)
        resolved_item_name = reward_item.strip()
        if reward_item_qty < 1:
            reward_item_qty = 1

    target_channel = channel or interaction.channel
    end_time = datetime.now(UTC) + timedelta(seconds=seconds)

    reward_parts = []
    if reward_balance > 0: reward_parts.append(f"💰 {reward_balance:,} coins")
    if reward_exp > 0: reward_parts.append(f"⭐ {reward_exp:,} EXP")
    if reward_tickets > 0: reward_parts.append(f"🎟 {reward_tickets} ticket(s)")
    if resolved_item_name: reward_parts.append(f"🎒 {reward_item_qty}x {resolved_item_name}")
    reward_summary = " + ".join(reward_parts) if reward_parts else "No reward"

    embed = discord.Embed(
        title="🎉 GIVEAWAY 🎉",
        description=(
            f"React with 🎉 to enter\n\n"
            f"**Prize:** {prize}\n"
            f"**Reward:** {reward_summary}\n"
            f"**Winners:** {winners}\n"
            f"**Ends:** <t:{int(end_time.timestamp())}:R>"
        ),
        color=TEMPLATES.get(template, discord.Color.gold())
    )
    if required_role:
        embed.add_field(name="Required Role", value=required_role.mention, inline=False)

    message = await target_channel.send(embed=embed)
    await message.add_reaction("🎉")

    prize_meta = json.dumps({
        "label": prize,
        "balance": reward_balance,
        "exp": reward_exp,
        "tickets": reward_tickets,
        "item": resolved_item_name,
        "item_qty": reward_item_qty if resolved_item_name else 0,
    })

    async with get_db() as db:
        await db.execute(
            "INSERT INTO giveaways (message_id, channel_id, prize, winners, reward, end_time, required_role, template, ended) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.id, target_channel.id, prize_meta, winners, reward_balance,
             int(end_time.timestamp()), required_role.id if required_role else 0, template, 0)
        )
        await db.commit()

    await interaction.response.send_message("✅ Giveaway created.", ephemeral=True)
    asyncio.create_task(giveaway_timer(message.id, seconds))

# ---------------- END GIVEAWAY ---------------- #

async def end_giveaway(message_id, reroll=False):
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT message_id, channel_id, prize, winners, reward, end_time, required_role, template, ended FROM giveaways WHERE message_id = ?",
                (message_id,)
            ) as cursor:
                data = await cursor.fetchone()
            if not data:
                print(f"[Giveaway] No giveaway found for {message_id}"); return
            (message_id, channel_id, prize_raw, winner_count, legacy_reward,
             end_time, required_role, template, ended) = data
            if ended and not reroll:
                print(f"[Giveaway] Already ended: {message_id}"); return
            if not reroll:
                await db.execute("UPDATE giveaways SET ended = 1 WHERE message_id = ?", (message_id,))
                await db.commit()

    # Parse prize metadata (new JSON format) or fall back to legacy plain string
    try:
        meta = json.loads(prize_raw)
        prize_label   = meta.get("label", prize_raw)
        prize_balance = int(meta.get("balance", 0))
        prize_exp     = int(meta.get("exp", 0))
        prize_tickets = int(meta.get("tickets", 0))
        prize_item    = meta.get("item")
        prize_item_qty = int(meta.get("item_qty", 1))
    except (json.JSONDecodeError, TypeError):
        prize_label = prize_raw
        prize_balance = legacy_reward
        prize_exp = prize_tickets = prize_item_qty = 0
        prize_item = None

    channel = bot.get_channel(channel_id)
    if not channel:
        print(f"[Giveaway] Channel not found: {channel_id}"); return
    try:
        message = await channel.fetch_message(message_id)
    except Exception as e:
        print(f"[Giveaway] Failed to fetch message {message_id}: {e}"); return

    reaction = next((r for r in message.reactions if str(r.emoji) == "🎉"), None)
    if not reaction:
        await channel.send("❌ Giveaway reaction was missing."); return

    users = []
    async for user in reaction.users():
        if user.bot: continue
        member = channel.guild.get_member(user.id)
        if not member: continue
        if required_role and required_role not in {role.id for role in member.roles}: continue
        users.append(user)

    if not users:
        await channel.send("No valid participants."); return

    weighted_users = []
    for user in users:
        level = await get_level(user.id)
        weighted_users.extend([user] * min(100, max(1, level)))

    winners = []
    while len(winners) < min(winner_count, len(users)) and weighted_users:
        selected = random.choice(weighted_users)
        if selected not in winners:
            winners.append(selected)

    winner_mentions = []
    async with db_lock:
        async with get_db() as db:
            if reroll:
                async with db.execute("SELECT winner_id, reward FROM giveaway_winners WHERE message_id = ?", (message_id,)) as cursor:
                    old_data = await cursor.fetchone()
                if old_data:
                    old_winner_id, old_reward = old_data
                    await db.execute("INSERT OR IGNORE INTO balances VALUES (?, 0)", (old_winner_id,))
                    await db.execute("UPDATE balances SET balance = MAX(0, balance - ?) WHERE user_id = ?", (old_reward, old_winner_id))
            for winner in winners:
                if prize_balance > 0:
                    await db.execute("INSERT OR IGNORE INTO balances VALUES (?, 0)", (winner.id,))
                    await db.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (prize_balance, winner.id))
                await db.execute("INSERT OR REPLACE INTO giveaway_winners VALUES (?, ?, ?)", (message_id, winner.id, prize_balance))
                winner_mentions.append(winner.mention)
            await db.commit()

    for winner in winners:
        if prize_exp > 0: await add_exp(winner.id, prize_exp)
        if prize_tickets > 0: await add_tickets(channel.guild.id, winner.id, prize_tickets)
        if prize_item: await inventory_add(winner.id, prize_item, prize_item_qty)

    reward_parts = []
    if prize_balance > 0: reward_parts.append(f"💰 {prize_balance:,} coins")
    if prize_exp > 0: reward_parts.append(f"⭐ {prize_exp:,} EXP")
    if prize_tickets > 0: reward_parts.append(f"🎟 {prize_tickets} ticket(s)")
    if prize_item: reward_parts.append(f"🎒 {prize_item_qty}x {prize_item}")
    reward_summary = " + ".join(reward_parts) if reward_parts else "No reward"

    embed = discord.Embed(
        title="🎊 Giveaway Ended",
        description=f"**Prize:** {prize_label}\n**Reward:** {reward_summary}\n**Winners:** {', '.join(winner_mentions)}",
        color=discord.Color.green()
    )
    await channel.send(embed=embed)

# ---------------- BALANCE COMMAND ---------------- #

@bot.tree.command(name="balance", description="Check a balance")
@command_enabled()
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    bal = await get_balance(user.id)
    embed = discord.Embed(
        title=f"💰 {user.display_name}'s Balance",
        description=f"{bal:,} coins",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)

# ---------------- ADD / REMOVE BALANCE ---------------- #

@bot.tree.command(name="addbalance", description="Add balance to a user")
@command_enabled()
async def addbalance(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    await add_balance(user.id, amount)
    await interaction.response.send_message(f"✅ Added {amount} coins to {user.mention}")

@bot.tree.command(name="removebalance", description="Remove balance from a user")
@command_enabled()
async def removebalance(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    await add_balance(user.id, -amount)
    await interaction.response.send_message(f"❌ Removed {amount} coins from {user.mention}")

# ---------------- REROLL ---------------- #

@bot.tree.command(name="reroll", description="Reroll a giveaway")
@command_enabled()
async def reroll(interaction: discord.Interaction, message_id: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return

    message_id = int(message_id)

    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT * FROM giveaways WHERE message_id = ?", (message_id,)
            ) as cursor:
                data = await cursor.fetchone()
            if not data:
                await interaction.response.send_message("❌ Giveaway not found.")
                return
            (
                _message_id, channel_id, prize, winner_count, reward,
                end_time, required_role, template, ended
            ) = data
            async with db.execute(
                "SELECT winner_id, reward FROM giveaway_winners WHERE message_id = ?",
                (message_id,)
            ) as cursor:
                old_data = await cursor.fetchone()

    channel = bot.get_channel(channel_id)
    if channel is None:
        print(f"Channel {channel_id} not found")
        return
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        print(f"Message {message_id} not found")
        return

    reaction = discord.utils.get(message.reactions, emoji="🎉")
    users = []
    async for user in reaction.users():
        if user.bot:
            continue
        member = channel.guild.get_member(user.id)
        if required_role:
            role_ids = [role.id for role in member.roles]
            if required_role not in role_ids:
                continue
        users.append(user)

    if not users:
        await interaction.response.send_message("❌ No participants.")
        return

    weighted_users = []
    for user in users:
        level = await get_level(user.id)
        weight = min(100, max(1, level))
        weighted_users.extend([user] * weight)

    new_winner = random.choice(weighted_users)

    async with db_lock:
        async with get_db() as db:
            if old_data:
                old_winner_id, old_reward = old_data
                await db.execute(
                    "INSERT OR IGNORE INTO balances VALUES (?, 0)", (old_winner_id,)
                )
                await db.execute(
                    "UPDATE balances SET balance = MAX(0, balance - ?) WHERE user_id = ?",
                    (old_reward, old_winner_id)
                )
            await db.execute(
                "INSERT OR IGNORE INTO balances VALUES (?, 0)", (new_winner.id,)
            )
            await db.execute(
                "UPDATE balances SET balance = balance + ? WHERE user_id = ?",
                (reward, new_winner.id)
            )
            await db.execute(
                "INSERT OR REPLACE INTO giveaway_winners VALUES (?, ?, ?)",
                (message_id, new_winner.id, reward)
            )
            await db.commit()

    embed = discord.Embed(
        title="🔄 Giveaway Rerolled",
        description=(
            f"Prize: **{prize}**\n"
            f"Reward: **{reward} coins**\n"
            f"New Winner: {new_winner.mention}"
        ),
        color=discord.Color.orange()
    )
    await channel.send(embed=embed)
    await interaction.response.send_message("✅ Giveaway rerolled.")

# ---------------- AUTO GIVEAWAY POOL ---------------- #

AUTO_GIVEAWAY_ENABLED = False
AUTO_GIVEAWAY_POOL = []

# ---------------- RAFFLE SYSTEM ---------------- #

RAFFLE_TICKET_PRICE = 100
RAFFLE_PRIZE = 0

async def get_tickets(guild_id, user_id):
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT tickets FROM raffle WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id)
            ) as cursor:
                data = await cursor.fetchone()
            if not data:
                await db.execute(
                    "INSERT INTO raffle VALUES (?, ?, ?)", (guild_id, user_id, 0)
                )
                await db.commit()
                return 0
            return data[0]

async def add_tickets(guild_id, user_id, amount):
    tickets = await get_tickets(guild_id, user_id)
    new_tickets = max(0, tickets + amount)
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "UPDATE raffle SET tickets = ? WHERE guild_id = ? AND user_id = ?",
                (new_tickets, guild_id, user_id)
            )
            await db.commit()

@bot.tree.command(name="buytickets", description="Buy raffle tickets")
@command_enabled()
async def buytickets(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be greater than 0.")
        return
    price = amount * RAFFLE_TICKET_PRICE
    balance = await get_balance(interaction.user.id)
    if balance < price:
        await interaction.response.send_message("❌ Not enough balance.")
        return
    await add_balance(interaction.user.id, -price)
    await add_tickets(interaction.guild.id, interaction.user.id, amount)
    user_tickets = await get_tickets(interaction.guild.id, interaction.user.id)
    async with get_db() as db:
        async with db.execute(
            "SELECT SUM(tickets) FROM raffle WHERE guild_id = ?", (interaction.guild.id,)
        ) as cursor:
            total_data = await cursor.fetchone()
    total_tickets = total_data[0] or 0
    chance = (user_tickets / total_tickets) * 100 if total_tickets > 0 else 0
    await interaction.response.send_message(
        f"🎟 Bought {amount} tickets.\n"
        f"You now have **{user_tickets}** tickets.\n"
        f"Current win chance: **{chance:.2f}%**"
    )
    await add_stat(interaction.user.id, "raffle_tickets_bought", amount)

@bot.tree.command(name="addtickets", description="Add raffle tickets")
@command_enabled()
async def addtickets(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    await add_tickets(interaction.guild.id, user.id, amount)
    await interaction.response.send_message(f"✅ Added {amount} tickets to {user.mention}")

@bot.tree.command(name="removetickets", description="Remove raffle tickets")
@command_enabled()
async def removetickets(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    await add_tickets(interaction.guild.id, user.id, -amount)
    await interaction.response.send_message(f"❌ Removed {amount} tickets from {user.mention}")

@bot.tree.command(name="rafflechance", description="Check raffle tickets and win chance")
@command_enabled()
async def rafflechance(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    tickets = await get_tickets(interaction.guild.id, user.id)
    async with get_db() as db:
        async with db.execute(
            "SELECT SUM(tickets) FROM raffle WHERE guild_id = ?", (interaction.guild.id,)
        ) as cursor:
            total_data = await cursor.fetchone()
    total_tickets = total_data[0] or 0
    chance = (tickets / total_tickets) * 100 if total_tickets > 0 else 0
    embed = discord.Embed(title="🎟 Raffle Stats", color=discord.Color.gold())
    embed.add_field(name="User", value=user.mention, inline=False)
    embed.add_field(name="Tickets", value=f"{tickets:,}", inline=False)
    embed.add_field(name="Winning Chance", value=f"{chance:.2f}%", inline=False)
    embed.add_field(name="Total Tickets", value=f"{total_tickets:,}", inline=False)
    await interaction.response.send_message(embed=embed)

# Change 4: /setrafflechannel command

@bot.tree.command(name="setrafflechannel", description="Set the channel where raffle winners are announced")
@command_enabled()
async def setrafflechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO raffle_config VALUES (?, ?)",
                (interaction.guild.id, channel.id)
            )
            await db.commit()
    await interaction.response.send_message(
        f"✅ Raffle announcements will now be sent to {channel.mention}"
    )

# Change 2: raffle fires at 18:00 UTC+2 (16:00 UTC) daily

async def raffle_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        now = datetime.now(UTC)
        # Target: 16:00 UTC = 18:00 UTC+2
        target = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if now >= target:
            # Already past today's 16:00 UTC, schedule for tomorrow
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        print(f"[Raffle] Next draw in {wait_seconds:.0f}s at {target} UTC")
        await asyncio.sleep(wait_seconds)

        for guild in bot.guilds:
            async with get_db() as db:
                async with db.execute(
                    "SELECT user_id, tickets FROM raffle WHERE guild_id = ?", (guild.id,)
                ) as cursor:
                    entries = await cursor.fetchall()

            pool = []
            for user_id, tickets in entries:
                pool.extend([user_id] * tickets)

            if not pool:
                continue

            winner_id = random.choice(pool)
            await add_balance(winner_id, RAFFLE_PRIZE)

            # Change 4: use configured raffle channel, fall back to system channel
            announce_channel = None
            async with get_db() as db:
                async with db.execute(
                    "SELECT channel_id FROM raffle_config WHERE guild_id = ?", (guild.id,)
                ) as cursor:
                    row = await cursor.fetchone()
            if row:
                announce_channel = bot.get_channel(row[0])
            if announce_channel is None:
                announce_channel = guild.system_channel

            if announce_channel:
                await announce_channel.send(
                    f"🎉 <@{winner_id}> won the daily raffle and will receive a huge pet!"
                )

            async with get_db() as db:
                await db.execute("DELETE FROM raffle WHERE guild_id = ?", (guild.id,))
                await db.commit()

# ---------------- CHEST SYSTEM ---------------- #

CHEST_COST = 750

# Change 5: Huge 4%, new 1k EXP 6%, totals still 100%
CHEST_PRIZES = [
    {"name": "250 EXP",    "exp": 250,   "balance": 0,     "chance": 40},
    {"name": "450 EXP",    "exp": 450,   "balance": 0,     "chance": 30},
    {"name": "1k EXP",     "exp": 1000,  "balance": 0,     "chance": 6},
    {"name": "1k Balance", "exp": 0,     "balance": 1000,  "chance": 15},
    {"name": "1 Huge",     "exp": 0,     "balance": 0,     "chance": 4},
    {"name": "25m Gems",   "exp": 0,     "balance": 0,     "chance": 4},
    {"name": "40k Balance","exp": 0,     "balance": 40000, "chance": 1},
]

# ---------------- ADD / REMOVE AUTO GIVEAWAY ---------------- #

@bot.tree.command(name="addautogiveaway", description="Add a giveaway to the auto pool")
@command_enabled()
async def addautogiveaway(
    interaction: discord.Interaction, prize: str, reward: int, winners: int
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return
    AUTO_GIVEAWAY_POOL.append({"prize": prize, "reward": reward, "winners": winners})
    await interaction.response.send_message(
        f"✅ Added auto giveaway:\nPrize: {prize}\nReward: {reward}\nWinners: {winners}"
    )

@bot.tree.command(name="removeautogiveaway", description="Remove auto giveaway by prize name")
@command_enabled()
async def removeautogiveaway(interaction: discord.Interaction, prize: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return
    global AUTO_GIVEAWAY_POOL
    before = len(AUTO_GIVEAWAY_POOL)
    AUTO_GIVEAWAY_POOL = [g for g in AUTO_GIVEAWAY_POOL if g["prize"].lower() != prize.lower()]
    if len(AUTO_GIVEAWAY_POOL) == before:
        await interaction.response.send_message("❌ Giveaway not found.")
    else:
        await interaction.response.send_message(f"🗑 Removed auto giveaway: {prize}")

# ---------------- START GIVEAWAYS ---------------- #
# Change 1: added optional `channel` parameter

@bot.tree.command(name="startgiveaways", description="Start automatic giveaways")
@app_commands.describe(
    interval_seconds="How often to post a new giveaway (seconds)",
    giveaway_duration_seconds="How long each giveaway lasts (seconds)",
    channel="Channel to post giveaways in (defaults to current channel)"
)
@command_enabled()
async def startgiveaways(
    interaction: discord.Interaction,
    interval_seconds: int,
    giveaway_duration_seconds: int,
    channel: discord.TextChannel = None
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    global AUTO_GIVEAWAY_ENABLED, auto_giveaway_task

    if auto_giveaway_task and not auto_giveaway_task.done():
        await interaction.response.send_message(
            "Automatic giveaways are already running.", ephemeral=True
        )
        return

    if not AUTO_GIVEAWAY_POOL:
        await interaction.response.send_message(
            "❌ No auto giveaways added.", ephemeral=True
        )
        return

    target_channel = channel or interaction.channel
    AUTO_GIVEAWAY_ENABLED = True

    async def auto_loop():
        global AUTO_GIVEAWAY_ENABLED
        while AUTO_GIVEAWAY_ENABLED:
            giveaway_data = random.choice(AUTO_GIVEAWAY_POOL)
            prize = giveaway_data["prize"]
            reward = giveaway_data["reward"]
            winners = giveaway_data["winners"]
            end_time = datetime.now(UTC) + timedelta(seconds=giveaway_duration_seconds)

            embed = discord.Embed(
                title="🎉 AUTOMATIC GIVEAWAY 🎉",
                description=(
                    f"React with 🎉 to enter\n\n"
                    f"Prize: **{prize}**\n"
                    f"Winners: **{winners}**\n"
                    f"Reward: **{reward} coins**\n"
                    f"Ends: <t:{int(end_time.timestamp())}:R>"
                ),
                color=discord.Color.gold()
            )
            message = await target_channel.send(embed=embed)
            await message.add_reaction("🎉")

            async with get_db() as db:
                await db.execute(
                    """
                    INSERT INTO giveaways (
                        message_id, channel_id, prize, winners, reward,
                        end_time, required_role, template, ended
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.id, target_channel.id, prize, winners, reward,
                        int(end_time.timestamp()), 0, "gold", 0
                    )
                )
                await db.commit()

            asyncio.create_task(giveaway_timer(message.id, giveaway_duration_seconds))
            await asyncio.sleep(interval_seconds)

    auto_giveaway_task = asyncio.create_task(auto_loop())
    await interaction.response.send_message("✅ Automatic giveaways started.")

# ---------------- STOP GIVEAWAYS ---------------- #
# Change 1: added optional `channel` parameter (informational only, stopping is global)

@bot.tree.command(name="stopgiveaways", description="Stop automatic giveaways")
@command_enabled()
async def stopgiveaways(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    global AUTO_GIVEAWAY_ENABLED, auto_giveaway_task
    AUTO_GIVEAWAY_ENABLED = False
    if auto_giveaway_task:
        auto_giveaway_task.cancel()
        auto_giveaway_task = None
    await interaction.response.send_message("🛑 Automatic giveaways stopped.")

# ---------------- CHEST COMMAND ---------------- #

@bot.tree.command(name="chest", description="Open an EXP chest")
@command_enabled()
async def chest(interaction: discord.Interaction, amount: int = 1):
    await interaction.response.defer()
    if amount <= 0:
        await interaction.followup.send("❌ Amount must be greater than 0.")
        return

    exp = await get_exp(interaction.user.id)
    if exp >= 1400:
        max_chests = exp // CHEST_COST
        amount = min(amount, max_chests)
    else:
        amount = 1

    total_cost = CHEST_COST * amount
    if exp < total_cost:
        await interaction.followup.send(f"❌ You need {total_cost:,} EXP.")
        return

    results = {}
    total_balance = 0
    total_exp_won = 0

    for _ in range(amount):
        prize = random.choices(
            CHEST_PRIZES,
            weights=[p["chance"] for p in CHEST_PRIZES],
            k=1
        )[0]
        name = prize["name"]
        results[name] = results.get(name, 0) + 1
        total_balance += prize["balance"]
        total_exp_won += prize["exp"]

    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR IGNORE INTO spent_exp VALUES (?, 0)", (interaction.user.id,)
            )
            await db.execute(
                "UPDATE spent_exp SET amount = amount + ? WHERE user_id = ?",
                (total_cost, interaction.user.id)
            )
            if total_balance > 0:
                await db.execute(
                    "INSERT OR IGNORE INTO balances VALUES (?, 0)", (interaction.user.id,)
                )
                await db.execute(
                    "UPDATE balances SET balance = balance + ? WHERE user_id = ?",
                    (total_balance, interaction.user.id)
                )
            if total_exp_won > 0:
                await db.execute(
                    "INSERT INTO exp_history VALUES (?, ?, ?)",
                    (interaction.user.id, total_exp_won, int(datetime.now(UTC).timestamp()))
                )
                await db.execute(
                    "INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (interaction.user.id,)
                )
                await db.execute(
                    "UPDATE user_stats SET total_exp = total_exp + ? WHERE user_id = ?",
                    (total_exp_won, interaction.user.id)
                )
            await db.execute(
                "INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (interaction.user.id,)
            )
            await db.execute(
                "UPDATE user_stats SET chests_opened = chests_opened + ? WHERE user_id = ?",
                (amount, interaction.user.id)
            )
            await db.commit()

    result_text = "\n".join(f"• {count}x {name}" for name, count in results.items())
    embed = discord.Embed(
        title="📦 Chest Results",
        description=result_text,
        color=discord.Color.purple()
    )
    embed.set_footer(text=f"Opened {amount} chest(s)")
    await interaction.followup.send(embed=embed)

# ---------------- EXP COMMANDS ---------------- #

@bot.tree.command(name="level", description="Check a level")
@command_enabled()
async def level(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    exp = await get_level_exp(user.id)
    usable_exp = await get_exp(user.id)
    lvl = await get_level(user.id)
    embed = discord.Embed(title=f"⭐ {user.display_name}'s Level", color=discord.Color.gold())
    embed.add_field(name="Level", value=str(lvl), inline=False)
    embed.add_field(name="Total EXP (7d)", value=f"{exp:,}", inline=False)
    embed.add_field(name="Usable EXP", value=f"{usable_exp:,}", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="addexp", description="Add EXP")
@command_enabled()
async def addexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    await add_exp(user.id, amount)
    await interaction.response.send_message(f"✅ Added {amount} EXP to {user.mention}")

@bot.tree.command(name="removeexp", description="Remove EXP")
@command_enabled()
async def removeexp(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    await add_exp(user.id, -amount)
    await interaction.response.send_message(f"❌ Removed {amount} EXP from {user.mention}")

# ---------------- INVENTORY HELPERS ---------------- #

async def inventory_add(user_id: int, item_name: str, quantity: int = 1):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, ?) ON CONFLICT(user_id, item_name) DO UPDATE SET quantity = quantity + excluded.quantity",
                (user_id, item_name, quantity)
            )
            await db.commit()

async def inventory_remove(user_id: int, item_name: str, quantity: int = 1) -> bool:
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT quantity FROM inventory WHERE user_id = ? AND item_name = ?", (user_id, item_name)) as cursor:
                row = await cursor.fetchone()
            if not row or row[0] < quantity:
                return False
            new_qty = row[0] - quantity
            if new_qty == 0:
                await db.execute("DELETE FROM inventory WHERE user_id = ? AND item_name = ?", (user_id, item_name))
            else:
                await db.execute("UPDATE inventory SET quantity = ? WHERE user_id = ? AND item_name = ?", (new_qty, user_id, item_name))
            await db.commit()
    return True

async def inventory_get(user_id: int) -> list[tuple[str, int]]:
    async with get_db() as db:
        async with db.execute("SELECT item_name, quantity FROM inventory WHERE user_id = ? ORDER BY item_name", (user_id,)) as cursor:
            return await cursor.fetchall()

# ---------------- VIP CHEST ---------------- #

@bot.tree.command(name="vipchest", description="Open a VIP Chest — consumes 1 VIP Chest Key from your inventory")
@command_enabled()
async def vipchest(interaction: discord.Interaction, amount: int = 1):
    await interaction.response.defer()
    if amount <= 0:
        await interaction.followup.send("❌ Amount must be at least 1."); return
    if amount > 10:
        await interaction.followup.send("❌ You can open at most 10 VIP chests at once."); return

    # Check and consume keys
    inv = await inventory_get(interaction.user.id)
    owned = {n.lower(): q for n, q in inv}
    available_keys = owned.get(VIP_CHEST_KEY.lower(), 0)
    if available_keys < amount:
        await interaction.followup.send(
            f"❌ You need **{amount}x {VIP_CHEST_KEY}** but only have **{available_keys}**.\n"
            f"Keys are given by admins and Nitro Boosters receive one daily!"
        ); return

    removed = await inventory_remove(interaction.user.id, VIP_CHEST_KEY, amount)
    if not removed:
        await interaction.followup.send("❌ Failed to consume keys."); return

    results: dict[str, int] = {}
    total_balance = 0
    total_exp_won = 0

    for _ in range(amount):
        prize = random.choices(VIP_CHEST_PRIZES, weights=[p["chance"] for p in VIP_CHEST_PRIZES], k=1)[0]
        name = prize["name"]
        results[name] = results.get(name, 0) + 1
        total_balance += prize["balance"]
        total_exp_won += prize["exp"]

    async with db_lock:
        async with get_db() as db:
            if total_balance > 0:
                await db.execute("INSERT OR IGNORE INTO balances VALUES (?, 0)", (interaction.user.id,))
                await db.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (total_balance, interaction.user.id))
            if total_exp_won > 0:
                await db.execute("INSERT INTO exp_history VALUES (?, ?, ?)", (interaction.user.id, total_exp_won, int(datetime.now(UTC).timestamp())))
                await db.execute("INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (interaction.user.id,))
                await db.execute("UPDATE user_stats SET total_exp = total_exp + ? WHERE user_id = ?", (total_exp_won, interaction.user.id))
            await db.commit()

    result_text = "\n".join(f"• {count}x {name}" for name, count in results.items())
    embed = discord.Embed(
        title="💎 VIP Chest Results",
        description=result_text,
        color=discord.Color.from_rgb(148, 0, 211)
    )
    embed.set_footer(text=f"Opened {amount} VIP chest(s) | {available_keys - amount} key(s) remaining")
    await interaction.followup.send(embed=embed)

    # Rare drop announcements for VIP chest prizes too
    rare_vip = {"1 Huge", "25m Gems", "100k Balance"}
    rare_items_won = {name: count for name, count in results.items() if name in rare_vip}
    if rare_items_won:
        rare_channel_id = await get_rare_drop_channel(interaction.guild.id)
        if rare_channel_id:
            rare_channel = bot.get_channel(rare_channel_id)
            if rare_channel:
                prizes_text = " and ".join(f"**{count}x {name}**" for name, count in rare_items_won.items())
                rare_embed = discord.Embed(
                    title="💎 VIP Rare Drop!",
                    description=f"{interaction.user.mention} just got {prizes_text} from a **VIP Chest**! 👑",
                    color=discord.Color.from_rgb(148, 0, 211)
                )
                rare_embed.set_thumbnail(url=interaction.user.display_avatar.url)
                await rare_channel.send(embed=rare_embed)

@bot.tree.command(name="givekey", description="Give VIP Chest Key(s) to a user")
@app_commands.describe(user="User to give keys to", amount="Number of keys to give (default 1)")
@command_enabled()
async def givekey(interaction: discord.Interaction, user: discord.Member, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be at least 1.", ephemeral=True); return
    await inventory_add(user.id, VIP_CHEST_KEY, amount)
    await interaction.response.send_message(f"🔑 Gave **{amount}x {VIP_CHEST_KEY}** to {user.mention}.")

@bot.tree.command(name="takekey", description="Take VIP Chest Key(s) from a user")
@app_commands.describe(user="User to take keys from", amount="Number of keys to take (default 1)")
@command_enabled()
async def takekey(interaction: discord.Interaction, user: discord.Member, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be at least 1.", ephemeral=True); return
    removed = await inventory_remove(user.id, VIP_CHEST_KEY, amount)
    if not removed:
        await interaction.response.send_message(f"❌ {user.mention} doesn't have {amount}x {VIP_CHEST_KEY}.", ephemeral=True); return
    await interaction.response.send_message(f"🗑 Took **{amount}x {VIP_CHEST_KEY}** from {user.mention}.")

async def daily_key_loop():
    """Gives one VIP Chest Key per day to every active Nitro Booster in each guild."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(UTC)
        # Run once per day at 12:00 UTC
        target = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        for guild in bot.guilds:
            for member in guild.members:
                if member.bot or member.premium_since is None:
                    continue
                try:
                    async with db_lock:
                        async with get_db() as db:
                            try:
                                await db.execute(
                                    "INSERT INTO daily_key_log (guild_id, user_id, date) VALUES (?, ?, ?)",
                                    (guild.id, member.id, today)
                                )
                                await db.commit()
                            except aiosqlite.IntegrityError:
                                continue  # already gave key today
                    await inventory_add(member.id, VIP_CHEST_KEY, 1)
                    print(f"[DailyKey] Gave key to {member} in {guild.name}")
                except Exception as e:
                    print(f"[DailyKey] Error for {member} in {guild.name}: {e}")

# ---------------- ITEM FUNCTIONS ---------------- #

async def add_item(item_name, price, role_id, description):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO item_store VALUES (?, ?, ?, ?)",
                (item_name, price, role_id, description)
            )
            await db.commit()

async def remove_item(item_name):
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM item_store WHERE item_name = ?", (item_name,)
            )
            await db.commit()

async def get_item(item_name):
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM item_store WHERE LOWER(item_name) = LOWER(?)", (item_name,)
        ) as cursor:
            return await cursor.fetchone()

async def get_all_items():
    async with get_db() as db:
        async with db.execute("SELECT * FROM item_store") as cursor:
            return await cursor.fetchall()

# ---------------- ITEM STORE COMMANDS ---------------- #

item_group = app_commands.Group(name="item", description="Item store commands")
bot.tree.add_command(item_group)

@item_group.command(name="add", description="Add item to store")
@command_enabled()
async def item_add(
    interaction: discord.Interaction, name: str, price: int,
    role: discord.Role, description: str
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    await add_item(name, price, role.id, description)
    await interaction.response.send_message(f"✅ Added item **{name}** to the store.")

@item_group.command(name="remove", description="Remove item from store")
@command_enabled()
async def item_remove(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    item = await get_item(name)
    if not item:
        await interaction.response.send_message("❌ Item not found.")
        return
    await remove_item(name)
    await interaction.response.send_message(f"🗑 Removed **{name}** from the store.")

@item_group.command(name="info", description="View item or box info")
@command_enabled()
async def item_info(interaction: discord.Interaction, name: str):
    # Check store first
    item = await get_item(name)
    if item:
        item_name, price, role_id, description = item
        role = interaction.guild.get_role(role_id)
        embed = discord.Embed(title=f"🛒 {item_name}", color=discord.Color.blurple())
        embed.add_field(name="Price", value=f"{price:,} coins", inline=False)
        embed.add_field(name="Role", value=role.mention if role else "Unknown Role", inline=False)
        embed.add_field(name="Description", value=description, inline=False)
        await interaction.response.send_message(embed=embed)
        return
    # Check boxes
    async with get_db() as db:
        async with db.execute(
            "SELECT box_name FROM abuse_boxes WHERE guild_id = ? AND LOWER(box_name) = LOWER(?)",
            (interaction.guild.id, name)
        ) as cursor:
            box_row = await cursor.fetchone()
    if box_row:
        box_name = box_row[0]
        async with get_db() as db:
            async with db.execute(
                "SELECT id, prize_type, prize_value, chance FROM abuse_box_prizes WHERE guild_id = ? AND box_name = ? ORDER BY id",
                (interaction.guild.id, box_name)
            ) as cursor:
                prizes = await cursor.fetchall()
        embed = discord.Embed(title=f"📦 {box_name}", color=discord.Color.orange())
        if not prizes:
            embed.description = "*No prizes configured yet.*"
        else:
            total_weight = sum(p[3] for p in prizes)
            lines = []
            for p_id, p_type, p_value, p_chance in prizes:
                pct = (p_chance / total_weight * 100) if total_weight > 0 else 0
                if p_type == "balance": desc = f"💰 {int(p_value):,} coins"
                elif p_type == "exp": desc = f"⭐ {int(p_value):,} EXP"
                elif p_type == "item": desc = f"🎒 {p_value}"
                else: desc = f"✨ {p_value}"
                lines.append(f"`#{p_id}` {desc} — **{pct:.1f}%**")
            embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)
        return
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
        embed.add_field(name=item_name, value=f"💰 {price:,} coins\n🎭 {role.mention if role else 'Unknown Role'}", inline=False)
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
        f"✅ You bought **{item_name}** for {price:,} coins.\nUse `/item use {item_name}` to redeem it!"
    )

@item_group.command(name="use", description="Use a store item from your inventory to receive its role")
@command_enabled()
async def item_use(interaction: discord.Interaction, name: str):
    item = await get_item(name)
    if not item:
        await interaction.response.send_message("❌ Item not found in the store."); return
    item_name, price, role_id, description = item
    inv = await inventory_get(interaction.user.id)
    owned = {n.lower(): q for n, q in inv}
    if owned.get(item_name.lower(), 0) < 1:
        await interaction.response.send_message(f"❌ You don't have **{item_name}** in your inventory."); return
    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.response.send_message("❌ Role no longer exists."); return
    member = interaction.guild.get_member(interaction.user.id)
    if role in member.roles:
        await interaction.response.send_message(f"❌ You already have the **{role.name}** role."); return
    if not await inventory_remove(interaction.user.id, item_name, 1):
        await interaction.response.send_message("❌ Failed to remove item from inventory."); return
    await member.add_roles(role)
    await interaction.response.send_message(f"✅ Used **{item_name}** — you've been given the {role.mention} role!")

@item_group.command(name="give", description="Give an item or box to a user (admin only)")
@app_commands.describe(user="Target user", name="Item or box name", quantity="How many to give (default 1)")
@command_enabled()
async def item_give(interaction: discord.Interaction, user: discord.Member, name: str, quantity: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if quantity <= 0:
        await interaction.response.send_message("❌ Quantity must be at least 1.", ephemeral=True); return
    # Accept store items or box names
    store_item = await get_item(name)
    canonical = store_item[0] if store_item else name.strip()
    # Verify it's a known box if not in store
    if not store_item:
        async with get_db() as db:
            async with db.execute(
                "SELECT box_name FROM abuse_boxes WHERE guild_id = ? AND LOWER(box_name) = LOWER(?)",
                (interaction.guild.id, name)
            ) as cursor:
                box_row = await cursor.fetchone()
        if not box_row and name.strip() != VIP_CHEST_KEY:
            await interaction.response.send_message(
                f"❌ **{name}** not found in store or boxes. Use the exact item/box name.", ephemeral=True
            ); return
        if box_row:
            canonical = box_row[0]
    await inventory_add(user.id, canonical, quantity)
    await interaction.response.send_message(f"✅ Gave **{quantity}x {canonical}** to {user.mention}.")

@item_group.command(name="take", description="Take an item or box from a user (admin only)")
@app_commands.describe(user="Target user", name="Item or box name", quantity="How many to take (default 1)")
@command_enabled()
async def item_take(interaction: discord.Interaction, user: discord.Member, name: str, quantity: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if quantity <= 0:
        await interaction.response.send_message("❌ Quantity must be at least 1.", ephemeral=True); return
    # Resolve canonical name
    store_item = await get_item(name)
    canonical = store_item[0] if store_item else name.strip()
    if not store_item:
        async with get_db() as db:
            async with db.execute(
                "SELECT box_name FROM abuse_boxes WHERE guild_id = ? AND LOWER(box_name) = LOWER(?)",
                (interaction.guild.id, name)
            ) as cursor:
                box_row = await cursor.fetchone()
        if box_row:
            canonical = box_row[0]
    removed = await inventory_remove(user.id, canonical, quantity)
    if not removed:
        await interaction.response.send_message(f"❌ {user.mention} doesn't have {quantity}x **{canonical}**."); return
    await interaction.response.send_message(f"🗑 Took **{quantity}x {canonical}** from {user.mention}.")

@item_group.command(name="inv", description="Check a user's inventory (items, boxes and keys)")
@app_commands.describe(user="User to check (defaults to yourself)")
@command_enabled()
async def item_inv(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    inv = await inventory_get(user.id)
    embed = discord.Embed(title=f"🎒 {user.display_name}'s Inventory", color=discord.Color.blurple())
    if not inv:
        embed.description = "Inventory is empty."
    else:
        lines = []
        for item_name, quantity in inv:
            store_item = await get_item(item_name)
            if store_item:
                role = interaction.guild.get_role(store_item[2])
                suffix = f" → {role.mention}" if role else ""
                lines.append(f"• **{item_name}** x{quantity}{suffix}")
            elif item_name == VIP_CHEST_KEY:
                lines.append(f"• 🔑 **{item_name}** x{quantity}")
            else:
                lines.append(f"• 📦 **{item_name}** x{quantity}")
        embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)

# ---------------- ENABLE / DISABLE COMMANDS ---------------- #
# Change 3: /enablecmd and /disablecmd

@bot.tree.command(name="disablecmd", description="Temporarily disable a command")
async def disablecmd(interaction: discord.Interaction, command_name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    # Protect the enable/disable commands themselves from being disabled
    if command_name in ("disablecmd", "enablecmd"):
        await interaction.response.send_message(
            "❌ You cannot disable the enable/disable commands.", ephemeral=True
        )
        return
    disabled_commands.add(command_name)
    await interaction.response.send_message(
        f"🔒 Command `/{command_name}` has been disabled."
    )

@bot.tree.command(name="enablecmd", description="Re-enable a disabled command")
async def enablecmd(interaction: discord.Interaction, command_name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    if command_name not in disabled_commands:
        await interaction.response.send_message(
            f"ℹ️ `/{command_name}` is not currently disabled.", ephemeral=True
        )
        return
    disabled_commands.discard(command_name)
    await interaction.response.send_message(
        f"🔓 Command `/{command_name}` has been re-enabled."
    )

# ---------------- LEADERBOARD ---------------- #

@bot.tree.command(name="leaderboard", description="View leaderboards")
@app_commands.choices(
    category=[
        app_commands.Choice(name="Total EXP",              value="total_exp"),
        app_commands.Choice(name="Current EXP",            value="current_exp"),
        app_commands.Choice(name="Balance",                value="balance"),
        app_commands.Choice(name="Lifetime Tickets",       value="raffle_tickets_bought"),
        app_commands.Choice(name="Current Raffle Tickets", value="current_tickets"),
        app_commands.Choice(name="Chests Opened",          value="chests_opened"),
        app_commands.Choice(name="Gifted Balance",         value="gifted_balance"),
    ]
)
@command_enabled()
async def leaderboard(interaction: discord.Interaction, category: app_commands.Choice[str]):
    value = category.value
    leaderboard_data = []

    async with get_db() as db:
        if value == "current_exp":
            async with db.execute(
                "SELECT DISTINCT user_id FROM exp_history"
            ) as cursor:
                users = await cursor.fetchall()
            for (user_id,) in users:
                exp = await get_exp(user_id)
                leaderboard_data.append((user_id, exp))
        elif value == "current_tickets":
            async with db.execute(
                "SELECT user_id, tickets FROM raffle WHERE guild_id = ? ORDER BY tickets DESC LIMIT 10",
                (interaction.guild.id,)
            ) as cursor:
                leaderboard_data = await cursor.fetchall()
        elif value == "balance":
            async with db.execute(
                "SELECT user_id, balance FROM balances ORDER BY balance DESC LIMIT 10"
            ) as cursor:
                leaderboard_data = await cursor.fetchall()
        else:
            async with db.execute(
                f"SELECT user_id, {value} FROM user_stats ORDER BY {value} DESC LIMIT 10"
            ) as cursor:
                leaderboard_data = await cursor.fetchall()

    if value == "current_exp":
        leaderboard_data.sort(key=lambda x: x[1], reverse=True)
        leaderboard_data = leaderboard_data[:10]

    if not leaderboard_data:
        await interaction.response.send_message("❌ No leaderboard data found.")
        return

    title_map = {
        "total_exp":             "🏆 Total EXP Leaderboard",
        "current_exp":           "⭐ Current EXP Leaderboard",
        "balance":               "💰 Balance Leaderboard",
        "raffle_tickets_bought": "🎟 Lifetime Tickets Leaderboard",
        "current_tickets":       "🎫 Current Raffle Tickets Leaderboard",
        "chests_opened":         "📦 Chests Opened Leaderboard",
        "gifted_balance":        "💸 Gifted Balance Leaderboard",
    }

    embed = discord.Embed(title=title_map[value], color=discord.Color.gold())
    medals = ["🥇", "🥈", "🥉"]

    for index, (user_id, amount) in enumerate(leaderboard_data, start=1):
        user = interaction.guild.get_member(user_id)
        if not user:
            continue
        medal = medals[index - 1] if index <= 3 else f"#{index}"
        embed.add_field(name=f"{medal} {user.display_name}", value=f"{amount:,}", inline=False)

    await interaction.response.send_message(embed=embed)

# ---------------- RARE DROP CHANNEL ---------------- #

RARE_CHEST_PRIZES = {"1 Huge", "25m Gems", "40k Balance"}

async def get_rare_drop_channel(guild_id: int):
    async with get_db() as db:
        async with db.execute("SELECT channel_id FROM rare_drop_config WHERE guild_id = ?", (guild_id,)) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None

@bot.tree.command(name="setraredropchannel", description="Set channel for rare chest drop announcements")
@command_enabled()
async def setraredropchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO rare_drop_config VALUES (?, ?)", (interaction.guild.id, channel.id))
            await db.commit()
    prizes_listed = ", ".join(f"**{p}**" for p in sorted(RARE_CHEST_PRIZES))
    await interaction.response.send_message(
        f"✅ Rare drop announcements will be posted in {channel.mention}.\nWatching for: {prizes_listed}"
    )

# ---------------- RAFFLE INFO CHANNEL ---------------- #

def build_raffle_info_embed(guild, total_tickets, top_entries):
    now = datetime.now(UTC)
    target = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= target: target += timedelta(days=1)
    end_ts = int(target.timestamp())
    embed = discord.Embed(title="🎟 Live Raffle Status", color=discord.Color.gold())
    embed.add_field(name="⏰ Next Draw", value=f"<t:{end_ts}:R> (<t:{end_ts}:F>)", inline=False)
    embed.add_field(name="🎫 Total Tickets in Pool", value=f"{total_tickets:,}", inline=False)
    if top_entries:
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (user_id, tickets) in enumerate(top_entries[:5]):
            medal = medals[i] if i < 3 else f"#{i+1}"
            member = guild.get_member(user_id)
            name = member.display_name if member else f"<@{user_id}>"
            chance = (tickets / total_tickets * 100) if total_tickets > 0 else 0
            lines.append(f"{medal} **{name}** — {tickets:,} tickets ({chance:.1f}%)")
        embed.add_field(name="🏆 Top Participants", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🏆 Top Participants", value="No tickets purchased yet.", inline=False)
    embed.set_footer(text=f"Updated: {datetime.now(UTC).strftime('%H:%M:%S UTC')}")
    return embed

@bot.tree.command(name="setraffleinfochannel", description="Set channel for the live raffle status board")
@command_enabled()
async def setraffleinfochannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT user_id, tickets FROM raffle WHERE guild_id = ? ORDER BY tickets DESC LIMIT 5", (interaction.guild.id,)) as cursor:
            top_entries = await cursor.fetchall()
        async with db.execute("SELECT SUM(tickets) FROM raffle WHERE guild_id = ?", (interaction.guild.id,)) as cursor:
            total_row = await cursor.fetchone()
    total_tickets = total_row[0] or 0
    embed = build_raffle_info_embed(interaction.guild, total_tickets, top_entries)
    info_message = await channel.send(embed=embed)
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO raffle_info_config VALUES (?, ?, ?)", (interaction.guild.id, channel.id, info_message.id))
            await db.commit()
    await interaction.response.send_message(f"✅ Live raffle status board posted in {channel.mention} and will update automatically.")

async def raffle_info_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        async with get_db() as db:
            async with db.execute("SELECT guild_id, channel_id, message_id FROM raffle_info_config") as cursor:
                configs = await cursor.fetchall()
        for guild_id, channel_id, message_id in configs:
            try:
                guild = bot.get_guild(guild_id)
                channel = bot.get_channel(channel_id)
                if not guild or not channel: continue
                async with get_db() as db:
                    async with db.execute("SELECT user_id, tickets FROM raffle WHERE guild_id = ? ORDER BY tickets DESC LIMIT 5", (guild_id,)) as cursor:
                        top_entries = await cursor.fetchall()
                    async with db.execute("SELECT SUM(tickets) FROM raffle WHERE guild_id = ?", (guild_id,)) as cursor:
                        total_row = await cursor.fetchone()
                total_tickets = total_row[0] or 0
                embed = build_raffle_info_embed(guild, total_tickets, top_entries)
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.edit(embed=embed)
                except discord.NotFound:
                    new_msg = await channel.send(embed=embed)
                    async with db_lock:
                        async with get_db() as db:
                            await db.execute("UPDATE raffle_info_config SET message_id = ? WHERE guild_id = ?", (new_msg.id, guild_id))
                            await db.commit()
            except Exception as e:
                print(f"[RaffleInfoLoop] guild={guild_id}: {e}")
        await asyncio.sleep(60)

# ---------------- EXP BOOST ---------------- #

@bot.tree.command(name="expboost", description="Set an EXP boost for a role (supports decimals and negatives)")
@app_commands.describe(role="Role to boost", boost="Boost % — e.g. 1.5 = +1.5%, -25 = penalty. All matching roles are summed.")
@command_enabled()
async def expboost(interaction: discord.Interaction, role: discord.Role, boost: float):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if boost == 0:
        await interaction.response.send_message("❌ Boost cannot be 0%.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO exp_boosts VALUES (?, ?, ?)", (interaction.guild.id, role.id, boost))
            await db.commit()
    sign = "+" if boost > 0 else ""
    await interaction.response.send_message(f"✅ Members with {role.mention} now earn **{sign}{boost}% EXP** per message.")

@bot.tree.command(name="removeexpboost", description="Remove an EXP boost from a role")
@command_enabled()
async def removeexpboost(interaction: discord.Interaction, role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("DELETE FROM exp_boosts WHERE guild_id = ? AND role_id = ?", (interaction.guild.id, role.id))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed EXP boost from {role.mention}.")

@bot.tree.command(name="listexpboosts", description="List all active EXP boosts")
@command_enabled()
async def listexpboosts(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute("SELECT role_id, boost_percent FROM exp_boosts WHERE guild_id = ? ORDER BY boost_percent DESC", (interaction.guild.id,)) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No EXP boosts configured."); return
    embed = discord.Embed(title="⚡ Active EXP Boosts", color=discord.Color.blurple())
    for role_id, boost in rows:
        role = interaction.guild.get_role(role_id)
        name = role.mention if role else f"<deleted role {role_id}>"
        sign = "+" if boost > 0 else ""
        embed.add_field(name=name, value=f"{sign}{boost}%", inline=False)
    await interaction.response.send_message(embed=embed)

# ---------------- TRADE SYSTEM ---------------- #

trade_sessions: dict = {}

class TradeOffer:
    def __init__(self):
        self.balance: int = 0
        self.exp: int = 0
        self.tickets: int = 0
        self.items: list[tuple[str, int]] = []

    def display(self) -> str:
        lines = []
        if self.balance > 0: lines.append(f"💰 {self.balance:,} coins")
        if self.exp > 0: lines.append(f"⭐ {self.exp:,} EXP")
        if self.tickets > 0: lines.append(f"🎟 {self.tickets:,} ticket(s)")
        for item_name, qty in self.items: lines.append(f"🎒 {qty}x {item_name}")
        return "\n".join(lines) if lines else "*Nothing*"

class TradeSession:
    def __init__(self, guild_id, initiator_id, target_id):
        self.guild_id = guild_id
        self.initiator_id = initiator_id
        self.target_id = target_id
        self.offers: dict = {initiator_id: None, target_id: None}
        self.confirmed: dict = {initiator_id: False, target_id: False}
        self.message = None
        self.done = False
        self.lock = asyncio.Lock()

    def session_key(self):
        return (self.guild_id, frozenset({self.initiator_id, self.target_id}))

    def build_embed(self, guild):
        init = guild.get_member(self.initiator_id)
        tgt = guild.get_member(self.target_id)
        embed = discord.Embed(title="🤝 Trade Offer", color=discord.Color.blurple())
        init_offer = self.offers[self.initiator_id]
        tgt_offer = self.offers[self.target_id]
        init_status = "✅" if self.confirmed[self.initiator_id] else ("📋" if init_offer else "❓")
        tgt_status = "✅" if self.confirmed[self.target_id] else ("📋" if tgt_offer else "❓")
        embed.add_field(name=f"{init.display_name if init else 'User'}'s offer {init_status}", value=init_offer.display() if init_offer else "*Not set yet*", inline=True)
        embed.add_field(name=f"{tgt.display_name if tgt else 'User'}'s offer {tgt_status}", value=tgt_offer.display() if tgt_offer else "*Not set yet*", inline=True)
        return embed

class TradeOfferModal(discord.ui.Modal, title="Set Your Trade Offer"):
    balance_input = discord.ui.TextInput(label="Balance to offer (0 for none)", default="0", max_length=20)
    exp_input     = discord.ui.TextInput(label="EXP to offer (0 for none)", default="0", max_length=20)
    tickets_input = discord.ui.TextInput(label="Raffle tickets to offer (0 for none)", default="0", max_length=20)
    items_input   = discord.ui.TextInput(label="Items/boxes to offer (blank for none)", placeholder="Name:qty, Name2:qty2", required=False, max_length=300)

    def __init__(self, session):
        super().__init__()
        self.session = session

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
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
                await interaction.response.send_message(f"❌ Bad format `{part}` — use Name:qty", ephemeral=True); return
            item_name, qty_str = part.rsplit(":", 1)
            try: qty = int(qty_str.strip()); assert qty > 0
            except: await interaction.response.send_message(f"❌ Invalid qty for {item_name}", ephemeral=True); return
            items.append((item_name.strip(), qty))

        if balance > 0 and await get_balance(user_id) < balance:
            await interaction.response.send_message(f"❌ Not enough coins.", ephemeral=True); return
        if exp > 0 and await get_exp(user_id) < exp:
            await interaction.response.send_message(f"❌ Not enough EXP.", ephemeral=True); return
        if tickets > 0 and await get_tickets(session.guild_id, user_id) < tickets:
            await interaction.response.send_message(f"❌ Not enough tickets.", ephemeral=True); return
        if items:
            user_inv = {n.lower(): q for n, q in await inventory_get(user_id)}
            for iname, qty in items:
                if user_inv.get(iname.lower(), 0) < qty:
                    await interaction.response.send_message(f"❌ Not enough {iname}.", ephemeral=True); return

        offer = TradeOffer()
        offer.balance = balance; offer.exp = exp; offer.tickets = tickets; offer.items = items
        session.offers[user_id] = offer
        session.confirmed[user_id] = False
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
                await interaction.response.send_message("✅ Confirmed! Waiting for other party.", ephemeral=True); return
            session.done = True
            success, err = await execute_trade(session)
            trade_sessions.pop(session.session_key(), None)
        if success:
            embed = discord.Embed(title="✅ Trade Complete!", color=discord.Color.green())
            await session.message.edit(embed=embed, view=None)
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
        await session.message.edit(embed=discord.Embed(title="❌ Trade Cancelled", color=discord.Color.red()), view=None)
        await interaction.response.send_message("Trade cancelled.", ephemeral=True)

    async def on_timeout(self):
        if not self.session.done:
            self.session.done = True
            trade_sessions.pop(self.session.session_key(), None)
            if self.session.message:
                try: await self.session.message.edit(embed=discord.Embed(title="⏰ Trade Expired", color=discord.Color.light_grey()), view=None)
                except: pass

async def execute_trade(session) -> tuple[bool, str]:
    init_id, tgt_id = session.initiator_id, session.target_id
    for uid, offer in [(init_id, session.offers[init_id]), (tgt_id, session.offers[tgt_id])]:
        if offer.balance > 0 and await get_balance(uid) < offer.balance: return False, f"<@{uid}> no longer has enough coins."
        if offer.exp > 0 and await get_exp(uid) < offer.exp: return False, f"<@{uid}> no longer has enough EXP."
        if offer.tickets > 0 and await get_tickets(session.guild_id, uid) < offer.tickets: return False, f"<@{uid}> no longer has enough tickets."
        inv = {n.lower(): q for n, q in await inventory_get(uid)}
        for iname, qty in offer.items:
            if inv.get(iname.lower(), 0) < qty: return False, f"<@{uid}> no longer has {qty}x {iname}."

    init_o, tgt_o = session.offers[init_id], session.offers[tgt_id]
    if init_o.balance > 0: await add_balance(init_id, -init_o.balance); await add_balance(tgt_id, init_o.balance)
    if tgt_o.balance > 0: await add_balance(tgt_id, -tgt_o.balance); await add_balance(init_id, tgt_o.balance)
    if init_o.exp > 0: await add_spent_exp(init_id, init_o.exp); await add_exp(tgt_id, init_o.exp)
    if tgt_o.exp > 0: await add_spent_exp(tgt_id, tgt_o.exp); await add_exp(init_id, tgt_o.exp)
    if init_o.tickets > 0: await add_tickets(session.guild_id, init_id, -init_o.tickets); await add_tickets(session.guild_id, tgt_id, init_o.tickets)
    if tgt_o.tickets > 0: await add_tickets(session.guild_id, tgt_id, -tgt_o.tickets); await add_tickets(session.guild_id, init_id, tgt_o.tickets)
    for iname, qty in init_o.items: await inventory_remove(init_id, iname, qty); await inventory_add(tgt_id, iname, qty)
    for iname, qty in tgt_o.items: await inventory_remove(tgt_id, iname, qty); await inventory_add(init_id, iname, qty)
    return True, ""

@bot.tree.command(name="trade", description="Initiate a trade with another user")
@command_enabled()
async def trade(interaction: discord.Interaction, user: discord.Member):
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't trade with yourself.", ephemeral=True); return
    if user.bot:
        await interaction.response.send_message("❌ You can't trade with a bot.", ephemeral=True); return
    key = (interaction.guild.id, frozenset({interaction.user.id, user.id}))
    if key in trade_sessions:
        await interaction.response.send_message("❌ A trade is already in progress.", ephemeral=True); return
    session = TradeSession(interaction.guild.id, interaction.user.id, user.id)
    trade_sessions[key] = session
    await interaction.response.send_message(
        f"🤝 {interaction.user.mention} wants to trade with {user.mention}!\nClick **Set Offer** to enter what you're offering (coins, EXP, tickets, items/boxes), then **Confirm**.",
        embed=session.build_embed(interaction.guild), view=TradeView(session)
    )
    session.message = await interaction.original_response()

# ---------------- ADMIN ABUSE BOX SYSTEM ---------------- #

@bot.tree.command(name="addbox", description="Create a new admin abuse box")
@command_enabled()
async def addbox(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO abuse_boxes VALUES (?, ?)", (interaction.guild.id, name))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(f"❌ Box **{name}** already exists.", ephemeral=True); return
    await interaction.response.send_message(f"✅ Created box **{name}**.")

@bot.tree.command(name="removebox", description="Delete an admin abuse box and all its prizes")
@command_enabled()
async def removebox(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id = ? AND box_name = ?", (interaction.guild.id, name)) as cursor:
                if not await cursor.fetchone():
                    await interaction.response.send_message(f"❌ Box **{name}** not found.", ephemeral=True); return
            await db.execute("DELETE FROM abuse_boxes WHERE guild_id = ? AND box_name = ?", (interaction.guild.id, name))
            await db.execute("DELETE FROM abuse_box_prizes WHERE guild_id = ? AND box_name = ?", (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed box **{name}** and all its prizes.")

@bot.tree.command(name="addboxprize", description="Add a prize to an abuse box")
@app_commands.describe(box="Box name", prize_type="Type of prize", chance="Weight (e.g. 50)", amount="Amount for balance/exp prizes", item_name="Item name for 'item' prizes", custom_label="Label for 'nothing'/'custom'")
@app_commands.choices(prize_type=[
    app_commands.Choice(name="Balance", value="balance"), app_commands.Choice(name="EXP", value="exp"),
    app_commands.Choice(name="Item", value="item"), app_commands.Choice(name="Nothing", value="nothing"),
    app_commands.Choice(name="Custom", value="custom"),
])
@command_enabled()
async def addboxprize(interaction: discord.Interaction, box: str, prize_type: str, chance: int, amount: int = 0, item_name: str = None, custom_label: str = None):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if chance <= 0:
        await interaction.response.send_message("❌ Chance must be > 0.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id = ? AND box_name = ?", (interaction.guild.id, box)) as cursor:
            if not await cursor.fetchone():
                await interaction.response.send_message(f"❌ Box **{box}** not found.", ephemeral=True); return
    if prize_type in ("balance", "exp"):
        if amount <= 0: await interaction.response.send_message(f"❌ Provide amount > 0.", ephemeral=True); return
        prize_value = str(amount)
    elif prize_type == "item":
        if not item_name: await interaction.response.send_message("❌ Provide item_name.", ephemeral=True); return
        item = await get_item(item_name)
        if not item: await interaction.response.send_message(f"❌ Item **{item_name}** not found.", ephemeral=True); return
        prize_value = item[0]
    elif prize_type == "nothing": prize_value = custom_label or "Nothing"
    else:
        if not custom_label: await interaction.response.send_message("❌ Provide custom_label.", ephemeral=True); return
        prize_value = custom_label
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO abuse_box_prizes (guild_id, box_name, prize_type, prize_value, prize_amount, chance) VALUES (?, ?, ?, ?, ?, ?)", (interaction.guild.id, box, prize_type, prize_value, amount, chance))
            await db.commit()
    await interaction.response.send_message(f"✅ Added to **{box}**: `{prize_type}` — **{prize_value}** (weight: {chance})")

@bot.tree.command(name="removeboxprize", description="Remove a prize from a box by ID (see /listboxes)")
@command_enabled()
async def removeboxprize(interaction: discord.Interaction, box: str, prize_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT id FROM abuse_box_prizes WHERE id = ? AND guild_id = ? AND box_name = ?", (prize_id, interaction.guild.id, box)) as cursor:
                if not await cursor.fetchone():
                    await interaction.response.send_message(f"❌ Prize #{prize_id} not found in **{box}**.", ephemeral=True); return
            await db.execute("DELETE FROM abuse_box_prizes WHERE id = ?", (prize_id,))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed prize #{prize_id} from **{box}**.")

@bot.tree.command(name="listboxes", description="List all abuse boxes and their prizes")
@command_enabled()
async def listboxes(interaction: discord.Interaction, box: str = None):
    async with get_db() as db:
        query = "SELECT box_name FROM abuse_boxes WHERE guild_id = ?" + (" AND box_name = ?" if box else "")
        params = (interaction.guild.id, box) if box else (interaction.guild.id,)
        async with db.execute(query, params) as cursor:
            boxes = await cursor.fetchall()
    if not boxes: await interaction.response.send_message("❌ No boxes found."); return
    embed = discord.Embed(title="📦 Admin Abuse Boxes", color=discord.Color.orange())
    for (box_name,) in boxes:
        async with get_db() as db:
            async with db.execute("SELECT id, prize_type, prize_value, chance FROM abuse_box_prizes WHERE guild_id = ? AND box_name = ? ORDER BY id", (interaction.guild.id, box_name)) as cursor:
                prizes = await cursor.fetchall()
        if not prizes: embed.add_field(name=f"📦 {box_name}", value="*No prizes yet*", inline=False); continue
        total_weight = sum(p[3] for p in prizes)
        lines = []
        for p_id, p_type, p_value, p_chance in prizes:
            pct = (p_chance / total_weight * 100) if total_weight > 0 else 0
            if p_type == "balance": desc = f"💰 {int(p_value):,} coins"
            elif p_type == "exp": desc = f"⭐ {int(p_value):,} EXP"
            elif p_type == "item": desc = f"🎒 {p_value}"
            else: desc = f"✨ {p_value}"
            lines.append(f"`#{p_id}` {desc} — **{pct:.1f}%** (weight: {p_chance})")
        embed.add_field(name=f"📦 {box_name}", value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="givebox", description="Give an abuse box to all members with a specific role")
@app_commands.describe(box="Box name", role="Role whose members receive the box", amount="How many boxes each member receives (default 1)")
@command_enabled()
async def givebox(interaction: discord.Interaction, box: str, role: discord.Role, amount: int = 1):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if amount <= 0: await interaction.response.send_message("❌ Amount must be ≥ 1.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id = ? AND box_name = ?", (interaction.guild.id, box)) as cursor:
            if not await cursor.fetchone(): await interaction.response.send_message(f"❌ Box **{box}** not found.", ephemeral=True); return
    members = [m for m in interaction.guild.members if role in m.roles and not m.bot]
    if not members: await interaction.response.send_message(f"❌ No members with {role.mention}.", ephemeral=True); return
    await interaction.response.defer()
    for member in members: await inventory_add(member.id, box, amount)
    await interaction.followup.send(f"✅ Gave **{amount}x {box}** to **{len(members)}** member(s) with {role.mention}.")

@bot.tree.command(name="openbox", description="Open one or more abuse boxes from your inventory")
@app_commands.describe(box="Box name", amount="How many to open at once (default 1)")
@command_enabled()
async def openbox(interaction: discord.Interaction, box: str, amount: int = 1):
    await interaction.response.defer()
    if amount <= 0: await interaction.followup.send("❌ Amount must be ≥ 1."); return
    if amount > 20: await interaction.followup.send("❌ Max 20 boxes at once."); return

    inv = await inventory_get(interaction.user.id)
    owned = {name.lower(): (name, qty) for name, qty in inv}
    if box.lower() not in owned or owned[box.lower()][1] < amount:
        have = owned.get(box.lower(), (box, 0))[1]
        await interaction.followup.send(f"❌ You need {amount}x **{box}** but only have {have}."); return

    canonical_box = owned[box.lower()][0]
    async with get_db() as db:
        async with db.execute("SELECT box_name FROM abuse_boxes WHERE guild_id = ? AND box_name = ?", (interaction.guild.id, canonical_box)) as cursor:
            if not await cursor.fetchone():
                await interaction.followup.send(f"❌ Box **{canonical_box}** no longer exists on this server."); return
        async with db.execute("SELECT prize_type, prize_value, prize_amount, chance FROM abuse_box_prizes WHERE guild_id = ? AND box_name = ?", (interaction.guild.id, canonical_box)) as cursor:
            prizes = await cursor.fetchall()
    if not prizes: await interaction.followup.send(f"❌ **{canonical_box}** has no prizes configured."); return

    removed = await inventory_remove(interaction.user.id, canonical_box, amount)
    if not removed: await interaction.followup.send("❌ Failed to remove boxes."); return

    results: dict = {}
    total_balance = 0
    total_exp = 0
    item_grants: dict = {}

    for _ in range(amount):
        prize_type, prize_value, prize_amount, _ = random.choices(prizes, weights=[p[3] for p in prizes], k=1)[0]
        if prize_type == "balance":
            amt = int(prize_value)
            total_balance += amt
            key_r = f"💰 {amt:,} coins"
        elif prize_type == "exp":
            amt = int(prize_value)
            total_exp += amt
            key_r = f"⭐ {amt:,} EXP"
        elif prize_type == "item":
            item_grants[prize_value] = item_grants.get(prize_value, 0) + 1
            key_r = f"🎒 {prize_value}"
        elif prize_type == "nothing":
            key_r = f"😔 {prize_value}"
        else:
            key_r = f"✨ {prize_value}"
        results[key_r] = results.get(key_r, 0) + 1

    if total_balance > 0: await add_balance(interaction.user.id, total_balance)
    if total_exp > 0: await add_exp(interaction.user.id, total_exp)
    for iname, qty in item_grants.items():
        item = await get_item(iname)
        await inventory_add(interaction.user.id, item[0] if item else iname, qty)

    result_text = "\n".join(f"• {count}x {desc}" for desc, count in results.items())
    embed = discord.Embed(
        title=f"📦 {canonical_box} × {amount}",
        description=result_text,
        color=discord.Color.orange()
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

# ---------------- RANDOM GAMES SYSTEM ---------------- #

@bot.tree.command(name="addgame", description="Add a random game to the pool")
@app_commands.describe(name="The question shown to players", reward_balance="Coin reward for winner", reward_exp="EXP reward for winner")
@command_enabled()
async def addgame(interaction: discord.Interaction, name: str, reward_balance: int = 0, reward_exp: int = 0):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            try:
                await db.execute("INSERT INTO games (guild_id, game_name, reward_balance, reward_exp) VALUES (?, ?, ?, ?)", (interaction.guild.id, name, reward_balance, reward_exp))
                await db.commit()
            except aiosqlite.IntegrityError:
                await interaction.response.send_message(f"❌ Game **{name}** already exists.", ephemeral=True); return
    await interaction.response.send_message(f"✅ Added game **{name}** (💰 {reward_balance:,} + ⭐ {reward_exp:,} EXP).\nUse `/addgameanswer` to add valid answers.")

@bot.tree.command(name="removegame", description="Remove a game and all its answers")
@command_enabled()
async def removegame(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id = ? AND game_name = ?", (interaction.guild.id, name)) as cursor:
                if not await cursor.fetchone():
                    await interaction.response.send_message(f"❌ Game **{name}** not found.", ephemeral=True); return
            await db.execute("DELETE FROM games WHERE guild_id = ? AND game_name = ?", (interaction.guild.id, name))
            await db.execute("DELETE FROM game_answers WHERE guild_id = ? AND game_name = ?", (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed game **{name}**.")

@bot.tree.command(name="enablegame", description="Enable a game so it appears in automatic games")
@command_enabled()
async def enablegame(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id = ? AND game_name = ?", (interaction.guild.id, name)) as cursor:
                if not await cursor.fetchone():
                    await interaction.response.send_message(f"❌ Game **{name}** not found.", ephemeral=True); return
            await db.execute("UPDATE games SET enabled = 1 WHERE guild_id = ? AND game_name = ?", (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"✅ Game **{name}** is now **enabled**.")

@bot.tree.command(name="disablegame", description="Disable a game without deleting it")
@command_enabled()
async def disablegame(interaction: discord.Interaction, name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT game_name FROM games WHERE guild_id = ? AND game_name = ?", (interaction.guild.id, name)) as cursor:
                if not await cursor.fetchone():
                    await interaction.response.send_message(f"❌ Game **{name}** not found.", ephemeral=True); return
            await db.execute("UPDATE games SET enabled = 0 WHERE guild_id = ? AND game_name = ?", (interaction.guild.id, name))
            await db.commit()
    await interaction.response.send_message(f"🔒 Game **{name}** is now **disabled** (not deleted).")

@bot.tree.command(name="addgameanswer", description="Add a valid answer to a game")
@command_enabled()
async def addgameanswer(interaction: discord.Interaction, game_name: str, answer: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT game_name FROM games WHERE guild_id = ? AND game_name = ?", (interaction.guild.id, game_name)) as cursor:
            if not await cursor.fetchone():
                await interaction.response.send_message(f"❌ Game **{game_name}** not found.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT INTO game_answers (guild_id, game_name, answer) VALUES (?, ?, ?)", (interaction.guild.id, game_name, answer))
            await db.commit()
    await interaction.response.send_message(f"✅ Added answer `{answer}` to **{game_name}**.")

@bot.tree.command(name="removegameanswer", description="Remove an answer from a game by its ID (see /listgames)")
@command_enabled()
async def removegameanswer(interaction: discord.Interaction, game_name: str, answer_id: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            async with db.execute("SELECT id FROM game_answers WHERE id = ? AND guild_id = ? AND game_name = ?", (answer_id, interaction.guild.id, game_name)) as cursor:
                if not await cursor.fetchone():
                    await interaction.response.send_message(f"❌ Answer #{answer_id} not found in **{game_name}**.", ephemeral=True); return
            await db.execute("DELETE FROM game_answers WHERE id = ?", (answer_id,))
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed answer #{answer_id} from **{game_name}**.")

@bot.tree.command(name="listgames", description="List all games and their answers")
@command_enabled()
async def listgames(interaction: discord.Interaction, game_name: str = None):
    async with get_db() as db:
        if game_name:
            async with db.execute("SELECT game_name, enabled, reward_balance, reward_exp FROM games WHERE guild_id = ? AND game_name = ?", (interaction.guild.id, game_name)) as cursor:
                games = await cursor.fetchall()
        else:
            async with db.execute("SELECT game_name, enabled, reward_balance, reward_exp FROM games WHERE guild_id = ?", (interaction.guild.id,)) as cursor:
                games = await cursor.fetchall()
    if not games: await interaction.response.send_message("❌ No games configured."); return
    embed = discord.Embed(title="🎮 Random Games", color=discord.Color.teal())
    for (gname, enabled, reward_bal, reward_exp) in games:
        async with get_db() as db:
            async with db.execute("SELECT id, answer FROM game_answers WHERE guild_id = ? AND game_name = ? ORDER BY id", (interaction.guild.id, gname)) as cursor:
                answers = await cursor.fetchall()
        status = "✅ Enabled" if enabled else "🔒 Disabled"
        reward_parts = []
        if reward_bal > 0: reward_parts.append(f"💰 {reward_bal:,}")
        if reward_exp > 0: reward_parts.append(f"⭐ {reward_exp:,}")
        reward_str = " + ".join(reward_parts) if reward_parts else "No reward"
        answer_lines = "\n".join(f"  `#{aid}` {ans}" for aid, ans in answers) if answers else "  *No answers yet*"
        embed.add_field(name=f"🎯 {gname} [{status}]", value=f"Reward: {reward_str}\nAnswers:\n{answer_lines}", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setgamechannel", description="Set the channel for random games and configure timing")
@app_commands.describe(channel="Channel where games will be posted", answer_time="Seconds to answer (default 30)", interval_seconds="Seconds between games (default 60)")
@command_enabled()
async def setgamechannel(interaction: discord.Interaction, channel: discord.TextChannel, answer_time: int = 30, interval_seconds: int = 60):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    if answer_time < 5: await interaction.response.send_message("❌ Answer time must be ≥ 5 seconds.", ephemeral=True); return
    if interval_seconds < 10: await interaction.response.send_message("❌ Interval must be ≥ 10 seconds.", ephemeral=True); return
    async with db_lock:
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO game_config (guild_id, channel_id, answer_time, interval_seconds) VALUES (?, ?, ?, ?)", (interaction.guild.id, channel.id, answer_time, interval_seconds))
            await db.commit()
    await interaction.response.send_message(f"✅ Game channel: {channel.mention} | Answer time: **{answer_time}s** | Interval: **{interval_seconds}s**")

@bot.tree.command(name="startgames", description="Start automatic random games")
@command_enabled()
async def startgames(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    guild_id = interaction.guild.id
    if guild_id in game_tasks and not game_tasks[guild_id].done():
        await interaction.response.send_message("❌ Games are already running.", ephemeral=True); return
    async with get_db() as db:
        async with db.execute("SELECT channel_id FROM game_config WHERE guild_id = ?", (guild_id,)) as cursor:
            if not await cursor.fetchone():
                await interaction.response.send_message("❌ Use `/setgamechannel` first.", ephemeral=True); return
        async with db.execute("SELECT game_name FROM games WHERE guild_id = ? AND enabled = 1", (guild_id,)) as cursor:
            if not await cursor.fetchall():
                await interaction.response.send_message("❌ No enabled games. Add some with `/addgame`.", ephemeral=True); return
    game_tasks[guild_id] = asyncio.create_task(guild_game_loop(guild_id))
    await interaction.response.send_message("🎮 Random games started!")

@bot.tree.command(name="stopgames", description="Stop automatic random games")
@command_enabled()
async def stopgames(interaction: discord.Interaction):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True); return
    guild_id = interaction.guild.id
    task = game_tasks.pop(guild_id, None)
    if task: task.cancel()
    active_game_sessions.pop(guild_id, None)
    await interaction.response.send_message("🛑 Random games stopped.")

async def guild_game_loop(guild_id: int):
    await bot.wait_until_ready()
    while not bot.is_closed():
        async with get_db() as db:
            async with db.execute("SELECT channel_id, answer_time, interval_seconds FROM game_config WHERE guild_id = ?", (guild_id,)) as cursor:
                config = await cursor.fetchone()
        if not config: break
        channel_id, answer_time, interval_seconds = config
        channel = bot.get_channel(channel_id)
        if not channel: await asyncio.sleep(30); continue

        # Pick a random enabled game that has answers
        async with get_db() as db:
            async with db.execute("SELECT game_name, reward_balance, reward_exp FROM games WHERE guild_id = ? AND enabled = 1", (guild_id,)) as cursor:
                games = await cursor.fetchall()
        eligible = []
        for gname, reward_bal, reward_exp in games:
            async with get_db() as db:
                async with db.execute("SELECT answer FROM game_answers WHERE guild_id = ? AND game_name = ?", (guild_id, gname)) as cursor:
                    answers = [row[0] for row in await cursor.fetchall()]
            if answers: eligible.append((gname, reward_bal, reward_exp, answers))

        if not eligible: await asyncio.sleep(interval_seconds); continue

        game_name, reward_bal, reward_exp, answers = random.choice(eligible)
        correct_answer = random.choice(answers)

        reward_parts = []
        if reward_bal > 0: reward_parts.append(f"💰 {reward_bal:,} coins")
        if reward_exp > 0: reward_parts.append(f"⭐ {reward_exp:,} EXP")

        embed = discord.Embed(title="🎮 Random Game!", color=discord.Color.teal(),
            description=f"**{game_name}**\n\nType your answer in chat!\n⏰ You have **{answer_time} seconds**.")
        if reward_parts: embed.add_field(name="🏆 Winner gets", value=" + ".join(reward_parts), inline=False)
        embed.set_footer(text=f"Answer within {answer_time} seconds!")
        await channel.send(embed=embed)

        active_game_sessions[guild_id] = {
            "game_name": game_name, "answer": correct_answer,
            "channel_id": channel_id, "answered": False, "winner": None,
        }

        await asyncio.sleep(answer_time)
        session = active_game_sessions.pop(guild_id, None)
        if not session: await asyncio.sleep(max(0, interval_seconds - answer_time)); continue

        if session.get("answered") and session.get("winner"):
            winner = session["winner"]
            if reward_bal > 0: await add_balance(winner.id, reward_bal)
            if reward_exp > 0: await add_exp(winner.id, reward_exp)
            result_embed = discord.Embed(title="🎉 Correct!", color=discord.Color.green(),
                description=f"{winner.mention} got it right! The answer was **{correct_answer}**.")
            if reward_parts: result_embed.add_field(name="Reward given", value=" + ".join(reward_parts), inline=False)
        else:
            result_embed = discord.Embed(title="⏰ Time's Up!", color=discord.Color.red(),
                description=f"Nobody guessed correctly. The answer was **{correct_answer}**.")
        await channel.send(embed=result_embed)

        await asyncio.sleep(max(0, interval_seconds - answer_time))

async def game_loop():
    """Master watchdog — restarts guild loops that crashed."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild_id, task in list(game_tasks.items()):
            if task.done():
                try:
                    if exc := task.exception():
                        print(f"[GameLoop] Guild {guild_id} died: {exc}")
                except Exception: pass
                game_tasks[guild_id] = asyncio.create_task(guild_game_loop(guild_id))
        await asyncio.sleep(30)

# ---------------- RUN BOT ---------------- #

bot.run(TOKEN)

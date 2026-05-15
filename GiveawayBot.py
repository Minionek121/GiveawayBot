import os
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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS raffle_config (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS rare_drop_config (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS raffle_info_config (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    message_id INTEGER
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS exp_boosts (
                    guild_id INTEGER,
                    role_id INTEGER,
                    boost_percent INTEGER,
                    PRIMARY KEY (guild_id, role_id)
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
            # Inventory: one row per (user, item), quantity tracked
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory (
                    user_id INTEGER,
                    item_name TEXT,
                    quantity INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, item_name)
                )
                """
            )
            await db.commit()

# ---------------- TEMPLATES ---------------- #

TEMPLATES = {
    "gold": discord.Color.gold(),
    "red": discord.Color.red(),
    "blue": discord.Color.blue(),
    "green": discord.Color.green(),
}

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
    now = datetime.now().timestamp()
    last_time = last_message_exp.get(message.author.id, 0)
    if now - last_time >= 30:
        content_length = len(message.content.strip())
        exp_gain = 30
        bonus = min(20, content_length // 10)
        randomness = random.randint(0, max(1, bonus))
        gained = min(50, exp_gain + randomness)

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
                    best_boost = max(row[0] for row in boost_rows)
                    gained = int(gained * (1 + best_boost / 100))

        await add_exp(message.author.id, gained)
        last_message_exp[message.author.id] = now
    await bot.process_commands(message)

# ---------------- READY EVENT ---------------- #

@bot.event
async def on_ready():
    await setup_database()

    # ----------------------------------------------------------------
    # ONE-TIME DUPLICATE FIX:
    # If commands show twice, paste your guild ID below, deploy once,
    # then delete these 4 lines and deploy again.
    guild_to_clear = discord.Object(id=1494356360241090661)
    bot.tree.clear_commands(guild=guild_to_clear)
    await bot.tree.sync(guild=guild_to_clear)
    print("Cleared guild-scoped commands.")
    # ----------------------------------------------------------------

    try:
        synced = await bot.tree.sync(guild=None)
        print(f"Synced {len(synced)} commands globally")
    except Exception as e:
        print(e)
    print(f"Logged in as {bot.user}")
    bot.loop.create_task(raffle_loop())
    bot.loop.create_task(giveaway_watcher())
    bot.loop.create_task(raffle_info_loop())

# ---------------- CREATE GIVEAWAY ---------------- #

@bot.tree.command(name="giveaway", description="Create a giveaway")
@app_commands.describe(
    prize="Prize name",
    minutes="Duration in minutes",
    winners="Number of winners",
    reward="Balance reward",
    channel="Channel to post the giveaway in (defaults to current channel)",
    required_role="Required role to enter",
    template="Template color"
)
@command_enabled()
async def giveaway(
    interaction: discord.Interaction,
    prize: str,
    minutes: int,
    winners: int,
    reward: int,
    channel: discord.TextChannel = None,
    required_role: discord.Role = None,
    template: str = "gold"
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    target_channel = channel or interaction.channel
    end_time = datetime.now(UTC) + timedelta(minutes=minutes)

    embed = discord.Embed(
        title="🎉 GIVEAWAY 🎉",
        description=(
            f"React with 🎉 to enter\n\n"
            f"Prize: **{prize}**\n"
            f"Winners: **{winners}**\n"
            f"Reward: **{reward} coins**\n"
            f"Ends: <t:{int(end_time.timestamp())}:R>"
        ),
        color=TEMPLATES.get(template, discord.Color.gold())
    )
    if required_role:
        embed.add_field(name="Required Role", value=required_role.mention, inline=False)

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
                int(end_time.timestamp()),
                required_role.id if required_role else 0,
                template, 0
            )
        )
        await db.commit()

    await interaction.response.send_message("✅ Giveaway created.", ephemeral=True)
    asyncio.create_task(giveaway_timer(message.id, minutes * 60))

# ---------------- END GIVEAWAY ---------------- #

async def end_giveaway(message_id, reroll=False):
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                """
                SELECT message_id, channel_id, prize, winners, reward,
                       end_time, required_role, template, ended
                FROM giveaways WHERE message_id = ?
                """,
                (message_id,)
            ) as cursor:
                data = await cursor.fetchone()
            if not data:
                print(f"[Giveaway] No giveaway found for {message_id}")
                return
            (
                message_id, channel_id, prize, winner_count, reward,
                end_time, required_role, template, ended
            ) = data
            if ended and not reroll:
                print(f"[Giveaway] Already ended: {message_id}")
                return
            if not reroll:
                await db.execute(
                    "UPDATE giveaways SET ended = 1 WHERE message_id = ?", (message_id,)
                )
                await db.commit()

    channel = bot.get_channel(channel_id)
    if channel is None:
        print(f"[Giveaway] Channel not found: {channel_id}")
        return
    try:
        message = await channel.fetch_message(message_id)
    except Exception as e:
        print(f"[Giveaway] Failed to fetch message {message_id}: {e}")
        return

    reaction = next((r for r in message.reactions if str(r.emoji) == "🎉"), None)
    if reaction is None:
        await channel.send("❌ Giveaway reaction was missing.")
        return

    users = []
    async for user in reaction.users():
        if user.bot:
            continue
        member = channel.guild.get_member(user.id)
        if member is None:
            continue
        if required_role:
            if required_role not in {role.id for role in member.roles}:
                continue
        users.append(user)

    if not users:
        await channel.send("No valid participants.")
        return

    weighted_users = []
    for user in users:
        level = await get_level(user.id)
        weight = random.randint(1, min(100, max(1, level)))
        weighted_users.extend([user] * weight)

    winners = []
    random.shuffle(weighted_users)
    while len(winners) < min(winner_count, len(users)) and weighted_users:
        selected = random.choice(weighted_users)
        if selected not in winners:
            winners.append(selected)

    winner_mentions = []
    async with db_lock:
        async with get_db() as db:
            if reroll:
                async with db.execute(
                    "SELECT winner_id, reward FROM giveaway_winners WHERE message_id = ?",
                    (message_id,)
                ) as cursor:
                    old_data = await cursor.fetchone()
                if old_data:
                    old_winner_id, old_reward = old_data
                    await db.execute(
                        "INSERT OR IGNORE INTO balances VALUES (?, 0)", (old_winner_id,)
                    )
                    await db.execute(
                        "UPDATE balances SET balance = MAX(0, balance - ?) WHERE user_id = ?",
                        (old_reward, old_winner_id)
                    )
            for winner in winners:
                await db.execute(
                    "INSERT OR IGNORE INTO balances VALUES (?, 0)", (winner.id,)
                )
                await db.execute(
                    "UPDATE balances SET balance = balance + ? WHERE user_id = ?",
                    (reward, winner.id)
                )
                await db.execute(
                    "INSERT OR REPLACE INTO giveaway_winners VALUES (?, ?, ?)",
                    (message_id, winner.id, reward)
                )
                winner_mentions.append(winner.mention)
            await db.commit()

    embed = discord.Embed(
        title="🎊 Giveaway Ended",
        description=(
            f"Prize: **{prize}**\n"
            f"Reward: **{reward} coins**\n"
            f"Winners: {', '.join(winner_mentions)}"
        ),
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

@bot.tree.command(name="buytickets", description="Buy raffle tickets for 100 balance each!")
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

# ---------------- RAFFLE LOOP ---------------- #

async def raffle_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.now(UTC)
        target = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if now >= target:
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

CHEST_PRIZES = [
    {"name": "250 EXP",     "exp": 250,   "balance": 0,     "chance": 40},
    {"name": "450 EXP",     "exp": 450,   "balance": 0,     "chance": 30},
    {"name": "1k EXP",      "exp": 1000,  "balance": 0,     "chance": 6},
    {"name": "1k Balance",  "exp": 0,     "balance": 1000,  "chance": 15},
    {"name": "1 Huge",      "exp": 0,     "balance": 0,     "chance": 4},
    {"name": "25m Gems",    "exp": 0,     "balance": 0,     "chance": 4},
    {"name": "40k Balance", "exp": 0,     "balance": 40000, "chance": 1},
]

RARE_CHEST_PRIZES = {"1 Huge", "25m Gems", "40k Balance"}

async def get_rare_drop_channel(guild_id: int) -> int | None:
    async with get_db() as db:
        async with db.execute(
            "SELECT channel_id FROM rare_drop_config WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None

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
    if exp >= 20000:
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

    rare_items_won = {name: count for name, count in results.items() if name in RARE_CHEST_PRIZES}
    if rare_items_won:
        rare_channel_id = await get_rare_drop_channel(interaction.guild.id)
        if rare_channel_id:
            rare_channel = bot.get_channel(rare_channel_id)
            if rare_channel:
                prizes_text = " and ".join(
                    f"**{count}x {name}**" for name, count in rare_items_won.items()
                )
                rare_embed = discord.Embed(
                    title="🌟 Rare Drop!",
                    description=f"{interaction.user.mention} just got {prizes_text} from a chest! 🎉",
                    color=discord.Color.gold()
                )
                rare_embed.set_thumbnail(url=interaction.user.display_avatar.url)
                await rare_channel.send(embed=rare_embed)

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
    """Add quantity of an item to a user's inventory."""
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO inventory (user_id, item_name, quantity)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, item_name)
                DO UPDATE SET quantity = quantity + excluded.quantity
                """,
                (user_id, item_name, quantity)
            )
            await db.commit()

async def inventory_remove(user_id: int, item_name: str, quantity: int = 1) -> bool:
    """
    Remove quantity of an item from a user's inventory.
    Returns True on success, False if they don't have enough.
    """
    async with db_lock:
        async with get_db() as db:
            async with db.execute(
                "SELECT quantity FROM inventory WHERE user_id = ? AND item_name = ?",
                (user_id, item_name)
            ) as cursor:
                row = await cursor.fetchone()
            if not row or row[0] < quantity:
                return False
            new_qty = row[0] - quantity
            if new_qty == 0:
                await db.execute(
                    "DELETE FROM inventory WHERE user_id = ? AND item_name = ?",
                    (user_id, item_name)
                )
            else:
                await db.execute(
                    "UPDATE inventory SET quantity = ? WHERE user_id = ? AND item_name = ?",
                    (new_qty, user_id, item_name)
                )
            await db.commit()
    return True

async def inventory_get(user_id: int) -> list[tuple[str, int]]:
    """Return [(item_name, quantity), ...] for a user, ordered by name."""
    async with get_db() as db:
        async with db.execute(
            "SELECT item_name, quantity FROM inventory WHERE user_id = ? ORDER BY item_name",
            (user_id,)
        ) as cursor:
            return await cursor.fetchall()

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

@item_group.command(name="info", description="View item info")
@command_enabled()
async def item_info(interaction: discord.Interaction, name: str):
    item = await get_item(name)
    if not item:
        await interaction.response.send_message("❌ Item not found.")
        return
    item_name, price, role_id, description = item
    role = interaction.guild.get_role(role_id)
    embed = discord.Embed(title=f"🛒 {item_name}", color=discord.Color.blurple())
    embed.add_field(name="Price", value=f"{price:,} coins", inline=False)
    embed.add_field(name="Role", value=role.mention if role else "Unknown Role", inline=False)
    embed.add_field(name="Description", value=description, inline=False)
    await interaction.response.send_message(embed=embed)

@item_group.command(name="store", description="View item store")
@command_enabled()
async def item_store_cmd(interaction: discord.Interaction):
    items = await get_all_items()
    if not items:
        await interaction.response.send_message("❌ Store is empty.")
        return
    embed = discord.Embed(title="🛒 Item Store", color=discord.Color.green())
    for item_name, price, role_id, description in items:
        role = interaction.guild.get_role(role_id)
        embed.add_field(
            name=item_name,
            value=f"💰 {price:,} coins\n🎭 {role.mention if role else 'Unknown Role'}",
            inline=False
        )
    await interaction.response.send_message(embed=embed)

@item_group.command(name="buy", description="Buy an item — it goes to your inventory")
@command_enabled()
async def item_buy(interaction: discord.Interaction, name: str):
    item = await get_item(name)
    if not item:
        await interaction.response.send_message("❌ Item not found.")
        return
    item_name, price, role_id, description = item

    balance = await get_balance(interaction.user.id)
    if balance < price:
        await interaction.response.send_message("❌ Not enough balance.")
        return

    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.response.send_message("❌ Role no longer exists.")
        return

    # Deduct balance and add to inventory — role is granted on /item use
    await add_balance(interaction.user.id, -price)
    await inventory_add(interaction.user.id, item_name, 1)

    await interaction.response.send_message(
        f"✅ You bought **{item_name}** for {price:,} coins.\n"
        f"It's now in your inventory — use `/item use {item_name}` to redeem it!"
    )

@item_group.command(name="use", description="Use an item from your inventory to receive its role")
@command_enabled()
async def item_use(interaction: discord.Interaction, name: str):
    item = await get_item(name)
    if not item:
        await interaction.response.send_message("❌ Item not found in the store.")
        return
    item_name, price, role_id, description = item

    inv = await inventory_get(interaction.user.id)
    owned = {i[0].lower(): (i[0], i[1]) for i in inv}  # lower -> (real_name, qty)
    if item_name.lower() not in owned or owned[item_name.lower()][1] < 1:
        await interaction.response.send_message(
            f"❌ You don't have **{item_name}** in your inventory."
        )
        return

    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.response.send_message("❌ Role no longer exists.")
        return

    member = interaction.guild.get_member(interaction.user.id)
    if role in member.roles:
        await interaction.response.send_message(
            f"❌ You already have the **{role.name}** role."
        )
        return

    removed = await inventory_remove(interaction.user.id, item_name, 1)
    if not removed:
        await interaction.response.send_message("❌ Failed to remove item from inventory.")
        return

    await member.add_roles(role)
    await interaction.response.send_message(
        f"✅ Used **{item_name}** — you've been given the {role.mention} role!"
    )

@item_group.command(name="give", description="Give an item to a user (admin only)")
@app_commands.describe(user="Target user", name="Item name", quantity="How many to give (default 1)")
@command_enabled()
async def item_give(
    interaction: discord.Interaction,
    user: discord.Member,
    name: str,
    quantity: int = 1
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    if quantity <= 0:
        await interaction.response.send_message("❌ Quantity must be at least 1.", ephemeral=True)
        return
    item = await get_item(name)
    if not item:
        await interaction.response.send_message("❌ Item not found in the store.")
        return
    item_name = item[0]
    await inventory_add(user.id, item_name, quantity)
    await interaction.response.send_message(
        f"✅ Gave **{quantity}x {item_name}** to {user.mention}."
    )

@item_group.command(name="take", description="Take an item from a user (admin only)")
@app_commands.describe(user="Target user", name="Item name", quantity="How many to take (default 1)")
@command_enabled()
async def item_take(
    interaction: discord.Interaction,
    user: discord.Member,
    name: str,
    quantity: int = 1
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    if quantity <= 0:
        await interaction.response.send_message("❌ Quantity must be at least 1.", ephemeral=True)
        return
    item = await get_item(name)
    if not item:
        await interaction.response.send_message("❌ Item not found in the store.")
        return
    item_name = item[0]
    removed = await inventory_remove(user.id, item_name, quantity)
    if not removed:
        await interaction.response.send_message(
            f"❌ {user.mention} doesn't have {quantity}x **{item_name}**."
        )
        return
    await interaction.response.send_message(
        f"🗑 Took **{quantity}x {item_name}** from {user.mention}."
    )

@item_group.command(name="inv", description="Check a user's item inventory")
@app_commands.describe(user="User to check (defaults to yourself)")
@command_enabled()
async def item_inv(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    inv = await inventory_get(user.id)

    embed = discord.Embed(
        title=f"🎒 {user.display_name}'s Inventory",
        color=discord.Color.blurple()
    )

    if not inv:
        embed.description = "No items in inventory."
    else:
        lines = []
        for item_name, quantity in inv:
            store_item = await get_item(item_name)
            if store_item:
                _, _, role_id, _ = store_item
                role = interaction.guild.get_role(role_id)
                role_text = f" → {role.mention}" if role else ""
            else:
                role_text = ""
            lines.append(f"• **{item_name}** x{quantity}{role_text}")
        embed.description = "\n".join(lines)

    await interaction.response.send_message(embed=embed)

# ---------------- ENABLE / DISABLE COMMANDS ---------------- #

@bot.tree.command(name="disablecmd", description="Temporarily disable a command")
async def disablecmd(interaction: discord.Interaction, command_name: str):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
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

# ---------------- RARE DROP CHANNEL ---------------- #

@bot.tree.command(name="setraredropchannel", description="Set channel for rare chest drop announcements")
@command_enabled()
async def setraredropchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO rare_drop_config VALUES (?, ?)",
                (interaction.guild.id, channel.id)
            )
            await db.commit()
    prizes_listed = ", ".join(f"**{p}**" for p in sorted(RARE_CHEST_PRIZES))
    await interaction.response.send_message(
        f"✅ Rare drop announcements will be posted in {channel.mention}.\n"
        f"Watching for: {prizes_listed}"
    )

# ---------------- RAFFLE INFO CHANNEL ---------------- #

def build_raffle_info_embed(guild: discord.Guild, total_tickets: int, top_entries: list) -> discord.Embed:
    now = datetime.now(UTC)
    target = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    end_ts = int(target.timestamp())

    embed = discord.Embed(title="🎟 Live Raffle Status", color=discord.Color.gold())
    embed.add_field(
        name="⏰ Next Draw",
        value=f"<t:{end_ts}:R> (<t:{end_ts}:F>)",
        inline=False
    )
    embed.add_field(name="🎫 Total Tickets in Pool", value=f"{total_tickets:,}", inline=False)

    if top_entries:
        lines = []
        medals = ["🥇", "🥈", "🥉"]
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
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return

    async with get_db() as db:
        async with db.execute(
            "SELECT user_id, tickets FROM raffle WHERE guild_id = ? ORDER BY tickets DESC LIMIT 5",
            (interaction.guild.id,)
        ) as cursor:
            top_entries = await cursor.fetchall()
        async with db.execute(
            "SELECT SUM(tickets) FROM raffle WHERE guild_id = ?", (interaction.guild.id,)
        ) as cursor:
            total_row = await cursor.fetchone()
    total_tickets = total_row[0] or 0

    embed = build_raffle_info_embed(interaction.guild, total_tickets, top_entries)
    info_message = await channel.send(embed=embed)

    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO raffle_info_config VALUES (?, ?, ?)",
                (interaction.guild.id, channel.id, info_message.id)
            )
            await db.commit()

    await interaction.response.send_message(
        f"✅ Live raffle status board posted in {channel.mention} and will update automatically."
    )

async def raffle_info_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        async with get_db() as db:
            async with db.execute(
                "SELECT guild_id, channel_id, message_id FROM raffle_info_config"
            ) as cursor:
                configs = await cursor.fetchall()

        for guild_id, channel_id, message_id in configs:
            try:
                guild = bot.get_guild(guild_id)
                if not guild:
                    continue
                channel = bot.get_channel(channel_id)
                if not channel:
                    continue

                async with get_db() as db:
                    async with db.execute(
                        "SELECT user_id, tickets FROM raffle WHERE guild_id = ? ORDER BY tickets DESC LIMIT 5",
                        (guild_id,)
                    ) as cursor:
                        top_entries = await cursor.fetchall()
                    async with db.execute(
                        "SELECT SUM(tickets) FROM raffle WHERE guild_id = ?", (guild_id,)
                    ) as cursor:
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
                            await db.execute(
                                "UPDATE raffle_info_config SET message_id = ? WHERE guild_id = ?",
                                (new_msg.id, guild_id)
                            )
                            await db.commit()
            except Exception as e:
                print(f"[RaffleInfoLoop] guild={guild_id}: {e}")

        await asyncio.sleep(60)

# ---------------- EXP BOOST ---------------- #

@bot.tree.command(name="expboost", description="Set an EXP boost for a role (percentage)")
@app_commands.describe(
    role="Role to boost",
    boost="Boost percentage (e.g. 50 = +50% EXP per message)"
)
@command_enabled()
async def expboost(interaction: discord.Interaction, role: discord.Role, boost: int):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    if boost <= 0:
        await interaction.response.send_message("❌ Boost must be greater than 0%.", ephemeral=True)
        return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO exp_boosts VALUES (?, ?, ?)",
                (interaction.guild.id, role.id, boost)
            )
            await db.commit()
    await interaction.response.send_message(
        f"✅ Members with {role.mention} now earn **+{boost}% EXP** per message."
    )

@bot.tree.command(name="removeexpboost", description="Remove an EXP boost from a role")
@command_enabled()
async def removeexpboost(interaction: discord.Interaction, role: discord.Role):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return
    async with db_lock:
        async with get_db() as db:
            await db.execute(
                "DELETE FROM exp_boosts WHERE guild_id = ? AND role_id = ?",
                (interaction.guild.id, role.id)
            )
            await db.commit()
    await interaction.response.send_message(f"🗑 Removed EXP boost from {role.mention}.")

@bot.tree.command(name="listexpboosts", description="List all active EXP boosts")
@command_enabled()
async def listexpboosts(interaction: discord.Interaction):
    async with get_db() as db:
        async with db.execute(
            "SELECT role_id, boost_percent FROM exp_boosts WHERE guild_id = ? ORDER BY boost_percent DESC",
            (interaction.guild.id,)
        ) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        await interaction.response.send_message("❌ No EXP boosts configured.")
        return
    embed = discord.Embed(title="⚡ Active EXP Boosts", color=discord.Color.blurple())
    for role_id, boost in rows:
        role = interaction.guild.get_role(role_id)
        name = role.mention if role else f"<deleted role {role_id}>"
        embed.add_field(name=name, value=f"+{boost}%", inline=False)
    await interaction.response.send_message(embed=embed)

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
            async with db.execute("SELECT DISTINCT user_id FROM exp_history") as cursor:
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

# ---------------- RUN BOT ---------------- #

bot.run(TOKEN)

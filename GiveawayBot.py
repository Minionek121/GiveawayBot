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

DATABASE = "giveaways.db"

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

async def is_allowed_to_giveaway(
    interaction: discord.Interaction
) -> bool:

    member = interaction.user

    if not isinstance(member, discord.Member):
        return False

    # Server admins always allowed
    if member.guild_permissions.administrator:
        return True

    async with aiosqlite.connect(DATABASE) as db:

        async with db.execute(
            """
            SELECT role_id
            FROM giveaway_roles
            WHERE guild_id = ?
            """,
            (interaction.guild.id,)
        ) as cursor:

            rows = await cursor.fetchall()

    allowed_roles = {
        row[0]
        for row in rows
    }

    return any(
        role.id in allowed_roles
        for role in member.roles
    )

# ---------------- DATABASE ---------------- #

async def setup_database():

    async with aiosqlite.connect(DATABASE) as db:


        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaway_roles (
                guild_id INTEGER,
                role_id INTEGER
            )
            """
        )
        # Giveaways table
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

        # User balances
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER
            )
            """
        )

        # EXP HISTORY
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS exp_history (
                user_id INTEGER,
                amount INTEGER,
                timestamp INTEGER
            )
            """
        )

        # RAFFLE
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

        # GIVEAWAY WINNERS
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaway_winners (
                message_id INTEGER PRIMARY KEY,
                winner_id INTEGER,
                reward INTEGER
            )
            """
        )
        try:

            await db.execute(
                """
                ALTER TABLE giveaways
                ADD COLUMN ended INTEGER DEFAULT 0
                """
            )

        except:
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

        await db.commit()

# ---------------- TEMPLATES ---------------- #

TEMPLATES = {
    "gold": discord.Color.gold(),
    "red": discord.Color.red(),
    "blue": discord.Color.blue(),
    "green": discord.Color.green(),
}

# ---------------- GIVEAWAY ROLES ---------------- #

@bot.tree.command(
    name="addgiveawayrole",
    description="Allow a role to manage giveaways"
)
@app_commands.checks.has_permissions(
    administrator=True
)
async def addgiveawayrole(
    interaction: discord.Interaction,
    role: discord.Role
):

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute(
            """
            INSERT INTO giveaway_roles
            VALUES (?, ?)
            """,
            (
                interaction.guild.id,
                role.id
            )
        )

        await db.commit()

    await interaction.response.send_message(
        f"✅ {role.mention} can now manage giveaways."
    )

@bot.tree.command(
    name="removegiveawayrole",
    description="Remove giveaway permissions from a role"
)
@app_commands.checks.has_permissions(
    administrator=True
)
async def removegiveawayrole(
    interaction: discord.Interaction,
    role: discord.Role
):

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute(
            """
            DELETE FROM giveaway_roles
            WHERE guild_id = ?
            AND role_id = ?
            """,
            (
                interaction.guild.id,
                role.id
            )
        )

        await db.commit()

    await interaction.response.send_message(
        f"🗑 Removed giveaway permissions from {role.mention}"
    )

@bot.tree.command(
    name="giveawayroles",
    description="View giveaway manager roles"
)
async def giveawayroles(
    interaction: discord.Interaction
):

    async with aiosqlite.connect(DATABASE) as db:

        async with db.execute(
            """
            SELECT role_id
            FROM giveaway_roles
            WHERE guild_id = ?
            """,
            (interaction.guild.id,)
        ) as cursor:

            rows = await cursor.fetchall()

    if not rows:

        await interaction.response.send_message(
            "❌ No giveaway roles configured."
        )

        return

    mentions = []

    for row in rows:

        role = interaction.guild.get_role(row[0])

        if role:
            mentions.append(role.mention)

    await interaction.response.send_message(
        "🎉 Giveaway Roles:\n" +
        "\n".join(mentions)
    )

# ---------------- BALANCE FUNCTIONS ---------------- #

async def get_balance(user_id):

    async with aiosqlite.connect(DATABASE) as db:

        async with db.execute(
            "SELECT balance FROM balances WHERE user_id = ?",
            (user_id,)
        ) as cursor:

            data = await cursor.fetchone()

        if data is None:

            await db.execute(
                "INSERT INTO balances VALUES (?, ?)",
                (user_id, 0)
            )

            await db.commit()

            return 0

        return data[0]

async def add_balance(user_id, amount):

    balance = await get_balance(user_id)

    new_balance = max(0, balance + amount)

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute(
            """
            UPDATE balances
            SET balance = ?
            WHERE user_id = ?
            """,
            (new_balance, user_id)
        )

        await db.commit()

@bot.tree.command(
    name="gift",
    description="Gift balance to another user"
)
async def gift(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int
):

    if amount <= 0:

        await interaction.response.send_message(
            "❌ Amount must be greater than 0.",
            ephemeral=True
        )

        return

    if user.id == interaction.user.id:

        await interaction.response.send_message(
            "❌ You cannot gift yourself.",
            ephemeral=True
        )

        return

    balance = await get_balance(interaction.user.id)

    if balance < amount:

        await interaction.response.send_message(
            "❌ You don't have enough balance.",
            ephemeral=True
        )

        return

    await add_balance(interaction.user.id, -amount)

    await add_balance(user.id, amount)

    await interaction.response.send_message(
        f"💸 You gifted {amount:,} coins to {user.mention}!"
    )

# ---------------- EXP SYSTEM ---------------- #

MESSAGE_EXP_MIN = 30
MESSAGE_EXP_MAX = 50
LEVEL_DIVISOR = 700

last_message_exp = {}

async def add_exp(user_id, amount):

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute(
            """
            INSERT INTO exp_history
            VALUES (?, ?, ?)
            """,
            (
                user_id,
                amount,
                int(datetime.now(UTC).timestamp())
            )
        )

        await db.commit()

async def get_exp(user_id):

    week_ago = int(
        (datetime.now(UTC) - timedelta(days=7)).timestamp()
    )

    async with aiosqlite.connect(DATABASE) as db:

        async with db.execute(
            """
            SELECT SUM(amount)
            FROM exp_history
            WHERE user_id = ?
            AND timestamp >= ?
            """,
            (user_id, week_ago)
        ) as cursor:

            data = await cursor.fetchone()

    gained_exp = max(data[0] or 0, 0)

    spent_exp = await get_spent_exp(user_id)

    return max(gained_exp - spent_exp, 0)

async def get_level_exp(user_id):

    week_ago = int(
        (datetime.now(UTC) - timedelta(days=7)).timestamp()
    )

    async with aiosqlite.connect(DATABASE) as db:

        async with db.execute(
            """
            SELECT SUM(amount)
            FROM exp_history
            WHERE user_id = ?
            AND timestamp >= ?
            """,
            (user_id, week_ago)
        ) as cursor:

            data = await cursor.fetchone()

    return max(data[0] or 0, 0)

async def get_level(user_id):

    exp = await get_level_exp(user_id)

    level = (exp // LEVEL_DIVISOR) + 1

    return min(level, 100)

async def get_spent_exp(user_id):

    async with aiosqlite.connect(DATABASE) as db:

        async with db.execute(
            """
            SELECT amount
            FROM spent_exp
            WHERE user_id = ?
            """,
            (user_id,)
        ) as cursor:

            data = await cursor.fetchone()

        if not data:

            await db.execute(
                "INSERT INTO spent_exp VALUES (?, ?)",
                (user_id, 0)
            )

            await db.commit()

            return 0

        return data[0]


async def add_spent_exp(user_id, amount):

    current = await get_spent_exp(user_id)

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute(
            """
            UPDATE spent_exp
            SET amount = ?
            WHERE user_id = ?
            """,
            (current + amount, user_id)
        )

        await db.commit()

# ---------------- AUTO GIVEAWAYS ---------------- #

AUTO_GIVEAWAY_ENABLED = False
AUTO_GIVEAWAY_INTERVAL_SECONDS = 60

AUTO_PRIZES = [
    ("500 bal", 500),
    ("300 bal", 300),
    ("200 bal", 200),
    ("100 bal", 100)
]

async def auto_giveaway_loop(channel):

    global AUTO_GIVEAWAY_ENABLED

    while AUTO_GIVEAWAY_ENABLED:

        prize, reward = random.choice(AUTO_PRIZES)

        duration_seconds = 30

        end_time = datetime.now(UTC) + timedelta(
            seconds=duration_seconds
        )

        embed = discord.Embed(
            title="🎉 AUTOMATIC GIVEAWAY 🎉",
            description=(
                f"React with 🎉 to enter\n\n"
                f"Prize: **{prize}**\n"
                f"Reward: **{reward} coins**\n"
                f"Ends: <t:{int(end_time.timestamp())}:R>"
            ),
            color=discord.Color.gold()
        )

        message = await channel.send(embed=embed)

        await message.add_reaction("🎉")

        async with aiosqlite.connect(DATABASE) as db:

            await db.execute(
                """
                INSERT INTO giveaways
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    channel.id,
                    prize,
                    1,
                    reward,
                    int(end_time.timestamp()),
                    0,
                    "gold",
                    0
                )
            )

            await db.commit()

        await asyncio.sleep(duration_seconds)

        await end_giveaway(message.id)

        await asyncio.sleep(
            AUTO_GIVEAWAY_INTERVAL_SECONDS
        )

# ----------------- MESSAGE EXP ----------------- #

@bot.event
async def on_message(message):

    if message.author.bot:
        return

    now = datetime.now().timestamp()

    last_time = last_message_exp.get(
        message.author.id,
        0
    )

    if now - last_time >= 30:

        content_length = len(message.content.strip())

        # Minimum EXP
        exp_gain = 30

        # Bonus EXP from message length
        # Caps at +20 EXP around 200 characters
        bonus = min(20, content_length // 10)

        # Randomness based on length
        randomness = random.randint(
            0,
            max(1, bonus)
        )

        gained = exp_gain + randomness

        gained = min(50, gained)

        await add_exp(
            message.author.id,
            gained
        )

        last_message_exp[message.author.id] = now

    await bot.process_commands(message)

# ---------------- READY EVENT ---------------- #

@bot.event
async def on_ready():

    await setup_database()

    try:

        synced = await bot.tree.sync(guild=None)

        print(f"Synced {len(synced)} commands")

    except Exception as e:
        print(e)

    print(f"Logged in as {bot.user}")
    bot.loop.create_task(raffle_loop())

# ---------------- CREATE GIVEAWAY ---------------- #

@bot.tree.command(
    name="giveaway",
    description="Create a giveaway"
)
@app_commands.describe(
    prize="Prize name",
    minutes="Duration",
    winners="Number of winners",
    reward="Balance reward",
    required_role="Required role",
    template="Template color"
)
async def giveaway(
    interaction: discord.Interaction,
    prize: str,
    minutes: int,
    winners: int,
    reward: int,
    required_role: discord.Role = None,
    template: str = "gold"
):

    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ You don’t have permission to use this command.",
            ephemeral=True
        )
        return
    
    end_time = datetime.now(UTC) + timedelta(
        minutes=minutes
    )

    embed = discord.Embed(
        title="🎉 GIVEAWAY 🎉",
        description=(
            f"React with 🎉 to enter\n\n"
            f"Prize: **{prize}**\n"
            f"Winners: **{winners}**\n"
            f"Reward: **{reward} coins**\n"
            f"Ends: <t:{int(end_time.timestamp())}:R>"
        ),
        color=TEMPLATES.get(
            template,
            discord.Color.gold()
        )
    )

    if required_role:

        embed.add_field(
            name="Required Role",
            value=required_role.mention,
            inline=False
        )

    message = await interaction.channel.send(
        embed=embed
    )

    await message.add_reaction("🎉")

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute(
            """
            INSERT INTO giveaways
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.id,
                interaction.channel.id,
                prize,
                winners,
                reward,
                int(end_time.timestamp()),
                required_role.id if required_role else 0,
                template,
                0
            )
        )

        await db.commit()

    await interaction.response.send_message(
        "✅ Giveaway created.",
        ephemeral=True
    )

    await asyncio.sleep(minutes * 60)

    await end_giveaway(message.id)

# ---------------- END GIVEAWAY ---------------- #

async def end_giveaway(message_id, reroll=False):

    async with aiosqlite.connect(DATABASE) as db:

        async with db.execute(
            """
            SELECT * FROM giveaways
            WHERE message_id = ?
            AND ended = 0
            """,
            (message_id,)
        ) as cursor:

            data = await cursor.fetchone()

        if not data:
            return

        (
            message_id,
            channel_id,
            prize,
            winner_count,
            reward,
            end_time,
            required_role,
            template,
            ended
        ) = data

    channel = bot.get_channel(channel_id)

    message = await channel.fetch_message(
        message_id
    )

    reaction = discord.utils.get(
        message.reactions,
        emoji="🎉"
    )

    users = []

    async for user in reaction.users():

        if user.bot:
            continue

        member = channel.guild.get_member(user.id)

        if required_role:

            role_ids = [
                role.id for role in member.roles
            ]

            if required_role not in role_ids:
                continue

        users.append(user)

    if not users:

        await channel.send(
            "No valid participants."
        )

        return

    weighted_users = []

    for user in users:

        level = await get_level(user.id)

        weight = min(100, max(1, level))

        weighted_users.extend([user] * weight)

    winners = []

    while (
        len(winners) < min(winner_count, len(users))
        and weighted_users
    ):

        selected = random.choice(weighted_users)

        if selected not in winners:
            winners.append(selected)

    winner_mentions = []

    async with aiosqlite.connect(DATABASE) as db:

        if reroll:

            async with db.execute(
                """
                SELECT winner_id, reward
                FROM giveaway_winners
                WHERE message_id = ?
                """,
                (message_id,)
            ) as cursor:

                old_data = await cursor.fetchone()

            if old_data:

                old_winner, old_reward = old_data

                await add_balance(
                    old_winner,
                    -old_reward
                )

        for winner in winners:

            await add_balance(
                winner.id,
                reward
            )

            await db.execute(
                """
                INSERT OR REPLACE INTO giveaway_winners
                VALUES (?, ?, ?)
                """,
                (
                    message_id,
                    winner.id,
                    reward
                )
            )

            winner_mentions.append(
                winner.mention
            )

        await db.commit()

        if not reroll:

            await db.execute(
                """
                UPDATE giveaways
                SET ended = 1
                WHERE message_id = ?
                """,
                (message_id,)
            )

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

@bot.tree.command(
    name="balance",
    description="Check a balance"
)
async def balance(
    interaction: discord.Interaction,
    user: discord.Member = None
):

    user = user or interaction.user

    bal = await get_balance(user.id)

    embed = discord.Embed(
        title=f"💰 {user.display_name}'s Balance",
        description=f"{bal:,} coins",
        color=discord.Color.green()
    )

    await interaction.response.send_message(
        embed=embed
    )

# ---------------- ADD BALANCE ---------------- #

@bot.tree.command(
    name="addbalance",
    description="Add balance to a user"
)
async def addbalance(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )
        return
    await add_balance(user.id, amount)

    await interaction.response.send_message(
        f"✅ Added {amount} coins to {user.mention}"
    )

# ---------------- REMOVE BALANCE ---------------- #

@bot.tree.command(
    name="removebalance",
    description="Remove balance from a user"
)
async def removebalance(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )
        return
    await add_balance(user.id, -amount)

    await interaction.response.send_message(
        f"❌ Removed {amount} coins from {user.mention}"
    )

# ---------------- REROLL ---------------- #

@bot.tree.command(
    name="reroll",
    description="Reroll a giveaway"
)
async def reroll(
    interaction: discord.Interaction,
    message_id: str
):

    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )
        return

    message_id = int(message_id)

    async with aiosqlite.connect(DATABASE) as db:

        async with db.execute(
            """
            SELECT *
            FROM giveaways
            WHERE message_id = ?
            """,
            (message_id,)
        ) as cursor:

            data = await cursor.fetchone()

        if not data:

            await interaction.response.send_message(
                "❌ Giveaway not found."
            )

            return

        (
            _message_id,
            channel_id,
            prize,
            winner_count,
            reward,
            end_time,
            required_role,
            template,
            ended
        ) = data

        async with db.execute(
            """
            SELECT winner_id, reward
            FROM giveaway_winners
            WHERE message_id = ?
            """,
            (message_id,)
        ) as cursor:

            old_data = await cursor.fetchone()

    channel = bot.get_channel(channel_id)

    message = await channel.fetch_message(message_id)

    reaction = discord.utils.get(
        message.reactions,
        emoji="🎉"
    )

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

        await interaction.response.send_message(
            "❌ No participants."
        )

        return

    weighted_users = []

    for user in users:

        level = await get_level(user.id)

        weight = min(100, max(1, level))

        weighted_users.extend([user] * weight)

    new_winner = random.choice(weighted_users)

    # Remove old reward
    if old_data:

        old_winner, old_reward = old_data

        await add_balance(
            old_winner,
            -old_reward
        )

    # Give new reward
    await add_balance(
        new_winner.id,
        reward
    )

    # Save new winner
    async with aiosqlite.connect(DATABASE) as db:

        await db.execute(
            """
            INSERT OR REPLACE INTO giveaway_winners
            VALUES (?, ?, ?)
            """,
            (
                message_id,
                new_winner.id,
                reward
            )
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

    await interaction.response.send_message(
        "✅ Giveaway rerolled."
    )

# ---------------- AUTO GIVEAWAY POOL ---------------- #

AUTO_GIVEAWAY_ENABLED = False

AUTO_GIVEAWAY_POOL = []

# Example structure:
# {
#     "prize": "Discord Nitro",
#     "reward": 500,
#     "winners": 1
# }

# ---------------- RAFFLE SYSTEM ---------------- #

RAFFLE_TICKET_PRICE = 100
RAFFLE_PRIZE = 10000

async def get_tickets(guild_id, user_id):

    async with aiosqlite.connect(DATABASE) as db:

        async with db.execute(
            """
            SELECT tickets
            FROM raffle
            WHERE guild_id = ?
            AND user_id = ?
            """,
            (
                guild_id,
                user_id
            )
        ) as cursor:

            data = await cursor.fetchone()

        if not data:

            await db.execute(
                """
                INSERT INTO raffle
                VALUES (?, ?, ?)
                """,
                (
                    guild_id,
                    user_id,
                    0
                )
            )

            await db.commit()

            return 0

        return data[0]
    
async def add_tickets(
    guild_id,
    user_id,
    amount
):

    tickets = await get_tickets(
        guild_id,
        user_id
    )

    new_tickets = max(
        0,
        tickets + amount
    )

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute(
            """
            UPDATE raffle
            SET tickets = ?
            WHERE guild_id = ?
            AND user_id = ?
            """,
            (
                new_tickets,
                guild_id,
                user_id
            )
        )

        await db.commit()


@bot.tree.command(
    name="buytickets",
    description="Buy raffle tickets"
)
async def buytickets(
    interaction: discord.Interaction,
    amount: int
):

    price = amount * RAFFLE_TICKET_PRICE

    balance = await get_balance(interaction.user.id)

    if balance < price:

        await interaction.response.send_message(
            "❌ Not enough balance."
        )

        return

    await add_balance(interaction.user.id, -price)

    await add_tickets(
        interaction.guild.id,
        interaction.user.id,
        amount
    )

    await interaction.response.send_message(
        f"🎟 Bought {amount} tickets."
    )


@bot.tree.command(
    name="tickets",
    description="Check tickets"
)
async def tickets(interaction: discord.Interaction):

    amount = await get_tickets(
        interaction.guild.id,
        interaction.user.id
    )

    await interaction.response.send_message(
        f"🎟 You have {amount} tickets."
    )


@bot.tree.command(
    name="addtickets",
    description="Add raffle tickets"
)
async def addtickets(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int
):

    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )
        return

    await add_tickets(
        interaction.guild.id,
        user.id,
        amount
    )

    await interaction.response.send_message(
        f"✅ Added {amount} tickets to {user.mention}"
    )




@bot.tree.command(
    name="removetickets",
    description="Remove raffle tickets"
)
async def removetickets(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int
):

    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )
        return

    await add_tickets(
        interaction.guild.id,
        user.id,
        -amount
    )

    await interaction.response.send_message(
        f"❌ Removed {amount} tickets from {user.mention}"
    )


async def raffle_loop():

    await bot.wait_until_ready()

    while not bot.is_closed():

        await asyncio.sleep(86400)

        for guild in bot.guilds:

            async with aiosqlite.connect(DATABASE) as db:

                async with db.execute(
                    """
                    SELECT user_id, tickets
                    FROM raffle
                    WHERE guild_id = ?
                    """,
                    (guild.id,)
                ) as cursor:

                    entries = await cursor.fetchall()

            pool = []

            for user_id, tickets in entries:

                pool.extend([user_id] * tickets)

            if not pool:
                continue

            winner_id = random.choice(pool)

            await add_balance(
                winner_id,
                RAFFLE_PRIZE
            )

            channel = guild.system_channel

            if channel:

                await channel.send(
                    f"🎉 <@{winner_id}> won the daily raffle "
                    f"and received {RAFFLE_PRIZE:,} coins!"
                )

            async with aiosqlite.connect(DATABASE) as db:

                await db.execute(
                    """
                    DELETE FROM raffle
                    WHERE guild_id = ?
                    """,
                    (guild.id,)
                )

                await db.commit()

# ---------------- CHEST SYSTEM ---------------- #

CHEST_COST = 750

CHEST_PRIZES = [
    {
        "name": "250 EXP",
        "exp": 250,
        "balance": 0,
        "chance": 40
    },
    {
        "name": "450 EXP",
        "exp": 450,
        "balance": 0,
        "chance": 30
    },
    {
        "name": "1k Balance",
        "exp": 0,
        "balance": 1000,
        "chance": 15
    },
    {
        "name": "1 Huge",
        "exp": 0,
        "balance": 0,
        "chance": 10
    },
    {
        "name": "25m Gems",
        "exp": 0,
        "balance": 0,
        "chance": 4
    },
    {
        "name": "40k Balance",
        "exp": 0,
        "balance": 40000,
        "chance": 1
    }
]

# ---------------- ADD AUTO GIVEAWAY ---------------- #

@bot.tree.command(
    name="addautogiveaway",
    description="Add a giveaway to the auto pool"
)
async def addautogiveaway(
    interaction: discord.Interaction,
    prize: str,
    reward: int,
    winners: int
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ You don’t have permission to use this command.",
            ephemeral=True
        )
        return
    
    AUTO_GIVEAWAY_POOL.append({
        "prize": prize,
        "reward": reward,
        "winners": winners
    })

    await interaction.response.send_message(
        f"✅ Added auto giveaway:\n"
        f"Prize: {prize}\n"
        f"Reward: {reward}\n"
        f"Winners: {winners}"
    )

# ---------------- REMOVE AUTO GIVEAWAY ---------------- #

@bot.tree.command(
    name="removeautogiveaway",
    description="Remove auto giveaway by prize name"
)
async def removeautogiveaway(
    interaction: discord.Interaction,
    prize: str
):
    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ You don’t have permission to use this command.",
            ephemeral=True
        )
        return
    
    global AUTO_GIVEAWAY_POOL

    before = len(AUTO_GIVEAWAY_POOL)

    AUTO_GIVEAWAY_POOL = [
        g for g in AUTO_GIVEAWAY_POOL
        if g["prize"].lower() != prize.lower()
    ]

    after = len(AUTO_GIVEAWAY_POOL)

    if before == after:

        await interaction.response.send_message(
            "❌ Giveaway not found."
        )

    else:

        await interaction.response.send_message(
            f"🗑 Removed auto giveaway: {prize}"
        )

# ---------------- START GIVEAWAYS ---------------- #

@bot.tree.command(
    name="startgiveaways",
    description="Start automatic giveaways"
)
async def startgiveaways(
    interaction: discord.Interaction,
    interval_seconds: int,
    giveaway_duration_seconds: int
):

    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ You don’t have permission to use this command.",
            ephemeral=True
        )
        return
    
    global AUTO_GIVEAWAY_ENABLED

    if AUTO_GIVEAWAY_ENABLED:

        await interaction.response.send_message(
            "Automatic giveaways are already running.",
            ephemeral=True
        )

        return

    if not AUTO_GIVEAWAY_POOL:

        await interaction.response.send_message(
            "❌ No auto giveaways added.",
            ephemeral=True
        )

        return

    AUTO_GIVEAWAY_ENABLED = True

    async def auto_loop():

        global AUTO_GIVEAWAY_ENABLED

        while AUTO_GIVEAWAY_ENABLED:

            giveaway_data = random.choice(
                AUTO_GIVEAWAY_POOL
            )

            prize = giveaway_data["prize"]
            reward = giveaway_data["reward"]
            winners = giveaway_data["winners"]

            end_time = datetime.now(UTC) + timedelta(
                seconds=giveaway_duration_seconds
            )

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

            message = await interaction.channel.send(
                embed=embed
            )

            await message.add_reaction("🎉")

            async with aiosqlite.connect(DATABASE) as db:

                await db.execute(
                    """
                    INSERT INTO giveaways
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.id,
                        interaction.channel.id,
                        prize,
                        winners,
                        reward,
                        int(end_time.timestamp()),
                        0,
                        "gold",
                        0
                    )
                )

                await db.commit()

            await asyncio.sleep(
                giveaway_duration_seconds
            )

            await end_giveaway(message.id)

            await asyncio.sleep(
                interval_seconds
            )

    asyncio.create_task(auto_loop())

    await interaction.response.send_message(
        "✅ Automatic giveaways started."
    )

# ---------------- STOP GIVEAWAYS ---------------- #

@bot.tree.command(
    name="stopgiveaways",
    description="Stop automatic giveaways"
)
async def stopgiveaways(
    interaction: discord.Interaction
):

    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ You don’t have permission to use this command.",
            ephemeral=True
        )
        return
    
    global AUTO_GIVEAWAY_ENABLED

    AUTO_GIVEAWAY_ENABLED = False

    await interaction.response.send_message(
        "🛑 Automatic giveaways stopped."
    )

# ---------------- CHEST COMMAND ---------------- #

@bot.tree.command(
    name="chest",
    description="Open an EXP chest"
)
async def chest(
    interaction: discord.Interaction,
    amount: int = 1
):

    await interaction.response.defer()
    if amount <= 0:

        await interaction.followup.send(
            "❌ Amount must be greater than 0."
        )
    
        return
    
    exp = await get_exp(interaction.user.id)

    if exp >= 20000:

        max_chests = exp // CHEST_COST

        amount = min(amount, max_chests)

    else:

        amount = 1

    total_cost = CHEST_COST * amount

    if exp < total_cost:

        await interaction.response.send_message(
            f"❌ You need {total_cost:,} EXP."
        )

        return
    
    await add_spent_exp(
        interaction.user.id,
        total_cost
    )

    results = {}

    for _ in range(amount):

        prize = random.choices(
            CHEST_PRIZES,
            weights=[p["chance"] for p in CHEST_PRIZES],
            k=1
        )[0]

        name = prize["name"]

        results[name] = results.get(name, 0) + 1

        if prize["balance"] > 0:

            await add_balance(
                interaction.user.id,
                prize["balance"]
            )

        if prize["exp"] > 0:

            await add_exp(
                interaction.user.id,
                prize["exp"]
            )

    result_text = "\n".join(
        f"• {count}x {name}"
        for name, count in results.items()
    )

    embed = discord.Embed(
        title="📦 Chest Results",
        description=result_text,
        color=discord.Color.purple()
    )

    embed.set_footer(
        text=f"Opened {amount} chest(s)"
    )

    await interaction.followup.send(
        embed=embed
    )

# ---------------- EXP COMMANDS ---------------- #

@bot.tree.command(
    name="level",
    description="Check a level"
)
async def level(
    interaction: discord.Interaction,
    user: discord.Member = None
):

    user = user or interaction.user

    exp = await get_level_exp(user.id)

    usable_exp = await get_exp(user.id)

    lvl = await get_level(user.id)

    embed = discord.Embed(
        title=f"⭐ {user.display_name}'s Level",
        color=discord.Color.gold()
    )

    embed.add_field(
        name="Level",
        value=str(lvl),
        inline=False
    )

    embed.add_field(
        name="Total EXP (7d)",
        value=f"{exp:,}",
        inline=False
    )

    embed.add_field(
        name="Usable EXP",
        value=f"{usable_exp:,}",
        inline=False
    )

    await interaction.response.send_message(
        embed=embed
    )


@bot.tree.command(
    name="addexp",
    description="Add EXP"
)
async def addexp(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int
):

    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )
        return

    await add_exp(user.id, amount)

    await interaction.response.send_message(
        f"✅ Added {amount} EXP to {user.mention}"
    )


@bot.tree.command(
    name="removeexp",
    description="Remove EXP"
)
async def removeexp(
    interaction: discord.Interaction,
    user: discord.Member,
    amount: int
):

    if not await is_allowed_to_giveaway(interaction):
        await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )
        return

    await add_exp(user.id, -amount)

    await interaction.response.send_message(
        f"❌ Removed {amount} EXP from {user.mention}"
    )

# ---------------- ITEM STORE SYSTEM ---------------- #

# DATABASE TABLE
# Add this inside setup_database()

"""
# ITEM STORE
await db.execute(
    '''
    CREATE TABLE IF NOT EXISTS item_store (
        item_name TEXT PRIMARY KEY,
        price INTEGER,
        role_id INTEGER,
        description TEXT
    )
    '''
)
"""

# ---------------- ITEM FUNCTIONS ---------------- #

async def add_item(item_name, price, role_id, description):

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute(
            """
            INSERT OR REPLACE INTO item_store
            VALUES (?, ?, ?, ?)
            """,
            (
                item_name,
                price,
                role_id,
                description
            )
        )

        await db.commit()


async def remove_item(item_name):

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute(
            """
            DELETE FROM item_store
            WHERE item_name = ?
            """,
            (item_name,)
        )

        await db.commit()


async def get_item(item_name):

    async with aiosqlite.connect(DATABASE) as db:

        async with db.execute(
            """
            SELECT *
            FROM item_store
            WHERE LOWER(item_name) = LOWER(?)
            """,
            (item_name,)
        ) as cursor:

            return await cursor.fetchone()


async def get_all_items():

    async with aiosqlite.connect(DATABASE) as db:

        async with db.execute(
            """
            SELECT *
            FROM item_store
            """
        ) as cursor:

            return await cursor.fetchall()

# ---------------- ITEM STORE COMMAND ---------------- #

item_group = app_commands.Group(
    name="item",
    description="Item store commands"
)

bot.tree.add_command(item_group)

# ---------------- /item add ---------------- #

@item_group.command(
    name="add",
    description="Add item to store"
)
async def item_add(
    interaction: discord.Interaction,
    name: str,
    price: int,
    role: discord.Role,
    description: str
):

    if not await is_allowed_to_giveaway(interaction):

        await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )

        return

    await add_item(
        name,
        price,
        role.id,
        description
    )

    await interaction.response.send_message(
        f"✅ Added item **{name}** to the store."
    )

# ---------------- /item remove ---------------- #

@item_group.command(
    name="remove",
    description="Remove item from store"
)
async def item_remove(
    interaction: discord.Interaction,
    name: str
):

    if not await is_allowed_to_giveaway(interaction):

        await interaction.response.send_message(
            "❌ No permission.",
            ephemeral=True
        )

        return

    item = await get_item(name)

    if not item:

        await interaction.response.send_message(
            "❌ Item not found."
        )

        return

    await remove_item(name)

    await interaction.response.send_message(
        f"🗑 Removed **{name}** from the store."
    )

# ---------------- /item info ---------------- #

@item_group.command(
    name="info",
    description="View item info"
)
async def item_info(
    interaction: discord.Interaction,
    name: str
):

    item = await get_item(name)

    if not item:

        await interaction.response.send_message(
            "❌ Item not found."
        )

        return

    item_name, price, role_id, description = item

    role = interaction.guild.get_role(role_id)

    embed = discord.Embed(
        title=f"🛒 {item_name}",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="Price",
        value=f"{price:,} coins",
        inline=False
    )

    embed.add_field(
        name="Role",
        value=role.mention if role else "Unknown Role",
        inline=False
    )

    embed.add_field(
        name="Description",
        value=description,
        inline=False
    )

    await interaction.response.send_message(
        embed=embed
    )

# ---------------- /item store ---------------- #

@item_group.command(
    name="store",
    description="View item store"
)
async def item_store(
    interaction: discord.Interaction
):

    items = await get_all_items()

    if not items:

        await interaction.response.send_message(
            "❌ Store is empty."
        )

        return

    embed = discord.Embed(
        title="🛒 Item Store",
        color=discord.Color.green()
    )

    for item_name, price, role_id, description in items:

        role = interaction.guild.get_role(role_id)

        embed.add_field(
            name=item_name,
            value=(
                f"💰 {price:,} coins\n"
                f"🎭 {role.mention if role else 'Unknown Role'}"
            ),
            inline=False
        )

    await interaction.response.send_message(
        embed=embed
    )

# ---------------- /item buy ---------------- #

@item_group.command(
    name="buy",
    description="Buy an item"
)
async def item_buy(
    interaction: discord.Interaction,
    name: str
):

    item = await get_item(name)

    if not item:

        await interaction.response.send_message(
            "❌ Item not found."
        )

        return

    item_name, price, role_id, description = item

    balance = await get_balance(
        interaction.user.id
    )

    if balance < price:

        await interaction.response.send_message(
            "❌ Not enough balance."
        )

        return

    role = interaction.guild.get_role(role_id)

    if not role:

        await interaction.response.send_message(
            "❌ Role no longer exists."
        )

        return

    member = interaction.guild.get_member(
        interaction.user.id
    )

    if role in member.roles:

        await interaction.response.send_message(
            "❌ You already own this item."
        )

        return

    await add_balance(
        interaction.user.id,
        -price
    )

    await member.add_roles(role)

    await interaction.response.send_message(
        f"✅ You bought **{item_name}** "
        f"for {price:,} coins."
    )

# ---------------- RUN BOT ---------------- #

bot.run(TOKEN)

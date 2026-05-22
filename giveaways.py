import random
import discord

from db import get_db
from economy import add_balance, add_exp
from gambling import add_token


active_giveaways = {}


# ---------- GIVE REWARD ---------- #
async def give_reward(user, reward):
    rtype = reward["type"]
    value = reward["value"]

    if rtype == "balance":
        await add_balance(user.id, value)

    elif rtype == "exp":
        await add_exp(user.id, value)

    elif rtype == "tokens":
        await add_token(user.id, value)


# ---------- REROLL ---------- #
async def reroll(giveaway_id: int, channel: discord.TextChannel):
    users = active_giveaways.get(giveaway_id, {}).get("participants", [])

    if not users:
        return "❌ No participants"

    winner = random.choice(users)

    reward = active_giveaways[giveaway_id]["reward"]

    await give_reward(winner, reward)

    await channel.send(f"🎉 New winner: {winner.mention}")
    return "OK"


# ---------- END GIVEAWAY ---------- #
async def end_giveaway(giveaway_id: int, channel: discord.TextChannel):
    data = active_giveaways.get(giveaway_id)

    if not data:
        return

    users = data["participants"]
    if not users:
        await channel.send("❌ No participants")
        return

    winner = random.choice(users)

    await give_reward(winner, data["reward"])

    await channel.send(f"🎉 Winner: {winner.mention}")

    active_giveaways.pop(giveaway_id, None)

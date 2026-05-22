import asyncio
import random
import discord

from db import get_db
from economy import add_balance, add_exp

active_sessions = {}


# ---------- PICK GAME ---------- #
async def pick_game(guild_id: int):
    async with get_db() as db:
        async with db.execute("""
            SELECT game_name, reward_balance, reward_exp
            FROM games
            WHERE guild_id=? AND enabled=1
        """, (guild_id,)) as cur:
            games = await cur.fetchall()

    if not games:
        return None

    eligible = []

    async with get_db() as db:
        for name, bal, exp in games:
            async with db.execute("""
                SELECT answer FROM game_answers
                WHERE guild_id=? AND game_name=?
            """, (guild_id, name)) as cur:
                answers = [r[0] for r in await cur.fetchall()]

            if answers:
                eligible.append((name, bal, exp, answers))

    if not eligible:
        return None

    return random.choice(eligible)


# ---------- GAME LOOP ---------- #
async def game_loop(bot, guild_id: int):
    await bot.wait_until_ready()

    while not bot.is_closed():

        async with get_db() as db:
            async with db.execute("""
                SELECT channel_id, answer_time, interval_seconds
                FROM game_config WHERE guild_id=?
            """, (guild_id,)) as cur:
                config = await cur.fetchone()

        if not config:
            await asyncio.sleep(10)
            continue

        channel_id, answer_time, interval = config
        channel = bot.get_channel(channel_id)

        if not channel:
            await asyncio.sleep(10)
            continue

        game = await pick_game(guild_id)
        if not game:
            await asyncio.sleep(interval)
            continue

        name, bal, exp, answers = game
        correct = random.choice(answers)

        embed = discord.Embed(
            title="🎮 Random Game",
            description=f"**{name}**\nType your answer!",
            color=discord.Color.teal()
        )
        embed.add_field(name="⏱ Time", value=f"{answer_time}s", inline=False)

        await channel.send(embed=embed)

        active_sessions[guild_id] = {
            "answer": correct,
            "answered": False,
            "winner": None,
            "reward_bal": bal,
            "reward_exp": exp
        }

        await asyncio.sleep(answer_time)

        session = active_sessions.pop(guild_id, None)

        if session and session["answered"]:
            winner = session["winner"]

            await add_balance(winner.id, session["reward_bal"])
            await add_exp(winner.id, session["reward_exp"])

            await channel.send(f"🎉 {winner.mention} won! Answer: **{correct}**")
        else:
            await channel.send(f"⏰ Time's up! Answer was **{correct}**")

        await asyncio.sleep(interval)

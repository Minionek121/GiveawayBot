import discord
from discord.ext import commands
import asyncio

from config import TOKEN
from db import init_db, migrate_db

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

active_game_sessions = {}
game_tasks = {}


async def setup_db():
    await init_db()
    await migrate_db()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.event
async def setup_hook():
    await setup_db()

    # 🔥 THIS is where cogs will load later
    await bot.load_extension("economy")
    await bot.load_extension("games")
    await bot.load_extension("gambling")
    await bot.load_extension("codes")
    await bot.load_extension("chests")
    await bot.load_extension("toggles")

    await bot.tree.sync()


bot.run(TOKEN)

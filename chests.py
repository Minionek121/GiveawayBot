import random
from db import get_db
from economy import add_balance, add_exp
from gambling import add_token


# ---------- ADD ITEM TO CHEST ---------- #
async def add_chest_item(guild_id: int, chest: str, item_type: str, value: int, weight: int = 1):
    async with get_db() as db:
        await db.execute("""
            INSERT INTO chest_items(guild_id, chest, item_type, value, weight)
            VALUES (?, ?, ?, ?, ?)
        """, (guild_id, chest, item_type, value, weight))
        await db.commit()


# ---------- OPEN CHEST ---------- #
async def open_chest(user_id: int, guild_id: int, chest: str):
    async with get_db() as db:
        async with db.execute("""
            SELECT item_type, value, weight
            FROM chest_items
            WHERE guild_id=? AND chest=?
        """, (guild_id, chest)) as cur:
            items = await cur.fetchall()

    if not items:
        return "❌ Chest empty"

    pool = []
    for item_type, value, weight in items:
        pool.extend([(item_type, value)] * weight)

    item_type, value = random.choice(pool)

    if item_type == "balance":
        await add_balance(user_id, value)
        return f"💰 +{value} coins"

    if item_type == "exp":
        await add_exp(user_id, value)
        return f"⭐ +{value} EXP"

    if item_type == "token":
        await add_token(user_id, value)
        return f"🎲 +{value} gamble tokens"

    return "❓ Unknown reward"

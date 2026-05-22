import time
from db import get_db
from utils import get_user


# ---------- ADD BALANCE ---------- #
async def add_balance(user_id: int, amount: int):
    await get_user(user_id)

    async with get_db() as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()


# ---------- ADD EXP (FIXED BUG SOURCE) ---------- #
async def add_exp(user_id: int, amount: int):
    await get_user(user_id)

    async with get_db() as db:
        await db.execute(
            "UPDATE users SET exp = exp + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()


# ---------- GET USER STATS (NO DESYNC) ---------- #
async def get_stats(user_id: int):
    await get_user(user_id)

    async with get_db() as db:
        async with db.execute(
            "SELECT balance, exp, gamble_tokens FROM users WHERE user_id=?",
            (user_id,)
        ) as cur:
            return await cur.fetchone()


# ---------- LEVEL SYSTEM (FIXED EXP DISPLAY ISSUE) ---------- #
def calculate_level(exp: int):
    # simple but stable leveling curve
    return max(1, exp // 1000)


async def get_level(user_id: int):
    _, exp, _ = await get_stats(user_id)
    return calculate_level(exp)

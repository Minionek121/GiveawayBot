import discord
from db import get_db
from utils import get_user
from economy import add_balance, add_exp, get_level


# ---------- CREATE CODE ---------- #
async def create_code(code, guild_id, level_req=0, balance=0, exp=0, role_id=None):
    async with get_db() as db:
        await db.execute("""
            INSERT INTO codes(code, guild_id, level_req, balance, exp, role_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (code, guild_id, level_req, balance, exp, role_id))
        await db.commit()


# ---------- REDEEM CODE (FIXED LOGIC) ---------- #
async def redeem_code(user_id: int, guild: discord.Guild, code: str):
    await get_user(user_id)

    async with get_db() as db:
        async with db.execute("""
            SELECT level_req, balance, exp, role_id
            FROM codes
            WHERE code=? AND guild_id=?
        """, (code, guild.id)) as cur:
            row = await cur.fetchone()

        if not row:
            return "❌ Invalid code"

        level_req, bal, exp, role_id = row

    # level check
    _, user_exp, _ = await get_user(user_id)
    user_level = user_exp // 1000

    if user_level < level_req:
        return "❌ Level too low"

    # apply rewards
    await add_balance(user_id, bal)
    await add_exp(user_id, exp)

    # delete code after use
    async with get_db() as db:
        await db.execute("DELETE FROM codes WHERE code=?", (code,))
        await db.commit()

    # role reward
    if role_id:
        role = guild.get_role(role_id)
        member = guild.get_member(user_id)
        if role and member:
            await member.add_roles(role)

    return "🎉 Code redeemed!"

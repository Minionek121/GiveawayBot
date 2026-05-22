from db import get_db

SYSTEMS = ["raffle", "vip", "gamble"]


# ---------- SET SYSTEM STATE ---------- #
async def set_system(guild_id: int, system: str, enabled: bool):
    if system not in SYSTEMS:
        return False

    async with get_db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO system_settings(guild_id, system, enabled)
            VALUES (?, ?, ?)
        """, (guild_id, system, int(enabled)))
        await db.commit()

    return True


# ---------- CHECK SYSTEM STATE ---------- #
async def is_enabled(guild_id: int, system: str):
    async with get_db() as db:
        async with db.execute("""
            SELECT enabled FROM system_settings
            WHERE guild_id=? AND system=?
        """, (guild_id, system)) as cur:
            row = await cur.fetchone()

    # default = enabled (important so nothing silently breaks)
    if not row:
        return True

    return bool(row[0])


# ---------- GUARD WRAPPER ---------- #
def require_system(system: str):
    async def wrapper(guild_id: int):
        return await is_enabled(guild_id, system)
    return wrapper

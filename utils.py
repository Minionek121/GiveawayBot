from db import get_db

async def ensure_user(user_id: int):
    async with get_db() as db:
        await db.execute("""
        INSERT OR IGNORE INTO users(user_id)
        VALUES (?)
        """, (user_id,))
        await db.commit()


async def get_user(user_id: int):
    await ensure_user(user_id)

    async with get_db() as db:
        async with db.execute("""
            SELECT balance, exp, gamble_tokens
            FROM users
            WHERE user_id=?
        """, (user_id,)) as cur:
            return await cur.fetchone()


async def update_user(user_id: int, **fields):
    await ensure_user(user_id)

    keys = list(fields.keys())
    values = list(fields.values())

    set_clause = ", ".join([f"{k}=COALESCE({k},0)+?" for k in keys])

    async with get_db() as db:
        await db.execute(
            f"UPDATE users SET {set_clause} WHERE user_id=?",
            (*values, user_id)
        )
        await db.commit()

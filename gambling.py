import random
from db import get_db
from utils import get_user
from economy import add_balance

# ---------- TOKEN CHECK ---------- #
async def has_tokens(user_id: int):
    _, _, tokens = await get_user(user_id)
    return tokens > 0


# ---------- CONSUME TOKEN ---------- #
async def use_token(user_id: int):
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET gamble_tokens = gamble_tokens - 1 WHERE user_id = ? AND gamble_tokens > 0",
            (user_id,)
        )
        await db.commit()


# ---------- GIVE TOKENS (DAILY / ADMIN) ---------- #
async def add_token(user_id: int, amount: int):
    await get_user(user_id)

    async with get_db() as db:
        await db.execute(
            "UPDATE users SET gamble_tokens = gamble_tokens + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()


# ---------- BLACKJACK ---------- #
def draw():
    return random.randint(2, 11)

def score(hand):
    return sum(hand)


async def blackjack(user_id: int, bet: int):
    await get_user(user_id)

    if not await has_tokens(user_id):
        return None, "❌ No gamble tokens"

    await use_token(user_id)

    async with get_db() as db:
        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,)
        ) as cur:
            balance = (await cur.fetchone())[0]

        if balance < bet:
            return None, "❌ Not enough balance"

    player = [draw(), draw()]
    dealer = [draw(), draw()]

    p, d = score(player), score(dealer)

    if p > d:
        result = bet
    elif p < d:
        result = -bet
    else:
        result = 0

    await add_balance(user_id, result)

    return {
        "player": player,
        "dealer": dealer,
        "result": result
    }, None


# ---------- ROULETTE ---------- #
async def roulette(user_id: int, bet: int, choice: str):
    await get_user(user_id)

    if not await has_tokens(user_id):
        return None, "❌ No gamble tokens"

    await use_token(user_id)

    async with get_db() as db:
        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,)
        ) as cur:
            balance = (await cur.fetchone())[0]

        if balance < bet:
            return None, "❌ Not enough balance"

    outcome = random.choice(["red", "black", "green"])

    if choice.lower() == outcome:
        payout = bet * (14 if outcome == "green" else 2)
    else:
        payout = -bet

    await add_balance(user_id, payout)

    return {
        "outcome": outcome,
        "payout": payout
    }, None

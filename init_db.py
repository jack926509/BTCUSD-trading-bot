"""
Database initialisation script.
Run once during deployment: python init_db.py
"""
import asyncio
import os

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

DB_PATH     = os.getenv("DB_PATH", "/app/data/trading.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "src", "storage", "schema.sql")


async def init():
    # Ensure the data directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    with open(SCHEMA_PATH, "r") as f:
        schema = f.read()

    async with aiosqlite.connect(DB_PATH, timeout=10.0) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.executescript(schema)
        await conn.commit()

    print(f"Database initialised at: {DB_PATH}")


if __name__ == "__main__":
    asyncio.run(init())

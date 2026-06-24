import aiosqlite
import pytest
from pathlib import Path
from soulogos_session.player_lookup import load_player_map, character_name


async def _seed_db(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE players (discord_user_id INTEGER, character_name TEXT)"
        )
        await db.executemany(
            "INSERT INTO players VALUES (?, ?)",
            [(123, "Thalindra"), (456, "Borin Ironfist")],
        )
        await db.commit()


async def test_load_player_map(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite"
    await _seed_db(db)
    m = await load_player_map(db)
    assert m == {123: "Thalindra", 456: "Borin Ironfist"}


async def test_missing_db_returns_empty(tmp_path: Path) -> None:
    m = await load_player_map(tmp_path / "nonexistent.sqlite")
    assert m == {}


async def test_missing_table_returns_empty(tmp_path: Path) -> None:
    db = tmp_path / "empty.sqlite"
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE unrelated (x INTEGER)")
        await conn.commit()
    m = await load_player_map(db)
    assert m == {}


def test_character_name_known() -> None:
    assert character_name({123: "Thalindra"}, 123, "Unknown") == "Thalindra"


def test_character_name_unknown() -> None:
    assert character_name({}, 999, "FallbackName") == "FallbackName"

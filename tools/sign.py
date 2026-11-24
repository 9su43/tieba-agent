"""每日签到（养号用）。新号先养几天再发帖，风控低很多。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiotieba import Client

ROOT = Path(__file__).resolve().parent.parent
AUTH = ROOT / "state" / "auth.json"
CFG = ROOT / "config.json"


def bduss() -> str:
    return (json.loads(AUTH.read_text(encoding="utf-8")).get("bduss") or "").strip()


def forums() -> list[str]:
    cfg_file = CFG if CFG.exists() else ROOT / "config.example.json"
    return json.loads(cfg_file.read_text(encoding="utf-8")).get("forums", [])


async def main() -> None:
    names = forums()
    async with Client(BDUSS=bduss()) as c:
        for name in names:
            try:
                res = await c.sign_forum(name)
                print(f"签到 {name}: {res}")
            except Exception as e:
                print(f"签到 {name} 失败: {e}")


if __name__ == "__main__":
    asyncio.run(main())

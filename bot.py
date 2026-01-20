import os
import json
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional

import aiohttp
import discord
from discord.ext import commands, tasks

# ================== CONFIG ==================

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = "bot_data.json"
POLL_INTERVAL_SECONDS = 300  # 5 minutes
ALLOWED_ROLE_NAMES = {"Admin", "Moderator", "Staff"}

MANGADEX_API_BASE = "https://api.mangadex.org"

# ================== STORAGE ==================


def load_data() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {"guilds": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(d: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


data = load_data()

# ================== DISCORD SETUP ==================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

allowed_mentions = discord.AllowedMentions(
    everyone=False,  # don't allow @everyone
    roles=True,      # allow role pings
    users=False
)

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    allowed_mentions=allowed_mentions,
)


def get_guild_data(guild_id: int) -> Dict[str, Any]:
    gid = str(guild_id)
    if "guilds" not in data:
        data["guilds"] = {}
    if gid not in data["guilds"]:
        data["guilds"][gid] = {
            "announce_channel_id": None,
            "tracked_series": {}  # series_id -> {name, last_seen_ts, role_id}
        }
    return data["guilds"][gid]


def is_staff_or_owner():
    async def predicate(ctx: commands.Context):
        if ctx.guild and ctx.author.id == ctx.guild.owner_id:
            return True

        if ctx.guild is None:
            app_info = await bot.application_info()
            return ctx.author.id == app_info.owner.id

        if isinstance(ctx.author, discord.Member):
            role_names = {r.name for r in ctx.author.roles}
            if ALLOWED_ROLE_NAMES & role_names:
                return True

        raise commands.CheckFailure("You don't have permission to use this command.")
    return commands.check(predicate)

# ================== MANGADEX HELPERS ==================


def parse_iso(dt_str: str) -> datetime:
    # MangaDex returns ISO8601, sometimes with Z
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    return datetime.fromisoformat(dt_str)


async def fetch_latest_for_series(
    session: aiohttp.ClientSession,
    manga_id: str,
    since_ts: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Call MangaDex to get the latest chapters for a given manga.

    Returns a list of chapters newer than since_ts:
    [
      {
        "id": "...",
        "chapter": "45",
        "url": "https://mangadex.org/chapter/...",
        "readableAt": "2024-05-01T12:34:56+00:00",
      },
      ...
    ]
    """
    params = {
        "manga": manga_id,
        "limit": 20,
        "order[readableAt]": "desc",
        "translatedLanguage[]": "en",  # adjust if you want other languages
        "includeFutureUpdates": "0",
    }

    async with session.get(f"{MANGADEX_API_BASE}/chapter", params=params) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    items = payload.get("data", [])
    chapters: List[Dict[str, Any]] = []

    since_dt: Optional[datetime] = None
    if since_ts:
        since_dt = parse_iso(since_ts)

    for ch in items:
        ch_id = ch.get("id")
        attr = ch.get("attributes", {})
        chapter_num = attr.get("chapter") or "?"
        readable_at = attr.get("readableAt")
        if not ch_id or not readable_at:
            continue

        ch_dt = parse_iso(readable_at)

        # Only keep chapters strictly newer than last seen
        if since_dt and ch_dt <= since_dt:
            continue

        chapters.append(
            {
                "id": ch_id,
                "chapter": chapter_num,
                "url": f"https://mangadex.org/chapter/{ch_id}",
                "readableAt": readable_at,
            }
        )

    # Sort oldest -> newest so notifications are in order
    chapters.sort(key=lambda x: parse_iso(x["readableAt"]))
    return chapters

# ================== BACKGROUND TASK ==================


@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def check_releases():
    await bot.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        for guild in bot.guilds:
            gdata = get_guild_data(guild.id)
            channel_id = gdata.get("announce_channel_id")
            if not channel_id:
                continue

            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    continue

            tracked = gdata.get("tracked_series", {})
            if not tracked:
                continue

            for series_id, sdata in list(tracked.items()):
                last_seen_ts = sdata.get("last_seen_ts")
                series_name = sdata.get("name", f"Series {series_id}")

                # FIRST TIME: prime series so you don't get spammed by old chapters
                if not last_seen_ts:
                    try:
                        all_recent = await fetch_latest_for_series(session, series_id, None)
                    except Exception as e:
                        print(f"[ERROR] MangaDex fetch (prime) failed for {series_id}: {e}")
                        continue

                    if all_recent:
                        latest = all_recent[-1]  # list is oldest -> newest
                        sdata["last_seen_ts"] = latest["readableAt"]
                        gdata["tracked_series"][series_id] = sdata
                        save_data(data)
                        print(f"[INFO] Primed series {series_id} at {latest['readableAt']}")
                    # skip notifications this cycle for newly added series
                    continue

                # NORMAL: only get chapters newer than last_seen_ts
                try:
                    new_chapters = await fetch_latest_for_series(
                        session, series_id, last_seen_ts
                    )
                except Exception as e:
                    print(f"[ERROR] MangaDex fetch failed for {series_id}: {e}")
                    continue

                if not new_chapters:
                    continue

                latest_ts_for_series = last_seen_ts

                for ch in new_chapters:
                    role_id = sdata.get("role_id")
                    role_mention = f"<@&{role_id}>" if role_id else ""

                    msg = (
                        f"{role_mention} New chapter released!\n"
                        f"**{series_name}** - Chapter {ch['chapter']}\n"
                        f"Link: {ch['url']}"
                    )
                    try:
                        await channel.send(msg)
                    except discord.Forbidden:
                        print(f"[WARN] No permission to send in channel {channel_id}")
                        break
                    except Exception as e:
                        print(f"[ERROR] Failed to send message in channel {channel_id}: {e}")
                        break

                    latest_ts_for_series = ch["readableAt"]

                if latest_ts_for_series:
                    sdata["last_seen_ts"] = latest_ts_for_series
                    gdata["tracked_series"][series_id] = sdata
            save_data(data)


@check_releases.before_loop
async def before_check_releases():
    print("Waiting for bot to be ready...")
    await bot.wait_until_ready()
    print("Release checker started.")

# ================== COMMANDS ==================


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if not check_releases.is_running():
        check_releases.start()


@bot.command(name="set_announce_channel")
@is_staff_or_owner()
async def set_announce_channel(ctx: commands.Context, channel: discord.TextChannel):
    gdata = get_guild_data(ctx.guild.id)
    gdata["announce_channel_id"] = channel.id
    save_data(data)
    await ctx.send(f"Announcement channel set to {channel.mention}.")


def extract_mangadex_id(series_arg: str) -> str:
    """
    Accept either a raw MangaDex ID or a full URL and return the ID.
    Examples:
      - "a1b2c3d4-..."  -> same
      - "https://mangadex.org/title/a1b2c3d4-.../name" -> "a1b2c3d4-..."
    """
    if "mangadex.org" in series_arg:
        parts = series_arg.strip("/").split("/")
        for part in parts:
            if "-" in part and len(part) >= 8:
                return part
    return series_arg


@bot.command(name="track")
@is_staff_or_owner()
async def track_series(
    ctx: commands.Context,
    series_id_or_url: str,
    role: discord.Role,
    *,
    name: str,
):
    """
    Add a series (MangaDex) to tracking with a specific ping role.

    Usage:
      !track <mangadex_id_or_url> @Role <name>
    Example:
      !track 9a414441-bbad-43f1-a3a7-dc262ca790a3 @OmniscientReaderPing Omniscient Reader's Viewpoint
    """
    series_id = extract_mangadex_id(series_id_or_url)

    gdata = get_guild_data(ctx.guild.id)
    tracked = gdata.setdefault("tracked_series", {})

    if series_id in tracked:
        await ctx.send(
            f"Already tracking `{series_id}` as **{tracked[series_id]['name']}** "
            f"with role <@&{tracked[series_id].get('role_id', 0)}>."
        )
        return

    tracked[series_id] = {
        "name": name,
        "last_seen_ts": None,
        "role_id": role.id,
    }
    save_data(data)
    await ctx.send(
        f"Now tracking **{name}** (MangaDex ID: `{series_id}`), pinging role {role.mention}."
    )


@bot.command(name="untrack")
@is_staff_or_owner()
async def untrack_series(ctx: commands.Context, series_id_or_url: str):
    series_id = extract_mangadex_id(series_id_or_url)

    gdata = get_guild_data(ctx.guild.id)
    tracked = gdata.setdefault("tracked_series", {})

    if series_id not in tracked:
        await ctx.send(f"Series `{series_id}` is not currently being tracked.")
        return

    removed = tracked.pop(series_id)
    save_data(data)
    await ctx.send(f"Stopped tracking **{removed['name']}** (ID: `{series_id}`).")


@bot.command(name="list_tracked")
@is_staff_or_owner()
async def list_tracked(ctx: commands.Context):
    gdata = get_guild_data(ctx.guild.id)
    tracked = gdata.get("tracked_series", {})

    if not tracked:
        await ctx.send("No series are currently being tracked.")
        return

    lines = []
    for sid, sdata in tracked.items():
        last_seen = sdata.get("last_seen_ts") or "none yet"
        role_id = sdata.get("role_id")
        role = ctx.guild.get_role(role_id) if role_id else None
        role_text = role.mention if role else "no role"
        lines.append(
            f"- **{sdata['name']}** (ID: `{sid}`, role: {role_text}, last seen: `{last_seen}`)"
        )

    await ctx.send("Tracked series:\n" + "\n".join(lines))


@bot.command(name="test_release")
@is_staff_or_owner()
async def test_release(ctx: commands.Context):
    """Send a fake release message for the first tracked series to test the bot."""
    if ctx.guild is None:
        await ctx.send("Run this command inside a server, not in DMs.")
        return

    gdata = get_guild_data(ctx.guild.id)
    channel_id = gdata.get("announce_channel_id")

    if not channel_id:
        await ctx.send("Announcement channel is not set. Use !set_announce_channel #channel first.")
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            await ctx.send("I couldn't fetch the announcement channel. Check my permissions.")
            return

    tracked = gdata.get("tracked_series", {})
    if not tracked:
        await ctx.send("No series are currently being tracked. Use !track first.")
        return

    # Just pick the first tracked series for testing
    series_id, sdata = next(iter(tracked.items()))
    series_name = sdata.get("name", f"Series {series_id}")
    role_id = sdata.get("role_id")
    role_mention = f"<@&{role_id}>" if role_id else ""

    msg = (
        f"{role_mention} Test: New chapter released!\n"
        f"**{series_name}** - Chapter 1 (test)\n"
        f"Link: https://example.com"
    )

    try:
        await channel.send(msg)
        await ctx.send(f"Sent a test release message for **{series_name}** in {channel.mention}.")
    except discord.Forbidden:
        await ctx.send("I don't have permission to send messages in the announcement channel.")
    except Exception as e:
        await ctx.send(f"Failed to send message: {e}")

# ================== ENTRYPOINT ==================


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN not set in code.")

    bot.run(TOKEN)

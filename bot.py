import argparse
import os
import sqlite3
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# -------------------------
# Daily puzzle settings
# -------------------------

# Puzzle #41 is on August 25, 2026.
START_DATE = date(2026, 8, 25)
START_PUZZLE = 41

# Eastern Time.
PUZZLE_TIMEZONE = ZoneInfo("America/New_York")

# PID file used to detect whether the normal bot is already running.
PID_FILE = "bot.pid"


def get_todays_puzzle():
    today = datetime.now(PUZZLE_TIMEZONE).date()
    days_since_start = (today - START_DATE).days
    return START_PUZZLE + days_since_start


def get_puzzle_for_date(requested_date):
    days_since_start = (requested_date - START_DATE).days
    return START_PUZZLE + days_since_start


# -------------------------
# PID / PROCESS MANAGEMENT
# -------------------------

def is_process_running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_bot_already_running():
    if not os.path.exists(PID_FILE):
        return False

    try:
        with open(PID_FILE, "r") as file:
            pid = int(file.read().strip())
    except (ValueError, OSError):
        return False

    if is_process_running(pid) and (pid != os.getpid()):
        return True

    # PID file is stale.
    try:
        os.remove(PID_FILE)
    except OSError:
        pass

    return False


def create_pid_file():
    with open(PID_FILE, "w") as file:
        file.write(str(os.getpid()))


def remove_pid_file():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


# -------------------------
# Database
# -------------------------

db = sqlite3.connect("leaderboard.db", check_same_thread=False)

cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS scores (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    puzzle INTEGER NOT NULL,
    score INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, puzzle)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    auto_leaderboard_enabled INTEGER NOT NULL DEFAULT 0,
    leaderboard_channel_id INTEGER,
    last_posted_puzzle INTEGER
)
""")

db.commit()


# -------------------------
# Message parsing
# -------------------------

def parse_score(message):
    """
    Expected format: Krillion #41 🦐 415
    Anything after the score is ignored.
    Returns: (puzzle, score)
    or None if the message is invalid.
    """

    parts = message.split()

    if len(parts) < 4:
        return None

    if parts[0] != "Krillion":
        return None

    if not parts[1].startswith("#"):
        return None

    if parts[2] != "🦐":
        return None

    try:
        puzzle = int(parts[1][1:])
        score = int(parts[3])
    except ValueError:
        return None

    return puzzle, score


# -------------------------
# Discord setup
# -------------------------

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ============================================================
# SYNC TODAY'S SCORES
# ============================================================

async def sync_today_scores(guild):
    """
    Read today's messages from the server and add any missing
    Krillion scores to the database.

    Messages are read oldest-first so the first submission
    from each user counts, matching normal message handling.

    Returns the number of new scores added.
    """

    today = datetime.now(PUZZLE_TIMEZONE).date()
    todays_puzzle = get_todays_puzzle()

    start_of_day = datetime.combine(today, datetime.min.time(), tzinfo=PUZZLE_TIMEZONE)
    end_of_day = datetime.combine(today, datetime.max.time(), tzinfo=PUZZLE_TIMEZONE)

    added = 0

    for channel in guild.text_channels:
        try:
            async for message in channel.history(
                    after=start_of_day,
                    before=end_of_day,
                    limit=None,
                    oldest_first=True
            ):

                if message.author.bot:
                    continue

                result = parse_score(message.content)

                if result is None:
                    continue

                message_puzzle, score = result

                if message_puzzle != todays_puzzle:
                    continue

                user_id = message.author.id
                username = message.author.display_name
                guild_id = guild.id

                cursor.execute("""
                    SELECT score
                    FROM scores
                    WHERE guild_id = ?
                    AND user_id = ?
                    AND puzzle = ?
                """, (
                    guild_id,
                    user_id,
                    todays_puzzle
                ))

                existing = cursor.fetchone()

                if existing:
                    continue

                cursor.execute("""
                    INSERT INTO scores
                    (guild_id, user_id, username, puzzle, score)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    guild_id,
                    user_id,
                    username,
                    todays_puzzle,
                    score
                ))

                db.commit()
                added += 1

        except discord.Forbidden:
            print(f"Could not read messages in #{channel.name} in {guild.name}: missing permissions.")

        except discord.HTTPException as error:
            print(f"Discord error reading #{channel.name}: {error}")

    return added


# ============================================================
# LEADERBOARD FUNCTIONS
# ============================================================

async def create_leaderboard_embed(guild_id, puzzle, requested_date):
    cursor.execute("""
        SELECT username, score
        FROM scores
        WHERE guild_id = ?
        AND puzzle = ?
        ORDER BY score ASC
    """, (
        guild_id,
        puzzle
    ))

    results = cursor.fetchall()

    if not results:
        return None

    lines = []

    medals = ["🥇", "🥈", "🥉"]

    for i, (username, score) in enumerate(results):

        if i < 3:
            prefix = medals[i]
        else:
            prefix = f"**{i + 1}.**"

        lines.append(f"{prefix} {username} — **{score}**")

    formatted_date = requested_date.strftime("%B %d, %Y").replace(" 0", " ")

    embed = discord.Embed(
        title=f"🏆 Puzzle #{puzzle}",
        description=(f"**{formatted_date}**\n\n" + "\n".join(lines)),
        color=discord.Color.gold()
    )
    return embed


# ============================================================
# POST NIGHTLY LEADERBOARDS
# ============================================================

async def post_nightly_leaderboards():
    """
    Sync and post the leaderboard for every guild that has
    automatic leaderboards enabled.

    Returns the number of leaderboards posted.
    """

    puzzle = get_todays_puzzle()
    today = datetime.now(PUZZLE_TIMEZONE).date()

    cursor.execute("""
        SELECT
            guild_id,
            leaderboard_channel_id,
            last_posted_puzzle
        FROM guild_settings
        WHERE auto_leaderboard_enabled = 1
    """)

    guilds = cursor.fetchall()

    posted = 0

    for (guild_id, channel_id, last_posted_puzzle) in guilds:

        if channel_id is None:
            continue

        if last_posted_puzzle == puzzle:
            continue

        guild = client.get_guild(guild_id)

        if guild is None:
            continue

        # Sync messages before creating the leaderboard.
        added = await sync_today_scores(guild)

        print(f"{guild.name}: synced {added} new score(s).")

        channel = guild.get_channel(channel_id)

        if channel is None:
            continue

        embed = await create_leaderboard_embed(guild_id, puzzle, today)

        if embed is None:
            continue

        try:
            await channel.send(embed=embed)

            cursor.execute("""
                UPDATE guild_settings
                SET last_posted_puzzle = ?
                WHERE guild_id = ?
            """, (
                puzzle,
                guild_id
            ))

            db.commit()

            posted += 1
            print(f"Posted puzzle #{puzzle} leaderboard to {guild.name}.")

        except discord.Forbidden:
            print(f"Could not post leaderboard in guild {guild_id}: missing permissions.")

        except discord.HTTPException as error:
            print(f"Discord error posting leaderboard in guild {guild_id}: {error}")

    return posted


# ============================================================
# NORMAL BOT STARTUP
# ============================================================

@client.event
async def on_ready():
    await tree.sync()

    # Scheduled one-shot mode
    if SCHEDULED_MODE:
        now = datetime.now(PUZZLE_TIMEZONE)

        print(f"Scheduled run started at {now.strftime('%I:%M %p')} Eastern.")

        # Always sync today's scores.
        for guild in client.guilds:
            added = await sync_today_scores(guild)
            print(f"{guild.name}: synced {added} new score(s).")

        # Only post leaderboard at 11:59 PM.
        if now.hour == 23 and now.minute == 59:
            print("11:59 PM — posting nightly leaderboards.")
            posted = await post_nightly_leaderboards()
            print(f"Posted {posted} leaderboard(s).")
        else:
            print("11:59 AM — sync only. No leaderboard posted.")

        # Close after the one-shot job is finished.
        await client.close()
        return

    # Normal always-running bot
    if not nightly_leaderboard.is_running():
        nightly_leaderboard.start()

    print(f"Logged in as {client.user}")


# ============================================================
# MESSAGE HANDLING
# ============================================================

@client.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild is None:
        return

    result = parse_score(message.content)

    if result is None:
        return

    puzzle, score = result

    todays_puzzle = get_todays_puzzle()

    if puzzle != todays_puzzle:
        return

    user_id = message.author.id
    username = message.author.display_name
    guild_id = message.guild.id

    cursor.execute("""
        SELECT score
        FROM scores
        WHERE guild_id = ?
        AND user_id = ?
        AND puzzle = ?
    """, (
        guild_id,
        user_id,
        puzzle
    ))

    existing = cursor.fetchone()

    if existing:
        return

    cursor.execute("""
        INSERT INTO scores
        (guild_id, user_id, username, puzzle, score)
        VALUES (?, ?, ?, ?, ?)
    """, (
        guild_id,
        user_id,
        username,
        puzzle,
        score
    ))

    db.commit()


# ============================================================
# /sync
# ============================================================

@tree.command(
    name="sync",
    description="Sync today's Krillion scores from server messages"
)
@app_commands.default_permissions(
    manage_guild=True
)
async def sync_command(
        interaction: discord.Interaction
):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    added = await sync_today_scores(interaction.guild)

    await interaction.followup.send(
        f"✅ Sync complete. Added **{added} new score(s)** "
        f"for puzzle **#{get_todays_puzzle()}**.",
        ephemeral=True
    )


# ============================================================
# /leaderboard
# ============================================================

@tree.command(
    name="leaderboard",
    description="Show the leaderboard for a puzzle"
)
@app_commands.describe(
    date="Optional date (MM/DD/YYYY). Leave blank for today."
)
async def leaderboard(
        interaction: discord.Interaction,
        date: Optional[str] = None
):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    if date is None:
        requested_date = datetime.now(PUZZLE_TIMEZONE).date()
    else:
        try:
            requested_date = datetime.strptime(date, "%m/%d/%Y").date()
        except ValueError:
            await interaction.response.send_message("Invalid date. Please use MM/DD/YYYY.", ephemeral=True)
            return

    today = datetime.now(PUZZLE_TIMEZONE).date()

    if requested_date < START_DATE:
        await interaction.response.send_message("There was no puzzle for that date.", ephemeral=True)
        return

    if requested_date > today:
        await interaction.response.send_message("That puzzle hasn't happened yet.", ephemeral=True)
        return

    puzzle = get_puzzle_for_date(requested_date)

    guild_id = interaction.guild.id

    embed = await create_leaderboard_embed(guild_id, puzzle, requested_date)

    if embed is None:
        await interaction.response.send_message(f"No scores have been submitted for puzzle #{puzzle}.", ephemeral=True)
        return

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# /leaderboard_auto
# ============================================================

@tree.command(
    name="leaderboard_auto",
    description="Turn the nightly public leaderboard on or off"
)
@app_commands.describe(
    enabled="Enable or disable the nightly leaderboard"
)
@app_commands.choices(
    enabled=[
        app_commands.Choice(
            name="On",
            value="on"
        ),
        app_commands.Choice(
            name="Off",
            value="off"
        )
    ]
)
@app_commands.default_permissions(
    manage_guild=True
)
async def leaderboard_auto(
        interaction: discord.Interaction,
        enabled: app_commands.Choice[str]
):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    guild_id = interaction.guild.id

    is_enabled = enabled.value == "on"

    cursor.execute("""
        INSERT OR IGNORE INTO guild_settings
        (guild_id)
        VALUES (?)
    """, (
        guild_id,
    ))

    cursor.execute("""
        UPDATE guild_settings
        SET auto_leaderboard_enabled = ?
        WHERE guild_id = ?
    """, (
        1 if is_enabled else 0,
        guild_id
    ))

    db.commit()

    if is_enabled:
        await interaction.response.send_message(
            "✅ The nightly public leaderboard is now **ON**.\n\n"
            "It will be posted at **11:59 PM Eastern** "
            "in the configured leaderboard channel.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message("✅ The nightly public leaderboard is now **OFF**.", ephemeral=True)


# ============================================================
# /leaderboard_auto_channel
# ============================================================

@tree.command(
    name="leaderboard_auto_channel",
    description="Set the channel for the nightly public leaderboard"
)
@app_commands.describe(
    channel="The channel where the nightly leaderboard should be posted"
)
@app_commands.default_permissions(
    manage_guild=True
)
async def leaderboard_auto_channel(
        interaction: discord.Interaction,
        channel: discord.TextChannel
):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    guild_id = interaction.guild.id

    cursor.execute("""
        INSERT OR IGNORE INTO guild_settings
        (guild_id)
        VALUES (?)
    """, (
        guild_id,
    ))

    cursor.execute("""
        UPDATE guild_settings
        SET leaderboard_channel_id = ?
        WHERE guild_id = ?
    """, (
        channel.id,
        guild_id
    ))

    db.commit()

    await interaction.response.send_message(
        f"✅ Nightly leaderboards will be posted in {channel.mention}.",
        ephemeral=True
    )


# ============================================================
# NORMAL NIGHTLY TASK
# ============================================================

@tasks.loop(minutes=1)
async def nightly_leaderboard():
    now = datetime.now(PUZZLE_TIMEZONE)

    # Only run at 11:59 PM Eastern.
    if now.hour != 23 or now.minute != 59:
        return

    print("Running normal nightly leaderboard.")

    await post_nightly_leaderboards()


# ============================================================
# /help
# ============================================================

@tree.command(
    name="help",
    description="Show Krillion leaderboard commands"
)
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🦐 Krillion Leaderboard Help",
        description="Commands available for the Krillion leaderboard.",
        color=discord.Color.gold()
    )

    embed.add_field(
        name="🦐 Play Krillion",
        value="[Play today's puzzle](https://playlin.io/game/krillion/)",
        inline=False
    )

    embed.add_field(
        name="🏆 Leaderboard",
        value=(
            "`/leaderboard` — View today's leaderboard\n"
            "`/leaderboard date:MM/DD/YYYY` — "
            "View a previous leaderboard"
        ),
        inline=False
    )

    embed.add_field(
        name="📢 Automatic Leaderboard",
        value=(
            "`/sync` — Sync today's scores from server messages\n"
            "`/leaderboard_auto enabled:On/Off` — "
            "Enable or disable the nightly public leaderboard\n"
            "`/leaderboard_auto_channel channel:#channel` — "
            "Choose where it is posted"
        ),
        inline=False
    )

    embed.set_footer(
        text="Automatic leaderboards are posted at 11:59 PM Eastern."
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# ONE-SHOT SCHEDULED MODE
# ============================================================

SCHEDULED_MODE = False


async def run_scheduled_mode():
    print("Starting scheduled mode.")

    try:
        await client.start(TOKEN)
    finally:
        await client.close()


# ============================================================
# MAIN
# ============================================================

def main():
    global SCHEDULED_MODE

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Run the scheduled sync/post once and then exit."
    )

    args = parser.parse_args()

    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set in the environment.")

    # --------------------------------------------------------
    # SCHEDULED MODE
    # --------------------------------------------------------

    if args.scheduled:
        if is_bot_already_running():
            print("Normal bot is already running. Skipping scheduled job.")
            return

        print("Normal bot is not running. Starting scheduled job.")

        SCHEDULED_MODE = True

        try:
            asyncio.run(run_scheduled_mode())
        except KeyboardInterrupt:
            print("Scheduled job interrupted.")
        return

    # --------------------------------------------------------
    # NORMAL BOT MODE
    # --------------------------------------------------------

    if is_bot_already_running():
        raise RuntimeError("Another copy of the bot is already running.")

    create_pid_file()

    try:
        client.run(TOKEN)
    finally:
        remove_pid_file()


if __name__ == "__main__":
    import asyncio

    main()

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
# This automatically handles EST/EDT.
PUZZLE_TIMEZONE = ZoneInfo("America/New_York")


def get_todays_puzzle():
    """
    Calculate today's puzzle number.

    August 25, 2026 = Puzzle #41
    August 26, 2026 = Puzzle #42
    etc.
    """

    today = datetime.now(PUZZLE_TIMEZONE).date()

    days_since_start = (today - START_DATE).days

    return START_PUZZLE + days_since_start


def get_puzzle_for_date(requested_date):
    """
    Convert a date into a Krillion puzzle number.
    """

    days_since_start = (
        requested_date - START_DATE
    ).days

    return START_PUZZLE + days_since_start


# -------------------------
# Database
# -------------------------

db = sqlite3.connect(
    "leaderboard.db",
    check_same_thread=False
)

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
    Expected format:

        Krillion #41 🦐 415

    Anything after the score is ignored.

    Returns:
        (puzzle, score)

    or None if the message is invalid.
    """

    parts = message.split()

    # Need at least:
    # Krillion #41 🦐 415
    if len(parts) < 4:
        return None

    # First word must be "Krillion"
    if parts[0] != "Krillion":
        return None

    # Second item must be a puzzle number beginning with #
    if not parts[1].startswith("#"):
        return None

    # Third item must be the shrimp emoji
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


# -------------------------
# Bot startup
# -------------------------

@client.event
async def on_ready():

    await tree.sync()

    # Start the nightly leaderboard task.
    if not nightly_leaderboard.is_running():
        nightly_leaderboard.start()

    print(f"Logged in as {client.user}")


# -------------------------
# Message handling
# -------------------------

@client.event
async def on_message(message):

    # Ignore messages sent by bots
    if message.author.bot:
        return

    # Make sure this is a server message
    if message.guild is None:
        return

    # Try to parse the score
    result = parse_score(message.content)

    if result is None:
        return

    puzzle, score = result

    # Only accept scores for today's puzzle.
    todays_puzzle = get_todays_puzzle()

    if puzzle != todays_puzzle:
        return

    # Get the real Discord user's identity
    user_id = message.author.id
    username = message.author.display_name
    guild_id = message.guild.id

    # -------------------------
    # Check existing score
    # -------------------------

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

    # -------------------------
    # Already submitted
    # -------------------------

    if existing:
        return

    # -------------------------
    # First submission
    # -------------------------

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
    puzzle = get_todays_puzzle()

    # Start/end of today in Eastern Time.
    start_of_day = datetime.combine(
        today,
        datetime.min.time(),
        tzinfo=PUZZLE_TIMEZONE
    )

    end_of_day = datetime.combine(
        today,
        datetime.max.time(),
        tzinfo=PUZZLE_TIMEZONE
    )

    added = 0

    for channel in guild.text_channels:

        try:
            async for message in channel.history(
                after=start_of_day,
                before=end_of_day,
                limit=None,
                oldest_first=True
            ):

                # Ignore messages sent by bots.
                if message.author.bot:
                    continue

                # Parse the message.
                result = parse_score(message.content)

                if result is None:
                    continue

                message_puzzle, score = result

                # Only accept today's puzzle.
                if message_puzzle != puzzle:
                    continue

                user_id = message.author.id
                username = message.author.display_name
                guild_id = guild.id

                # Check whether this user already has a score.
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
                    continue

                # Add the score.
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
                added += 1

        except discord.Forbidden:
            print(
                f"Could not read messages in #{channel.name} "
                f"in {guild.name}: missing permissions."
            )

        except discord.HTTPException as error:
            print(
                f"Discord error reading #{channel.name}: {error}"
            )

    return added


# ============================================================
# LEADERBOARD FUNCTIONS
# ============================================================

async def create_leaderboard_embed(
    guild_id,
    puzzle,
    requested_date
):
    """
    Create the leaderboard embed for a guild/puzzle.
    """

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

    medals = [
        "🥇",
        "🥈",
        "🥉"
    ]

    for i, (username, score) in enumerate(results):

        if i < 3:
            prefix = medals[i]
        else:
            prefix = f"**{i + 1}.**"

        lines.append(
            f"{prefix} {username} — **{score}**"
        )

    formatted_date = requested_date.strftime(
        "%B %d, %Y"
    ).replace(" 0", " ")

    embed = discord.Embed(
        title=f"🏆 Puzzle #{puzzle}",
        description=(
            f"**{formatted_date}**\n\n"
            + "\n".join(lines)
        ),
        color=discord.Color.gold()
    )

    return embed


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
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    await interaction.response.defer(
        ephemeral=True
    )

    added = await sync_today_scores(
        interaction.guild
    )

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

    # Make sure this is being used in a server
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )
        return

    # -------------------------
    # Determine requested date
    # -------------------------

    if date is None:

        requested_date = datetime.now(
            PUZZLE_TIMEZONE
        ).date()

    else:

        try:
            requested_date = datetime.strptime(
                date,
                "%m/%d/%Y"
            ).date()

        except ValueError:

            await interaction.response.send_message(
                "Invalid date. Please use MM/DD/YYYY.",
                ephemeral=True
            )

            return

    # -------------------------
    # Make sure date is valid
    # -------------------------

    today = datetime.now(
        PUZZLE_TIMEZONE
    ).date()

    if requested_date < START_DATE:

        await interaction.response.send_message(
            "There was no puzzle for that date.",
            ephemeral=True
        )

        return

    if requested_date > today:

        await interaction.response.send_message(
            "That puzzle hasn't happened yet.",
            ephemeral=True
        )

        return

    # -------------------------
    # Calculate puzzle number
    # -------------------------

    puzzle = get_puzzle_for_date(
        requested_date
    )

    guild_id = interaction.guild.id

    # -------------------------
    # Create leaderboard
    # -------------------------

    embed = await create_leaderboard_embed(
        guild_id,
        puzzle,
        requested_date
    )

    if embed is None:

        await interaction.response.send_message(
            f"No scores have been submitted for "
            f"puzzle #{puzzle}.",
            ephemeral=True
        )

        return

    # -------------------------
    # PRIVATE leaderboard
    # -------------------------

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ============================================================
# AUTOMATIC LEADERBOARD SETTINGS
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

        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )

        return

    guild_id = interaction.guild.id

    is_enabled = enabled.value == "on"

    # Make sure settings row exists.
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

        await interaction.response.send_message(
            "✅ The nightly public leaderboard is now **OFF**.",
            ephemeral=True
        )


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

        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True
        )

        return

    guild_id = interaction.guild.id

    # Make sure settings row exists.
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
        f"✅ Nightly leaderboards will be posted in "
        f"{channel.mention}.",
        ephemeral=True
    )


# ============================================================
# NIGHTLY LEADERBOARD TASK
# ============================================================

@tasks.loop(minutes=1)
async def nightly_leaderboard():

    now = datetime.now(PUZZLE_TIMEZONE)

    # Only run at 11:59 PM Eastern.
    if now.hour != 23 or now.minute != 59:
        return

    # Today's puzzle.
    puzzle = get_todays_puzzle()
    today = now.date()

    # Get every guild that has automatic leaderboards enabled.
    cursor.execute("""
        SELECT
            guild_id,
            leaderboard_channel_id,
            last_posted_puzzle
        FROM guild_settings
        WHERE auto_leaderboard_enabled = 1
    """)

    guilds = cursor.fetchall()

    for (
        guild_id,
        channel_id,
        last_posted_puzzle
    ) in guilds:

        # No channel configured.
        if channel_id is None:
            continue

        # Already posted this puzzle.
        if last_posted_puzzle == puzzle:
            continue

        guild = client.get_guild(guild_id)

        if guild is None:
            continue

        # Sync any scores submitted while the bot
        # was offline before creating the leaderboard.
        await sync_today_scores(guild)

        channel = guild.get_channel(channel_id)

        if channel is None:
            continue

        # Build leaderboard.
        embed = await create_leaderboard_embed(
            guild_id,
            puzzle,
            today
        )

        # Don't post an empty leaderboard.
        if embed is None:
            continue

        try:

            await channel.send(
                embed=embed
            )

            # Record that we posted this puzzle.
            cursor.execute("""
                UPDATE guild_settings
                SET last_posted_puzzle = ?
                WHERE guild_id = ?
            """, (
                puzzle,
                guild_id
            ))

            db.commit()

        except discord.Forbidden:

            print(
                f"Could not post leaderboard in "
                f"guild {guild_id}: missing permissions."
            )

        except discord.HTTPException as error:

            print(
                f"Discord error posting leaderboard "
                f"in guild {guild_id}: {error}"
            )


# -------------------------
# /help command
# -------------------------

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
            "`/leaderboard date:MM/DD/YYYY` — View a previous leaderboard"
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
# Start bot
# ============================================================

if __name__ == "__main__":

    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is not set in the environment."
        )

    client.run(TOKEN)
# Stylo — Discord Fashion Battle Bot
# -------------------------------------------------------------
# Tech: discord.py, sqlite3 (WAL), aiohttp, Pillow, tasks.loop
# Env:  DISCORD_TOKEN (required), STYLO_DB_PATH, CONFETTI_GIF_PATH, LOG_CHANNEL_ID
# Perms: Admin-only to start/reset/configure; per-ticket private channels
# Style: Neon pink/purple embed colour
# -------------------------------------------------------------

import os
import io
import math
import asyncio
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, List

import aiohttp
from PIL import Image, ImageOps, ImageDraw

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ----------------------- Config / Env -----------------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in your environment")

DB_PATH = os.getenv("STYLO_DB_PATH", "stylo.db")
CONFETTI_GIF_PATH = os.getenv("CONFETTI_GIF_PATH", "")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # to listen for ticket uploads
INTENTS.members = True

EMBED_COLOUR = discord.Colour.from_rgb(224, 64, 255)  # neon pink/purple vibe

# ----------------------- Bot Setup --------------------------
class StyloBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=INTENTS)
        self.tree = app_commands.CommandTree(self)
        self.session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        await self.tree.sync()
        scheduler.start()

bot = StyloBot()

# ----------------------- DB Helpers -------------------------
def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

SQL_INIT = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS event (
  guild_id INTEGER PRIMARY KEY,
  theme TEXT,
  state TEXT CHECK(state IN ('entry','voting','closed')) DEFAULT 'closed',
  entry_end_utc TEXT,
  vote_hours REAL DEFAULT 0,
  vote_seconds INTEGER DEFAULT 0,
  round_index INTEGER DEFAULT 0,
  main_channel_id INTEGER,
  start_msg_id INTEGER
);

CREATE TABLE IF NOT EXISTS entrant (
  guild_id INTEGER,
  user_id INTEGER,
  display_name TEXT,
  caption TEXT,
  image_url TEXT,
  PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS ticket (
  guild_id INTEGER,
  user_id INTEGER,
  channel_id INTEGER,
  PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS match (
  guild_id INTEGER,
  round_index INTEGER,
  match_id INTEGER,
  left_id INTEGER,
  right_id INTEGER,
  end_utc TEXT,
  left_votes INTEGER DEFAULT 0,
  right_votes INTEGER DEFAULT 0,
  winner_id INTEGER,
  message_id INTEGER,
  thread_id INTEGER,
  PRIMARY KEY (guild_id, round_index, match_id)
);

CREATE TABLE IF NOT EXISTS voter (
  guild_id INTEGER,
  round_index INTEGER,
  match_id INTEGER,
  user_id INTEGER,
  side TEXT CHECK(side IN ('left','right')),
  PRIMARY KEY (guild_id, round_index, match_id, user_id)
);

-- Guild settings (log_channel_id, ticket_category_id)
CREATE TABLE IF NOT EXISTS guild_settings (
  guild_id INTEGER PRIMARY KEY,
  log_channel_id INTEGER,
  ticket_category_id INTEGER
);
"""

def init_db():
    con = db_connect()
    con.executescript(SQL_INIT)
    con.commit()
    con.close()

init_db()

# ---------------------- Utility funcs -----------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def fetch_image_bytes(url: str, session: aiohttp.ClientSession) -> Optional[bytes]:
    try:
        async with session.get(url, timeout=30) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception:
        return None
    return None


def humanize_seconds(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    mins = seconds // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins / 60
    if hours.is_integer():
        return f"{int(hours)}h"
    return f"{hours:.1f}h"


def is_admin(member: discord.abc.User | discord.Member) -> bool:
    if isinstance(member, discord.Member) and (member.guild_permissions.manage_guild or member.guild_permissions.administrator):
        return True
    return False


# ---------------------- Settings helpers --------------------

def get_settings(guild_id: int) -> dict:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    con.close()
    return dict(row) if row else {"guild_id": guild_id, "log_channel_id": None, "ticket_category_id": None}


def set_log_channel_id(guild_id: int, channel_id: int):
    con = db_connect()
    con.execute(
        "INSERT INTO guild_settings (guild_id, log_channel_id) VALUES (?,?)\n         ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
        (guild_id, channel_id),
    )
    con.commit(); con.close()


def set_ticket_category_id(guild_id: int, category_id: int):
    con = db_connect()
    con.execute(
        "INSERT INTO guild_settings (guild_id, ticket_category_id) VALUES (?,?)\n         ON CONFLICT(guild_id) DO UPDATE SET ticket_category_id=excluded.ticket_category_id",
        (guild_id, category_id),
    )
    con.commit(); con.close()


# ---------------------- Image composer ----------------------
async def build_vs_card(left_url: str, right_url: str, session: aiohttp.ClientSession) -> Optional[discord.File]:
    """Fetch two images, pad to square, compose side-by-side with divider.
    Returns a discord.File ready to attach."""
    lb = await fetch_image_bytes(left_url, session)
    rb = await fetch_image_bytes(right_url, session)
    if not lb or not rb:
        return None

    def to_square(img: Image.Image) -> Image.Image:
        # Pad to square with transparent background, then fit
        size = max(img.width, img.height)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        x = (size - img.width) // 2
        y = (size - img.height) // 2
        canvas.paste(img, (x, y))
        return canvas

    with Image.open(io.BytesIO(lb)).convert("RGBA") as L, Image.open(io.BytesIO(rb)).convert("RGBA") as R:
        L = to_square(L)
        R = to_square(R)
        # Standard side length
        side = 768
        L = ImageOps.fit(L, (side, side))
        R = ImageOps.fit(R, (side, side))

        gap = 10
        W = side * 2 + gap
        H = side
        canvas = Image.new("RGBA", (W, H), (10, 10, 10, 255))
        canvas.paste(L, (0, 0))
        canvas.paste(R, (side + gap, 0))

        # Divider
        draw = ImageDraw.Draw(canvas)
        draw.rectangle([side, 0, side + gap, H], fill=(224, 64, 255, 255))

        # Export
        bio = io.BytesIO()
        canvas.convert("RGB").save(bio, format="JPEG", quality=90)
        bio.seek(0)
        return discord.File(bio, filename="stylo_vs.jpg")


# ---------------------- UI: Modals & Views ------------------
class StyloStartModal(discord.ui.Modal, title="Start Stylo Event"):
    theme = discord.ui.TextInput(label="Theme", max_length=100)
    entry_window = discord.ui.TextInput(label="Entry Window (e.g., 2h, 45m, 1.5h)", placeholder="e.g. 2h")
    vote_window = discord.ui.TextInput(label="Vote Window (e.g., 30m or 0.5h)", placeholder="e.g. 30m")

    def __init__(self, inter: discord.Interaction):
        super().__init__()
        self.inter = inter

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.inter.user.id:
            await interaction.response.send_message("Only the admin who opened this can submit.", ephemeral=True)
            return
        # Parse durations
        def parse_dur(s: str) -> int:
            s = s.strip().lower()
            if s.endswith("h"):
                return int(float(s[:-1]) * 3600)
            if s.endswith("m"):
                return int(float(s[:-1]) * 60)
            # default seconds
            return int(float(s))

        entry_seconds = parse_dur(str(self.entry_window))
        vote_seconds = parse_dur(str(self.vote_window))

        end = utcnow() + timedelta(seconds=entry_seconds)
        con = db_connect()
        con.execute(
            "INSERT INTO event (guild_id, theme, state, entry_end_utc, vote_seconds, round_index, main_channel_id)\n             VALUES (?,?,?,?,?,?,?)\n             ON CONFLICT(guild_id) DO UPDATE SET theme=excluded.theme, state='entry', entry_end_utc=excluded.entry_end_utc, vote_seconds=excluded.vote_seconds, round_index=0, main_channel_id=excluded.main_channel_id",
            (interaction.guild_id, str(self.theme), "entry", end.isoformat(), vote_seconds, 0, interaction.channel_id),
        )
        con.commit(); con.close()

        # Post start embed with Join button
        view = JoinView()
        em = discord.Embed(title=f"Stylo — {self.theme}", colour=EMBED_COLOUR)
        em.description = (
            f"**Entries are open!**\n"
            f"Theme: **{self.theme}**\n\n"
            f"Entries close in **{humanize_seconds(entry_seconds)}**.\n"
            f"Voting window per round: **{humanize_seconds(vote_seconds)}**\n\n"
            f"Press **Join** to submit your entry."
        )
        msg = await interaction.channel.send(embed=em, view=view)

        # Save start message id
        con = db_connect()
        con.execute("UPDATE event SET start_msg_id=? WHERE guild_id=?", (msg.id, interaction.guild_id))
        con.commit(); con.close()

        await msg.pin()
        await interaction.response.send_message("Stylo started!", ephemeral=True)


class JoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return
        con = db_connect()
        cur = con.cursor()
        cur.execute("SELECT state FROM event WHERE guild_id=?", (interaction.guild_id,))
        row = cur.fetchone()
        con.close()
        if not row or row["state"] != "entry":
            await interaction.response.send_message("Entries are not open.", ephemeral=True)
            return
        await interaction.response.send_modal(EntrantModal())


class EntrantModal(discord.ui.Modal, title="Enter Stylo"):
    display_name = discord.ui.TextInput(label="Display Name (shown on cards)", max_length=80)
    caption = discord.ui.TextInput(label="Caption (optional)", max_length=200, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return
        # Save entrant basic info
        con = db_connect()
        con.execute(
            "INSERT INTO entrant (guild_id, user_id, display_name, caption) VALUES (?,?,?,?)\n             ON CONFLICT(guild_id, user_id) DO UPDATE SET display_name=excluded.display_name, caption=excluded.caption",
            (interaction.guild_id, interaction.user.id, str(self.display_name), str(self.caption)),
        )
        con.commit(); con.close()

        # Create private ticket channel
        settings = get_settings(interaction.guild_id)
        category = None
        if settings.get("ticket_category_id"):
            category = interaction.guild.get_channel(settings["ticket_category_id"])  # type: ignore

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, embed_links=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),  # type: ignore
        }
        channel = await interaction.guild.create_text_channel(
            name=f"stylo-{interaction.user.name}",
            category=category,
            overwrites=overwrites,
            reason="Stylo entry ticket",
        )
        # Persist ticket
        con = db_connect()
        con.execute(
            "INSERT INTO ticket (guild_id, user_id, channel_id) VALUES (?,?,?)\n             ON CONFLICT(guild_id, user_id) DO UPDATE SET channel_id=excluded.channel_id",
            (interaction.guild_id, interaction.user.id, channel.id),
        )
        con.commit(); con.close()

        text = (
            "Please upload **one** image for your entry in this channel.\n"
            "• **Must be square (1:1)**\n"
            "• **At least 800px** on the long side\n"
            "You may re-upload to replace it - the **last** image before entries close is used.\n"
            "This channel will **self-destruct** after the event ends."
        )
        em = discord.Embed(title="Your Stylo Ticket", description=text, colour=EMBED_COLOUR)
        em.set_footer(text="The bot will react ✅ when your image is saved")
        await channel.send(content=interaction.user.mention, embed=em)

        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)


class VoteView(discord.ui.View):
    def __init__(self, guild_id: int, round_index: int, match_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.round_index = round_index
        self.match_id = match_id

    async def _record_vote(self, interaction: discord.Interaction, side: str):
        # Check event still in voting and match not closed
        con = db_connect()
        cur = con.cursor()
        cur.execute("SELECT state FROM event WHERE guild_id=?", (self.guild_id,))
        ev = cur.fetchone()
        cur.execute("SELECT end_utc, left_votes, right_votes, winner_id FROM match WHERE guild_id=? AND round_index=? AND match_id=?", (self.guild_id, self.round_index, self.match_id))
        m = cur.fetchone()
        if not ev or ev["state"] != "voting" or not m:
            con.close()
            await interaction.response.send_message("Voting is not active.", ephemeral=True)
            return
        if m["winner_id"] is not None:
            con.close()
            await interaction.response.send_message("This match has ended.", ephemeral=True)
            return
        # One vote per user per match (no switching)
        cur.execute("SELECT side FROM voter WHERE guild_id=? AND round_index=? AND match_id=? AND user_id=?",
                    (self.guild_id, self.round_index, self.match_id, interaction.user.id))
        if cur.fetchone():
            con.close()
            await interaction.response.send_message("You already voted in this match.", ephemeral=True)
            return
        # Insert vote
        cur.execute("INSERT INTO voter (guild_id, round_index, match_id, user_id, side) VALUES (?,?,?,?,?)",
                    (self.guild_id, self.round_index, self.match_id, interaction.user.id, side))
        if side == "left":
            cur.execute("UPDATE match SET left_votes=left_votes+1 WHERE guild_id=? AND round_index=? AND match_id=?",
                        (self.guild_id, self.round_index, self.match_id))
        else:
            cur.execute("UPDATE match SET right_votes=right_votes+1 WHERE guild_id=? AND round_index=? AND match_id=?",
                        (self.guild_id, self.round_index, self.match_id))
        con.commit()

        # Update live totals on the embed
        cur.execute("SELECT message_id, left_votes, right_votes FROM match WHERE guild_id=? AND round_index=? AND match_id=?",
                    (self.guild_id, self.round_index, self.match_id))
        row = cur.fetchone()
        con.close()

        if row and interaction.channel:
            try:
                msg = await interaction.channel.fetch_message(row["message_id"])  # type: ignore
                em = msg.embeds[0] if msg.embeds else discord.Embed()
                total = row["left_votes"] + row["right_votes"]
                split = f"{row['left_votes']} vs {row['right_votes']}"
                pct_l = 0 if total == 0 else int(round(row['left_votes'] * 100 / total))
                pct_r = 0 if total == 0 else 100 - pct_l
                em.set_field_at(0, name="Live totals", value=f"{split}  •  {pct_l}% - {pct_r}%", inline=False)
                await msg.edit(embed=em)
            except Exception:
                pass

        await interaction.response.send_message("Vote recorded!", ephemeral=True)

    @discord.ui.button(label="Vote Left", style=discord.ButtonStyle.success)
    async def vote_left(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record_vote(interaction, "left")

    @discord.ui.button(label="Vote Right", style=discord.ButtonStyle.danger)
    async def vote_right(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record_vote(interaction, "right")


# ---------------------- Commands ----------------------------
@bot.tree.command(name="stylo", description="Start a Stylo event (admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def stylo_start(inter: discord.Interaction):
    await inter.response.send_modal(StyloStartModal(inter))


@bot.tree.command(name="stylo_reset", description="Reset Stylo data for this server (admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def stylo_reset(inter: discord.Interaction):
    con = db_connect()
    con.execute("DELETE FROM event WHERE guild_id=?", (inter.guild_id,))
    con.execute("DELETE FROM entrant WHERE guild_id=?", (inter.guild_id,))
    con.execute("DELETE FROM ticket WHERE guild_id=?", (inter.guild_id,))
    con.execute("DELETE FROM match WHERE guild_id=?", (inter.guild_id,))
    con.execute("DELETE FROM voter WHERE guild_id=?", (inter.guild_id,))
    con.commit(); con.close()
    await inter.response.send_message("Stylo data wiped.", ephemeral=True)


settings_group = app_commands.Group(name="stylo_settings", description="Stylo settings")


@settings_group.command(name="set_log_channel", description="Set the log channel for Stylo")
@app_commands.describe(channel="Select a channel to log to")
async def stylo_set_log_channel(inter: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    set_log_channel_id(inter.guild_id, channel.id)
    await inter.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)


@settings_group.command(name="set_ticket_category", description="Set the category where entry tickets are created")
@app_commands.describe(category="Choose a category for entry tickets")
async def stylo_set_ticket_category(inter: discord.Interaction, category: discord.CategoryChannel):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    set_ticket_category_id(inter.guild_id, category.id)
    await inter.response.send_message(f"✅ Ticket category set to **{category.name}**", ephemeral=True)


@settings_group.command(name="show", description="Show current Stylo status")
async def stylo_show(inter: discord.Interaction):
    con = db_connect(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE guild_id=?", (inter.guild_id,))
    ev = cur.fetchone(); con.close()
    if not ev:
        await inter.response.send_message("No active Stylo event.", ephemeral=True); return
    em = discord.Embed(title=f"Stylo — {ev['theme']}", colour=EMBED_COLOUR)
    em.add_field(name="State", value=ev['state'])
    if ev['state'] == 'entry':
        rem = max(0, int((datetime.fromisoformat(ev['entry_end_utc']) - utcnow()).total_seconds()))
        em.add_field(name="Entries close in", value=humanize_seconds(rem))
    elif ev['state'] == 'voting':
        em.add_field(name="Round", value=str(ev['round_index']))
        em.add_field(name="Vote window", value=humanize_seconds(int(ev['vote_seconds'])))
    await inter.response.send_message(embed=em, ephemeral=True)


@settings_group.command(name="show_ticket_category", description="Show configured ticket category")
async def stylo_show_ticket_category(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    settings = get_settings(inter.guild_id)
    if settings.get("ticket_category_id"):
        cat = inter.guild.get_channel(settings["ticket_category_id"])  # type: ignore
        await inter.response.send_message(f"Ticket category: **{getattr(cat,'name','unknown')}**", ephemeral=True)
    else:
        await inter.response.send_message("Ticket category not set.", ephemeral=True)


@bot.tree.command(name="stylo_debug", description="Diagnostics (admin)")
@app_commands.checks.has_permissions(manage_guild=True)
async def stylo_debug(inter: discord.Interaction):
    con = db_connect(); cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM entrant WHERE guild_id=? AND image_url IS NOT NULL", (inter.guild_id,))
    n_imgs = cur.fetchone()[0]
    cur.execute("SELECT * FROM event WHERE guild_id=?", (inter.guild_id,))
    ev = cur.fetchone()
    con.close()
    await inter.response.send_message(f"Imgs: {n_imgs}, Event: {dict(ev) if ev else 'none'}", ephemeral=True)


bot.tree.add_command(settings_group)

# ---------------------- Ticket image capture ----------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    # Is this a ticket channel?
    con = db_connect(); cur = con.cursor()
    cur.execute("SELECT user_id FROM ticket WHERE guild_id=? AND channel_id=?", (message.guild.id, message.channel.id))
    row = cur.fetchone()
    if not row:
        con.close(); return

    # If attachment(s) or image embeds, take the last image
    image_url: Optional[str] = None
    if message.attachments:
        # take last image-like attachment
        for att in message.attachments[::-1]:
            if att.content_type and att.content_type.startswith("image"):
                image_url = att.url
                break
    if not image_url:
        for e in message.embeds[::-1]:
            if e.image and e.image.url:
                image_url = e.image.url
                break
    if not image_url:
        con.close(); return

    # Save to entrant
    cur.execute("UPDATE entrant SET image_url=? WHERE guild_id=? AND user_id=?", (image_url, message.guild.id, row["user_id"]))
    if cur.rowcount == 0:
        # entrant missing (user typed image before opening modal) — create minimal row
        cur.execute("INSERT OR IGNORE INTO entrant (guild_id, user_id, display_name, caption, image_url) VALUES (?,?,?,?,?)",
                    (message.guild.id, row["user_id"], message.author.display_name, "", image_url))
    con.commit(); con.close()
    try:
        await message.add_reaction("✅")
    except Exception:
        pass


# ---------------------- Match helpers -----------------------
async def publish_match_card(channel: discord.TextChannel, guild_id: int, round_index: int, match_id: int,
                             left_id: int, right_id: int) -> Tuple[int, Optional[int]]:
    """Compose image, post embed with Vote buttons, create supporter thread. Returns (message_id, thread_id)."""
    con = db_connect(); cur = con.cursor()
    cur.execute("SELECT display_name, image_url FROM entrant WHERE guild_id=? AND user_id=?", (guild_id, left_id))
    L = cur.fetchone()
    cur.execute("SELECT display_name, image_url FROM entrant WHERE guild_id=? AND user_id=?", (guild_id, right_id))
    R = cur.fetchone(); con.close()

    em = discord.Embed(title=f"Round {round_index} — Match {match_id+1}", colour=EMBED_COLOUR)
    em.add_field(name="Live totals", value="0 vs 0  •  0% - 100%", inline=False)
    em.add_field(name="Left", value=L['display_name'] if L else str(left_id))
    em.add_field(name="Right", value=R['display_name'] if R else str(right_id))

    file: Optional[discord.File] = None
    if L and L['image_url'] and R and R['image_url']:
        if not bot.session:
            bot.session = aiohttp.ClientSession()
        file = await build_vs_card(L['image_url'], R['image_url'], bot.session)
        if file:
            em.set_image(url=f"attachment://{file.filename}")

    view = VoteView(guild_id, round_index, match_id)
    msg = await channel.send(embed=em, view=view, file=file) if file else await channel.send(embed=em, view=view)

    thread: Optional[discord.Thread] = None
    try:
        thread = await msg.create_thread(name=f"Supporters — Match {match_id+1}")
    except Exception:
        thread = None

    return msg.id, (thread.id if thread else None)


# ---------------------- Scheduler ---------------------------
@tasks.loop(seconds=20)
async def scheduler():
    # Periodic job: move entry->voting, end matches, handle ties, advance rounds
    await bot.wait_until_ready()

    for guild in bot.guilds:
        try:
            await handle_guild_tick(guild)
        except Exception as e:
            # soft-fail per guild
            if LOG_CHANNEL_ID:
                ch = guild.get_channel(LOG_CHANNEL_ID)
                if isinstance(ch, discord.TextChannel):
                    try:
                        await ch.send(f"Scheduler error: {e}")
                    except Exception:
                        pass


async def handle_guild_tick(guild: discord.Guild):
    con = db_connect(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE guild_id=?", (guild.id,))
    ev = cur.fetchone()
    if not ev:
        con.close(); return

    state = ev["state"]
    main_channel: Optional[discord.TextChannel] = guild.get_channel(ev["main_channel_id"]) if ev["main_channel_id"] else None  # type: ignore

    # Update countdown on start embed if present
    if ev["start_msg_id"] and main_channel:
        try:
            msg = await main_channel.fetch_message(ev["start_msg_id"])  # type: ignore
            if msg and msg.embeds:
                em = msg.embeds[0]
                if state == 'entry':
                    remaining = max(0, int((datetime.fromisoformat(ev['entry_end_utc']) - utcnow()).total_seconds()))
                    em.description = em.description.split("\n\n")[0] + f"\n\nEntries close in **{humanize_seconds(remaining)}**."
                    await msg.edit(embed=em, view=(JoinView() if remaining>0 else None))
        except Exception:
            pass

    now = utcnow()

    if state == "entry":
        # If entry closed and >= 2 images, move to voting
        end = datetime.fromisoformat(ev["entry_end_utc"]) if ev["entry_end_utc"] else now
        if now >= end:
            cur.execute("SELECT user_id FROM entrant WHERE guild_id=? AND image_url IS NOT NULL", (guild.id,))
            entrants = [r["user_id"] for r in cur.fetchall()]
            if len(entrants) >= 2:
                # Close entries
                cur.execute("UPDATE event SET state='voting', round_index=1 WHERE guild_id=?", (guild.id,))
                con.commit()
                # Delete ticket channels
                cur.execute("SELECT channel_id FROM ticket WHERE guild_id=?", (guild.id,))
                chans = [r["channel_id"] for r in cur.fetchall()]
                for cid in chans:
                    ch = guild.get_channel(cid)
                    if isinstance(ch, discord.TextChannel):
                        try:
                            await ch.delete(reason="Stylo entries closed")
                        except Exception:
                            pass
                cur.execute("DELETE FROM ticket WHERE guild_id=?", (guild.id,))
                con.commit()

                # Create pairs
                random.shuffle(entrants)
                pairs = [(entrants[i], entrants[i+1]) for i in range(0, len(entrants)-1, 2)]
                if len(entrants) % 2 == 1:
                    # odd - last gets bye to next round; store as match with immediate winner
                    last = entrants[-1]
                    pairs.append((last, 0))  # 0 represents bye

                vote_seconds = int(ev["vote_seconds"]) or int((ev["vote_hours"] or 0) * 3600)
                round_end = now + timedelta(seconds=vote_seconds)

                # Post matches
                if main_channel:
                    # Lock chat (send info message)
                    try:
                        await main_channel.send("🔒 Voting in progress. Chat is moderated during rounds.")
                    except Exception:
                        pass
                    match_id = 0
                    for L, R in pairs:
                        if R == 0:
                            # bye
                            cur.execute("INSERT OR REPLACE INTO match (guild_id, round_index, match_id, left_id, right_id, end_utc, winner_id) VALUES (?,?,?,?,?,?,?)",
                                        (guild.id, 1, match_id, L, R, round_end.isoformat(), L))
                            match_id += 1
                            continue
                        msg_id, thread_id = await publish_match_card(main_channel, guild.id, 1, match_id, L, R)
                        cur.execute("INSERT OR REPLACE INTO match (guild_id, round_index, match_id, left_id, right_id, end_utc, message_id, thread_id) VALUES (?,?,?,?,?,?,?,?)",
                                    (guild.id, 1, match_id, L, R, round_end.isoformat(), msg_id, thread_id))
                        match_id += 1
                    con.commit()
                # Disable Join + unpin
                if ev["start_msg_id"] and main_channel:
                    try:
                        msg = await main_channel.fetch_message(ev["start_msg_id"])  # type: ignore
                        await msg.edit(view=None)
                        await msg.unpin()
                    except Exception:
                        pass
            else:
                # Not enough entries; keep waiting but disable join
                if ev["start_msg_id"] and main_channel:
                    try:
                        msg = await main_channel.fetch_message(ev["start_msg_id"])  # type: ignore
                        await msg.edit(view=None)
                    except Exception:
                        pass

    elif state == "voting":
        round_index = int(ev["round_index"]) or 1
        vote_seconds = int(ev["vote_seconds"]) or int((ev["vote_hours"] or 0) * 3600)

        # Check matches for this round
        cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=?", (guild.id, round_index))
        matches = cur.fetchall()
        if not matches:
            con.close(); return

        all_finished = True
        winners: List[int] = []

        for m in matches:
            if m["winner_id"] is not None:
                winners.append(m["winner_id"])
                continue
            end = datetime.fromisoformat(m["end_utc"]) if m["end_utc"] else now
            if now < end:
                all_finished = False
                continue
            # Time's up - tally
            L = m["left_votes"]; R = m["right_votes"]
            if L == R:
                # Tie — extend
                new_end = now + timedelta(seconds=vote_seconds)
                cur.execute("UPDATE match SET end_utc=?, left_votes=0, right_votes=0 WHERE guild_id=? AND round_index=? AND match_id=?",
                            (new_end.isoformat(), guild.id, round_index, m["match_id"]))
                cur.execute("DELETE FROM voter WHERE guild_id=? AND round_index=? AND match_id=?",
                            (guild.id, round_index, m["match_id"]))
                con.commit()
                # Announce tie-break
                if main_channel:
                    try:
                        await main_channel.send(f"⚔️ Tie-break re-opened for Match {m['match_id']+1}! Vote again.")
                    except Exception:
                        pass
                all_finished = False
            else:
                winner_id = m["left_id"] if L > R else m["right_id"]
                cur.execute("UPDATE match SET winner_id=? WHERE guild_id=? AND round_index=? AND match_id=?",
                            (winner_id, guild.id, round_index, m["match_id"]))
                con.commit()
                winners.append(winner_id)

                # Post results embed
                total = max(1, L + R)
                pctL = int(round(L * 100 / total))
                pctR = 100 - pctL
                em = discord.Embed(title=f"Results — Round {round_index}, Match {m['match_id']+1}", colour=EMBED_COLOUR)
                em.add_field(name="Split", value=f"{L} vs {R}  •  {pctL}% - {pctR}%", inline=False)
                # Winner image (if available)
                cur2 = db_connect().cursor()
                cur2.execute("SELECT display_name, image_url FROM entrant WHERE guild_id=? AND user_id=?", (guild.id, winner_id))
                W = cur2.fetchone(); cur2.connection.close()
                if W and W['image_url']:
                    em.set_thumbnail(url=W['image_url'])
                if main_channel:
                    try: await main_channel.send(embed=em)
                    except Exception: pass

        if not all_finished:
            con.close(); return

        # Advance bracket
        winners = [w for w in winners if w and w != 0]
        if len(winners) <= 1:
            # Champion!
            champion_id = winners[0] if winners else None
            cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (guild.id,))
            con.commit(); con.close()
            if main_channel and champion_id:
                # Champion embed with image + confetti
                cur3 = db_connect().cursor()
                cur3.execute("SELECT display_name, image_url FROM entrant WHERE guild_id=? AND user_id=?", (guild.id, champion_id))
                C = cur3.fetchone(); cur3.connection.close()
                em = discord.Embed(title="🏆 Stylo Champion!", description=f"**{C['display_name'] if C else champion_id}**", colour=EMBED_COLOUR)
                if C and C['image_url']:
                    em.set_thumbnail(url=C['image_url'])
                file = None
                if CONFETTI_GIF_PATH and os.path.exists(CONFETTI_GIF_PATH):
                    try:
                        file = discord.File(CONFETTI_GIF_PATH, filename="confetti.gif")
                        em.set_image(url="attachment://confetti.gif")
                    except Exception:
                        file = None
                try:
                    await main_channel.send(embed=em, file=file) if file else await main_channel.send(embed=em)
                except Exception:
                    pass
            return

        # Build next round
        next_round = round_index + 1
        random.shuffle(winners)
        pairs = [(winners[i], winners[i+1]) for i in range(0, len(winners)-1, 2)]
        if len(winners) % 2 == 1:
            pairs.append((winners[-1], 0))
        new_end = now + timedelta(seconds=vote_seconds)

        cur.execute("UPDATE event SET round_index=? WHERE guild_id=?", (next_round, guild.id))
        con.commit()

        if main_channel:
            match_id = 0
            for Lp, Rp in pairs:
                if Rp == 0:
                    cur.execute("INSERT OR REPLACE INTO match (guild_id, round_index, match_id, left_id, right_id, end_utc, winner_id) VALUES (?,?,?,?,?,?,?)",
                                (guild.id, next_round, match_id, Lp, Rp, new_end.isoformat(), Lp))
                    match_id += 1
                    continue
                msg_id, thread_id = await publish_match_card(main_channel, guild.id, next_round, match_id, Lp, Rp)
                cur.execute("INSERT OR REPLACE INTO match (guild_id, round_index, match_id, left_id, right_id, end_utc, message_id, thread_id) VALUES (?,?,?,?,?,?,?,?)",
                            (guild.id, next_round, match_id, Lp, Rp, new_end.isoformat(), msg_id, thread_id))
                match_id += 1
            con.commit()

    con.close()


# ---------------------- Bot lifecycle -----------------------
@bot.event
async def on_ready():
    if not bot.session:
        bot.session = aiohttp.ClientSession()
    # optional: log online
    for g in bot.guilds:
        if LOG_CHANNEL_ID:
            ch = g.get_channel(LOG_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send("Stylo bot back online ✨")
                except Exception:
                    pass
    print(f"Logged in as {bot.user}")


async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

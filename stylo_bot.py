import os, io, math, asyncio, random, sqlite3
from datetime import datetime, timedelta, timezone

import aiohttp
from PIL import Image, ImageOps, ImageDraw
import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

DB_PATH = os.getenv("STYLO_DB_PATH", "stylo.db")
EMBED_COLOUR = discord.Colour.from_rgb(224, 64, 255)  # neon pink/purple vibe

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # not needed
INTENTS.guilds = True
INTENTS.members = True

# Local confetti file (Stylo/assets/confetti.gif)
CONFETTI_GIF_PATH = os.getenv("CONFETTI_GIF_PATH") or os.path.join(os.path.dirname(__file__), "assets", "confetti.gif")


bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---------- SQLite helpers ----------

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.executescript(
        """
        PRAGMA journal_mode=WAL;
    
        CREATE TABLE IF NOT EXISTS event (
            guild_id         INTEGER PRIMARY KEY,
            theme            TEXT NOT NULL,
            state            TEXT NOT NULL,         -- 'entry','voting','closed'
            entry_end_utc    TEXT NOT NULL,         -- ISO (used as entry end, then round end)
            vote_hours       INTEGER NOT NULL,
            vote_seconds     INTEGER,
            round_index      INTEGER NOT NULL DEFAULT 0,
            main_channel_id  INTEGER,
            start_msg_id     INTEGER
        );
    
        CREATE TABLE IF NOT EXISTS entrant (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            name         TEXT NOT NULL,
            caption      TEXT,
            image_url    TEXT,                      -- set after ticket upload
            UNIQUE(guild_id, user_id)
        );
    
        CREATE TABLE IF NOT EXISTS ticket (
            entrant_id   INTEGER UNIQUE,
            channel_id   INTEGER,
            FOREIGN KEY(entrant_id) REFERENCES entrant(id) ON DELETE CASCADE
        );
    
        CREATE TABLE IF NOT EXISTS match (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            round_index  INTEGER NOT NULL,
            left_id      INTEGER NOT NULL,
            right_id     INTEGER NOT NULL,
            msg_id       INTEGER,                   -- parent message in main channel
            thread_id    INTEGER,                   -- chat thread
            end_utc      TEXT,                      -- shared round end
            left_votes   INTEGER NOT NULL DEFAULT 0,
            right_votes  INTEGER NOT NULL DEFAULT 0,
            winner_id    INTEGER,                   -- set at round end
            FOREIGN KEY(left_id)  REFERENCES entrant(id),
            FOREIGN KEY(right_id) REFERENCES entrant(id)
        );
    
        -- single authoritative declaration
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id           INTEGER PRIMARY KEY,
            log_channel_id     INTEGER,
            ticket_category_id INTEGER
        );
    
        CREATE TABLE IF NOT EXISTS voter (
            match_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            side         TEXT NOT NULL,             -- 'L' or 'R'
            PRIMARY KEY (match_id, user_id),
            FOREIGN KEY(match_id) REFERENCES match(id) ON DELETE CASCADE
        );
        """
    )

    con.commit(); con.close()

def migrate_db_for_minutes():
    con = db(); cur = con.cursor()
    cur.execute("PRAGMA table_info(event)")
    cols = {r["name"] for r in cur.fetchall()}
    if "vote_seconds" not in cols:
        cur.execute("ALTER TABLE event ADD COLUMN vote_seconds INTEGER")
        con.commit()
    con.close()


def migrate_add_start_msg_id():
    """Add event.start_msg_id if missing (stores the pinned Join message id)."""
    con = db(); cur = con.cursor()
    cur.execute("PRAGMA table_info(event)")
    cols = {r["name"] for r in cur.fetchall()}
    if "start_msg_id" not in cols:
        cur.execute("ALTER TABLE event ADD COLUMN start_msg_id INTEGER")
        con.commit()
    con.close()

def rel_ts(dt_utc: datetime) -> str:
    """Return a Discord relative timestamp like '<t:1699999999:R>'."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    else:
        dt_utc = dt_utc.astimezone(timezone.utc)
    return f"<t:{int(dt_utc.timestamp())}:R>"
    
def fmt_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h:01d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ---------- Permissions helper ----------
import re

def parse_duration_to_seconds(text: str, default_unit="h") -> int:
    """
    Accepts values like: 24, 24h, 1.5h, 90m, 45 m, .5h
    Returns total seconds (int). default_unit is used when no suffix is provided.
    """
    s = (text or "").strip().lower().replace(" ", "")
    if not s:
        raise ValueError("empty duration")
    m = re.match(r"^([0-9]*\.?[0-9]+)([mh])?$", s)
    if not m:
        # allow pure '-' etc to fail cleanly
        raise ValueError("invalid duration format")
    val = float(m.group(1))
    unit = m.group(2) or default_unit
    minutes = val * (60 if unit == "h" else 1)
    seconds = int(round(minutes * 60))
    # bound it a bit to avoid accidents
    return max(60, min(seconds, 60 * 60 * 24 * 10))  # 1 minute .. 10 days

def migrate_db():
    con = db(); cur = con.cursor()
    # Add ticket_category_id if the column is missing
    cur.execute("PRAGMA table_info(guild_settings)")
    cols = {row["name"] for row in cur.fetchall()}
    if "ticket_category_id" not in cols:
        try:
            cur.execute("ALTER TABLE guild_settings ADD COLUMN ticket_category_id INTEGER")
            con.commit()
            print("DB migrated: added guild_settings.ticket_category_id")
        except Exception as e:
            print("Migration error:", e)
    con.close()

def humanize_seconds(sec: int) -> str:
    m = round(sec / 60)
    if m % 60 == 0:
        return f"{m//60}h"
    return f"{m}m"

def build_join_view(enabled: bool = True) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    btn = discord.ui.Button(style=discord.ButtonStyle.success, label="Join", custom_id="stylo:join", disabled=not enabled)
    async def join_cb(i: discord.Interaction):
        if i.user.bot:
            return
        await i.response.send_modal(EntrantModal(i))
    btn.callback = join_cb
    view.add_item(btn)
    return view


# ---- DB setup (call once, after all helpers are defined) ----
init_db()
migrate_db()                # add guild_settings.ticket_category_id if missing
migrate_db_for_minutes()    # add event.vote_seconds if missing
migrate_add_start_msg_id()  # add event.start_msg_id if missing


def get_ticket_category_id(guild_id: int) -> int | None:
    con = db(); cur = con.cursor()
    cur.execute("SELECT ticket_category_id FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone(); con.close()  # <-- cur.fetchone()
    return row["ticket_category_id"] if row and row["ticket_category_id"] else None

def set_ticket_category_id(guild_id: int, category_id: int | None):
    con = db(); cur = con.cursor()
    if category_id is None:
        cur.execute("UPDATE guild_settings SET ticket_category_id=NULL WHERE guild_id=?", (guild_id,))
    else:
        cur.execute("INSERT INTO guild_settings(guild_id, ticket_category_id) VALUES(?,?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET ticket_category_id=excluded.ticket_category_id",
                    (guild_id, category_id))
    con.commit(); con.close()

def get_log_channel_id(guild_id: int) -> int | None:
    con = db(); cur = con.cursor()
    cur.execute("SELECT log_channel_id FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    con.close()
    return row["log_channel_id"] if row and row["log_channel_id"] else None

def set_log_channel_id(guild_id: int, channel_id: int | None):
    con = db(); cur = con.cursor()
    if channel_id is None:
        cur.execute("DELETE FROM guild_settings WHERE guild_id=?", (guild_id,))
    else:
        cur.execute("INSERT INTO guild_settings(guild_id, log_channel_id) VALUES(?,?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
                    (guild_id, channel_id))
    con.commit(); con.close()

def is_admin(user: discord.Member) -> bool:
    return user.guild_permissions.manage_guild or user.guild_permissions.administrator

async def cleanup_tickets_for_guild(guild: discord.Guild, reason: str = "Stylo: entries closed"):
    """Delete all Stylo entry ticket channels for this guild and clear DB rows."""
    if not guild:
        return
    con = db(); cur = con.cursor()
    try:
        # Get all ticket channels for this guild
        cur.execute(
            "SELECT t.channel_id FROM ticket t "
            "JOIN entrant e ON e.id = t.entrant_id "
            "WHERE e.guild_id=?",
            (guild.id,)
        )
        rows = cur.fetchall()

        # Delete channels (best-effort)
        for r in rows:
            cid = r["channel_id"]
            ch = guild.get_channel(cid)
            if ch:
                try:
                    await ch.delete(reason=reason)
                except Exception:
                    pass
                await asyncio.sleep(0.4)  # be nice to rate limits

        # Clear ticket rows
        cur.execute(
            "DELETE FROM ticket WHERE entrant_id IN (SELECT id FROM entrant WHERE guild_id=?)",
            (guild.id,)
        )
        con.commit()
    finally:
        con.close()

# ---------- Pillow VS card (no-crop / letterboxed) ----------
async def build_vs_card(left_url: str, right_url: str, width: int = 1200, gap: int = 24) -> io.BytesIO:
    async with aiohttp.ClientSession() as sess:
        async with sess.get(left_url) as r1: Lb = await r1.read()
        async with sess.get(right_url) as r2: Rb = await r2.read()

    L = Image.open(io.BytesIO(Lb)).convert("RGB")
    R = Image.open(io.BytesIO(Rb)).convert("RGB")

    # Card geometry
    tile_w = (width - gap) // 2

    # 1) Scale *without cropping* so each fits inside tile_w x max_h
    #    First make temporary contained versions with a generous max height.
    max_h_guess = int(tile_w * 2.0)  # tall enough so we keep detail
    Lc = ImageOps.contain(L, (tile_w, max_h_guess), method=Image.LANCZOS)
    Rc = ImageOps.contain(R, (tile_w, max_h_guess), method=Image.LANCZOS)

    # 2) Use the taller of the two as the final tile height
    target_h = max(Lc.height, Rc.height)

    # 3) Create pillarbox tiles so images are centered with padding (no crop)
    def make_tile(img):
        tile = Image.new("RGB", (tile_w, target_h), (20, 20, 30))  # background
        x = (tile_w - img.width) // 2
        y = (target_h - img.height) // 2
        tile.paste(img, (x, y))
        return tile

    Ltile = make_tile(Lc)
    Rtile = make_tile(Rc)

    # 4) Compose final canvas
    canvas = Image.new("RGB", (width, target_h), (20, 20, 30))
    canvas.paste(Ltile, (0, 0))
    canvas.paste(Rtile, (tile_w + gap, 0))

    # 5) Divider
    from PIL import ImageDraw
    draw = ImageDraw.Draw(canvas)
    x0 = tile_w
    draw.rectangle([x0, 0, x0 + gap, target_h], fill=(45, 45, 60))

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out


async def update_entry_embed_countdown(message: discord.Message, entry_end: datetime, vote_sec: int):
    """Tick the start embed every ~5s; stop at 00:00 and disable Join."""
    try:
        # ensure aware UTC
        if entry_end.tzinfo is None:
            entry_end = entry_end.replace(tzinfo=timezone.utc)
        else:
            entry_end = entry_end.astimezone(timezone.utc)

        def fmt_hms(sec: int) -> str:
            sec = max(0, int(sec))
            h, r = divmod(sec, 3600)
            m, s = divmod(r, 60)
            return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

        while True:
            now = datetime.now(timezone.utc)
            remaining = int((entry_end - now).total_seconds())
            if remaining < 0:
                remaining = 0

            if not message.embeds:
                return
            em = message.embeds[0]

            # Entries (mm:ss), never show "ago"
            entries_val = f"Closes in **{fmt_hms(remaining)}**" if remaining > 0 else "**Closed**"

            # Voting preview starts AFTER entries end
            vote_preview_end = entry_end + timedelta(seconds=vote_sec)
            voting_val = (
                f"Each round runs **{humanize_seconds(vote_sec)}**\n"
                f"Round 1 closes {rel_ts(vote_preview_end)}"
            )

            # Ensure two fields exist and update them
            if len(em.fields) >= 2:
                em.set_field_at(0, name="Entries", value=entries_val, inline=True)
                em.set_field_at(1, name="Voting", value=voting_val, inline=True)
            else:
                if len(em.fields) == 0:
                    em.add_field(name="Entries", value=entries_val, inline=True)
                    em.add_field(name="Voting", value=voting_val, inline=True)
                else:
                    em.set_field_at(0, name="Entries", value=entries_val, inline=True)
                    em.add_field(name="Voting", value=voting_val, inline=True)

            if remaining == 0:
                # final edit: disable Join, then stop
                try:
                    await message.edit(embed=em, view=build_join_view(enabled=False))
                except discord.HTTPException:
                    pass
                return

            # still open: keep Join enabled and tick again
            try:
                await message.edit(embed=em, view=build_join_view(enabled=True))
            except discord.HTTPException:
                pass

            await asyncio.sleep(3)  # safe cadence; use 3s if you want faster

    except Exception:
        # never crash the bot from a countdown
        return
async def publish_match_card(channel, match_id: int, round_label: str = ""):
    # Get pair and timing
    cur.execute("""
        SELECT m.round_index,
               l.id, l.name, l.image_url,
               r.id, r.name, r.image_url,
               m.end_utc
        FROM match m
        JOIN entrant l ON l.id = m.left_id
        JOIN entrant r ON r.id = m.right_id
        WHERE m.id=?
    """, (match_id,))
    (round_idx,
     L_id, L_name, L_img,
     R_id, R_name, R_img,
     end_utc) = cur.fetchone()

    # Compose side-by-side image if you have a composer; otherwise use one image
    file = None
    composed_url = None
    if 'compose_pair_image' in globals():
        try:
            file, composed_url = await compose_pair_image(L_img, R_img)
        except Exception:
            composed_url = L_img or R_img
    else:
        composed_url = L_img or R_img

    em = discord.Embed(
        title=f"{round_label} — {L_name} vs {R_name}" if round_label else f"Round {round_idx + 1} — {L_name} vs {R_name}",
        colour=discord.Colour.blurple()
    )
    em.description = (
        "Tap a button to vote. One vote per person.\n"
        "Live totals\n"
        "Total votes: 0\n"
        "Split: 0% / 0%\n"
    )
    if composed_url:
        em.set_image(url=composed_url)

    # Your existing voting view
    view = VoteView(match_id=match_id, left_id=L_id, right_id=R_id, end_utc=end_utc)

    if file:
        await channel.send(embed=em, view=view, file=file)
    else:
        await channel.send(embed=em, view=view)

# ---------- Views ----------
class MatchView(discord.ui.View):
    def __init__(self, match_id: int, end_utc: datetime, left_label: str, right_label: str):
        # timeout ensures buttons disable visually if bot misses edit
        timeout = max(1, int((end_utc - datetime.now(timezone.utc)).total_seconds()))
        super().__init__(timeout=timeout)
        self.match_id = match_id
        self.left_label = left_label
        self.right_label = right_label
        self.btn_left.label = f"Vote {left_label}"
        self.btn_right.label = f"Vote {right_label}"

    async def _vote(self, interaction: discord.Interaction, side: str):
        # One vote per user per match; anonymous split
        con = db(); cur = con.cursor()
        cur.execute("SELECT left_votes, right_votes, end_utc FROM match WHERE id=?", (self.match_id,))
        row = cur.fetchone()
        if not row:
            await interaction.response.send_message("Match not found.", ephemeral=True); con.close(); return
        end_dt = datetime.fromisoformat(row["end_utc"]).replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= end_dt:
            await interaction.response.send_message("Voting has ended for this match.", ephemeral=True); con.close(); return

        try:
            cur.execute("INSERT INTO voter(match_id, user_id, side) VALUES(?,?,?)",
                        (self.match_id, interaction.user.id, side))
        except sqlite3.IntegrityError:
            await interaction.response.send_message("You’ve already voted for this match. 👍", ephemeral=True)
            con.close(); return

        if side == "L":
            cur.execute("UPDATE match SET left_votes = left_votes + 1 WHERE id=?", (self.match_id,))
        else:
            cur.execute("UPDATE match SET right_votes = right_votes + 1 WHERE id=?", (self.match_id,))
        con.commit()

        # Read back counts for live update
        cur.execute("SELECT left_votes, right_votes FROM match WHERE id=?", (self.match_id,))
        m = cur.fetchone(); con.close()
        L, R = m["left_votes"], m["right_votes"]
        total = L + R
        pa = math.floor((L / total) * 100) if total else 0
        pb = 100 - pa if total else 0

        # Update live field on the parent embed
        if interaction.message and interaction.message.embeds:
            em = interaction.message.embeds[0]
            # Field 0 assumed "Live totals"
            if em.fields:
                em.set_field_at(0, name="Live totals",
                                value=f"Total votes: **{total}**\nSplit: **{pa}% / {pb}%**",
                                inline=False)
            else:
                em.add_field(name="Live totals", value=f"Total votes: **{total}**\nSplit: **{pa}% / {pb}%**", inline=False)
            await interaction.response.edit_message(embed=em, view=self)
        else:
            await interaction.response.edit_message(view=self)

        await interaction.followup.send("Vote registered. ✅", ephemeral=True)

    @discord.ui.button(style=discord.ButtonStyle.primary, custom_id="stylo:vote_left")
    async def btn_left(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await self._vote(interaction, "L")

    @discord.ui.button(style=discord.ButtonStyle.danger, custom_id="stylo:vote_right")
    async def btn_right(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await self._vote(interaction, "R")

    async def on_timeout(self):
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True


# ---------------- Per-guild settings: /stylo_settings ----------------
settings_group = app_commands.Group(name="stylo_settings", description="Configure Stylo per server")

@settings_group.command(name="set_log_channel", description="Set the channel where Stylo posts status updates.")
@app_commands.describe(channel="Pick a text channel (use a private logs channel if you prefer).")
async def stylo_set_log_channel(inter: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    set_log_channel_id(inter.guild_id, channel.id)
    await inter.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

@settings_group.command(name="show", description="Show current Stylo settings for this server.")
async def stylo_show_settings(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    ch_id = get_log_channel_id(inter.guild_id)
    mention = f"<#{ch_id}>" if ch_id else "— not set —"
    em = discord.Embed(title="Stylo Settings", colour=EMBED_COLOUR)
    em.add_field(name="Log channel", value=mention, inline=False)
    await inter.response.send_message(embed=em, ephemeral=True)

@bot.command(name="stylo_debug")
@commands.has_guild_permissions(manage_guild=True)
async def stylo_debug_prefix(ctx: commands.Context):
    inter_guild_id = ctx.guild.id
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE guild_id=?", (inter_guild_id,))
    ev = cur.fetchone()
    if not ev:
        con.close()
        await ctx.reply("No active event found.")
        return

    cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=?", (inter_guild_id,))
    total_entrants = cur.fetchone()["c"] or 0
    cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=? AND image_url IS NOT NULL", (inter_guild_id,))
    with_image = cur.fetchone()["c"] or 0
    cur.execute("SELECT COUNT(*) AS c FROM match WHERE guild_id=? AND round_index=?", (inter_guild_id, ev["round_index"]))
    matches_in_round = cur.fetchone()["c"] or 0

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    entry_end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
    con.close()

    msg = (
        f"**Event state:** `{ev['state']}`  |  **Round:** `{ev['round_index']}`\n"
        f"**Entrants (total):** {total_entrants}\n"
        f"**Entrants with image:** {with_image}\n"
        f"**Matches in this round:** {matches_in_round}\n"
        f"**Entry end (UTC):** {entry_end.isoformat()}  |  **Now:** {now.isoformat()}\n"
    )
    if ev["state"] == "entry" and with_image >= 2 and now >= entry_end:
        msg += "\n➡️ Entries ended and there are at least 2 images. Scheduler should create pairs on its next tick."
    elif ev["state"] == "entry" and with_image < 2 and now >= entry_end:
        msg += "\n⛔ Entries ended but fewer than 2 images were saved — no pairs can be created."
    elif ev["state"] == "voting" and matches_in_round == 0:
        msg += "\n⚠️ State is 'voting' but no matches exist; something blocked pair creation."
    else:
        msg += "\nℹ️ Status looks consistent."
    await ctx.reply(msg)


# Register the group
bot.tree.add_command(settings_group)

# ---------- Modal: Admin start ----------
class StyloStartModal(discord.ui.Modal, title="Start Stylo Challenge"):
    theme = discord.ui.TextInput(label="Theme / Title", placeholder="Enchanted Garden", max_length=100)
    entry_hours = discord.ui.TextInput(label="Entry window (hours)", default="24")
    vote_hours = discord.ui.TextInput(label="Vote window per round (hours)", default="24")

    def __init__(self, inter: discord.Interaction):
        super().__init__()
        self._origin = inter

    async def on_submit(self, inter: discord.Interaction):
        if not is_admin(inter.user):
            await inter.response.send_message("Admins only.", ephemeral=True)
            return

        try:
            # parse "2", "2h", "90m", "1.5h" etc.
            entry_sec = parse_duration_to_seconds(str(self.entry_hours), default_unit="h")
            vote_sec  = parse_duration_to_seconds(str(self.vote_hours),  default_unit="h")

            theme = str(self.theme).strip()
            if not theme:
                await inter.response.send_message("Theme is required.", ephemeral=True)
                return

            now_utc   = datetime.now(timezone.utc)
            entry_end = now_utc + timedelta(seconds=entry_sec)

            con = db(); cur = con.cursor()
            cur.execute(
                "REPLACE INTO event(guild_id, theme, state, entry_end_utc, vote_hours, vote_seconds, round_index, main_channel_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    inter.guild_id,
                    theme,
                    "entry",
                    entry_end.isoformat(),
                    int(round(vote_sec/3600)),
                    int(vote_sec),
                    0,
                    inter.channel_id,
                ),
            )
            con.commit(); con.close()

            # Build the Join embed (with countdowns)
            join_em = discord.Embed(
                title=f"✨ Stylo: {theme}",
                description=(
                    "Entries are now **open**!\n"
                    "Press **Join** to submit your look. Your final image (square size) must be posted in your ticket before entries close."
                ),
                colour=EMBED_COLOUR,
            )
            join_em.add_field(
                name="Entries",
                value=f"Open for **{humanize_seconds(entry_sec)}**\nCloses {rel_ts(entry_end)}",
                inline=True,
            )
            # Show example voting duration so folks know the cadence
            vote_preview_end = entry_end + timedelta(seconds=vote_sec)   # <-- starts after entries close
            join_em.add_field(
                name="Voting",
                value=f"Each round runs **{humanize_seconds(vote_sec)}**\n"
                      f"Round 1 closes {rel_ts(vote_preview_end)}",
                inline=True,
            )

            # View + working Join button
            view = discord.ui.View(timeout=None)
            join_btn = discord.ui.Button(style=discord.ButtonStyle.success, label="Join", custom_id="stylo:join")

            async def join_callback(btn_inter: discord.Interaction):
                if btn_inter.user.bot:
                    return
                await btn_inter.response.send_modal(EntrantModal(btn_inter))

            join_btn.callback = join_callback

            # use helper so Join button can be toggled later
            view = build_join_view(enabled=True)
            
            await inter.response.send_message(embed=join_em, view=view)
            
            # get the message we just sent
            sent = await inter.original_response()
            
            # Pin so it stays at the top during entries
            try:
                await sent.pin(reason="Stylo: keep Join visible during entries")
            except Exception:
                pass
            
            # Store the message id so we can update/unpin later
            con = db(); cur = con.cursor()
            cur.execute("UPDATE event SET start_msg_id=? WHERE guild_id=?", (sent.id, inter.guild_id))
            con.commit(); con.close()
            
            # start the countdown updater
            asyncio.create_task(update_entry_embed_countdown(sent, entry_end, vote_sec))


        except Exception as e:
            import traceback, textwrap, sys
            traceback.print_exc(file=sys.stderr)
            msg = textwrap.shorten(f"Error: {e!r}", width=300)
            try:
                await inter.response.send_message(msg, ephemeral=True)
            except discord.InteractionResponded:
                await inter.followup.send(msg, ephemeral=True)
            return  # <-- keep your original block below, but ensure it won’t run on error

            # Target category (optional)
            category = None
            cat_id = get_ticket_category_id(guild.id)
            if cat_id:
                maybe = guild.get_channel(cat_id)
                if isinstance(maybe, discord.CategoryChannel):
                    # Hard limit 50 + ensure the bot can see & manage inside that category
                    perms = maybe.permissions_for(guild.me)
                    if not (perms.view_channel and perms.manage_channels):
                        # tell user exactly what's missing
                        missing = []
                        if not perms.view_channel: missing.append("View Channel (category)")
                        if not perms.manage_channels: missing.append("Manage Channels (category)")
                        con.close()
                        await inter.response.send_message(
                            "I can’t create your ticket in the selected category — missing: **"
                            + ", ".join(missing) + "**. "
                            "Ask an admin to fix the category permissions or re-run `/stylo_settings set_ticket_category`.",
                            ephemeral=True
                        ); return
                    if len(maybe.channels) >= 50:
                        con.close()
                        await inter.response.send_message(
                            "The ticket category is full (50 channels). Set a new one with "
                            "`/stylo_settings set_ticket_category`.", ephemeral=True
                        ); return
                    category = maybe
                # if not a category, treat as None (fallback below)

            # Overwrites for the ticket
            default = guild.default_role
            admin_roles = [r for r in guild.roles if r.permissions.administrator]
            overwrites = {
                default:   discord.PermissionOverwrite(view_channel=False),
                guild.me:  discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
                inter.user:discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
            }
            for r in admin_roles:
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True)

            ticket_name = f"stylo-entry-{inter.user.name}".lower()[:90]

            # Try creating in the chosen category; if it fails, fallback to no category
            try:
                ticket = await guild.create_text_channel(
                    ticket_name, overwrites=overwrites, reason="Stylo entry ticket", category=category
                )
            except discord.Forbidden:
                # fallback: create at guild root where the bot can manage
                ticket = await guild.create_text_channel(
                    ticket_name, overwrites=overwrites, reason="Stylo entry ticket (fallback)"
                )

            cur.execute("INSERT OR REPLACE INTO ticket(entrant_id, channel_id) VALUES(?,?)", (entrant_id, ticket.id))
            con.commit(); con.close()

            
        except Exception as e:
            import traceback, textwrap, sys
            traceback.print_exc(file=sys.stderr)
            msg = textwrap.shorten(f"Join failed: {e!r}", width=300)
            try:
                await inter.response.send_message(msg, ephemeral=True)
            except discord.InteractionResponded:
                await inter.followup.send(msg, ephemeral=True)

# ---------- Modal: Entrant info (name, caption) ----------
class EntrantModal(discord.ui.Modal, title="Join Stylo"):
    display_name = discord.ui.TextInput(label="Display name / alias", placeholder="MikeyMoon / Mike ", max_length=50)
    caption = discord.ui.TextInput(label="Caption (optional)", style=discord.TextStyle.paragraph, required=False, max_length=200)

    def __init__(self, inter: discord.Interaction):
        super().__init__()
        self._origin = inter

    async def on_submit(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("Guild context missing.", ephemeral=True); return

        try:
            con = db(); cur = con.cursor()

            # Ensure event is open
            cur.execute("SELECT * FROM event WHERE guild_id=?", (inter.guild_id,))
            ev = cur.fetchone()
            now_utc = datetime.now(timezone.utc)
            if not ev or ev["state"] != "entry":
                con.close()
                await inter.response.send_message("Entries are not open.", ephemeral=True)
                return
            
            # NEW: hard stop if entry window already elapsed
            entry_end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
            if now_utc >= entry_end:
                con.close()
                await inter.response.send_message("Entries have just closed. Please wait for voting to begin.", ephemeral=True)
                return
            

            # Upsert entrant
            name = str(self.display_name).strip()
            cap  = (str(self.caption).strip() if self.caption is not None else "")
            try:
                cur.execute(
                    "INSERT INTO entrant(guild_id, user_id, name, caption) VALUES(?,?,?,?)",
                    (inter.guild_id, inter.user.id, name, cap)
                )
            except sqlite3.IntegrityError:
                cur.execute(
                    "UPDATE entrant SET name=?, caption=? WHERE guild_id=? AND user_id=?",
                    (name, cap, inter.guild_id, inter.user.id)
                )
            con.commit()

            cur.execute("SELECT id FROM entrant WHERE guild_id=? AND user_id=?", (inter.guild_id, inter.user.id))
            entrant_id = cur.fetchone()["id"]

            # --- prevent duplicate ticket channels for this entrant ---
            cur.execute("SELECT channel_id FROM ticket WHERE entrant_id=?", (entrant_id,))
            existing = cur.fetchone()
            if existing:
                # If the saved channel still exists, just point the user to it and stop here.
                already = inter.guild.get_channel(existing["channel_id"])
                if already:
                    con.close()
                    await inter.response.send_message(
                        f"You already have a ticket: {already.mention}", ephemeral=True
                    )
                    return
                else:
                    # Channel no longer exists but the DB row is still there -> cleanup the row
                    cur.execute("DELETE FROM ticket WHERE entrant_id=?", (entrant_id,))
                    con.commit()
            # --- end duplicate guard ---
            

            guild = inter.guild

            # Resolve category (optional) and validate perms/limits
            category = None
            cat_id = get_ticket_category_id(guild.id)
            if cat_id:
                maybe = guild.get_channel(cat_id)
                if isinstance(maybe, discord.CategoryChannel):
                    perms = maybe.permissions_for(guild.me)
                    if not (perms.view_channel and perms.manage_channels):
                        missing = []
                        if not perms.view_channel: missing.append("View Channel (category)")
                        if not perms.manage_channels: missing.append("Manage Channels (category)")
                        con.close()
                        await inter.response.send_message(
                            "I can’t create your ticket in the selected category — missing: **"
                            + ", ".join(missing) + "**.\n"
                            "Ask an admin to fix the category permissions or set a new one with "
                            "`/stylo_settings set_ticket_category`.",
                            ephemeral=True
                        ); return
                    if len(maybe.channels) >= 50:
                        con.close()
                        await inter.response.send_message(
                            "The ticket category is full (50 channels). Set a new one with "
                            "`/stylo_settings set_ticket_category`.",
                            ephemeral=True
                        ); return
                    category = maybe

            # Overwrites
            default = guild.default_role
            admin_roles = [r for r in guild.roles if r.permissions.administrator]
            overwrites = {
                default:   discord.PermissionOverwrite(view_channel=False),
                guild.me:  discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
                inter.user:discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
            }
            for r in admin_roles:
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True)

            ticket_name = f"stylo-entry-{inter.user.name}".lower()[:90]

            # Try category; if forbidden, fallback to root
            try:
                ticket = await guild.create_text_channel(
                    ticket_name, overwrites=overwrites, reason="Stylo entry ticket", category=category
                )
            except discord.Forbidden:
                ticket = await guild.create_text_channel(
                    ticket_name, overwrites=overwrites, reason="Stylo entry ticket (fallback)"
                )

            cur.execute("INSERT OR REPLACE INTO ticket(entrant_id, channel_id) VALUES(?,?)", (entrant_id, ticket.id))
            con.commit(); con.close()

            info = discord.Embed(
                title="📸 Submit your outfit image",
                description=(
                    "Please upload **one** image for your entry in this channel.\n"
                    "• **Must be square (1:1)**\n"
                    "You may re-upload to replace it - the **last** image before entries close is used.\n"
                    "If you’re unsure, feel free to ask an Admin for guidance.\n\n"
                    "⚠️ This channel will vanish into the IMVU void when the voting starts."
                ),
                colour=EMBED_COLOUR
            )

            await ticket.send(content=inter.user.mention, embed=info)
            await inter.response.send_message("Ticket created — please upload your image there. ✅", ephemeral=True)

        except Exception as e:
            import traceback, textwrap, sys
            traceback.print_exc(file=sys.stderr)
            msg = textwrap.shorten(f"Join failed: {e!r}", width=300)
            try:
                await inter.response.send_message(msg, ephemeral=True)
            except discord.InteractionResponded:
                await inter.followup.send(msg, ephemeral=True)


# ---------- Message listener: capture image in ticket ----------
@bot.event
async def on_message(message: discord.Message):
    # ignore bots / DMs
    if message.author.bot or not message.guild:
        return

    # --- Is this a Stylo ticket channel? ---
    con = db(); cur = con.cursor()
    cur.execute(
        "SELECT entrant.id AS entrant_id, entrant.user_id "
        "FROM ticket JOIN entrant ON entrant.id = ticket.entrant_id "
        "WHERE ticket.channel_id=?",
        (message.channel.id,),
    )
    row = cur.fetchone()
    if not row:
        con.close(); 
        return  # not a ticket channel -> do nothing

    # --- Only accept the entrant's upload OR an Admin rescue upload ---
    is_admin_uploader = isinstance(message.author, discord.Member) and (
        message.author.guild_permissions.manage_guild or message.author.guild_permissions.administrator
    )
    if message.author.id != row["user_id"] and not is_admin_uploader:
        con.close(); 
        return

    # must have an image attachment
    if not message.attachments:
        con.close(); 
        return

    def is_image(att: discord.Attachment) -> bool:
        if att.content_type and att.content_type.startswith("image/"):
            return True
        ext = (att.filename or "").lower().rsplit(".", 1)[-1]
        return ext in {"png", "jpg", "jpeg", "gif", "webp", "heic", "heif"}

    img_url = next((a.url for a in message.attachments if is_image(a)), None)
    if not img_url:
        con.close(); 
        return

    # save image url
    cur.execute("UPDATE entrant SET image_url=? WHERE id=?", (img_url, row["entrant_id"]))
    con.commit(); con.close()

    # acknowledgement (best-effort)
    try:
        await message.add_reaction("✅")
    except Exception:
        pass
    try:
        await message.channel.send(
            f"Saved your entry, {message.author.mention}! Your latest image will be used."
        )
    except Exception:
        pass



# ---------- Slash command: /stylo (admin) ----------
@bot.tree.command(name="stylo", description="Start a Stylo challenge (admin only).")
async def stylo_cmd(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    await inter.response.send_modal(StyloStartModal(inter))

# ---------- Background loop: handles entry close & voting close ----------
@tasks.loop(seconds=20)
async def scheduler():
    now = datetime.now(timezone.utc)

    # -------- ENTRY → VOTING --------
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE state='entry'")
    for ev in cur.fetchall():
        entry_end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
        if now < entry_end:
            continue

        # At least two valid images?
        cur.execute("SELECT * FROM entrant WHERE guild_id=? AND image_url IS NOT NULL", (ev["guild_id"],))
        entrants = cur.fetchall()
        if len(entrants) < 2:
            cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
            con.commit()
            continue

        # Build pairs
        random.shuffle(entrants)
        pairs = [(entrants[i], entrants[i+1]) for i in range(0, len(entrants) - len(entrants) % 2, 2)]
        # odd entrant gets a bye (ignored here to keep structure; you can add later if wanted)

        round_index = 1
        vote_sec = ev["vote_seconds"] or int(ev["vote_hours"]) * 3600
        vote_end = now + timedelta(seconds=vote_sec)

        # Persist matches
        for L, R in pairs:
            cur.execute("""
                INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc)
                VALUES(?,?,?,?,?)
            """, (ev["guild_id"], round_index, L["id"], R["id"], vote_end.isoformat()))
        con.commit()

        # Move event into voting
        cur.execute("""
            UPDATE event SET state='voting', round_index=?, entry_end_utc=? WHERE guild_id=?
        """, (round_index, vote_end.isoformat(), ev["guild_id"]))
        con.commit()

        # Resolve guild/channel
        guild = bot.get_guild(ev["guild_id"])
        ch = guild.get_channel(ev["main_channel_id"]) if guild else None
        if not ch and guild and guild.system_channel:
            ch = guild.system_channel

        # Disable Join & unpin start embed
        start_msg_id = ev["start_msg_id"] if "start_msg_id" in ev.keys() else None
        if ch and start_msg_id:
            try:
                start_msg = await ch.fetch_message(start_msg_id)
                if start_msg and start_msg.embeds:
                    em = start_msg.embeds[0]
                    if em.fields:
                        em.set_field_at(0, name="Entries", value="**Closed**", inline=True)
                    await start_msg.edit(embed=em, view=build_join_view(enabled=False))
                    await start_msg.unpin(reason="Stylo: entries closed")
            except Exception:
                pass

        # Announce round start + lock chat
        if ch:
            await ch.send(embed=discord.Embed(
                title=f"🆚 Stylo — Round {round_index} begins!",
                description=f"All matches posted. Voting closes {rel_ts(vote_end)}.\n"
                            f"Main chat is locked; use each match thread for hype.",
                colour=EMBED_COLOUR
            ))
            try:
                await ch.set_permissions(guild.default_role, send_messages=False)
            except Exception:
                pass

            # Post every pair
            cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=?", (ev["guild_id"], round_index))
            matches = cur.fetchall()
            for m in matches:
                try:
                    cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],)); L = cur.fetchone()
                    cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],)); R = cur.fetchone()

                    card = await build_vs_card(L["image_url"], R["image_url"])
                    file = discord.File(fp=card, filename="versus.png")
                    file.spoiler = True          # ← add this line
                    
                    em = discord.Embed(
                        title=f"Round {round_index} — {L['name']} vs {R['name']}",
                        description="Tap a button to vote. One vote per person.",
                        colour=EMBED_COLOUR
                    )
                    em.add_field(name="Live totals", value="Total votes: **0**\nSplit: **0% / 0%**", inline=False)
                    em.set_image(url="attachment://versus.png")
                    
                    msg = await ch.send(embed=em, view=view, file=file)
                    

                    # Supporter thread
                    try:
                        thread = await msg.create_thread(
                            name=f"💬 {L['name']} vs {R['name']} — Chat",
                            auto_archive_duration=1440
                        )
                        await thread.send(embed=discord.Embed(
                            title="Supporter Chat",
                            description="Talk here! Votes are via buttons on the parent post above.",
                            colour=discord.Colour.dark_grey()
                        ))
                        thread_id = thread.id
                    except Exception:
                        thread_id = None

                    cur.execute("UPDATE match SET msg_id=?, thread_id=? WHERE id=?", (msg.id, thread_id, m["id"]))
                    con.commit()
                    await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"[stylo] error posting match {m['id']}: {e}")
                    continue

            # Cleanup all entry tickets now that voting started
            try:
                await cleanup_tickets_for_guild(guild, reason="Stylo: entries closed — cleanup tickets")
            except Exception:
                pass
    con.close()

    # -------- VOTING END → RESULTS / NEXT ROUND / CHAMPION --------
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE state='voting'")
    for ev in cur.fetchall():
        round_end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)  # stored vote end
        if now < round_end:
            continue

        guild = bot.get_guild(ev["guild_id"])
        ch = guild.get_channel(ev["main_channel_id"]) if guild else None

        cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=?", (ev["guild_id"], ev["round_index"]))
        matches = cur.fetchall()
        winners_flat = []         # for champion/next round
        any_revote = False
        vote_sec = ev["vote_seconds"] or int(ev["vote_hours"]) * 3600

        for m in matches:
            L = m["left_votes"]; R = m["right_votes"]
            # Names (used in both paths)
            cur.execute("SELECT name FROM entrant WHERE id=?", (m["left_id"],)); LN = (cur.fetchone() or {"name": "Left"})["name"]
            cur.execute("SELECT name FROM entrant WHERE id=?", (m["right_id"],)); RN = (cur.fetchone() or {"name": "Right"})["name"]

            if L == R:
                # Tie → reset votes, extend end, clear voters, re-enable buttons, announce
                any_revote = True
                new_end = now + timedelta(seconds=vote_sec)
                cur.execute("UPDATE match SET left_votes=0, right_votes=0, end_utc=?, winner_id=NULL WHERE id=?",
                            (new_end.isoformat(), m["id"]))
                cur.execute("DELETE FROM voter WHERE match_id=?", (m["id"],))
                con.commit()

                if ch and m["msg_id"]:
                    try:
                        msg = await ch.fetch_message(m["msg_id"])
                        em = msg.embeds[0] if msg.embeds else discord.Embed(
                            title=f"Round {ev['round_index']} — {LN} vs {RN}",
                            description="Tap a button to vote. One vote per person.",
                            colour=EMBED_COLOUR
                        )
                        if em.fields:
                            em.set_field_at(0, name="Live totals", value="Total votes: **0**\nSplit: **0% / 0%**", inline=False)
                        else:
                            em.add_field(name="Live totals", value="Total votes: **0**\nSplit: **0% / 0%**", inline=False)

                        view = MatchView(m["id"], new_end, LN, RN)
                        await msg.edit(embed=em, view=view)
                    except Exception:
                        pass

                if guild and m["thread_id"]:
                    try:
                        thread = await guild.fetch_channel(m["thread_id"])
                        await thread.edit(locked=False, archived=False)
                    except Exception:
                        pass

                if ch:
                    try:
                        await ch.send(embed=discord.Embed(
                            title=f"🔁 Tie-break — {LN} vs {RN}",
                            description=f"Tied at {L}-{R}. Re-vote is open now and closes {rel_ts(new_end)}.",
                            colour=discord.Colour.orange()
                        ))
                    except Exception:
                        pass

                continue  # to next match

            # Normal winner path
            winner_id = m["left_id"] if L > R else m["right_id"]
            cur.execute("UPDATE match SET winner_id=?, end_utc=? WHERE id=?",
                        (winner_id, now.isoformat(), m["id"]))
            con.commit()
            winners_flat.append(winner_id)

            # Disable buttons and lock thread for finished matches
            if ch and m["msg_id"]:
                try:
                    msg = await ch.fetch_message(m["msg_id"])
                    view = MatchView(m["id"], now - timedelta(seconds=1), LN, RN)
                    for c in view.children:
                        if isinstance(c, discord.ui.Button):
                            c.disabled = True
                    await msg.edit(view=view)
                except Exception:
                    pass
            if guild and m["thread_id"]:
                try:
                    thread = await guild.fetch_channel(m["thread_id"])
                    await thread.edit(locked=True, archived=True)
                except Exception:
                    pass

            # Announce result with percentages and winner image/@mention where available
            total = L + R
            pL = round((L / total) * 100, 1) if total else 0.0
            pR = round((R / total) * 100, 1) if total else 0.0
            if ch:
                try:
                    cur.execute("SELECT name, image_url, user_id FROM entrant WHERE id=?", (winner_id,))
                    row = cur.fetchone()
                    win_name = row["name"] if row else (LN if winner_id == m["left_id"] else RN)
                    win_img  = row["image_url"] if row and row["image_url"] else None
                    member   = guild.get_member(row["user_id"]) if row else None
                    win_display = member.mention if member else win_name

                    em = discord.Embed(
                        title=f"🏁 Result — {LN} vs {RN}",
                        description=(f"**{LN}**: {L} ({pL}%)\n"
                                     f"**{RN}**: {R} ({pR}%)\n\n"
                                     f"**Winner:** {win_display}"),
                        colour=discord.Colour.green()
                    )
                    if win_img:
                        em.set_image(url=win_img)
                    await ch.send(embed=em)
                except Exception:
                    pass

        # If any re-vote exists, push event cursor to latest match end and stay in this round
        if any_revote:
            cur.execute("SELECT MAX(end_utc) AS mx FROM match WHERE guild_id=? AND round_index=?",
                        (ev["guild_id"], ev["round_index"]))
            mx = cur.fetchone()["mx"]
            if mx:
                cur.execute("UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                            (mx, ev["guild_id"]))
                con.commit()
            continue

        # No re-votes: unlock main chat
        if ch:
            try:
                await ch.set_permissions(guild.default_role, send_messages=True)
            except Exception:
                pass

        # Champion?
        if len(winners_flat) == 1:
            champion_id = winners_flat[0]
            cur.execute("SELECT name, image_url, user_id FROM entrant WHERE id=?", (champion_id,))
            row = cur.fetchone()
            champ_name = row["name"] if row else "Winner"
            champ_img  = row["image_url"] if row and row["image_url"] else None
            champ_user = guild.get_member(row["user_id"]) if row else None
            champ_display = champ_user.mention if champ_user else champ_name

            if ch:
                em = discord.Embed(
                    title=f"👑 Stylo Champion — {ev['theme']}",
                    colour=discord.Colour.gold()
                )
                if champ_img:
                    em.set_image(url=champ_img)
                footer_name = champ_user.display_name if champ_user else champ_name
                em.set_footer(text=f"Winner by public vote: {footer_name}")
                await ch.send(embed=em)

            cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
            con.commit()
            continue

        # Build next round from winners
        placeholders = ",".join("?" for _ in winners_flat)
        cur.execute(f"SELECT * FROM entrant WHERE id IN ({placeholders})", winners_flat)
        next_entrants = cur.fetchall()
        random.shuffle(next_entrants)

        pairs = [(next_entrants[i], next_entrants[i+1]) for i in range(0, len(next_entrants) - len(next_entrants) % 2, 2)]
        # odd bye ignored deliberately for stability

        new_round = ev["round_index"] + 1
        vote_end = now + timedelta(seconds=vote_sec)

        # Save next-round matches
        for L, R in pairs:
            cur.execute("INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                        (ev["guild_id"], new_round, L["id"], R["id"], vote_end.isoformat()))
        con.commit()

        # Advance event cursor
        cur.execute("UPDATE event SET round_index=?, entry_end_utc=?, state='voting' WHERE guild_id=?",
                    (new_round, vote_end.isoformat(), ev["guild_id"]))
        con.commit()

        # Announce new round + lock chat, then post pairs
        if ch:
            await ch.send(embed=discord.Embed(
                title=f"🆚 Stylo — Round {new_round} begins!",
                description=f"All matches posted. Voting closes {rel_ts(vote_end)}.\n"
                            "Main chat is locked; use each match thread for hype.",
                colour=EMBED_COLOUR
            ))
            try:
                await ch.set_permissions(guild.default_role, send_messages=False)
            except Exception:
                pass

            cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=?", (ev["guild_id"], new_round))
            matches = cur.fetchall()
            for m in matches:
                try:
                    cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],)); L = cur.fetchone()
                    cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],)); R = cur.fetchone()

                    card = await build_vs_card(L["image_url"], R["image_url"])
                    file = discord.File(fp=card, filename="versus.png")

                    em = discord.Embed(
                        title=f"Round {new_round} — {L['name']} vs {R['name']}",
                        description="Tap a button to vote. One vote per person.",
                        colour=EMBED_COLOUR
                    )
                    em.add_field(name="Live totals", value="Total votes: **0**\nSplit: **0% / 0%**", inline=False)
                    em.set_image(url="attachment://versus.png")

                    view = MatchView(m["id"], vote_end, L["name"], R["name"])
                    msg = await ch.send(embed=em, view=view, file=file)

                    try:
                        thread = await msg.create_thread(
                            name=f"💬 {L['name']} vs {R['name']} — Chat",
                            auto_archive_duration=1440
                        )
                        await thread.send(embed=discord.Embed(
                            title="Supporter Chat",
                            description="Talk here! Votes are via buttons on the parent post above.",
                            colour=discord.Colour.dark_grey()
                        ))
                        thread_id = thread.id
                    except Exception:
                        thread_id = None

                    cur.execute("UPDATE match SET msg_id=?, thread_id=? WHERE id=?", (msg.id, thread_id, m["id"]))
                    con.commit()
                    await asyncio.sleep(0.4)
                except Exception as e:
                    print(f"[stylo] error posting match {m['id']}: {e}")
                    continue
    con.close()

@scheduler.before_loop
async def _wait_ready():
    await bot.wait_until_ready()

import os

LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

@settings_group.command(name="set_ticket_category", description="Set the category where Stylo will create entry tickets.")
@app_commands.describe(category="Choose a category for entry ticket channels.")
async def stylo_set_ticket_category(inter: discord.Interaction, category: discord.CategoryChannel):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    set_ticket_category_id(inter.guild_id, category.id)
    await inter.response.send_message(f"✅ Ticket category set to **{category.name}**", ephemeral=True)

@settings_group.command(name="show_ticket_category", description="Show the currently configured ticket category.")
async def stylo_show_ticket_category(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    cat_id = get_ticket_category_id(inter.guild_id)
    mention = f"<#{cat_id}>" if cat_id else "— not set —"
    em = discord.Embed(title="Stylo Settings", colour=EMBED_COLOUR)
    em.add_field(name="Ticket Category", value=mention, inline=False)
    await inter.response.send_message(embed=em, ephemeral=True)

#added to see what's making pair error
@bot.tree.command(name="stylo_debug", description="Show Stylo status for this server (admin only).")
async def stylo_debug(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return

    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE guild_id=?", (inter.guild_id,))
    ev = cur.fetchone()

    if not ev:
        con.close()
        await inter.response.send_message("No active event found.", ephemeral=True)
        return

    # Counts
    cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=?", (inter.guild_id,))
    total_entrants = cur.fetchone()["c"] or 0
    cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=? AND image_url IS NOT NULL", (inter.guild_id,))
    with_image = cur.fetchone()["c"] or 0
    cur.execute("SELECT COUNT(*) AS c FROM match WHERE guild_id=? AND round_index=?", (inter.guild_id, ev["round_index"]))
    matches_in_round = cur.fetchone()["c"] or 0

    # Times
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    entry_end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)

    con.close()

    msg = (
        f"**Event state:** `{ev['state']}`  |  **Round:** `{ev['round_index']}`\n"
        f"**Entrants (total):** {total_entrants}\n"
        f"**Entrants with image:** {with_image}\n"
        f"**Matches in this round:** {matches_in_round}\n"
        f"**Entry end (UTC):** {entry_end.isoformat()}  |  **Now:** {now.isoformat()}\n\n"
    )

    # Simple diagnosis
    if ev["state"] == "entry" and with_image >= 2 and now >= entry_end:
        msg += "➡️ Entries ended and there are at least 2 images. Scheduler should create pairs on its next tick."
    elif ev["state"] == "entry" and with_image < 2 and now >= entry_end:
        msg += "⛔ Entries ended but fewer than 2 images were saved — no pairs can be created."
    elif ev["state"] == "voting" and matches_in_round == 0:
        msg += "⚠️ State is 'voting' but no matches exist; something blocked pair creation."
    else:
        msg += "ℹ️ Status looks consistent."

    await inter.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="stylo_reset", description="⚠️ Admin only — reset Stylo event and matches for testing.")
async def stylo_reset(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True)
        return

    con = db(); cur = con.cursor()
    cur.execute("DELETE FROM match")
    cur.execute("DELETE FROM event")
    cur.execute("DELETE FROM entrant")
    cur.execute("DELETE FROM ticket")
    cur.execute("DELETE FROM voter")
    con.commit(); con.close()

    await inter.response.send_message("🧹 Stylo database reset complete. You can now run `/stylo` fresh.", ephemeral=True)

# ---------- Ready ----------
@bot.event
async def on_ready():
    try:
        # Force per-guild sync so new/changed commands show instantly
        for g in bot.guilds:
            try:
                await bot.tree.sync(guild=discord.Object(id=g.id))
                print(f"Synced app commands to guild {g.name} ({g.id})")
            except Exception as e:
                print(f"Per-guild sync failed for {g.id}: {e}")
        # Also do a global sync (fine if it no-ops)
        await bot.tree.sync()
        print("Global app commands synced.")
    except Exception as e:
        print("Slash sync error:", e)

    if not scheduler.is_running():
        scheduler.start()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    log_id = int(os.getenv("LOG_CHANNEL_ID", "0"))
    if log_id:
        ch = bot.get_channel(log_id)
        if ch:
            await ch.send("✨ Stylo updated to the latest version and is back online!")

if __name__ == "__main__":
    bot.run(TOKEN)

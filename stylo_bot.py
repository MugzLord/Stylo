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
            guild_id      INTEGER PRIMARY KEY,
            theme         TEXT NOT NULL,
            state         TEXT NOT NULL,         -- 'entry','voting','closed'
            entry_end_utc TEXT NOT NULL,         -- ISO
            vote_hours    INTEGER NOT NULL,
            round_index   INTEGER NOT NULL DEFAULT 0,
            main_channel_id INTEGER               -- where posts happen
        );

        CREATE TABLE IF NOT EXISTS entrant (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            name         TEXT NOT NULL,
            caption      TEXT,
            image_url    TEXT,                   -- set after ticket upload
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
            msg_id       INTEGER,                -- parent message in main channel
            thread_id    INTEGER,                -- chat thread
            end_utc      TEXT,                   -- shared round end
            left_votes   INTEGER NOT NULL DEFAULT 0,
            right_votes  INTEGER NOT NULL DEFAULT 0,
            winner_id    INTEGER,                -- set at round end
            FOREIGN KEY(left_id)  REFERENCES entrant(id),
            FOREIGN KEY(right_id) REFERENCES entrant(id)
        );
        
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id       INTEGER PRIMARY KEY,
            log_channel_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS voter (
            match_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            side         TEXT NOT NULL,          -- 'L' or 'R'
            PRIMARY KEY (match_id, user_id),
            FOREIGN KEY(match_id) REFERENCES match(id) ON DELETE CASCADE
        );
        
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id       INTEGER PRIMARY KEY,
            log_channel_id INTEGER,
            ticket_category_id INTEGER
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

# ---------- Permissions/helper ----------
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
        raise ValueError("invalid duration format")
    val = float(m.group(1))
    unit = m.group(2) or default_unit
    minutes = val * (60 if unit == "h" else 1)
    seconds = int(round(minutes * 60))
    return max(60, min(seconds, 60 * 60 * 24 * 10))  # 1 minute .. 10 days

def migrate_db():
    con = db(); cur = con.cursor()
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

# ---- DB setup ----
init_db()
migrate_db()                # add guild_settings.ticket_category_id if missing
migrate_db_for_minutes()    # add event.vote_seconds if missing
migrate_add_start_msg_id()  # add event.start_msg_id if missing

def get_ticket_category_id(guild_id: int) -> int | None:
    con = db(); cur = con.cursor()
    cur.execute("SELECT ticket_category_id FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone(); con.close()
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
        cur.execute(
            "SELECT t.channel_id FROM ticket t "
            "JOIN entrant e ON e.id = t.entrant_id "
            "WHERE e.guild_id=?",
            (guild.id,)
        )
        rows = cur.fetchall()

        for r in rows:
            cid = r["channel_id"]
            ch = guild.get_channel(cid)
            if ch:
                try:
                    await ch.delete(reason=reason)
                except Exception:
                    pass
                await asyncio.sleep(0.4)

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

    tile_w = (width - gap) // 2
    max_h_guess = int(tile_w * 2.0)
    Lc = ImageOps.contain(L, (tile_w, max_h_guess), method=Image.LANCZOS)
    Rc = ImageOps.contain(R, (tile_w, max_h_guess), method=Image.LANCZOS)
    target_h = max(Lc.height, Rc.height)

    def make_tile(img):
        tile = Image.new("RGB", (tile_w, target_h), (20, 20, 30))
        x = (tile_w - img.width) // 2
        y = (target_h - img.height) // 2
        tile.paste(img, (x, y))
        return tile

    Ltile = make_tile(Lc)
    Rtile = make_tile(Rc)

    canvas = Image.new("RGB", (width, target_h), (20, 20, 30))
    canvas.paste(Ltile, (0, 0))
    canvas.paste(Rtile, (tile_w + gap, 0))

    draw = ImageDraw.Draw(canvas)
    x0 = tile_w
    draw.rectangle([x0, 0, x0 + gap, target_h], fill=(45, 45, 60))

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out

async def update_entry_embed_countdown(message: discord.Message, entry_end: datetime, vote_sec: int):
    """Tick the start embed every ~3s; stop at 00:00 and disable Join."""
    try:
        if entry_end.tzinfo is None:
            entry_end = entry_end.replace(tzinfo=timezone.utc)
        else:
            entry_end = entry_end.astimezone(timezone.utc)

        def _fmt(sec: int) -> str:
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

            entries_val = f"Closes in **{_fmt(remaining)}**" if remaining > 0 else "**Closed**"
            vote_preview_end = entry_end + timedelta(seconds=vote_sec)
            voting_val = (
                f"Each round runs **{humanize_seconds(vote_sec)}**\n"
                f"Round 1 closes {rel_ts(vote_preview_end)}"
            )

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
                try:
                    await message.edit(embed=em, view=build_join_view(enabled=False))
                except discord.HTTPException:
                    pass
                return

            try:
                await message.edit(embed=em, view=build_join_view(enabled=True))
            except discord.HTTPException:
                pass

            await asyncio.sleep(3)

    except Exception:
        return

# ---------- Views ----------
class MatchView(discord.ui.View):
    def __init__(self, match_id: int, end_utc: datetime, left_label: str, right_label: str):
        timeout = max(1, int((end_utc - datetime.now(timezone.utc)).total_seconds()))
        super().__init__(timeout=timeout)
        self.match_id = match_id
        self.left_label = left_label
        self.right_label = right_label
        self.btn_left.label = f"Vote {left_label}"
        self.btn_right.label = f"Vote {right_label}"

    async def _vote(self, interaction: discord.Interaction, side: str):
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

        cur.execute("SELECT left_votes, right_votes FROM match WHERE id=?", (self.match_id,))
        m = cur.fetchone(); con.close()
        L, R = m["left_votes"], m["right_votes"]
        total = L + R
        pa = math.floor((L / total) * 100) if total else 0
        pb = 100 - pa if total else 0

        if interaction.message and interaction.message.embeds:
            em = interaction.message.embeds[0]
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

        # Single clean try/except (previous stacked excepts caused syntax/logic issues)
        try:
            try:
                await inter.response.defer(ephemeral=False)
            except discord.InteractionResponded:
                pass

            entry_sec = parse_duration_to_seconds(str(self.entry_hours), default_unit="h")
            vote_sec  = parse_duration_to_seconds(str(self.vote_hours),  default_unit="h")

            theme = str(self.theme).strip()
            if not theme:
                await inter.followup.send("Theme is required.", ephemeral=True)
                return

            ch = inter.channel
            me = inter.guild.me if inter.guild else None
            if ch and me:
                perms = ch.permissions_for(me)
                missing = []
                if not perms.send_messages: missing.append("Send Messages")
                if not perms.embed_links:   missing.append("Embed Links")
                if missing:
                    await inter.followup.send(
                        "I’m missing: **" + ", ".join(missing) + "** in this channel.",
                        ephemeral=True
                    ); return

            now_utc   = datetime.now(timezone.utc)
            entry_end = now_utc + timedelta(seconds=entry_sec)

            con = db(); cur = con.cursor()
            cur.execute(
                "REPLACE INTO event(guild_id, theme, state, entry_end_utc, vote_hours, vote_seconds, round_index, main_channel_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    inter.guild_id, theme, "entry",
                    entry_end.isoformat(),
                    int(round(vote_sec/3600)), int(vote_sec),
                    0, inter.channel_id,
                ),
            )
            con.commit(); con.close()

            join_em = discord.Embed(
                title=f"✨ Stylo: {theme}",
                description=(
                    "Entries are now **open**!\n"
                    "Press **Join** to submit your look. Your final image (square) must be posted in your ticket before entries close."
                ),
                colour=EMBED_COLOUR,
            )
            join_em.add_field(
                name="Entries",
                value=f"Open for **{humanize_seconds(entry_sec)}**\nCloses {rel_ts(entry_end)}",
                inline=True,
            )
            vote_preview_end = entry_end + timedelta(seconds=vote_sec)
            join_em.add_field(
                name="Voting",
                value=f"Each round runs **{humanize_seconds(vote_sec)}**\nRound 1 closes {rel_ts(vote_preview_end)}",
                inline=True,
            )

            sent = await inter.followup.send(embed=join_em, view=build_join_view(enabled=True), wait=True)

            try:
                await sent.pin(reason="Stylo: keep Join visible during entries")
            except Exception:
                pass

            con = db(); cur = con.cursor()
            cur.execute("UPDATE event SET start_msg_id=? WHERE guild_id=?", (sent.id, inter.guild_id))
            con.commit(); con.close()

            asyncio.create_task(update_entry_embed_countdown(sent, entry_end, vote_sec))

        except Exception as e:
            import traceback, sys, textwrap
            traceback.print_exc(file=sys.stderr)
            msg = textwrap.shorten(f"Start failed: {e!r}", width=300)
            try:
                await inter.followup.send(msg, ephemeral=True)
            except Exception:
                pass

# ---------- Modal: Entrant info (name, caption) ----------
class EntrantModal(discord.ui.Modal, title="Join Stylo"):
    display_name = discord.ui.TextInput(label="Display name / alias", placeholder="MikeyMoon / Mike", max_length=50)
    caption = discord.ui.TextInput(label="Caption (optional)", style=discord.TextStyle.paragraph, required=False, max_length=200)

    def __init__(self, inter: discord.Interaction):
        super().__init__()
        self._origin = inter

    async def on_submit(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("Guild context missing.", ephemeral=True); return

        try:
            con = db(); cur = con.cursor()

            cur.execute("SELECT * FROM event WHERE guild_id=?", (inter.guild_id,))
            ev = cur.fetchone()
            now_utc = datetime.now(timezone.utc)
            if not ev or ev["state"] != "entry":
                con.close()
                await inter.response.send_message("Entries are not open.", ephemeral=True)
                return
            
            entry_end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
            if now_utc >= entry_end:
                con.close()
                await inter.response.send_message("Entries have just closed. Please wait for voting to begin.", ephemeral=True)
                return

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

            # prevent duplicate ticket channels
            cur.execute("SELECT channel_id FROM ticket WHERE entrant_id=?", (entrant_id,))
            existing = cur.fetchone()
            if existing:
                already = inter.guild.get_channel(existing["channel_id"])
                if already:
                    con.close()
                    await inter.response.send_message(
                        f"You already have a ticket: {already.mention}", ephemeral=True
                    )
                    return
                else:
                    cur.execute("DELETE FROM ticket WHERE entrant_id=?", (entrant_id,))
                    con.commit()

            guild = inter.guild

            # Resolve category (optional)
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
    if message.author.bot or not message.guild:
        return

    print(f"[stylo] on_message in #{message.channel.name} from {message.author} | atts={len(message.attachments)}")

    con = db(); cur = con.cursor()
    cur.execute(
        "SELECT entrant.id AS entrant_id, entrant.user_id "
        "FROM ticket JOIN entrant ON entrant.id = ticket.entrant_id "
        "WHERE ticket.channel_id=?",
        (message.channel.id,),
    )
    row = cur.fetchone()
    if not row:
        con.close()
        # allow other prefix commands to work in non-ticket channels
        await bot.process_commands(message)
        return

    if message.author.id != row["user_id"]:
        con.close(); 
        await bot.process_commands(message)
        return
    if not message.attachments:
        con.close(); 
        await bot.process_commands(message)
        return

    def is_image(att: discord.Attachment) -> bool:
        if att.content_type and att.content_type.startswith("image/"):
            return True
        ext = (att.filename or "").lower().rsplit(".", 1)[-1]
        return ext in {"png", "jpg", "jpeg", "gif", "webp", "heic", "heif"}

    img_url = next((a.url for a in message.attachments if is_image(a)), None)
    if not img_url:
        con.close(); 
        await bot.process_commands(message)
        return

    cur.execute("UPDATE entrant SET image_url=? WHERE id=?", (img_url, row["entrant_id"]))
    con.commit(); con.close()

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

    # important: do not swallow prefix commands
    await bot.process_commands(message)

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

    # Handle entry -> voting transition
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE state='entry'")
    for ev in cur.fetchall():
        entry_end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
        if now >= entry_end:
            # Build entrant list with images
            cur.execute("SELECT * FROM entrant WHERE guild_id=? AND image_url IS NOT NULL", (ev["guild_id"],))
            entrants = cur.fetchall()
            print(f"[stylo] entry->voting: entrants with image = {len(entrants)} (guild {ev['guild_id']})")

            if len(entrants) < 2:
                # Not enough entrants; close
                cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
                con.commit(); continue

            random.shuffle(entrants)
            pairs = []
            for i in range(0, len(entrants), 2):
                if i + 1 < len(entrants):
                    pairs.append((entrants[i], entrants[i + 1]))
            # (optional) byes if odd
            byes = entrants[-1:] if (len(entrants) % 2 == 1) else []


            round_index = 1
            vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
            vote_end = now + timedelta(seconds=vote_sec)

            # Save matches
            for L, R in pairs:
                cur.execute("INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                            (ev["guild_id"], round_index, L["id"], R["id"], vote_end.isoformat()))
            con.commit()

            # Update event to voting state + end
            cur.execute("UPDATE event SET state='voting', round_index=?, entry_end_utc=?, main_channel_id=? WHERE guild_id=?",
                        (round_index, vote_end.isoformat(), ev["main_channel_id"], ev["guild_id"]))
            con.commit()
            
            # Resolve channel
            guild = bot.get_guild(ev["guild_id"])
            ch = guild.get_channel(ev["main_channel_id"]) if guild else None
            
            # 🔧 NEW: nuke all entry tickets once voting begins
            if guild:
                await cleanup_tickets_for_guild(guild, reason="Stylo: entries closed - deleting tickets")


            print(f"[stylo] posting to channel: {ev['main_channel_id']} resolved={bool(ch)}")
            if not ch and guild and guild.system_channel:
                ch = guild.system_channel
                print(f"[stylo] fallback to system_channel id={ch.id}")

            # Update & unpin the join message
            start_msg_id = ev["start_msg_id"] if ("start_msg_id" in ev.keys()) else None
            if ch and start_msg_id:
                try:
                    start_msg = await ch.fetch_message(start_msg_id)
                    if start_msg and start_msg.embeds:
                        em = start_msg.embeds[0]
                        if em.fields:
                            em.set_field_at(0, name="Entries", value="**Closed**", inline=True)
                        try:
                            await start_msg.edit(embed=em, view=build_join_view(enabled=False))
                        except Exception:
                            pass
                    try:
                        await start_msg.unpin(reason="Stylo: entries closed")
                    except Exception:
                        pass
                except Exception:
                    pass
            
            # Announce with the correct countdown
            if ch:
                await ch.send(embed=discord.Embed(
                    title=f"🆚 Stylo — Round {round_index} begins!",
                    description=f"All matches posted. Voting closes {rel_ts(vote_end)}.\n"
                                "Main chat is locked; use each match thread for hype.",
                    colour=EMBED_COLOUR
                ))

                # Lock main channel chat
                default = guild.default_role
                try:
                    await ch.set_permissions(default, send_messages=False)
                except Exception:
                    pass

                # Fetch matches for posting (ROUND 1)
                cur.execute(
                    "SELECT * FROM match WHERE guild_id=? AND round_index=? AND msg_id IS NULL",
                    (ev["guild_id"], round_index)
                )
                matches = cur.fetchall()
                
                for m in matches:
                    try:
                        # Fetch entrants (robust)
                        cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],))
                        L = cur.fetchone()
                        cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],))
                        R = cur.fetchone()
                        if not L or not R:
                            print(f"[stylo] missing entrant rows for match {m['id']}; skipping")
                            continue
                
                        # Base embed
                        em = discord.Embed(
                            title=f"Round {round_index} — {L['name']} vs {R['name']}",
                            description="Tap a button to vote. One vote per person.",
                            colour=EMBED_COLOUR
                        )
                        em.add_field(name="Live totals",
                                     value="Total votes: **0**\nSplit: **0% / 0%**",
                                     inline=False)
                
                        end_dt = vote_end  # timezone-aware UTC
                        view = MatchView(m["id"], end_dt, L["name"], R["name"])
                
                        # Try composite VS card; fall back to links if fetch/composite fails
                        try:
                            card = await build_vs_card(L["image_url"], R["image_url"])
                            file = discord.File(fp=card, filename="versus.png")
                            msg = await ch.send(embed=em, view=view, file=file)
                        except Exception as e:
                            print(f"[stylo] VS card failed for match {m['id']}: {e!r}")
                            em.add_field(
                                name="Looks",
                                value=f"[{L['name']}]({L['image_url']})  vs  [{R['name']}]({R['image_url']})",
                                inline=False
                            )
                            msg = await ch.send(embed=em, view=view)
                
                        # Thread for hype (best effort)
                        thread_id = None
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
                        except Exception as e:
                            print(f"[stylo] Thread create failed for match {m['id']}: {e!r}")
                
                        # Persist msg/thread IDs
                        cur.execute("UPDATE match SET msg_id=?, thread_id=? WHERE id=?", (msg.id, thread_id, m["id"]))
                        con.commit()
                
                        await asyncio.sleep(0.4)  # rate-limit friendly
                
                    except Exception as e:
                        # Never let one bad pair kill the rest
                        print(f"[stylo] posting match {m['id']} failed: {e!r}")
                        continue
                
                        guild = bot.get_guild(ev["guild_id"])
                        ch = guild.get_channel(ev["main_channel_id"]) if guild else None
                
                        cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
                                    (ev["guild_id"], ev["round_index"]))
                        matches = cur.fetchall()
                        winners = []
                        vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
                        any_revote = False
                
                        for m in matches:
                            L = m["left_votes"]; R = m["right_votes"]
                        
                            cur.execute("SELECT name FROM entrant WHERE id=?", (m["left_id"],))
                            LN = (cur.fetchone() or {"name": "Left"})["name"]
                            cur.execute("SELECT name FROM entrant WHERE id=?", (m["right_id"],))
                            RN = (cur.fetchone() or {"name": "Right"})["name"]
                        
                            # Tie -> re-vote
                            if L == R:
                                any_revote = True
                                new_end = now + timedelta(seconds=vote_sec)
                        
                                cur.execute(
                                    "UPDATE match SET left_votes=0, right_votes=0, end_utc=?, winner_id=NULL WHERE id=?",
                                    (new_end.isoformat(), m["id"]),
                                )
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
                                            em.set_field_at(0, name="Live totals",
                                                            value="Total votes: **0**\nSplit: **0% / 0%**",
                                                            inline=False)
                                        else:
                                            em.add_field(name="Live totals",
                                                         value="Total votes: **0**\nSplit: **0% / 0%**",
                                                         inline=False)
                                
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
                        
                                continue  # go next match
        
            # Normal path -> set winner, announce with image
            winner_id = m["left_id"] if L > R else m["right_id"]
            cur.execute("UPDATE match SET winner_id=?, end_utc=? WHERE id=?",
                        (winner_id, now.isoformat(), m["id"]))
            con.commit()
        
            winners.append((m["id"], winner_id, LN, RN, L, R))
        
            total = L + R
            pL = round((L / total) * 100, 1) if total else 0.0
            pR = round((R / total) * 100, 1) if total else 0.0
        
            if ch:
                try:
                    if winner_id == m["left_id"]:
                        cur.execute("SELECT name, image_url, user_id FROM entrant WHERE id=?", (m["left_id"],))
                    else:
                        cur.execute("SELECT name, image_url, user_id FROM entrant WHERE id=?", (m["right_id"],))
                    winner_entry = cur.fetchone()
                
                    winner_img = winner_entry["image_url"] if winner_entry and winner_entry["image_url"] else None
                    winner_member = guild.get_member(winner_entry["user_id"]) if winner_entry else None
                    winner_mention = (
                        winner_member.mention
                        if winner_member
                        else (f"<@{winner_entry['user_id']}>" if winner_entry else "the winner")
                    )
                
                    em = discord.Embed(
                        title=f"🏁 Result — {LN} vs {RN}",
                        description=(
                            f"**{LN}**: {L} ({pL}%)\n"
                            f"**{RN}**: {R} ({pR}%)\n\n"
                            f"🏆 **Winner:** {winner_mention}\n"
                        ),
                        colour=discord.Colour.green(),
                    )
                    if winner_img:
                        em.set_thumbnail(url=winner_img)
                    await ch.send(embed=em)

                except Exception:
                    pass

        # If any re-vote, extend event window and wait
        if any_revote:
            cur.execute(
                "SELECT MAX(end_utc) AS mx FROM match WHERE guild_id=? AND round_index=?",
                (ev["guild_id"], ev["round_index"])
            )
            mx = cur.fetchone()["mx"]
            if mx:
                cur.execute(
                    "UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                    (mx, ev["guild_id"])
                )
                con.commit()
            continue

        # Unlock main channel after round
        if ch:
            default = guild.default_role
            try:
                await ch.set_permissions(default, send_messages=True)
            except Exception:
                pass

        # Champion?
        if len(winners) == 1:
            raw = winners[0]
            winner_id = raw[1] if isinstance(raw, tuple) else raw
        
            cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
            con.commit()
        
            cur.execute("SELECT name, image_url, user_id FROM entrant WHERE id=?", (winner_id,))
            w = cur.fetchone()
            winner_name = (w["name"] if w else "Unknown")
            winner_img  = (w["image_url"] if w else None)
        
            winner_mention = ""
            if w and guild:
                mem = guild.get_member(w["user_id"])
                if mem:
                    winner_mention = f"\n{mem.mention}"
                elif w["user_id"]:
                    winner_mention = f"\n<@{w['user_id']}>"
        
            em = discord.Embed(
                title=f"👑 Stylo Champion — {ev['theme']}",
                description=f"Winner by public vote: **{winner_name}**{winner_mention}",
                colour=discord.Colour.gold()
            )
            if winner_img:
                em.set_image(url=winner_img)
        
            if ch:
                await ch.send(embed=em)
        
            continue

        # Next round
        placeholders = ",".join("?" for _ in winners)
        # winners stored as tuples (match_id, winner_id, ...)
        winner_ids_only = [w[1] if isinstance(w, tuple) else w for w in winners]
        cur.execute(f"SELECT * FROM entrant WHERE id IN ({placeholders})", winner_ids_only)
        next_entrants = cur.fetchall()
        random.shuffle(next_entrants)
        pairs = []
        for i in range(0, len(next_entrants), 2):
            if i + 1 < len(next_entrants):
                pairs.append((next_entrants[i], next_entrants[i + 1]))
        byes = next_entrants[-1:] if (len(next_entrants) % 2 == 1) else []


        new_round = ev["round_index"] + 1
        vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
        vote_end = now + timedelta(seconds=vote_sec)
        
        for L, R in pairs:
            cur.execute("INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                        (ev["guild_id"], new_round, L["id"], R["id"], vote_end.isoformat()))
        con.commit()
        
        cur.execute(
            "UPDATE event SET round_index=?, entry_end_utc=?, state='voting' WHERE guild_id=?",
            (new_round, vote_end.isoformat(), ev["guild_id"])
        )
        con.commit()

        if ch:
            await ch.send(embed=discord.Embed(
                title=f"🆚 Stylo — Round {new_round} begins!",
                description=f"All matches posted. Voting closes {rel_ts(vote_end)}.\n"
                            f"Main chat is locked; use each match thread for hype.",
                colour=EMBED_COLOUR
            ))

@scheduler.before_loop
async def _wait_ready():
    await bot.wait_until_ready()

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

# added to see what's making pair error
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

    cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=?", (inter.guild_id,))
    total_entrants = cur.fetchone()["c"] or 0
    cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=? AND image_url IS NOT NULL", (inter.guild_id,))
    with_image = cur.fetchone()["c"] or 0
    cur.execute("SELECT COUNT(*) AS c FROM match WHERE guild_id=? AND round_index=?", (inter.guild_id, ev["round_index"]))
    matches_in_round = cur.fetchone()["c"] or 0

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

    if ev["state"] == "entry" and with_image >= 2 and now >= entry_end:
        msg += "➡️ Entries ended and there are at least 2 images. Scheduler should create pairs on its next tick."
    elif ev["state"] == "entry" and with_image < 2 and now >= entry_end:
        msg += "⛔ Entries ended but fewer than 2 images were saved — no pairs can be created."
    elif ev["state"] == "voting" and matches_in_round == 0:
        msg += "⚠️ State is 'voting' but no matches exist; something blocked pair creation."
    else:
        msg += "ℹ️ Status looks consistent."

    await inter.response.send_message(msg, ephemeral=True)
# end of the error debugger

# ---------- Ready ----------
@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
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

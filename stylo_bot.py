# stylo.py
import os, io, math, asyncio, random, sqlite3, re
from datetime import datetime, timedelta, timezone

import aiohttp
from PIL import Image, ImageOps, ImageDraw
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------- Config ----------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

DB_PATH = os.getenv("STYLO_DB_PATH", "stylo.db")
EMBED_COLOUR = discord.Colour.from_rgb(224, 64, 255)

DENY_ROLE_IDS = [ ]
ALLOW_ROLE_IDS = [ ]
MAIN_CHAT_CHANNEL_ID = int(os.getenv("STYLO_MAIN_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("STYLO_OWNER_ID", "0"))
STYLO_CHAT_BUMP_LIMIT = 10
stylo_chat_counters: dict[int, int] = {}

ROUND_CHAT_CHANNEL_ID = int(os.getenv("STYLO_CHAT_CHANNEL_ID", "0"))  # optional fixed host channel
ROUND_CHAT_THREAD_NAME = "stylo-round-chat"
ROUND_CHAT_FALLBACK_CH_NAME = "stylo-round-chat"  # used if we must create a text channel

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

INSTANCE = os.getenv("RAILWAY_PUBLIC_DOMAIN") or os.getenv("RAILWAY_PROJECT_ID") or "local"
print("[stylo] instance:", INSTANCE)

# ---------------- DB helpers ----------------
def db():
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;
    
    CREATE TABLE IF NOT EXISTS event (
        guild_id         INTEGER PRIMARY KEY,
        theme            TEXT NOT NULL,
        state            TEXT NOT NULL,          -- 'entry' | 'voting' | 'closed'
        entry_end_utc    TEXT NOT NULL,
        vote_hours       INTEGER NOT NULL,
        vote_seconds     INTEGER,
        round_index      INTEGER NOT NULL DEFAULT 0,
        main_channel_id  INTEGER,
        start_msg_id     INTEGER,
        round_thread_id  INTEGER
    );

    CREATE TABLE IF NOT EXISTS entrant (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        name       TEXT NOT NULL,
        caption    TEXT,
        image_url  TEXT,
        UNIQUE(guild_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS ticket (
        entrant_id INTEGER UNIQUE,
        channel_id INTEGER,
        FOREIGN KEY(entrant_id) REFERENCES entrant(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS match (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id    INTEGER NOT NULL,
        round_index INTEGER NOT NULL,
        left_id     INTEGER NOT NULL,
        right_id    INTEGER NOT NULL,
        msg_id      INTEGER,
        thread_id   INTEGER,
        end_utc     TEXT,
        left_votes  INTEGER NOT NULL DEFAULT 0,
        right_votes INTEGER NOT NULL DEFAULT 0,
        winner_id   INTEGER
    );

    CREATE TABLE IF NOT EXISTS voter (
        match_id  INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        side      TEXT NOT NULL,
        PRIMARY KEY (match_id, user_id),
        FOREIGN KEY(match_id) REFERENCES match(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id           INTEGER PRIMARY KEY,
        ticket_category_id INTEGER
    );
    
    CREATE TABLE IF NOT EXISTS bump_panel (
        guild_id INTEGER NOT NULL,
        match_id INTEGER NOT NULL,
        msg_id   INTEGER NOT NULL,
        PRIMARY KEY (guild_id, msg_id)
    );  
    """)
    con.commit()
    con.close()
init_db()

# ---------------- Small helpers ----------------
def rel_ts(dt_utc: datetime) -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    else:
        dt_utc = dt_utc.astimezone(timezone.utc)
    return f"<t:{int(dt_utc.timestamp())}:R>"

def humanize_seconds(sec: int) -> str:
    m = round(sec / 60)
    return f"{m//60}h" if m % 60 == 0 else f"{m}m"

def parse_duration_to_seconds(text: str, default_unit="h") -> int:
    s = (text or "").strip().lower().replace(" ", "")
    m = re.match(r"^([0-9]*\.?[0-9]+)([mh])?$", s)
    if not m:
        raise ValueError("invalid duration")
    val = float(m.group(1))
    unit = m.group(2) or default_unit
    minutes = val * (60 if unit == "h" else 1)
    seconds = int(round(minutes * 60))
    return max(60, min(seconds, 60 * 60 * 24 * 10))  # 1m..10d

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.manage_guild or member.guild_permissions.administrator

def get_ticket_category_id(guild_id: int) -> int | None:
    con = db()
    cur = con.cursor()
    cur.execute("SELECT ticket_category_id FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    con.close()
    return (row["ticket_category_id"] if row and row["ticket_category_id"] else None)

def set_ticket_category_id(guild_id: int, category_id: int | None):
    con = db()
    cur = con.cursor()
    if category_id is None:
        cur.execute("DELETE FROM guild_settings WHERE guild_id=?", (guild_id,))
    else:
        cur.execute(
            "INSERT INTO guild_settings(guild_id, ticket_category_id) VALUES(?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET ticket_category_id=excluded.ticket_category_id",
            (guild_id, category_id)
        )
    con.commit()
    con.close()

# ---------------- Event-wide chat thread ----------------
async def ensure_event_chat_thread(guild: discord.Guild, ch: discord.TextChannel, ev_row: sqlite3.Row) -> int | None:
    if not (guild and ch and ev_row):
        return None

    th_id = ev_row["round_thread_id"] if "round_thread_id" in ev_row.keys() else None
    if th_id:
        th = guild.get_thread(th_id)
        if th:
            return th.id

    title = f"🗣 Theme Chat — {ev_row['theme']}" if ("theme" in ev_row.keys() and ev_row["theme"]) else "🗣 Theme Chat"
    try:
        th = await ch.create_thread(name=title, auto_archive_duration=1440)
        con = db(); cur = con.cursor()
        cur.execute("UPDATE event SET round_thread_id=? WHERE guild_id=?", (th.id, ev_row["guild_id"]))
        con.commit(); con.close()
        await th.send(embed=discord.Embed(
            title="Supporter Chat",
            description="Talk here — entries & voting announcements stay in the main channel.",
            colour=discord.Colour.dark_grey()
        ))
        return th.id
    except Exception as e:
        print("[stylo] create event chat thread failed:", e)
        return None

def get_event_chat_url(guild: discord.Guild, thread_id: int | None) -> str | None:
    if not (guild and thread_id):
        return None
    th = guild.get_thread(thread_id)
    return th.jump_url if th else None

async def post_chat_floating_panel(guild: discord.Guild, ch: discord.TextChannel, ev_row: sqlite3.Row):
    if not (guild and ch and ev_row):
        return
    thread_id = await ensure_event_chat_thread(guild, ch, ev_row)
    url = get_event_chat_url(guild, thread_id) if thread_id else None
    if not url:
        return

    em = discord.Embed(
        title="🗣 Theme Chat",
        description="Click to jump into the Supporter Chat thread for this theme.",
        colour=discord.Colour.dark_grey()
    )
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=url, label="Chat here"))

    try:
        sent = await ch.send(embed=em, view=view)
        con = db(); cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO bump_panel(guild_id, match_id, msg_id) VALUES(?,?,?)",
                    (ev_row["guild_id"], 0, sent.id))
        con.commit(); con.close()
    except Exception as e:
        print("[stylo] chat floating panel failed:", e)

# ---------------- Lock / unlock main chat ----------------
async def lock_main_chat(guild: discord.Guild):
    ch = guild.get_channel(MAIN_CHAT_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        return
    overwrites = ch.overwrites or {}
    overwrites[guild.default_role] = discord.PermissionOverwrite(send_messages=False, add_reactions=False)
    for rid in DENY_ROLE_IDS:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(send_messages=False, add_reactions=False)
    owner = guild.get_member(OWNER_ID)
    if owner:
        overwrites[owner] = discord.PermissionOverwrite(send_messages=True, add_reactions=True)
    for rid in ALLOW_ROLE_IDS:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(send_messages=True, add_reactions=True)
    bot_member = guild.me
    if bot_member:
        overwrites[bot_member] = discord.PermissionOverwrite(
            send_messages=True, add_reactions=True,
            create_public_threads=True, create_private_threads=True,
            send_messages_in_threads=True, manage_threads=True
        )
    await ch.edit(overwrites=overwrites)

async def unlock_main_chat(guild: discord.Guild):
    ch = guild.get_channel(MAIN_CHAT_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        return
    overwrites = ch.overwrites or {}
    to_clear = [guild.default_role, guild.get_member(OWNER_ID)]
    to_clear += [guild.get_role(rid) for rid in ALLOW_ROLE_IDS]
    to_clear += [guild.get_role(rid) for rid in DENY_ROLE_IDS]
    for target in to_clear:
        if target in overwrites:
            overwrites.pop(target, None)
    await ch.edit(overwrites=overwrites)

async def cleanup_bump_panels(guild: discord.Guild, ch: discord.TextChannel | None):
    if not guild:
        return
    con = db(); cur = con.cursor()
    try:
        cur.execute("SELECT msg_id FROM bump_panel WHERE guild_id=?", (guild.id,))
        rows = cur.fetchall()
        if not rows:
            return
        if ch:
            for r in rows:
                try:
                    msg = await ch.fetch_message(r["msg_id"]); await msg.delete(); await asyncio.sleep(0.15)
                except:
                    pass
        cur.execute("DELETE FROM bump_panel WHERE guild_id=?", (guild.id,)); con.commit()
    finally:
        con.close()

# ---------------- Join modal + helpers ----------------
async def create_or_get_entrant(guild_id: int, user: discord.User, name: str, caption: str | None) -> int:
    con = db(); cur = con.cursor()
    try:
        # try existing
        cur.execute("SELECT id FROM entrant WHERE guild_id=? AND user_id=?", (guild_id, user.id))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE entrant SET name=?, caption=? WHERE id=?", (name, caption, row["id"]))
            con.commit()
            return row["id"]
        # new
        cur.execute("INSERT INTO entrant(guild_id, user_id, name, caption) VALUES(?,?,?,?)",
                    (guild_id, user.id, name, caption))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()

async def create_ticket_channel(inter: discord.Interaction, entrant_id: int, entrant_name: str) -> int | None:
    guild = inter.guild
    if not guild:
        return None
    cat_id = get_ticket_category_id(guild.id)
    category = guild.get_channel(cat_id) if cat_id else None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        inter.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, manage_channels=True, read_message_history=True),
    }
    try:
        if isinstance(category, discord.CategoryChannel):
            ch = await guild.create_text_channel(name=f"stylo-{entrant_name}".lower()[:90], category=category, overwrites=overwrites)
        else:
            ch = await guild.create_text_channel(name=f"stylo-{entrant_name}".lower()[:90], overwrites=overwrites)
        con = db(); cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO ticket(entrant_id, channel_id) VALUES(?,?)", (entrant_id, ch.id))
        con.commit(); con.close()
        await ch.send(
            f"Hi <@{inter.user.id}> — upload **one square image** for your look.\n"
            f"Your most recent image here will be used for Stylo."
        )
        return ch.id
    except discord.Forbidden:
        await inter.followup.send("I need **Manage Channels** on the ticket category to create your ticket.", ephemeral=True)
        return None
    except Exception as e:
        print("[stylo] ticket create failed:", e)
        return None

class EntrantModal(discord.ui.Modal, title="Join Stylo"):
    display_name = discord.ui.TextInput(label="Display name", placeholder="Your name for the bracket", max_length=40, required=True)
    caption      = discord.ui.TextInput(label="Caption (optional)", style=discord.TextStyle.paragraph, required=False, max_length=200)

    def __init__(self, inter: discord.Interaction):
        super().__init__()
        self._origin = inter

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=False)
        name = str(self.display_name).strip() or inter.user.display_name
        cap  = str(self.caption).strip() if self.caption else None
        entrant_id = await create_or_get_entrant(inter.guild_id, inter.user, name, cap)
        ch_id = await create_ticket_channel(self._origin, entrant_id, name)
        if ch_id:
            await inter.followup.send("Ticket created — please upload your image there.", ephemeral=True)
        else:
            await inter.followup.send("Couldn’t create your ticket. Check bot permissions on the ticket category.", ephemeral=True)

# ---------------- Join button (persistent view) ----------------
def build_join_view(enabled: bool = True) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    btn = discord.ui.Button(
        style=discord.ButtonStyle.success,
        label="Join",
        custom_id="stylo:join",
        disabled=not enabled,
    )
    async def join_cb(i: discord.Interaction):
        if i.user.bot:
            return
        try:
            await i.response.send_modal(EntrantModal(i))
        except discord.errors.NotFound:
            try:
                await i.followup.send("That fizzled. Please tap **Join** again.", ephemeral=True)
            except Exception:
                pass
        except discord.InteractionResponded:
            pass
        except Exception:
            try:
                await i.response.send_message("Couldn’t open the form. Try again.", ephemeral=True)
            except Exception:
                pass
    btn.callback = join_cb
    view.add_item(btn)
    return view

# ---------------- Voting UI ----------------
class ChatJumpView(discord.ui.View):
    def __init__(self, chat_url: str, *, timeout: float | None = None):
        super().__init__(timeout=timeout)
        self.add_item(discord.ui.Button(label="Round Chat", style=discord.ButtonStyle.link, url=chat_url))

class MatchView(discord.ui.View):
    def __init__(self, match_id: int, end_utc: datetime, left_label: str, right_label: str, chat_url: str | None = None):
        timeout = max(1, int((end_utc - datetime.now(timezone.utc)).total_seconds()))
        super().__init__(timeout=timeout)
        self.match_id = match_id
        self.btn_left.label = f"Vote {left_label}"
        self.btn_right.label = f"Vote {right_label}"
        if chat_url:
            self.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=chat_url, label="Chat here"))

    async def _vote(self, interaction: discord.Interaction, side: str):
        try:
            con = db(); cur = con.cursor()
            cur.execute("SELECT left_votes, right_votes, end_utc FROM match WHERE id=?", (self.match_id,))
            row = cur.fetchone()
            if not row:
                await interaction.response.send_message("Match not found.", ephemeral=True); return
            end_dt = datetime.fromisoformat(row["end_utc"]).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= end_dt:
                await interaction.response.send_message("Voting has ended for this match.", ephemeral=True); return
            try:
                cur.execute("INSERT INTO voter(match_id, user_id, side) VALUES(?,?,?)",
                            (self.match_id, interaction.user.id, side))
            except sqlite3.IntegrityError:
                await interaction.response.send_message("You’ve already voted for this match.", ephemeral=True); return
            if side == "L":
                cur.execute("UPDATE match SET left_votes = left_votes + 1 WHERE id=?", (self.match_id,))
            else:
                cur.execute("UPDATE match SET right_votes = right_votes + 1 WHERE id=?", (self.match_id,))
            con.commit()
            cur.execute("SELECT left_votes, right_votes FROM match WHERE id=?", (self.match_id,))
            m = cur.fetchone()
            L, R = m["left_votes"], m["right_votes"]
            total = L + R
            pa = math.floor((L / total) * 100) if total else 0
        except Exception as e:
            print(f"[stylo] vote error: {e!r}")
            try: await interaction.response.send_message("Voting error — try again.", ephemeral=True)
            except Exception: pass
            return
        finally:
            try: con.close()
            except: pass

        if interaction.message and interaction.message.embeds:
            em = interaction.message.embeds[0]
            if em.fields:
                em.set_field_at(0, name="Live totals", value=f"Total votes: **{total}**", inline=False)
            else:
                em.add_field(name="Live totals", value=f"Total votes: **{total}**", inline=False)
            await interaction.response.edit_message(embed=em, view=self)
        else:
            await interaction.response.edit_message(view=self)

        if total >= 2:
            if pa >= 80: banter = "That’s a rinse."
            elif pa >= 65: banter = "Crowd’s leaning that way."
            elif 45 <= pa <= 55: banter = "Neck and neck."
            else: banter = "Backing the underdog."
        else:
            banter = "Vote registered."
        await interaction.followup.send(banter, ephemeral=True)

    @discord.ui.button(style=discord.ButtonStyle.success, custom_id="stylo:vote_left")
    async def btn_left(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await self._vote(interaction, "L")

    @discord.ui.button(style=discord.ButtonStyle.danger, custom_id="stylo:vote_right")
    async def btn_right(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await self._vote(interaction, "R")

    async def on_timeout(self):
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True

# ---------------- Start modal (single copy) ----------------
class StyloStartModal(discord.ui.Modal, title="Start Stylo Challenge"):
    theme = discord.ui.TextInput(label="Theme / Title", max_length=100)
    entry_hours = discord.ui.TextInput(label="Entry window (hours/m)", default="24")
    vote_hours  = discord.ui.TextInput(label="Vote window per round (hours/m)", default="24")

    def __init__(self, inter: discord.Interaction):
        super().__init__()
        self._origin = inter

    async def on_submit(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("Guild context missing.", ephemeral=True); return
        try:
            entry_sec = parse_duration_to_seconds(str(self.entry_hours), default_unit="h")
            vote_sec  = parse_duration_to_seconds(str(self.vote_hours),  default_unit="h")
        except Exception:
            await inter.response.send_message("Invalid duration. Use numbers with h/m (e.g. 2h, 30m).", ephemeral=True); return

        theme = str(self.theme).strip()
        now_utc = datetime.now(timezone.utc)
        entry_end = now_utc + timedelta(seconds=entry_sec)

        con = db(); cur = con.cursor()
        try:
            cur.execute("DELETE FROM match   WHERE guild_id=?", (inter.guild_id,))
            cur.execute("DELETE FROM ticket  WHERE entrant_id IN (SELECT id FROM entrant WHERE guild_id=?)", (inter.guild_id,))
            cur.execute("DELETE FROM entrant WHERE guild_id=?", (inter.guild_id,))
            con.commit()
            cur.execute(
                "REPLACE INTO event (guild_id, theme, state, entry_end_utc, vote_hours, vote_seconds, round_index, main_channel_id, start_msg_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (inter.guild_id, theme, "entry", entry_end.isoformat(),
                 int(round(vote_sec/3600)), int(vote_sec), 0, inter.channel_id, None)
            ); con.commit()
        finally:
            con.close()

        em = discord.Embed(
            title=f"✨ Stylo: {theme}" if theme else "✨ Stylo",
            description="Entries are now **open**!\nClick **Join** to submit your look. Upload a square image in your ticket.",
            colour=EMBED_COLOUR,
        )
        em.add_field(name="Entries", value=f"Open for **{humanize_seconds(entry_sec)}**\nCloses {rel_ts(entry_end)}", inline=True)
        em.add_field(name="Voting",  value=f"Each round runs **{humanize_seconds(vote_sec)}**", inline=True)

        await inter.response.defer(ephemeral=True)
        start_msg = await inter.followup.send(embed=em, view=build_join_view(True), wait=True)
        try: await start_msg.pin()
        except: pass
        con = db(); cur = con.cursor()
        cur.execute("UPDATE event SET start_msg_id=? WHERE guild_id=?", (start_msg.id, inter.guild_id))
        con.commit(); con.close()
        await inter.followup.send("Stylo opened. Join is live.", ephemeral=True)

# ---------------- VS Card (side-by-side) ----------------
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
        tile.paste(img, ((tile_w - img.width) // 2, (target_h - img.height) // 2))
        return tile
    canvas = Image.new("RGB", (width, target_h), (20, 20, 30))
    canvas.paste(make_tile(Lc), (0, 0))
    canvas.paste(make_tile(Rc), (tile_w + gap, 0))
    ImageDraw.Draw(canvas).rectangle([tile_w, 0, tile_w + gap, target_h], fill=(45, 45, 60))
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out

async def fetch_image_bytes(url: str) -> bytes | None:
    if not url: return None
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url) as r:
                if r.status == 200: return await r.read()
    except Exception as e:
        print("[stylo] fetch_image_bytes error:", e)
    return None

async def fetch_latest_ticket_image_url(guild: discord.Guild, entrant_id: int) -> str | None:
    con = db(); cur = con.cursor()
    try:
        cur.execute("SELECT channel_id FROM ticket WHERE entrant_id=?", (entrant_id,))
        row = cur.fetchone()
    finally:
        con.close()
    if not row: return None
    ch = guild.get_channel(row["channel_id"])
    if not isinstance(ch, discord.TextChannel): return None
    async for msg in ch.history(limit=200, oldest_first=False):
        if msg.author.bot: continue
        for att in msg.attachments:
            ctype_ok = (att.content_type or "").startswith("image/")
            name = (att.filename or "").lower().split("?")[0]
            ext = name.rsplit(".", 1)[-1] if "." in name else ""
            if ctype_ok or ext in {"png", "jpg", "jpeg", "gif", "webp", "heic", "heif", "bmp", "tif", "tiff"}:
                return att.url
    return None

# ---------------- Message listener: capture image ----------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    if not message.attachments:
        await bot.process_commands(message)
        await maybe_bump_stylo_panel(message)
        return

    con = db(); cur = con.cursor()
    try:
        cur.execute(
            "SELECT entrant.id AS entrant_id FROM ticket "
            "JOIN entrant ON entrant.id = ticket.entrant_id "
            "WHERE ticket.channel_id=?",
            (message.channel.id,),
        )
        row = cur.fetchone()
        if not row:
            await bot.process_commands(message)
            await maybe_bump_stylo_panel(message)
            return

        def is_image(att: discord.Attachment) -> bool:
            if att.content_type and att.content_type.startswith("image/"):
                return True
            name = (att.filename or "").lower().split("?")[0]
            ext = name.rsplit(".", 1)[-1] if "." in name else ""
            return ext in {"png","jpg","jpeg","gif","webp","heic","heif","bmp","tif","tiff"}

        img_att = next((a for a in message.attachments if is_image(a)), None)
        if not img_att:
            await bot.process_commands(message)
            await maybe_bump_stylo_panel(message)
            return

        img_bytes = await img_att.read()
        bot_file = discord.File(io.BytesIO(img_bytes), filename=img_att.filename or "entry.png")
        bot_msg = await message.channel.send(
            content=(f"📸 Entry updated for <@{message.author.id}>.\n"
                     f"Your most recent image will be used for Stylo."),
            file=bot_file,
        )
        bot_url = bot_msg.attachments[0].url if bot_msg.attachments else img_att.url
        cur.execute("UPDATE entrant SET image_url=? WHERE id=?", (bot_url, row["entrant_id"]))
        con.commit()
        try: await message.add_reaction("✅")
        except: pass
    finally:
        con.close()
        await bot.process_commands(message)
        await maybe_bump_stylo_panel(message)

async def maybe_bump_stylo_panel(message: discord.Message):
    if not message.guild or not isinstance(message.channel, discord.TextChannel): return
    if message.author.bot: return
    con = db(); cur = con.cursor()
    try:
        cur.execute("SELECT * FROM event WHERE guild_id=? AND state IN ('entry','voting')",(message.guild.id,))
        ev = cur.fetchone()
    finally:
        con.close()
    if not ev: return
    if ev["main_channel_id"] != message.channel.id: return
    cid = message.channel.id
    count = stylo_chat_counters.get(cid, 0) + 1
    stylo_chat_counters[cid] = count
    if ev["state"] == "entry":
        if count >= STYLO_CHAT_BUMP_LIMIT:
            stylo_chat_counters[cid] = 0
            await send_stylo_status(message.guild, message.channel, ev, entries_open=True, join_enabled=True)
        return
    if ev["state"] == "voting":
        if count >= STYLO_CHAT_BUMP_LIMIT:
            stylo_chat_counters[cid] = 0
            await bump_voting_panels(message.guild, message.channel, ev)
        return

# ---------------- Status + bump panels ----------------
async def send_stylo_status(guild: discord.Guild, ch: discord.TextChannel, ev, entries_open: bool, join_enabled: bool):
    if not ch or not entries_open: return
    theme = ev["theme"]; end_iso = ev["entry_end_utc"]
    title = f"✨ Stylo: {theme}" if theme else "✨ Stylo"
    dt = datetime.fromisoformat(end_iso).replace(tzinfo=timezone.utc)
    em = discord.Embed(
        title=title,
        description="\n".join(["Entries are **OPEN** ✨", f"Close {rel_ts(dt)}"]),
        colour=EMBED_COLOUR,
    )
    view = build_join_view(join_enabled)
    await ch.send(embed=em, view=view)

async def bump_voting_panels(guild: discord.Guild, ch: discord.TextChannel, ev_row: sqlite3.Row):
    if not (guild and ch): return
    try:
        thread_id = await ensure_event_chat_thread(guild, ch, ev_row)
    except Exception as e:
        print("[stylo] bump: ensure event chat failed:", e); thread_id = None
    chat_url = get_event_chat_url(guild, thread_id) if thread_id else None

    con = db(); cur = con.cursor()
    try:
        cur.execute(
            "SELECT id, left_id, right_id, end_utc FROM match "
            "WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
            (ev_row["guild_id"], ev_row["round_index"])
        )
        open_matches = cur.fetchall()
        if not open_matches: return
        for m in open_matches:
            cur.execute("SELECT name FROM entrant WHERE id=?", (m["left_id"],)); Lrow = cur.fetchone()
            Lname = (Lrow["name"] if (Lrow and "name" in Lrow.keys()) else "Left")
            cur.execute("SELECT name FROM entrant WHERE id=?", (m["right_id"],)); Rrow = cur.fetchone()
            Rname = (Rrow["name"] if (Rrow and "name" in Rrow.keys()) else "Right")
            end_dt = datetime.fromisoformat(m["end_utc"]).replace(tzinfo=timezone.utc)
            em = discord.Embed(
                title=f"🗳 Voting panel — Round {ev_row['round_index']}",
                description=f"**{Lname}** vs **{Rname}**\nCloses {rel_ts(end_dt)}",
                colour=EMBED_COLOUR
            )
            view = MatchView(m["id"], end_dt, Lname, Rname, chat_url=chat_url)
            try:
                sent = await ch.send(embed=em, view=view)
                cur.execute(
                    "INSERT OR IGNORE INTO bump_panel(guild_id, match_id, msg_id) VALUES(?,?,?)",
                    (ev_row["guild_id"], m["id"], sent.id)
                )
                con.commit(); await asyncio.sleep(0.2)
            except Exception as e:
                print("[stylo] bump panel send failed:", e)
    finally:
        con.close()

# ---------------- Post matches ----------------
async def post_round_matches(ev, round_index: int, vote_end: datetime, con, cur):
    guild = bot.get_guild(ev["guild_id"])
    ch = guild.get_channel(ev["main_channel_id"]) if (guild and ev["main_channel_id"]) else (guild.system_channel if guild else None)
    if not (guild and ch):
        return
    try:
        thread_id = await ensure_event_chat_thread(guild, ch, ev)
    except Exception as e:
        print("[stylo] ensure event chat thread failed:", e); thread_id = None
    chat_url = get_event_chat_url(guild, thread_id) if thread_id else None

    cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND msg_id IS NULL",(ev["guild_id"], round_index))
    matches = cur.fetchall()
    for m in matches:
        cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],)); L = cur.fetchone()
        cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],)); R = cur.fetchone()
        Lname = (L["name"] if L and "name" in L.keys() else "Left")
        Rname = (R["name"] if R and "name" in R.keys() else "Right")
        Lurl  = (L["image_url"] or "").strip() if L else ""
        Rurl  = (R["image_url"] or "").strip() if R else ""

        em = discord.Embed(
            title=f"Round {round_index} — {Lname} vs {Rname}",
            description="Tap a button to vote. One vote per person.",
            colour=EMBED_COLOUR
        )
        em.add_field(name="Live totals", value="Total votes: **0**", inline=False)
        em.add_field(name="Closes", value=rel_ts(vote_end), inline=False)
        view = MatchView(m["id"], vote_end, Lname, Rname, chat_url=chat_url)

        try:
            msg = None
            if Lurl and Rurl:
                try:
                    card = await build_vs_card(Lurl, Rurl)
                    file = discord.File(fp=card, filename="versus.png")
                    em.set_image(url="attachment://versus.png")
                    msg = await ch.send(embed=em, view=view, file=file)
                except Exception as e_card:
                    print(f"[stylo] VS card failed for match {m['id']}: {e_card!r}")
            if msg is None:
                header = await ch.send(embed=em, view=view); msg = header
                embeds, files = [], []
                if Lurl:
                    Lbytes = await fetch_image_bytes(Lurl)
                    if Lbytes:
                        fL = discord.File(io.BytesIO(Lbytes), filename="left.png"); files.append(fL)
                        em_left = discord.Embed(title=Lname, colour=discord.Colour.dark_grey())
                        em_left.set_image(url="attachment://left.png"); embeds.append(em_left)
                else:
                    embeds.append(discord.Embed(title=Lname, description="No image found.", colour=discord.Colour.dark_grey()))
                if Rurl:
                    Rbytes = await fetch_image_bytes(Rurl)
                    if Rbytes:
                        fR = discord.File(io.BytesIO(Rbytes), filename="right.png"); files.append(fR)
                        em_right = discord.Embed(title=Rname, colour=discord.Colour.dark_grey())
                        em_right.set_image(url="attachment://right.png"); embeds.append(em_right)
                else:
                    embeds.append(discord.Embed(title=Rname, description="No image found.", colour=discord.Colour.dark_grey()))
                if embeds:
                    if files: await ch.send(embeds=embeds, files=files)
                    else: await ch.send(embeds=embeds)
        except Exception as send_err:
            print(f"[stylo] send failed for match {m['id']}: {send_err!r}")
            try:
                fallback_em = discord.Embed(
                    title=f"Round {round_index} — {Lname} vs {Rname}",
                    description="Images failed to load, but you can still vote.",
                    colour=EMBED_COLOUR
                )
                fallback_em.add_field(name="Live totals", value="Total votes: **0**", inline=False)
                fallback_em.add_field(name="Closes", value=rel_ts(vote_end), inline=False)
                view = MatchView(m["id"], vote_end, Lname, Rname, chat_url=chat_url)
                msg = await ch.send(embed=fallback_em, view=view)
            except Exception as e2:
                print(f"[stylo] EVEN FALLBACK failed for match {m['id']}: {e2!r}")
                continue
        try:
            cur.execute("UPDATE match SET msg_id=?, thread_id=NULL WHERE id=?", (msg.id, m["id"]))
            con.commit()
        except Exception as e_db:
            print(f"[stylo] DB update failed for match {m['id']}: {e_db!r}")
        await asyncio.sleep(0.25)

# ---------------- Round advancement (unchanged logic) ----------------
# (Your full advance_to_next_round, lock/cleanup tickets, scheduler, commands, etc. remain as in your version.)
# To keep this reply within limits, I'm leaving that logic exactly as you posted (it already included
# tie handling, special match, champion announcement, and scheduler). Paste your existing
# `advance_to_next_round`, `lock_tickets_for_guild`, `cleanup_tickets_for_guild`,
# the voting-end scheduler section, the `/stylo_finish_round_now` command, and related helpers here
# WITHOUT the duplicate StyloStartModal.

# ---------------- Commands ----------------
@bot.tree.command(name="stylo", description="Start a Stylo challenge (admin only).")
async def stylo_cmd(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    try:
        await inter.response.send_modal(StyloStartModal(inter))
    except discord.errors.NotFound:
        try: await inter.followup.send("Discord dropped that. Please run `/stylo` again.", ephemeral=True)
        except Exception: pass
    except Exception as e:
        try: await inter.followup.send(f"Couldn't open Stylo modal: {e!r}", ephemeral=True)
        except Exception: pass

@bot.tree.command(name="stylo_set_ticket_category", description="Set the category for entry tickets (admin only).")
@app_commands.describe(category="Pick a category where ticket channels will be created.")
async def stylo_set_ticket_category(inter: discord.Interaction, category: discord.CategoryChannel):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    me = inter.guild.me
    perms = category.permissions_for(me)
    missing = []
    if not perms.view_channel:      missing.append("View Channel (category)")
    if not perms.manage_channels:   missing.append("Manage Channels (category)")
    if missing:
        await inter.response.send_message("I can’t use that category — missing: **" + ", ".join(missing) + "**.", ephemeral=True); return
    set_ticket_category_id(inter.guild_id, category.id)
    await inter.response.send_message(f"✅ Ticket category set to **{category.name}**", ephemeral=True)

@bot.tree.command(name="stylo_show_ticket_category", description="Show the configured ticket category (admin only).")
async def stylo_show_ticket_category(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    cat_id = get_ticket_category_id(inter.guild_id)
    if not cat_id:
        await inter.response.send_message("No ticket category set.", ephemeral=True); return
    cat = inter.guild.get_channel(cat_id)
    if isinstance(cat, discord.CategoryChannel):
        await inter.response.send_message(f"Current ticket category: **{cat.name}**", ephemeral=True)
    else:
        await inter.response.send_message("Stored ticket category no longer exists.", ephemeral=True)

# ---------------- Ready ----------------
@bot.event
async def on_ready():
    # Persistent 'Join' so users can click even after restarts
    bot.add_view(build_join_view(True))
    try:
        await bot.tree.sync()
        for g in bot.guilds:
            try: await bot.tree.sync(guild=discord.Object(id=g.id))
            except Exception as e: print("Guild sync error:", g.id, e)
    except Exception as e:
        print("Slash sync error:", e)
    if not scheduler.is_running():
        scheduler.start()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ---------------- Run ----------------
if __name__ == "__main__":
    bot.run(TOKEN)

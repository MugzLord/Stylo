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
        start_msg_id     INTEGER
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

    # --- schema patch: store one chat thread per round ---
    try:
        con.execute("ALTER TABLE event ADD COLUMN round_thread_id INTEGER")
    except Exception:
        pass  # already there

    # --- (if you already added bump_panel earlier, keep it; else create it) ---
    con.execute("""
    CREATE TABLE IF NOT EXISTS bump_panel (
        guild_id INTEGER NOT NULL,
        match_id INTEGER NOT NULL,
        msg_id   INTEGER NOT NULL,
        PRIMARY KEY (guild_id, msg_id)
    );
    """)


init_db()


# ---------------- Utils ----------------
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

async def ensure_round_chat_thread(guild: discord.Guild, ch: discord.TextChannel, ev_row: sqlite3.Row) -> int | None:
    """Create (or reuse) a single Supporter Chat thread for the current round."""
    if not (guild and ch and ev_row):
        return None
    if ev_row["round_thread_id"]:
        # sanity: confirm it still exists
        th = guild.get_thread(ev_row["round_thread_id"])
        if th:
            return th.id

    # Create a public thread directly under the channel
    try:
        th = await ch.create_thread(
            name=f"🗣 Round {ev_row['round_index']} — Chat",
            auto_archive_duration=1440
        )
        # Save it
        con = db(); cur = con.cursor()
        cur.execute("UPDATE event SET round_thread_id=? WHERE guild_id=?", (th.id, ev_row["guild_id"]))
        con.commit(); con.close()

        # First message in the thread
        await th.send(embed=discord.Embed(
            title="Supporter Chat",
            description="Talk here — voting is on the main posts.",
            colour=discord.Colour.dark_grey()
        ))
        return th.id
    except Exception as e:
        print("[stylo] create round chat thread failed:", e)
        return None


def get_round_chat_url(guild: discord.Guild, thread_id: int | None) -> str | None:
    if not (guild and thread_id):
        return None
    th = guild.get_thread(thread_id)
    return th.jump_url if th else None


async def post_chat_floating_panel(guild: discord.Guild, ch: discord.TextChannel, ev_row: sqlite3.Row):
    """Drop a small 'Chat here' panel with a link button; track it to auto-delete at round end."""
    if not (guild and ch and ev_row):
        return
    # Ensure thread exists
    thread_id = await ensure_round_chat_thread(guild, ch, ev_row)
    url = get_round_chat_url(guild, thread_id)
    if not url:
        return

    em = discord.Embed(
        title=f"🗣 Round {ev_row['round_index']} — Supporter Chat",
        description="Click the button to jump into the chat thread.",
        colour=discord.Colour.dark_grey()
    )
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=url, label="Chat here"))

    try:
        sent = await ch.send(embed=em, view=view)
        con = db(); cur = con.cursor()
        # We store match_id=0 to mark 'chat-only' bump
        cur.execute("INSERT OR IGNORE INTO bump_panel(guild_id, match_id, msg_id) VALUES(?,?,?)",
                    (ev_row["guild_id"], 0, sent.id))
        con.commit(); con.close()
    except Exception as e:
        print("[stylo] chat floating panel failed:", e)

async def lock_main_chat(guild: discord.Guild):
    ch = guild.get_channel(MAIN_CHAT_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        return

    overwrites = ch.overwrites or {}

    # 1) Deny @everyone from sending/reactions
    overwrites[guild.default_role] = discord.PermissionOverwrite(
        send_messages=False,
        add_reactions=False
    )

    # 2) Explicitly deny listed roles (if they don't have Administrator)
    for rid in DENY_ROLE_IDS:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                send_messages=False,
                add_reactions=False
            )

    # 3) Allow ONLY the owner (you)
    owner = guild.get_member(OWNER_ID)
    if owner:
        overwrites[owner] = discord.PermissionOverwrite(
            send_messages=True,
            add_reactions=True
        )

    # 4) (Optional) allow a couple of roles
    for rid in ALLOW_ROLE_IDS:
        role = guild.get_role(rid)
        if role:
            # Allow these roles to speak
            overwrites[role] = discord.PermissionOverwrite(
                send_messages=True,
                add_reactions=True
            )

    # allow the bot itself to talk & thread
    bot_member = guild.me
    if bot_member:
        overwrites[bot_member] = discord.PermissionOverwrite(
            send_messages=True,
            add_reactions=True,
            create_public_threads=True,
            create_private_threads=True,
            send_messages_in_threads=True,
            manage_threads=True  # optional, helpful for archiving
        )


    await ch.edit(overwrites=overwrites)
    
async def unlock_main_chat(guild: discord.Guild):
    ch = guild.get_channel(MAIN_CHAT_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        return

    overwrites = ch.overwrites or {}

    # Remove the specific user/role overrides we added
    to_clear = [guild.default_role, guild.get_member(OWNER_ID)]
    to_clear += [guild.get_role(rid) for rid in ALLOW_ROLE_IDS]
    to_clear += [guild.get_role(rid) for rid in DENY_ROLE_IDS]
    for target in to_clear:
        if target in overwrites:
            overwrites.pop(target, None)

    await ch.edit(overwrites=overwrites)

async def cleanup_bump_panels(guild: discord.Guild, ch: discord.TextChannel | None):
    """Delete any temporary voting-bump panels for this guild (if they still exist)."""
    if not guild:
        return
    con = db()
    cur = con.cursor()
    try:
        cur.execute("SELECT msg_id FROM bump_panel WHERE guild_id=?", (guild.id,))
        rows = cur.fetchall()
        if not rows:
            return
        # Try delete each message (ignore failures)
        if ch:
            for r in rows:
                try:
                    msg = await ch.fetch_message(r["msg_id"])
                    await msg.delete()
                    await asyncio.sleep(0.15)
                except:
                    pass
        cur.execute("DELETE FROM bump_panel WHERE guild_id=?", (guild.id,))
        con.commit()
    finally:
        con.close()

async def get_or_make_round_chat(guild: discord.Guild, fallback_channel: discord.TextChannel) -> discord.abc.GuildChannel:
    """
    Returns a channel-like object with .jump_url (Thread OR TextChannel) that acts as the Round Chat.
    Order:
      1) Use ROUND_CHAT_CHANNEL_ID if valid, else fallback_channel
      2) Try to find existing open thread named ROUND_CHAT_THREAD_NAME
      3) Try to create a new thread off a seed message
      4) If threads are blocked, create a dedicated TEXT CHANNEL 'stylo-round-chat'
    """

    # Choose a host text channel (never a thread)
    host_ch = guild.get_channel(ROUND_CHAT_CHANNEL_ID) if ROUND_CHAT_CHANNEL_ID else fallback_channel
    if not isinstance(host_ch, discord.TextChannel):
        # last resort: pick guild's first text channel
        host_ch = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
        if host_ch is None:
            raise RuntimeError("No usable text channel for Round Chat.")

    # Ensure bot has explicit allow perms on host channel
    try:
        ow = host_ch.overwrites or {}
        ow[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            add_reactions=True,
            create_public_threads=True,
            create_private_threads=True,
            send_messages_in_threads=True,
            manage_threads=True
        )
        await host_ch.edit(overwrites=ow)
    except Exception:
        pass  # not fatal; we'll still try

    # 1) Reuse existing active/open thread
    try:
        async for th in host_ch.active_threads():
            if th.name == ROUND_CHAT_THREAD_NAME and not th.locked and not th.archived:
                return th
    except Exception:
        pass

    # 2) Try to create a thread from a seed message
    perms = host_ch.permissions_for(guild.me)
    if perms.send_messages and (perms.create_public_threads or perms.create_private_threads):
        try:
            seed = await host_ch.send("🧵 Round chat is here. Keep voting posts clean.")
            th = await host_ch.create_thread(
                name=ROUND_CHAT_THREAD_NAME,
                message=seed,
                auto_archive_duration=1440
            )
            await th.send("Chat about this round here. Votes via buttons on pair posts. 👍")
            return th
        except discord.Forbidden:
            pass
        except Exception:
            pass

        # Thread sans seed (some servers block sending but allow thread create)
        try:
            th = await host_ch.create_thread(
                name=ROUND_CHAT_THREAD_NAME,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440
            )
            await th.send("Chat about this round here. Votes via buttons on pair posts. 👍")
            return th
        except Exception:
            pass

    # 3) Fallback: make a dedicated TEXT CHANNEL (guaranteed space)
    try:
        # Try to locate an existing text channel with the same name
        existing = discord.utils.get(guild.text_channels, name=ROUND_CHAT_FALLBACK_CH_NAME)
        if existing:
            # make sure bot can speak
            ow = existing.overwrites or {}
            ow[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                add_reactions=True,
                send_messages_in_threads=True,
            )
            await existing.edit(overwrites=ow)
            await existing.send("🧵 Round chat is open here (fallback channel).")
            return existing

        # Create under same category if possible
        cat = host_ch.category
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        new_ch = await guild.create_text_channel(
            name=ROUND_CHAT_FALLBACK_CH_NAME,
            category=cat,
            overwrites=overwrites
        )
        await new_ch.send("🧵 Round chat is open here (fallback channel).")
        return new_ch
    except Exception as e:
        raise RuntimeError(f"Failed to create any Round Chat target: {e}")

async def ensure_round_chat_thread(guild: discord.Guild, fallback_channel: discord.TextChannel) -> discord.Thread:
    # Prefer a dedicated channel if set; else use the fallback (where you post pairs)
    host_ch = guild.get_channel(ROUND_CHAT_CHANNEL_ID) if ROUND_CHAT_CHANNEL_ID else fallback_channel
    if not isinstance(host_ch, discord.TextChannel):
        host_ch = fallback_channel  # never host on a Thread

    # quick perms check for the bot in host_ch
    perms = host_ch.permissions_for(guild.me)
    if not (perms.send_messages and (perms.create_public_threads or perms.create_private_threads)):
        # try to open it up just for the bot so we can proceed
        try:
            ow = host_ch.overwrites or {}
            ow[guild.me] = discord.PermissionOverwrite(
                send_messages=True,
                add_reactions=True,
                create_public_threads=True,
                create_private_threads=True,
                send_messages_in_threads=True
            )
            await host_ch.edit(overwrites=ow)
        except Exception:
            # if we still can’t, fall back to the fallback_channel with perms check
            host_ch = fallback_channel
            perms = host_ch.permissions_for(guild.me)
            if not (perms.send_messages and (perms.create_public_threads or perms.create_private_threads)):
                raise RuntimeError("Bot lacks permissions to create Round Chat thread in both host and fallback channels")

    # find existing open thread by name
    try:
        async for th in host_ch.active_threads():
            if th.name == ROUND_CHAT_THREAD_NAME and not th.locked and not th.archived:
                return th
    except Exception:
        pass

    # create via seed message (preferred)
    try:
        seed_msg = await host_ch.send("🧵 Round chat is here. Keep voting posts clean.")
        th = await host_ch.create_thread(
            name=ROUND_CHAT_THREAD_NAME,
            message=seed_msg,
            auto_archive_duration=1440  # 24h
        )
        await th.send("Chat about this round here. Votes via buttons on pair posts. 👍")
        return th
    except discord.Forbidden:
        # fallback: create a thread without a seed message (some servers disallow sends but allow thread create)
        try:
            th = await host_ch.create_thread(
                name=ROUND_CHAT_THREAD_NAME,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440
            )
            await th.send("Chat about this round here. Votes via buttons on pair posts. 👍")
            return th
        except Exception as e:
            raise e



async def bump_voting_panels(guild: discord.Guild, ch: discord.TextChannel, ev_row: sqlite3.Row):
    """
    Re-post compact voting panels (with live buttons) for all CURRENT-ROUND matches
    that are still open (winner_id is NULL). These are temporary and cleaned up
    as soon as the round ends.
    """
    if not (guild and ch):
        return

    con = db()
    cur = con.cursor()
    try:
        cur.execute(
            "SELECT id, left_id, right_id, end_utc FROM match "
            "WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
            (ev_row["guild_id"], ev_row["round_index"])
        )
        open_matches = cur.fetchall()
        if not open_matches:
            return

        for m in open_matches:
            # names
            cur.execute("SELECT name FROM entrant WHERE id=?", (m["left_id"],))
            Lname = (cur.fetchone() or {}).get("name", "Left") if hasattr(sqlite3.Row, "get") else (cur.fetchone()["name"] if cur.fetchone() else "Left")
            cur.execute("SELECT name FROM entrant WHERE id=?", (m["right_id"],))
            Rname_row = cur.fetchone()
            Rname = Rname_row["name"] if (Rname_row and "name" in Rname_row.keys()) else "Right"

            # end time
            end_dt = datetime.fromisoformat(m["end_utc"]).replace(tzinfo=timezone.utc)

            em = discord.Embed(
                title=f"🗳 Voting panel — Round {ev_row['round_index']}",
                description=f"**{Lname}** vs **{Rname}**\nCloses {rel_ts(end_dt)}",
                colour=EMBED_COLOUR
            )
            view = MatchView(m["id"], end_dt, Lname, Rname)

            try:
                sent = await ch.send(embed=em, view=view)
                cur.execute(
                    "INSERT OR IGNORE INTO bump_panel(guild_id, match_id, msg_id) VALUES(?,?,?)",
                    (ev_row["guild_id"], m["id"], sent.id)
                )
                con.commit()
                await asyncio.sleep(0.2)
            except Exception as e:
                print("[stylo] bump panel send failed:", e)
    finally:
        con.close()


# ---------------- VS Card (side-by-side) ----------------
async def build_vs_card(left_url: str, right_url: str, width: int = 1200, gap: int = 24) -> io.BytesIO:
    async with aiohttp.ClientSession() as sess:
        async with sess.get(left_url) as r1:
            Lb = await r1.read()
        async with sess.get(right_url) as r2:
            Rb = await r2.read()
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
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url) as r:
                if r.status == 200:
                    return await r.read()
    except Exception as e:
        print("[stylo] fetch_image_bytes error:", e)
    return None


async def fetch_latest_ticket_image_url(guild: discord.Guild, entrant_id: int) -> str | None:
    """Best-effort: look inside their ticket channel and pick the latest image."""
    con = db()
    cur = con.cursor()
    try:
        cur.execute("SELECT channel_id FROM ticket WHERE entrant_id=?", (entrant_id,))
        row = cur.fetchone()
    finally:
        con.close()
    if not row:
        return None
    ch = guild.get_channel(row["channel_id"])
    if not isinstance(ch, discord.TextChannel):
        return None

    async for msg in ch.history(limit=200, oldest_first=False):
        if msg.author.bot:
            continue
        for att in msg.attachments:
            ctype_ok = (att.content_type or "").startswith("image/")
            name = (att.filename or "").lower().split("?")[0]
            ext = name.rsplit(".", 1)[-1] if "." in name else ""
            if ctype_ok or ext in {"png", "jpg", "jpeg", "gif", "webp", "heic", "heif", "bmp", "tif", "tiff"}:
                return att.url
    return None


# ---------------- Join button ----------------
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
        await i.response.send_modal(EntrantModal(i))

    btn.callback = join_cb
    view.add_item(btn)
    return view


# ---------------- Voting UI ----------------
class MatchView(discord.ui.View):
    def __init__(self, match_id: int, end_utc: datetime, left_label: str, right_label: str, chat_url: str | None = None):
        timeout = max(1, int((end_utc - datetime.now(timezone.utc)).total_seconds()))
        super().__init__(timeout=timeout)
        self.match_id = match_id
        self.btn_left.label = f"Vote {left_label}"
        self.btn_right.label = f"Vote {right_label}"
        if chat_url:
            self.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=chat_url, label="Chat here"))

class ChatJumpView(discord.ui.View):
    def __init__(self, chat_url: str, *, timeout: float | None = None):
        super().__init__(timeout=timeout)
        self.add_item(discord.ui.Button(
            label="Round Chat",
            style=discord.ButtonStyle.link,
            url=chat_url
        ))

class MatchView(discord.ui.View):
    def __init__(self, match_id: int, end_utc: datetime, left_label: str, right_label: str):
        timeout = max(1, int((end_utc - datetime.now(timezone.utc)).total_seconds()))
        super().__init__(timeout=timeout)
        self.match_id = match_id
        self.btn_left.label = f"Vote {left_label}"
        self.btn_right.label = f"Vote {right_label}"

    async def _vote(self, interaction: discord.Interaction, side: str):
        try:
            con = db()
            cur = con.cursor()
            cur.execute("SELECT left_votes, right_votes, end_utc FROM match WHERE id=?", (self.match_id,))
            row = cur.fetchone()
            if not row:
                await interaction.response.send_message("Match not found.", ephemeral=True)
                return
            end_dt = datetime.fromisoformat(row["end_utc"]).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= end_dt:
                await interaction.response.send_message("Voting has ended for this match.", ephemeral=True)
                return

            try:
                cur.execute(
                    "INSERT INTO voter(match_id, user_id, side) VALUES(?,?,?)",
                    (self.match_id, interaction.user.id, side)
                )
            except sqlite3.IntegrityError:
                await interaction.response.send_message("You’ve already voted for this match.", ephemeral=True)
                return

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
            try:
                await interaction.response.send_message("Voting error — try again.", ephemeral=True)
            except Exception:
                pass
            return
        finally:
            try:
                con.close()
            except:
                pass

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
            if pa >= 80:
                banter = "That’s a rinse."
            elif pa >= 65:
                banter = "Crowd’s leaning that way."
            elif 45 <= pa <= 55:
                banter = "Neck and neck."
            else:
                banter = "Backing the underdog."
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


# ---------------- Start modal ----------------
class StyloStartModal(discord.ui.Modal, title="Start Stylo Challenge"):
    theme = discord.ui.TextInput(label="Theme / Title", max_length=100)
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
            try:
                await inter.response.defer(ephemeral=False)
            except discord.InteractionResponded:
                pass

            entry_sec = parse_duration_to_seconds(str(self.entry_hours), default_unit="h")
            vote_sec = parse_duration_to_seconds(str(self.vote_hours), default_unit="h")
            theme = str(self.theme).strip()
            if not theme:
                await inter.followup.send("Theme is required.", ephemeral=True)
                return

            now_utc = datetime.now(timezone.utc)
            entry_end = now_utc + timedelta(seconds=entry_sec)

            con = db()
            cur = con.cursor()

            # reset previous event data for this guild
            cur.execute("DELETE FROM match   WHERE guild_id=?", (inter.guild_id,))
            cur.execute("DELETE FROM ticket  WHERE entrant_id IN (SELECT id FROM entrant WHERE guild_id=?)", (inter.guild_id,))
            cur.execute("DELETE FROM entrant WHERE guild_id=?", (inter.guild_id,))
            con.commit()

            cur.execute(
                "REPLACE INTO event (guild_id, theme, state, entry_end_utc, vote_hours, vote_seconds, round_index, main_channel_id, start_msg_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    inter.guild_id,
                    theme,
                    "entry",
                    entry_end.isoformat(),
                    int(round(vote_sec / 3600)),
                    int(vote_sec),
                    0,
                    inter.channel_id,
                    None,
                ),
            )
            con.commit()
            con.close()

            em = discord.Embed(
                title=f"✨ Stylo: {theme}",
                description=(
                    "Entries are now **open**!\n"
                    "Click **Join** to submit your look. Upload a square image in your ticket."
                ),
                colour=EMBED_COLOUR,
            )
            em.add_field(
                name="Entries",
                value=f"Open for **{humanize_seconds(entry_sec)}**\nCloses {rel_ts(entry_end)}",
                inline=True,
            )
            em.add_field(
                name="Voting",
                value=f"Each round runs **{humanize_seconds(vote_sec)}**",
                inline=True,
            )

            sent = await inter.followup.send(embed=em, view=build_join_view(True), wait=True)
            try:
                await sent.pin(reason="Stylo: keep Join visible during entries")
            except:
                pass

            con = db()
            cur = con.cursor()
            cur.execute("UPDATE event SET start_msg_id=? WHERE guild_id=?", (sent.id, inter.guild_id))
            con.commit()
            con.close()

        except Exception as e:
            import traceback, sys, textwrap
            traceback.print_exc(file=sys.stderr)
            await inter.followup.send(textwrap.shorten(f"Start failed: {e!r}", width=300), ephemeral=True)


# ---------------- Join modal ----------------
class EntrantModal(discord.ui.Modal, title="Join Stylo"):
    display_name = discord.ui.TextInput(label="Display name / alias", max_length=50)
    caption = discord.ui.TextInput(label="Caption (optional)", style=discord.TextStyle.paragraph, required=False, max_length=200)

    def __init__(self, inter: discord.Interaction):
        super().__init__()
        self._origin = inter

    async def on_submit(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("Guild context missing.", ephemeral=True)
            return
        try:
            con = db()
            cur = con.cursor()
            cur.execute("SELECT * FROM event WHERE guild_id=?", (inter.guild_id,))
            ev = cur.fetchone()
            if not ev or ev["state"] != "entry":
                con.close()
                await inter.response.send_message("Entries are not open.", ephemeral=True)
                return

            entry_end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= entry_end:
                con.close()
                await inter.response.send_message("Entries have just closed.", ephemeral=True)
                return

            name = str(self.display_name).strip()
            cap = (str(self.caption).strip() if self.caption is not None else "")
            try:
                cur.execute(
                    "INSERT INTO entrant(guild_id, user_id, name, caption) VALUES(?,?,?,?)",
                    (inter.guild_id, inter.user.id, name, cap),
                )
            except sqlite3.IntegrityError:
                cur.execute(
                    "UPDATE entrant SET name=?, caption=? WHERE guild_id=? AND user_id=?",
                    (name, cap, inter.guild_id, inter.user.id),
                )
            con.commit()

            cur.execute("SELECT id FROM entrant WHERE guild_id=? AND user_id=?", (inter.guild_id, inter.user.id))
            entrant_id = cur.fetchone()["id"]

            cur.execute("SELECT channel_id FROM ticket WHERE entrant_id=?", (entrant_id,))
            existing = cur.fetchone()
            if existing:
                already = inter.guild.get_channel(existing["channel_id"])
                if already:
                    con.close()
                    await inter.response.send_message(f"You already have a ticket: {already.mention}", ephemeral=True)
                    return
                else:
                    cur.execute("DELETE FROM ticket WHERE entrant_id=?", (entrant_id,))
                    con.commit()

            guild = inter.guild
            category = None
            cat_id = get_ticket_category_id(guild.id)
            if cat_id:
                maybe = guild.get_channel(cat_id)
                if isinstance(maybe, discord.CategoryChannel):
                    category = maybe

            default = guild.default_role
            admin_roles = [r for r in guild.roles if r.permissions.administrator]
            overwrites = {
                default: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
                inter.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
            }
            for r in admin_roles:
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True)

            ticket_name = f"stylo-entry-{inter.user.name}".lower()[:90]
            try:
                ticket = await guild.create_text_channel(
                    ticket_name,
                    overwrites=overwrites,
                    reason="Stylo entry ticket",
                    category=category,
                )
            except discord.Forbidden:
                ticket = await guild.create_text_channel(
                    ticket_name,
                    overwrites=overwrites,
                    reason="Stylo entry ticket (fallback)",
                )

            cur.execute("INSERT OR REPLACE INTO ticket(entrant_id, channel_id) VALUES(?,?)", (entrant_id, ticket.id))
            con.commit()
            con.close()

            theme = ev["theme"] if ev and "theme" in ev.keys() else None

            info = discord.Embed(
                title=f"✨ {theme}\n📸 Submit your outfit image" if theme else "📸 Submit your outfit image",
                description=(
                    "Upload **one** square (1:1) image here.\n"
                    "Your latest upload before entries close will be used.\n"
                    "This channel may be locked or deleted when voting starts."
                ),
                colour=EMBED_COLOUR,
            )

            await ticket.send(content=inter.user.mention, embed=info)
            await inter.response.send_message("Ticket created — please upload your image there.", ephemeral=True)

        except Exception as e:
            import traceback, sys, textwrap
            traceback.print_exc(file=sys.stderr)
            try:
                await inter.response.send_message(textwrap.shorten(f"Join failed: {e!r}", width=300), ephemeral=True)
            except discord.InteractionResponded:
                await inter.followup.send(textwrap.shorten(f"Join failed: {e!r}", width=300), ephemeral=True)


# ---------------- Helper: post matches ----------------
async def post_round_matches(ev, round_index: int, vote_end: datetime, con, cur):
    guild = bot.get_guild(ev["guild_id"])
    ch = guild.get_channel(ev["main_channel_id"]) if (guild and ev["main_channel_id"]) else (guild.system_channel if guild else None)
    if not (guild and ch):
        return

    cur.execute(
        "SELECT * FROM match WHERE guild_id=? AND round_index=? AND msg_id IS NULL",
        (ev["guild_id"], round_index)
    )
    matches = cur.fetchall()

    for m in matches:
        try:
            cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],))
            L = cur.fetchone()
            cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],))
            R = cur.fetchone()

            Lname = L["name"] if L else "Left"
            Rname = R["name"] if R else "Right"
            Lurl = (L["image_url"] or "").strip() if L else ""
            Rurl = (R["image_url"] or "").strip() if R else ""

            em = discord.Embed(
                title=f"Round {round_index} — {Lname} vs {Rname}",
                description="Tap a button to vote. One vote per person.",
                colour=EMBED_COLOUR
            )
            em.add_field(name="Live totals", value="Total votes: **0**", inline=False)
            em.add_field(name="Closes", value=rel_ts(vote_end), inline=False)
            view = MatchView(m["id"], vote_end, Lname, Rname)

            msg = None

            if Lurl and Rurl:
                try:
                    card = await build_vs_card(Lurl, Rurl)
                    file = discord.File(fp=card, filename="versus.png")
                    msg = await ch.send(embed=em, view=view, file=file)
                except Exception as e:
                    print(f"[stylo] VS card failed for match {m['id']}: {e!r}")

            if msg is None:
                header = await ch.send(embed=em, view=view)
                msg = header

                embeds = []
                files = []

                if Lurl:
                    Lbytes = await fetch_image_bytes(Lurl)
                    if Lbytes:
                        fL = discord.File(io.BytesIO(Lbytes), filename="left.png")
                        files.append(fL)
                        em_left = discord.Embed(title=Lname, colour=discord.Colour.dark_grey())
                        em_left.set_image(url="attachment://left.png")
                        embeds.append(em_left)
                else:
                    embeds.append(discord.Embed(title=Lname, description="No image found.", colour=discord.Colour.dark_grey()))

                if Rurl:
                    Rbytes = await fetch_image_bytes(Rurl)
                    if Rbytes:
                        fR = discord.File(io.BytesIO(Rbytes), filename="right.png")
                        files.append(fR)
                        em_right = discord.Embed(title=Rname, colour=discord.Colour.dark_grey())
                        em_right.set_image(url="attachment://right.png")
                        embeds.append(em_right)
                else:
                    embeds.append(discord.Embed(title=Rname, description="No image found.", colour=discord.Colour.dark_grey()))

                if embeds:
                    if files:
                        await ch.send(embeds=embeds, files=files)
                    else:
                        await ch.send(embeds=embeds)
            #
            # Attach a 'Chat here' link to the round-wide thread (if present)
            chat_url = get_round_chat_url(guild, ev.get("round_thread_id") if hasattr(ev, "get") else ev["round_thread_id"])
            # Rebuild the view with chat button
            view = MatchView(m["id"], vote_end, Lname, Rname, chat_url=chat_url)
            try:
                await msg.edit(view=view)
            except:
                pass

            cur.execute("UPDATE match SET msg_id=?, thread_id=NULL WHERE id=?", (msg.id, m["id"]))

            con.commit()

            await asyncio.sleep(0.25)

        except Exception as e:
            print(f"[stylo] hard failure posting match {m['id']}: {e!r}")
            try:
                fallback_em = discord.Embed(
                    title=f"Round {round_index} — Match {m['id']}",
                    description="Images failed to load, but you can still vote.",
                    colour=EMBED_COLOUR
                )
                fallback_em.add_field(name="Live totals", value="Total votes: **0**", inline=False)
                fallback_em.add_field(name="Closes", value=rel_ts(vote_end), inline=False)
                fallback_view = MatchView(m["id"], vote_end, "Left", "Right")
                fallback_msg = await ch.send(embed=fallback_em, view=fallback_view)
                cur.execute("UPDATE match SET msg_id=?, thread_id=? WHERE id=?", (fallback_msg.id, None, m["id"]))
                con.commit()
            except Exception as e2:
                print(f"[stylo] EVEN FALLBACK failed for match {m['id']}: {e2!r}")
            continue


async def send_stylo_status(guild: discord.Guild, ch: discord.TextChannel, ev, entries_open: bool, join_enabled: bool):
    if not ch or not entries_open:
        return

    theme = ev["theme"]
    end_iso = ev["entry_end_utc"]
    title = f"✨ Stylo: {theme}" if theme else "✨ Stylo"

    dt = datetime.fromisoformat(end_iso).replace(tzinfo=timezone.utc)

    em = discord.Embed(
        title=title,
        description="\n".join([
            "Entries are **OPEN** ✨",
            f"Close {rel_ts(dt)}",
        ]),
        colour=EMBED_COLOUR,
    )

    view = build_join_view(join_enabled)
    await ch.send(embed=em, view=view)


# ---------------- Message listener: capture image ----------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    if not message.attachments:
        await bot.process_commands(message)
        await maybe_bump_stylo_panel(message)
        return

    con = db()
    cur = con.cursor()
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
            return ext in {
                "png", "jpg", "jpeg", "gif", "webp", "heic", "heif", "bmp", "tif", "tiff",
            }

        img_att = next((a for a in message.attachments if is_image(a)), None)
        if not img_att:
            await bot.process_commands(message)
            await maybe_bump_stylo_panel(message)
            return

        img_bytes = await img_att.read()

        bot_file = discord.File(io.BytesIO(img_bytes), filename=img_att.filename or "entry.png")
        bot_msg = await message.channel.send(
            content=(
                f"📸 Entry updated for <@{message.author.id}>.\n"
                f"Your most recent image will be used for Stylo."
            ),
            file=bot_file,
        )
        bot_url = bot_msg.attachments[0].url if bot_msg.attachments else img_att.url

        cur.execute("UPDATE entrant SET image_url=? WHERE id=?", (bot_url, row["entrant_id"]))
        con.commit()

        try:
            await message.add_reaction("✅")
        except:
            pass

    finally:
        con.close()
        await bot.process_commands(message)
        await maybe_bump_stylo_panel(message)


async def maybe_bump_stylo_panel(message: discord.Message):
    if not message.guild or not isinstance(message.channel, discord.TextChannel):
        return
    if message.author.bot:
        return

    con = db()
    cur = con.cursor()
    try:
        cur.execute(
            "SELECT * FROM event WHERE guild_id=? AND state IN ('entry','voting')",
            (message.guild.id,),
        )
        ev = cur.fetchone()
    finally:
        con.close()

    if not ev:
        return

    # Only react in the configured main channel
    if ev["main_channel_id"] != message.channel.id:
        return

    cid = message.channel.id
    count = stylo_chat_counters.get(cid, 0) + 1
    stylo_chat_counters[cid] = count

    # ENTRY: bump the Join panel as you already do
    if ev["state"] == "entry":
        if count >= STYLO_CHAT_BUMP_LIMIT:
            stylo_chat_counters[cid] = 0
            await send_stylo_status(
                message.guild,
                message.channel,
                ev,
                entries_open=True,
                join_enabled=True,
            )
        return

    # VOTING: bump compact voting panels (temporary)
    if ev["state"] == "voting":
        if count >= STYLO_CHAT_BUMP_LIMIT:
            stylo_chat_counters[cid] = 0
            await bump_voting_panels(message.guild, message.channel, ev)
        return



# ---------------- Commands ----------------
@bot.tree.command(name="stylo", description="Start a Stylo challenge (admin only).")
async def stylo_cmd(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True)
        return

    try:
        await inter.response.send_modal(StyloStartModal(inter))
    except discord.errors.NotFound:
        try:
            await inter.followup.send("Discord dropped that. Please run `/stylo` again.", ephemeral=True)
        except Exception:
            pass
    except Exception as e:
        try:
            await inter.followup.send(f"Couldn't open Stylo modal: {e!r}", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="stylo_set_ticket_category", description="Set the category for entry tickets (admin only).")
@app_commands.describe(category="Pick a category where ticket channels will be created.")
async def stylo_set_ticket_category(inter: discord.Interaction, category: discord.CategoryChannel):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True)
        return
    me = inter.guild.me
    perms = category.permissions_for(me)
    missing = []
    if not perms.view_channel:
        missing.append("View Channel (category)")
    if not perms.manage_channels:
        missing.append("Manage Channels (category)")
    if missing:
        await inter.response.send_message(
            "I can’t use that category — missing: **" + ", ".join(missing) + "**.",
            ephemeral=True
        )
        return
    set_ticket_category_id(inter.guild_id, category.id)
    await inter.response.send_message(f"✅ Ticket category set to **{category.name}**", ephemeral=True)


@bot.tree.command(name="stylo_show_ticket_category", description="Show the configured ticket category (admin only).")
async def stylo_show_ticket_category(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True)
        return
    cat_id = get_ticket_category_id(inter.guild_id)
    if not cat_id:
        await inter.response.send_message("No ticket category set.", ephemeral=True)
        return
    cat = inter.guild.get_channel(cat_id)
    if isinstance(cat, discord.CategoryChannel):
        await inter.response.send_message(f"Current ticket category: **{cat.name}**", ephemeral=True)
    else:
        await inter.response.send_message("Stored ticket category no longer exists.", ephemeral=True)


@bot.tree.command(name="stylo_set_round_time_left", description="Shorten or extend the CURRENT Stylo voting round (admin).")
async def stylo_set_round_time_left(inter: discord.Interaction, minutes: int):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True)
        return

    if minutes < 1:
        minutes = 1

    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE guild_id=? AND state='voting'", (inter.guild_id,))
    ev = cur.fetchone()
    if not ev:
        con.close()
        await inter.response.send_message("No Stylo round currently in voting state.", ephemeral=True)
        return

    new_end = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    cur.execute("UPDATE event SET entry_end_utc=? WHERE guild_id=?", (new_end.isoformat(), inter.guild_id))
    con.commit()
    con.close()

    await inter.response.send_message(f"⏱ Current round will now end in **{minutes} minutes**.", ephemeral=True)


@bot.tree.command(name="stylo_finish_round_now", description="Force the current Stylo voting round to finish NOW and post winners. (admin)")
async def stylo_finish_round_now(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True)
        return

    await inter.response.defer(ephemeral=True)

    now = datetime.now(timezone.utc)
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE guild_id=? AND state='voting'", (inter.guild_id,))
    ev = cur.fetchone()
    if not ev:
        con.close()
        await inter.followup.send("No Stylo round in voting state for this guild.", ephemeral=True)
        return

    guild = inter.guild
    ch = guild.get_channel(ev["main_channel_id"]) if (guild and ev["main_channel_id"]) else (guild.system_channel if guild else None)

    cur.execute(
        "SELECT * FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
        (ev["guild_id"], ev["round_index"])
    )
    matches = cur.fetchall()

    vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
    any_revote = False
    winners = []

    for m in matches:
        L = m["left_votes"]
        R = m["right_votes"]

        cur.execute("SELECT name, user_id, image_url FROM entrant WHERE id=?", (m["left_id"],))
        Lrow = cur.fetchone()
        cur.execute("SELECT name, user_id, image_url FROM entrant WHERE id=?", (m["right_id"],))
        Rrow = cur.fetchone()
        LN = (Lrow["name"] if Lrow else "Left")
        RN = (Rrow["name"] if Rrow else "Right")
        Lurl = (Lrow["image_url"] or "").strip() if Lrow else ""
        Rurl = (Rrow["image_url"] or "").strip() if Rrow else ""

        # TIE -> re-vote with side-by-side
        if L == R:
            any_revote = True
            new_end = now + timedelta(seconds=vote_sec)
            cur.execute(
                "UPDATE match SET left_votes=0, right_votes=0, end_utc=?, winner_id=NULL WHERE id=?",
                (new_end.isoformat(), m["id"])
            )
            cur.execute("DELETE FROM voter WHERE match_id=?", (m["id"],))
            con.commit()

            # edit original message
            if ch and m["msg_id"]:
                try:
                    msg = await ch.fetch_message(m["msg_id"])
                    em = msg.embeds[0] if msg.embeds else discord.Embed(
                        title=f"Round {ev['round_index']} — {LN} vs {RN}",
                        description="Tap a button to vote. One vote per person.",
                        colour=EMBED_COLOUR
                    )

                    if em.fields:
                        em.set_field_at(0, name="Live totals", value="Total votes: **0**", inline=False)
                    else:
                        em.add_field(name="Live totals", value="Total votes: **0**", inline=False)

                    closes_idx = None
                    for idx, f in enumerate(em.fields):
                        if f.name.lower() == "closes":
                            closes_idx = idx
                            break
                    if closes_idx is not None:
                        em.set_field_at(closes_idx, name="Closes", value=rel_ts(new_end), inline=False)
                    else:
                        em.add_field(name="Closes", value=rel_ts(new_end), inline=False)

                    view = MatchView(m["id"], new_end, LN, RN)
                    await msg.edit(embed=em, view=view)
                except Exception as ex:
                    print("[stylo_finish] tie edit failed:", ex)

            # post a fresh side-by-side for the tie
            if ch:
                try:
                    if Lurl and Rurl:
                        card = await build_vs_card(Lurl, Rurl)
                        file = discord.File(card, filename="tie.png")
                        view2 = MatchView(m["id"], new_end, LN, RN)
                        await ch.send(
                            embed=discord.Embed(
                                title=f"🔁 Tie-break — {LN} vs {RN}",
                                description=f"Re-vote is open now and closes {rel_ts(new_end)}.",
                                colour=discord.Colour.orange()
                            ),
                            view=view2,
                            file=file
                        )
                    else:
                        view2 = MatchView(m["id"], new_end, LN, RN)
                        await ch.send(
                            embed=discord.Embed(
                                title=f"🔁 Tie-break — {LN} vs {RN}",
                                description=f"Re-vote is open now and closes {rel_ts(new_end)}.",
                                colour=discord.Colour.orange()
                            ),
                            view=view2
                        )
                except Exception as ex:
                    print("[stylo_finish] tie images send failed:", ex)

            continue

        # normal winner
        winner_id = m["left_id"] if L > R else m["right_id"]
        cur.execute("UPDATE match SET winner_id=?, end_utc=? WHERE id=?",
                    (winner_id, now.isoformat(), m["id"]))
        con.commit()
        winners.append((m["id"], winner_id, LN, RN, L, R))

        if ch:
            try:
                total = max(1, L + R)
                pL = round((L / total) * 100, 1)
                pR = round((R / total) * 100, 1)
                cur.execute("SELECT user_id, image_url FROM entrant WHERE id=?", (winner_id,))
                wrow = cur.fetchone()
                winner_mention = (f"<@{wrow['user_id']}>" if wrow and wrow["user_id"] else "the winner")
                em = discord.Embed(
                    title=f"🏁 Result — {LN} vs {RN}",
                    description=(
                        f"**{LN}**: {L} ({pL}%)\n"
                        f"**{RN}**: {R} ({pR}%)\n\n"
                        f"🏆 **Winner:** {winner_mention}"
                    ),
                    colour=discord.Colour.green()
                )
                file = None
                wurl = (wrow["image_url"] or "").strip() if wrow else ""
                if wurl:
                    data = await fetch_image_bytes(wurl)
                    if data:
                        file = discord.File(io.BytesIO(data), filename=f"winner_{m['id']}.png")
                        em.set_thumbnail(url=f"attachment://winner_{m['id']}.png")
                if file:
                    await ch.send(embed=em, file=file)
                else:
                    await ch.send(embed=em)
            except Exception as ex:
                print("[stylo_finish] result send err:", ex)

    if any_revote:
        # just extend event to latest tie and stop
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
        con.close()
        await inter.followup.send("Round extended due to tie-breaks.", ephemeral=True)
        return

    # no ties -> advance like scheduler does
    await cleanup_bump_panels(guild, ch)
    await advance_to_next_round(ev, now, con, cur, guild, ch)
    con.close()
    await inter.followup.send("Round finished.", ephemeral=True)


# ---------------- ticket helpers for scheduler ----------------
async def lock_tickets_for_guild(guild: discord.Guild):
    if not guild:
        return
    con = db()
    cur = con.cursor()
    try:
        cur.execute(
            "SELECT t.channel_id FROM ticket t JOIN entrant e ON e.id=t.entrant_id WHERE e.guild_id=?",
            (guild.id,)
        )
        rows = cur.fetchall()
        for r in rows:
            ch = guild.get_channel(r["channel_id"])
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.set_permissions(guild.default_role, send_messages=False)
                    await ch.set_permissions(guild.me, send_messages=True, attach_files=True, read_message_history=True)
                except:
                    pass
    finally:
        con.close()


async def cleanup_tickets_for_guild(guild: discord.Guild, reason: str):
    if not guild:
        return
    con = db()
    cur = con.cursor()
    try:
        cur.execute(
            "SELECT t.channel_id FROM ticket t JOIN entrant e ON e.id=t.entrant_id WHERE e.guild_id=?",
            (guild.id,)
        )
        rows = cur.fetchall()
        for r in rows:
            ch = guild.get_channel(r["channel_id"])
            if ch:
                try:
                    await ch.delete(reason=reason)
                except:
                    pass
                await asyncio.sleep(0.25)
        cur.execute("DELETE FROM ticket WHERE entrant_id IN (SELECT id FROM entrant WHERE guild_id=?)", (guild.id,))
        con.commit()
    finally:
        con.close()


# ---------------- Common: advance logic (no late entrants) ----------------
async def advance_to_next_round(ev, now, con, cur, guild, ch):
    """Shared between scheduler and /stylo_finish_round_now.
       Fixed: robust leftover vs strongest-loser (Round 1) and for any odd winners in later rounds.
    """
    gid = ev["guild_id"]
    cur_round = ev["round_index"]
    vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600

    # collect winners for this round
    cur.execute(
        "SELECT winner_id FROM match WHERE guild_id=? AND round_index=?",
        (gid, cur_round)
    )
    all_winners_this_round = [r["winner_id"] for r in cur.fetchall() if r["winner_id"]]

    # ---- helper: pick opponent with safe fallbacks ----
    def _pick_leftover_opponent(cur, gid: int, cur_round: int, losers_with_votes: list[tuple[int,int]]):
        """
        Return an opponent entrant_id for the leftover, or None to award a bye.
        Priority:
          1) highest losing-vote count
          2) highest total match votes among losers
          3) deterministic fallback: lowest entrant id among losers
          4) None -> bye
        """
        # 1) strongest by losing votes
        if losers_with_votes:
            losers_with_votes.sort(key=lambda t: (t[1], -t[0]), reverse=True)
            return losers_with_votes[0][0]

        # Build losers/totals from decided matches in this round
        cur.execute(
            "SELECT left_id, right_id, left_votes, right_votes, winner_id "
            "FROM match WHERE guild_id=? AND round_index=?",
            (gid, cur_round)
        )
        rows = cur.fetchall()
        losers_totals = []   # (loser_id, total_votes)
        losers_all = set()

        for m in rows:
            L, R = m["left_votes"], m["right_votes"]
            if not m["winner_id"]:
                continue
            if m["winner_id"] == m["left_id"]:
                losers_all.add(m["right_id"])
                losers_totals.append((m["right_id"], L + R))
            else:
                losers_all.add(m["left_id"])
                losers_totals.append((m["left_id"], L + R))

        # 2) highest total votes among losers
        if losers_totals:
            losers_totals.sort(key=lambda t: (t[1], -t[0]), reverse=True)
            return losers_totals[0][0]

        # 3) deterministic fallback: lowest entrant id among losers
        if losers_all:
            return min(losers_all)

        # 4) no losers available -> bye
        return None

    # odd-fix only for round 1: leftover vs strongest loser (hardened + mirrored pool)
    leftover_ids = []
    if cur_round == 1:
        cur.execute(
            "SELECT left_id, right_id, left_votes, right_votes, winner_id FROM match WHERE guild_id=? AND round_index=?",
            (gid, cur_round)
        )
        match_rows = cur.fetchall()

        fought_ids = set()
        losers_with_votes = []  # (loser_id, losing_vote_count)

        for m in match_rows:
            fought_ids.add(m["left_id"])
            fought_ids.add(m["right_id"])
            if m["winner_id"]:
                if m["winner_id"] == m["left_id"]:
                    loser_id = m["right_id"]
                    loser_votes = m["right_votes"]
                else:
                    loser_id = m["left_id"]
                    loser_votes = m["left_votes"]
                losers_with_votes.append((loser_id, loser_votes))

        # mirror scheduler pool selection
        cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=?", (gid,))
        total_entrants = cur.fetchone()["c"] or 0

        cur.execute(
            "SELECT id FROM entrant WHERE guild_id=? AND image_url IS NOT NULL AND TRIM(image_url) <> ''",
            (gid,)
        )
        with_image = {r["id"] for r in cur.fetchall()}

        if len(with_image) >= 2:
            pool = with_image
        elif total_entrants >= 2:
            cur.execute("SELECT id FROM entrant WHERE guild_id=?", (gid,))
            pool = {r["id"] for r in cur.fetchall()}
        else:
            pool = with_image  # edge case: 0/1 entrant

        leftover_ids = list(pool - set(fought_ids) - set(all_winners_this_round))

        opponent_id = _pick_leftover_opponent(cur, gid, cur_round, losers_with_votes)

        # if exactly 1 leftover and we have an opponent, create special match
        if len(leftover_ids) == 1 and opponent_id is not None:
            vote_end_2 = now + timedelta(seconds=vote_sec)
            cur.execute(
                "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                (gid, cur_round, leftover_ids[0], opponent_id, vote_end_2.isoformat())
            )
            con.commit()
            # extend event to let this special match finish
            cur.execute(
                "UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                (vote_end_2.isoformat(), gid)
            )
            con.commit()
            if ch:
                await ch.send(embed=discord.Embed(
                    title="🆚 Stylo — Special Match",
                    description="The extra one goes head-to-head with a wildcard..",
                    colour=EMBED_COLOUR
                ))
            await post_round_matches(ev, cur_round, vote_end_2, con, cur)
            # stop here — scheduler will finish it later
            return
        elif len(leftover_ids) == 1 and opponent_id is None:
            # no opponent -> push leftover forward as a bye
            all_winners_this_round.append(leftover_ids[0])
            leftover_ids = []

    # unlock chat
    if ch and guild:
        try:
            await ch.set_permissions(guild.default_role, send_messages=True)
        except:
            pass

    # champion?
    if len(all_winners_this_round) == 1 and not leftover_ids:
        champ_id = all_winners_this_round[0]
        cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (gid,))
        con.commit()

        cur.execute("SELECT name, image_url, user_id FROM entrant WHERE id=?", (champ_id,))
        w = cur.fetchone()
        winner_name = (w["name"] if w else "Unknown")
        em = discord.Embed(
            title=f"👑 Stylo Champion — {ev['theme']}",
            description=f"Winner by public vote: **{winner_name}**" + (f"\n<@{w['user_id']}>" if w and w["user_id"] else ""),
            colour=discord.Colour.gold()
        )
        file = None
        wurl = (w["image_url"] or "").strip() if w else ""
        if wurl:
            data = await fetch_image_bytes(wurl)
            if data:
                file = discord.File(io.BytesIO(data), filename="champion.png")
                em.set_image(url="attachment://champion.png")

        if ch:
            if file:
                await ch.send(embed=em, file=file)
            else:
                await ch.send(embed=em)

        if guild:
            try:
                await cleanup_tickets_for_guild(guild, reason="Stylo: finished - deleting tickets")
            except:
                pass

        return

    # ---- NEW: Any round with odd winners -> run special in CURRENT round ----
    if len(all_winners_this_round) >= 1 and (len(all_winners_this_round) % 2 == 1):
        # Build losers_with_votes from current round (if not already done in r1)
        cur.execute(
            "SELECT left_id, right_id, left_votes, right_votes, winner_id FROM match WHERE guild_id=? AND round_index=?",
            (gid, cur_round)
        )
        rows = cur.fetchall()
        losers_with_votes_any = []
        fought_ids_any = set()
        for m in rows:
            fought_ids_any.add(m["left_id"]); fought_ids_any.add(m["right_id"])
            if m["winner_id"]:
                if m["winner_id"] == m["left_id"]:
                    losers_with_votes_any.append((m["right_id"], m["right_votes"]))
                else:
                    losers_with_votes_any.append((m["left_id"], m["left_votes"]))

        opponent_id_any = _pick_leftover_opponent(cur, gid, cur_round, losers_with_votes_any)

        # Choose a deterministic leftover winner: lowest id among winners
        leftover_winner = sorted(all_winners_this_round)[-1]  # pick highest id for a bit of variety
        all_winners_this_round = [w for w in all_winners_this_round if w != leftover_winner]

        if opponent_id_any is not None:
            vote_end_2 = now + timedelta(seconds=vote_sec)
            cur.execute(
                "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                (gid, cur_round, leftover_winner, opponent_id_any, vote_end_2.isoformat())
            )
            con.commit()
            cur.execute(
                "UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                (vote_end_2.isoformat(), gid)
            )
            con.commit()
            if ch:
                # optional: fetch names best-effort
                def _name(eid: int) -> str:
                    try:
                        cur.execute("SELECT name FROM entrant WHERE id=?", (eid,))
                        r = cur.fetchone()
                        return r["name"] if r and r["name"] else f"Entrant {eid}"
                    except:
                        return f"Entrant {eid}"
                await ch.send(embed=discord.Embed(
                    title="🆚 Stylo — Special Match",
                    description=f"Odd winners this round: **{_name(leftover_winner)}** battles a wildcard for a slot in the next round.",
                    colour=EMBED_COLOUR
                ))
            await post_round_matches(ev, cur_round, vote_end_2, con, cur)
            return
        else:
            # no opponent -> let the leftover winner pass as a bye
            all_winners_this_round.append(leftover_winner)

    # build next round
    if len(all_winners_this_round) >= 2:
        random.shuffle(all_winners_this_round)
        new_round = cur_round + 1
        vote_end = now + timedelta(seconds=vote_sec)

        for i in range(0, len(all_winners_this_round), 2):
            if i + 1 < len(all_winners_this_round):
                cur.execute(
                    "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                    (gid, new_round, all_winners_this_round[i], all_winners_this_round[i+1], vote_end.isoformat())
                )
        con.commit()

        cur.execute(
            "UPDATE event SET round_index=?, entry_end_utc=?, state='voting' WHERE guild_id=?",
            (new_round, vote_end.isoformat(), gid)
        )
        con.commit()

        if ch:
            await ch.send(embed=discord.Embed(
                title=f"🆚 Stylo — Round {new_round} begins!",
                description=f"All matches posted. Voting closes {rel_ts(vote_end)}.\nMain chat is locked; use the match threads.",
                colour=EMBED_COLOUR
            ))
            if guild:
                try:
                    await ch.set_permissions(guild.default_role, send_messages=False)
                except Exception as e:
                    print("[stylo] Failed to lock main chat:", e)

        await post_round_matches(ev, new_round, vote_end, con, cur)
        return

    # nothing to advance
    if ch:
        try:
            await ch.send(embed=discord.Embed(
                title="⛔ Stylo ended",
                description="No valid matches to advance.",
                colour=discord.Colour.red()
            ))
        except:
            pass
    cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (gid,))
    con.commit()


# ---------------- Scheduler ----------------
@tasks.loop(seconds=20)
async def scheduler():
    now = datetime.now(timezone.utc)

    # ENTRY -> VOTING
    try:
        con = db()
        cur = con.cursor()
        cur.execute("SELECT * FROM event WHERE state='entry'")
        for ev in cur.fetchall():
            entry_end = datetime.fromisoformat(ev["entry_end_utc"]).astimezone(timezone.utc)
            if now < entry_end:
                continue

            guild = bot.get_guild(ev["guild_id"])
            ch = (
                guild.get_channel(ev["main_channel_id"])
                if (guild and ev["main_channel_id"])
                else (guild.system_channel if guild else None)
            )

            # entrants with actual images
            cur.execute(
                "SELECT * FROM entrant WHERE guild_id=? AND image_url IS NOT NULL AND TRIM(image_url) <> ''",
                (ev["guild_id"],)
            )
            entrants = cur.fetchall()

            # total entrants
            cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=?", (ev["guild_id"],))
            total_entrants = cur.fetchone()["c"] or 0

            if len(entrants) < 2:
                if total_entrants >= 2:
                    # use everyone even if someone forgot to upload
                    cur.execute("SELECT * FROM entrant WHERE guild_id=?", (ev["guild_id"],))
                    entrants = cur.fetchall()
                else:
                    # really not enough -> close
                    cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
                    con.commit()
                    if ch:
                        try:
                            await ch.send(embed=discord.Embed(
                                title="⛔ Stylo cancelled",
                                description="Entries closed but there were not enough looks to start.",
                                colour=discord.Colour.red()
                            ))
                            if guild:
                                await ch.set_permissions(guild.default_role, send_messages=True)
                        except:
                            pass
                    continue

            random.shuffle(entrants)
            pairs = []
            for i in range(0, len(entrants), 2):
                if i + 1 < len(entrants):
                    pairs.append((entrants[i], entrants[i + 1]))
                # if odd -> handled later in voting end

            vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
            vote_end = now + timedelta(seconds=vote_sec)
            round_index = 1

            for L, R in pairs:
                cur.execute(
                    "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                    (ev["guild_id"], round_index, L["id"], R["id"], vote_end.isoformat())
                )
            con.commit()

            cur.execute(
                "UPDATE event SET state='voting', round_index=?, entry_end_utc=?, main_channel_id=? WHERE guild_id=?",
                (round_index, vote_end.isoformat(), ev["main_channel_id"], ev["guild_id"])
            )
            con.commit()

            # disable join button on original embed
            if ch and ev["start_msg_id"]:
                try:
                    start_msg = await ch.fetch_message(ev["start_msg_id"])
                    if start_msg and start_msg.embeds:
                        em = start_msg.embeds[0]
                        if em.fields:
                            em.set_field_at(0, name="Entries", value="**Closed**", inline=True)
                        view = build_join_view(False)
                        await start_msg.edit(embed=em, view=view)
                        try:
                            await start_msg.pin(reason="Stylo: keep Join visible always")
                        except:
                            pass
                except Exception as ex:
                    print("[stylo] failed to edit start msg on entry->voting:", ex)

            if ch and guild:
                try:
                    await ch.send(embed=discord.Embed(
                        title="🆚 Stylo — Round 1 begins!",
                        description=f"All matches posted. Voting closes {rel_ts(vote_end)}.\nMain chat is now **locked** — use each match thread.",
                        colour=EMBED_COLOUR
                    ))
                    await ch.set_permissions(guild.default_role, send_messages=False)
                except Exception as e:
                    print("[stylo] Failed to announce/lock chat:", e)

            await post_round_matches(ev, round_index, vote_end, con, cur)

            if guild:
                try:
                    await lock_tickets_for_guild(guild)
                except:
                    pass

        con.close()
    except Exception as e:
        import traceback, sys
        print(f"[stylo] ERROR entry->voting: {e!r}")
        traceback.print_exc(file=sys.stderr)

    # VOTING END -> RESULTS / NEXT ROUND / CHAMPION
    try:
        con = db()
        cur = con.cursor()
        cur.execute("SELECT * FROM event WHERE state='voting'")
        for ev in cur.fetchall():
            round_end = datetime.fromisoformat(ev["entry_end_utc"]).astimezone(timezone.utc)
            if now < round_end:
                continue

            guild = bot.get_guild(ev["guild_id"])
            ch = (
                guild.get_channel(ev["main_channel_id"])
                if (guild and ev["main_channel_id"])
                else (guild.system_channel if guild else None)
            )

            cur.execute(
                "SELECT * FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
                (ev["guild_id"], ev["round_index"])
            )
            matches = cur.fetchall()

            vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
            any_revote = False

            for m in matches:
                L = m["left_votes"]
                R = m["right_votes"]

                cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],))
                Lrow = cur.fetchone()
                cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],))
                Rrow = cur.fetchone()
                Lname = Lrow["name"] if Lrow else "Left"
                Rname = Rrow["name"] if Rrow else "Right"
                Lurl = (Lrow["image_url"] or "").strip() if Lrow else ""
                Rurl = (Rrow["image_url"] or "").strip() if Rrow else ""

                if L == R:
                    any_revote = True
                    new_end = now + timedelta(seconds=vote_sec)
                    cur.execute(
                        "UPDATE match SET left_votes=0, right_votes=0, end_utc=?, winner_id=NULL WHERE id=?",
                        (new_end.isoformat(), m["id"])
                    )
                    cur.execute("DELETE FROM voter WHERE match_id=?", (m["id"],))
                    con.commit()

                    if ch and m["msg_id"]:
                        try:
                            msg = await ch.fetch_message(m["msg_id"])
                            em = msg.embeds[0] if msg.embeds else discord.Embed(
                                title=f"Round {ev['round_index']} — {Lname} vs {Rname}",
                                description="Tap a button to vote. One vote per person.",
                                colour=EMBED_COLOUR
                            )
                            if em.fields:
                                em.set_field_at(0, name="Live totals", value="Total votes: **0**", inline=False)
                            else:
                                em.add_field(name="Live totals", value="Total votes: **0**", inline=False)

                            closes_idx = None
                            for idx, f in enumerate(em.fields):
                                if f.name.lower() == "closes":
                                    closes_idx = idx
                                    break
                            if closes_idx is not None:
                                em.set_field_at(closes_idx, name="Closes", value=rel_ts(new_end), inline=False)
                            else:
                                em.add_field(name="Closes", value=rel_ts(new_end), inline=False)

                            view = MatchView(m["id"], new_end, Lname, Rname)
                            await msg.edit(embed=em, view=view)
                        except Exception as e_edit:
                            print("[stylo] tie edit failed:", e_edit)

                    # post side-by-side for tie
                    if ch:
                        try:
                            if Lurl and Rurl:
                                card = await build_vs_card(Lurl, Rurl)
                                file = discord.File(card, filename="tie.png")
                                view2 = MatchView(m["id"], new_end, Lname, Rname)
                                await ch.send(
                                    embed=discord.Embed(
                                        title=f"🔁 Tie-break — {Lname} vs {Rname}",
                                        description=f"Tied at {L}-{R}. Re-vote open until {rel_ts(new_end)}.",
                                        colour=discord.Colour.orange()
                                    ),
                                    view=view2,
                                    file=file
                                )
                            else:
                                view2 = MatchView(m["id"], new_end, Lname, Rname)
                                await ch.send(
                                    embed=discord.Embed(
                                        title=f"🔁 Tie-break — {Lname} vs {Rname}",
                                        description=f"Tied at {L}-{R}. Re-vote open until {rel_ts(new_end)}.",
                                        colour=discord.Colour.orange()
                                    ),
                                    view=view2
                                )
                        except:
                            pass
                    continue

                # normal winner
                winner_id = m["left_id"] if L > R else m["right_id"]
                cur.execute(
                    "UPDATE match SET winner_id=?, end_utc=? WHERE id=?",
                    (winner_id, now.isoformat(), m["id"])
                )
                con.commit()

                if ch:
                    try:
                        total = max(1, L + R)
                        pL = round((L / total) * 100, 1)
                        pR = round((R / total) * 100, 1)
                        cur.execute("SELECT user_id, image_url FROM entrant WHERE id=?", (winner_id,))
                        wrow = cur.fetchone()
                        winner_mention = f"<@{wrow['user_id']}>" if wrow and wrow["user_id"] else "the winner"
                        em = discord.Embed(
                            title=f"🏁 Result — {Lname} vs {Rname}",
                            description=(
                                f"**{Lname}**: {L} ({pL}%)\n"
                                f"**{Rname}**: {R} ({pR}%)\n\n"
                                f"🏆 **Winner:** {winner_mention}"
                            ),
                            colour=discord.Colour.green()
                        )
                        file = None
                        wurl = (wrow["image_url"] or "").strip() if wrow else ""
                        if wurl:
                            data = await fetch_image_bytes(wurl)
                            if data:
                                file = discord.File(io.BytesIO(data), filename=f"winner_{m['id']}.png")
                                em.set_thumbnail(url=f"attachment://winner_{m['id']}.png")
                        if file:
                            await ch.send(embed=em, file=file)
                        else:
                            await ch.send(embed=em)
                    except Exception as ex_res:
                        print("[stylo] result send error:", ex_res)

            if any_revote:
                # extend round to latest tie and skip advance
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

            # no ties -> advance
            await cleanup_bump_panels(guild, ch)
            await advance_to_next_round(ev, now, con, cur, guild, ch)

        con.close()
    except Exception as e:
        import traceback, sys
        print(f"[stylo] ERROR voting-end: {e!r}")
        traceback.print_exc(file=sys.stderr)

@scheduler.before_loop
async def _wait_ready():
    await bot.wait_until_ready()

# ---------------- Ready ----------------
@bot.event
async def on_ready():
    bot.add_view(build_join_view(True))

    try:
        await bot.tree.sync()
        for g in bot.guilds:
            try:
                await bot.tree.sync(guild=discord.Object(id=g.id))
            except Exception as e:
                print("Guild sync error:", g.id, e)
    except Exception as e:
        print("Slash sync error:", e)

    if not scheduler.is_running():
        scheduler.start()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ---------------- Run ----------------
if __name__ == "__main__":
    bot.run(TOKEN)

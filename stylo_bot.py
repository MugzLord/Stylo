# stylo.py — clean rebuild
import os, io, math, asyncio, random, sqlite3, re
from datetime import datetime, timedelta, timezone

import aiohttp
from PIL import Image, ImageOps, ImageDraw

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ------------- Config -------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

DB_PATH = os.getenv("STYLO_DB_PATH", "stylo.db")
EMBED_COLOUR = discord.Colour.from_rgb(224, 64, 255)

MAIN_CHAT_CHANNEL_ID = int(os.getenv("STYLO_MAIN_CHAT_ID", "0"))  # optional
ROUND_CHAT_CHANNEL_ID = int(os.getenv("STYLO_CHAT_CHANNEL_ID", "0"))  # optional fixed channel
ROUND_CHAT_THREAD_NAME = "stylo-round-chat"
STYLO_CHAT_BUMP_LIMIT = 10
stylo_chat_counters: dict[int, int] = {}

# ------------- Discord client -------------
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.guilds = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)
INSTANCE = os.getenv("RAILWAY_PUBLIC_DOMAIN") or os.getenv("RAILWAY_PROJECT_ID") or "local"
print("[stylo] instance:", INSTANCE)

# ------------- DB helpers -------------
def db():
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def init_db():
    con = db(); cur = con.cursor()
    cur.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS event (
      guild_id INTEGER PRIMARY KEY,
      theme TEXT NOT NULL,
      state TEXT NOT NULL,                -- 'entry'|'voting'|'closed'
      entry_end_utc TEXT NOT NULL,        -- doubles as current round end in 'voting'
      vote_hours INTEGER NOT NULL,
      vote_seconds INTEGER,
      round_index INTEGER NOT NULL DEFAULT 0,
      main_channel_id INTEGER,
      start_msg_id INTEGER,
      round_thread_id INTEGER             -- ONE chat thread per event
    );

    CREATE TABLE IF NOT EXISTS entrant(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      caption TEXT,
      image_url TEXT,
      UNIQUE(guild_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS ticket(
      entrant_id INTEGER UNIQUE,
      channel_id INTEGER,
      FOREIGN KEY(entrant_id) REFERENCES entrant(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS match(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id INTEGER NOT NULL,
      round_index INTEGER NOT NULL,
      left_id INTEGER NOT NULL,
      right_id INTEGER NOT NULL,
      msg_id INTEGER,
      end_utc TEXT,
      left_votes INTEGER NOT NULL DEFAULT 0,
      right_votes INTEGER NOT NULL DEFAULT 0,
      winner_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS voter(
      match_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL,
      side TEXT NOT NULL,
      PRIMARY KEY(match_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS guild_settings(
      guild_id INTEGER PRIMARY KEY,
      ticket_category_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS bump_panel(
      guild_id INTEGER NOT NULL,
      match_id INTEGER NOT NULL,
      msg_id INTEGER NOT NULL,
      PRIMARY KEY (guild_id, msg_id)
    );
    """)
    con.commit(); con.close()
init_db()

# ------------- Utils -------------
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
    if not m: raise ValueError("invalid duration")
    val = float(m.group(1)); unit = m.group(2) or default_unit
    minutes = val * (60 if unit == "h" else 1)
    return max(60, min(int(round(minutes * 60)), 60 * 60 * 24 * 10))  # 1m..10d

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.manage_guild or member.guild_permissions.administrator

def get_ticket_category_id(guild_id: int) -> int | None:
    con = db(); cur = con.cursor()
    cur.execute("SELECT ticket_category_id FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone(); con.close()
    return row["ticket_category_id"] if row and row["ticket_category_id"] else None

def set_ticket_category_id(guild_id: int, category_id: int | None):
    con = db(); cur = con.cursor()
    if category_id is None:
        cur.execute("DELETE FROM guild_settings WHERE guild_id=?", (guild_id,))
    else:
        cur.execute(
            "INSERT INTO guild_settings(guild_id, ticket_category_id) VALUES(?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET ticket_category_id=excluded.ticket_category_id",
            (guild_id, category_id)
        )
    con.commit(); con.close()

# ------------- Event-wide chat -------------
async def ensure_event_chat_thread(guild: discord.Guild, ch: discord.TextChannel, ev_row: sqlite3.Row) -> int | None:
    if not (guild and ch and ev_row):
        return None

    th_id = ev_row["round_thread_id"]
    if th_id:
        th = guild.get_thread(th_id)
        # only reuse if it exists, not archived, and still under this channel
        if th and not th.archived and th.parent_id == ch.id:
            return th.id

    # create a fresh PUBLIC thread under the visible main channel
    title = f"🗣 Theme Chat — {ev_row['theme']}" if ev_row["theme"] else "🗣 Theme Chat"
    th = await ch.create_thread(
        name=title,
        type=discord.ChannelType.public_thread,
        auto_archive_duration=1440,
    )

    con = db(); cur = con.cursor()
    cur.execute("UPDATE event SET round_thread_id=? WHERE guild_id=?", (th.id, ev_row["guild_id"]))
    con.commit(); con.close()
    await th.send("Chat here about the theme. Voting posts stay clean.")
    return th.id


def chat_jump_url(guild: discord.Guild, thread_id: int | None) -> str | None:
    if not (guild and thread_id): return None
    th = guild.get_thread(thread_id)
    return th.jump_url if th else None

async def post_chat_floating_panel(guild: discord.Guild, ch: discord.TextChannel, ev_row: sqlite3.Row):
    th_id = await ensure_event_chat_thread(guild, ch, ev_row)
    url = chat_jump_url(guild, th_id)
    if not url: return
    em = discord.Embed(title="🗣 Theme Chat", description="Tap below to jump to chat.", colour=discord.Colour.dark_grey())
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=url, label="Chat here"))
    msg = await ch.send(embed=em, view=v)
    con = db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO bump_panel(guild_id, match_id, msg_id) VALUES(?,?,?)",
                (ev_row["guild_id"], 0, msg.id))
    con.commit(); con.close()

async def cleanup_bump_panels(guild: discord.Guild, ch: discord.TextChannel | None):
    con = db(); cur = con.cursor()
    cur.execute("SELECT msg_id FROM bump_panel WHERE guild_id=?", (guild.id,))
    rows = cur.fetchall()
    if ch:
        for r in rows:
            try:
                m = await ch.fetch_message(r["msg_id"])
                await m.delete()
                await asyncio.sleep(0.1)
            except:
                pass
    cur.execute("DELETE FROM bump_panel WHERE guild_id=?", (guild.id,))
    con.commit(); con.close()

async def cleanup_tickets_for_guild(guild: discord.Guild):
    """Delete all Stylo ticket channels for this guild and clear the DB rows."""
    if not guild:
        return
    con = db(); cur = con.cursor()
    cur.execute(
        "SELECT ticket.channel_id FROM ticket "
        "JOIN entrant ON entrant.id = ticket.entrant_id "
        "WHERE entrant.guild_id=?",
        (guild.id,)
    )
    rows = cur.fetchall()
    for r in rows:
        ch = guild.get_channel(r["channel_id"])
        if ch:
            try:
                await ch.delete(reason="Stylo ticket cleanup")
                await asyncio.sleep(0.2)
            except Exception:
                pass
    cur.execute(
        "DELETE FROM ticket WHERE entrant_id IN (SELECT id FROM entrant WHERE guild_id=?)",
        (guild.id,)
    )
    con.commit(); con.close()

# ------------- Join modal & persistent view -------------
async def create_or_get_entrant(guild_id: int, user: discord.Member, name: str, caption: str | None) -> int:
    con = db(); cur = con.cursor()
    cur.execute("SELECT id FROM entrant WHERE guild_id=? AND user_id=?", (guild_id, user.id))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE entrant SET name=?, caption=? WHERE id=?", (name, caption, row["id"]))
        con.commit(); con.close(); return row["id"]
    cur.execute("INSERT INTO entrant(guild_id,user_id,name,caption) VALUES(?,?,?,?)",
                (guild_id, user.id, name, caption))
    con.commit()
    cur.execute("SELECT last_insert_rowid() AS id"); eid = cur.fetchone()["id"]
    con.close(); return eid

async def create_ticket_channel(origin_inter: discord.Interaction, entrant_id: int, display_name: str) -> int | None:
    guild = origin_inter.guild
    if not guild: return None
    cat_id = get_ticket_category_id(guild.id)
    category = guild.get_channel(cat_id) if cat_id else None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        origin_inter.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
    }
    name = f"stylo-{display_name.lower().strip().replace(' ', '-')}-{entrant_id}"
    ch = await guild.create_text_channel(name=name[:95], category=category, overwrites=overwrites, reason="Stylo ticket")
    con = db(); cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO ticket(entrant_id, channel_id) VALUES(?,?)", (entrant_id, ch.id))
    con.commit(); con.close()
    # pin an instruction
    msg = await ch.send(f"📌 <@{origin_inter.user.id}> upload your **square** image here. I’ll use the latest upload.")
    try: await msg.pin()
    except: pass
    return ch.id

class EntrantModal(discord.ui.Modal, title="Join Stylo"):
    display_name = discord.ui.TextInput(label="Display name", max_length=40, required=True,
                                        placeholder="Name for the bracket")
    caption = discord.ui.TextInput(label="Caption (optional)", style=discord.TextStyle.paragraph,
                                   required=False, max_length=200)

    def __init__(self, inter: discord.Interaction):
        super().__init__()
        self._origin = inter

    async def on_submit(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("Guild missing.", ephemeral=True)
            return
        await inter.response.defer(ephemeral=True, thinking=False)
        name = str(self.display_name).strip() or inter.user.display_name
        cap  = str(self.caption).strip() if self.caption else None
        eid = await create_or_get_entrant(inter.guild_id, inter.user, name, cap)
        ch_id = await create_ticket_channel(self._origin, eid, name)
        if ch_id:
            ch = inter.guild.get_channel(ch_id)
            if ch:
                await inter.followup.send(f"Ticket created — go to {ch.mention} and upload your **square** image.", ephemeral=True)
                return
        await inter.followup.send("Couldn’t create your ticket. Check bot perms on the ticket category.", ephemeral=True)

def build_join_view(enabled: bool = True) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    btn = discord.ui.Button(style=discord.ButtonStyle.success, label="Join",
                            custom_id="stylo:join", disabled=not enabled)
    async def join_cb(i: discord.Interaction):
        if i.user.bot: return
        try:
            await i.response.send_modal(EntrantModal(i))
        except discord.errors.NotFound:
            try: await i.followup.send("That fizzled. Tap **Join** again.", ephemeral=True)
            except: pass
        except Exception:
            try: await i.response.send_message("Couldn’t open the form. Try again.", ephemeral=True)
            except: pass
    btn.callback = join_cb
    view.add_item(btn)
    return view

# ------------- Images -------------
async def fetch_image_bytes(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                if r.status == 200:
                    return await r.read()
    except Exception:
        return None
    return None

async def build_vs_card(left_url: str, right_url: str, width: int = 1200, gap: int = 24) -> io.BytesIO:
    async with aiohttp.ClientSession() as s:
        Lb = await (await s.get(left_url)).read()
        Rb = await (await s.get(right_url)).read()
    L = Image.open(io.BytesIO(Lb)).convert("RGB")
    R = Image.open(io.BytesIO(Rb)).convert("RGB")
    tile_w = (width - gap)//2
    max_h = int(tile_w * 2.0)
    Lc = ImageOps.contain(L, (tile_w, max_h), method=Image.LANCZOS)
    Rc = ImageOps.contain(R, (tile_w, max_h), method=Image.LANCZOS)
    h = max(Lc.height, Rc.height)
    def tile(img):
        t = Image.new("RGB", (tile_w, h), (20,20,30))
        t.paste(img, ((tile_w-img.width)//2, (h-img.height)//2))
        return t
    canvas = Image.new("RGB", (width, h), (20,20,30))
    canvas.paste(tile(Lc), (0,0))
    canvas.paste(tile(Rc), (tile_w+gap,0))
    ImageDraw.Draw(canvas).rectangle([tile_w,0,tile_w+gap,h], fill=(45,45,60))
    out = io.BytesIO(); canvas.save(out, format="PNG", optimize=True); out.seek(0)
    return out

async def fetch_latest_ticket_image_url(guild: discord.Guild, entrant_id: int) -> str | None:
    con = db(); cur = con.cursor()
    cur.execute("SELECT channel_id FROM ticket WHERE entrant_id=?", (entrant_id,))
    row = cur.fetchone(); con.close()
    if not row: return None
    ch = guild.get_channel(row["channel_id"])
    if not isinstance(ch, discord.TextChannel): return None
    async for msg in ch.history(limit=200, oldest_first=False):
        if msg.author.bot: continue
        for a in msg.attachments:
            ctype_ok = (a.content_type or "").startswith("image/")
            name = (a.filename or "").lower().split("?")[0]
            ext = name.rsplit(".",1)[-1] if "." in name else ""
            if ctype_ok or ext in {"png","jpg","jpeg","gif","webp","heic","heif","bmp","tif","tiff"}:
                return a.url
    return None

# ------------- Voting UI -------------
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
                cur.execute("INSERT INTO voter(match_id,user_id,side) VALUES(?,?,?)", (self.match_id, interaction.user.id, side))
            except sqlite3.IntegrityError:
                await interaction.response.send_message("You’ve already voted here.", ephemeral=True); return
            if side == "L":
                cur.execute("UPDATE match SET left_votes=left_votes+1 WHERE id=?", (self.match_id,))
            else:
                cur.execute("UPDATE match SET right_votes=right_votes+1 WHERE id=?", (self.match_id,))
            con.commit()
            cur.execute("SELECT left_votes,right_votes FROM match WHERE id=?", (self.match_id,))
            m = cur.fetchone(); L, R = m["left_votes"], m["right_votes"]; total = L+R
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

        pa = math.floor((L/total)*100) if total else 0
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

# ------------- Posting matches -------------
async def post_round_matches(ev, round_index: int, vote_end: datetime, con, cur):
    guild = bot.get_guild(ev["guild_id"])
    ch = guild.get_channel(ev["main_channel_id"]) if (guild and ev["main_channel_id"]) else (guild.system_channel if guild else None)
    if not (guild and ch): return

    th_id = await ensure_event_chat_thread(guild, ch, ev)
    url = chat_jump_url(guild, th_id)

    cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND msg_id IS NULL",
                (ev["guild_id"], round_index))
    rows = cur.fetchall()

    for m in rows:
        cur.execute("SELECT name,image_url FROM entrant WHERE id=?", (m["left_id"],)); L = cur.fetchone()
        cur.execute("SELECT name,image_url FROM entrant WHERE id=?", (m["right_id"],)); R = cur.fetchone()
        Lname = (L["name"] if L else "Left"); Rname = (R["name"] if R else "Right")
        Lurl = (L["image_url"] or "").strip() if L else ""; Rurl = (R["image_url"] or "").strip() if R else ""

        em = discord.Embed(
            title=f"Round {round_index} — {Lname} vs {Rname}",
            description="Tap a button to vote. One vote per person.",
            colour=EMBED_COLOUR
        )
        em.add_field(name="Live totals", value="Total votes: **0**", inline=False)
        em.add_field(name="Closes", value=rel_ts(vote_end), inline=False)
        view = MatchView(m["id"], vote_end, Lname, Rname, chat_url=url)

        msg = None
        try:
            if Lurl and Rurl:
                card = await build_vs_card(Lurl, Rurl)
                file = discord.File(fp=card, filename="versus.png")
                em.set_image(url="attachment://versus.png")
                msg = await ch.send(embed=em, view=view, file=file)
            elif Lurl or Rurl:
                one_url = Lurl or Rurl
                data = await fetch_image_bytes(one_url)
                if data:
                    file = discord.File(io.BytesIO(data), filename="look.png")
                    em.set_image(url="attachment://look.png")
                    msg = await ch.send(embed=em, view=view, file=file)
        except Exception:
            msg = None

        if msg is None:
            msg = await ch.send(embed=em, view=view)


        cur.execute("UPDATE match SET msg_id=? WHERE id=?", (msg.id, m["id"]))
        con.commit()
        await asyncio.sleep(0.2)

# ------------- Round advance -------------
async def _disable_all_join_buttons(ch: discord.TextChannel):
    """
    Finds any recent Stylo 'Join' panels in this channel and disables the button.
    Catches both the pinned starter and any bumped copies.
    """
    if not isinstance(ch, discord.TextChannel):
        return
    async for msg in ch.history(limit=100, oldest_first=False):
        if msg.author.bot and msg.components:
            try:
                # look for our custom_id
                has_join = any(
                    getattr(child, "custom_id", None) == "stylo:join"
                    for row in msg.components
                    for child in getattr(row, "children", [])
                )
                if has_join:
                    await msg.edit(view=build_join_view(False))
            except Exception:
                pass

async def advance_to_next_round(ev, now, con, cur, guild, ch):
    gid = ev["guild_id"]
    cur_round = ev["round_index"]
    vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600

    # winners from this round
    cur.execute(
        "SELECT winner_id FROM match WHERE guild_id=? AND round_index=?",
        (gid, cur_round)
    )
    winners = [r["winner_id"] for r in cur.fetchall() if r["winner_id"]]

    # helper: pick strongest loser from this round
    def pick_opponent():
        cur.execute(
            "SELECT left_id,right_id,left_votes,right_votes,winner_id "
            "FROM match WHERE guild_id=? AND round_index=?",
            (gid, cur_round)
        )
        rows = cur.fetchall()
        losers = []
        for m in rows:
            if not m["winner_id"]:
                continue
            if m["winner_id"] == m["left_id"]:
                losers.append(
                    (m["right_id"], m["right_votes"], m["left_votes"] + m["right_votes"])
                )
            else:
                losers.append(
                    (m["left_id"], m["left_votes"], m["left_votes"] + m["right_votes"])
                )
        if not losers:
            return None
        # losing votes desc, then total votes desc
        losers.sort(key=lambda t: (t[1], t[2]), reverse=True)
        return losers[0][0]

    # detect any entrant that has NEVER played yet (true leftover)
    cur.execute(
        "SELECT left_id,right_id FROM match WHERE guild_id=? AND round_index<=?",
        (gid, cur_round)
    )
    used_ids: set[int] = set()
    for row in cur.fetchall():
        used_ids.add(row["left_id"])
        used_ids.add(row["right_id"])

    cur.execute(
        "SELECT id FROM entrant "
        "WHERE guild_id=? AND image_url IS NOT NULL AND TRIM(image_url)<>''",
        (gid,)
    )
    all_ids = {r["id"] for r in cur.fetchall()}
    unpaired = [pid for pid in all_ids - used_ids]

    # --- CASE 1: one winner + a true leftover -> special match (3 entrants case) ---
    if len(winners) == 1 and unpaired:
        leftover = unpaired[0]
        opp = pick_opponent()
        if opp is not None:
            vote_end2 = now + timedelta(seconds=vote_sec)
            cur.execute(
                "INSERT INTO match(guild_id,round_index,left_id,right_id,end_utc) "
                "VALUES(?,?,?,?,?)",
                (gid, cur_round, leftover, opp, vote_end2.isoformat())
            )
            con.commit()
            cur.execute(
                "UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                (vote_end2.isoformat(), gid)
            )
            con.commit()
            if ch:
                await ch.send(embed=discord.Embed(
                    title="🆚 Stylo — Special Match",
                    description="Odd number of looks: leftover battles a wildcard for a place in the next round.",
                    colour=EMBED_COLOUR
                ))
            await post_round_matches(ev, cur_round, vote_end2, con, cur)
            return
        else:
            # no opponent found, treat leftover as auto-advance
            winners.append(leftover)

    # --- CASE 2: real champion (only one player left, no leftover anywhere) ---
    if len(winners) == 1 and not unpaired:
        champ_id = winners[0]
        cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (gid,))
        con.commit()

        cur.execute(
            "SELECT name,image_url,user_id FROM entrant WHERE id=?",
            (champ_id,)
        )
        w = cur.fetchone()
        winner_name = w["name"] if w else "Unknown"
        winner_mention = f"\n<@{w['user_id']}>" if w and w["user_id"] else ""

        em = discord.Embed(
            title=f"👑 Stylo Champion — {ev['theme']}",
            description=f"Winner by public vote: **{winner_name}**{winner_mention}",
            colour=discord.Colour.gold()
        )

        if ch:
            file = None
            if w and w["image_url"]:
                data = await fetch_image_bytes(w["image_url"])
                if data:
                    file = discord.File(io.BytesIO(data), filename="champion.png")
                    em.set_image(url="attachment://champion.png")
            if file:
                await ch.send(embed=em, file=file)
            else:
                await ch.send(embed=em)

        await cleanup_bump_panels(guild, ch)
        try:
            await cleanup_tickets_for_guild(guild)
        except NameError:
            # if you haven't added the helper yet, this just skips it
            pass
        return

    # --- CASE 3: odd winner count (>=3) -> leftover winner vs strongest loser ---
    if len(winners) % 2 == 1 and len(winners) >= 3:
        leftover = sorted(winners)[-1]
        winners = [w for w in winners if w != leftover]
        opp = pick_opponent()
        if opp is not None:
            vote_end2 = now + timedelta(seconds=vote_sec)
            cur.execute(
                "INSERT INTO match(guild_id,round_index,left_id,right_id,end_utc) "
                "VALUES(?,?,?,?,?)",
                (gid, cur_round, leftover, opp, vote_end2.isoformat())
            )
            con.commit()
            cur.execute(
                "UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                (vote_end2.isoformat(), gid)
            )
            con.commit()
            if ch:
                await ch.send(embed=discord.Embed(
                    title="🆚 Stylo — Special Match",
                    description="Odd winners this round: leftover battles a wildcard for a slot in the next round.",
                    colour=EMBED_COLOUR
                ))
            await post_round_matches(ev, cur_round, vote_end2, con, cur)
            return
        else:
            winners.append(leftover)  # bye

    # --- CASE 4: normal next round ---
    if len(winners) >= 2:
        random.shuffle(winners)
        nr = cur_round + 1
        vote_end = now + timedelta(seconds=vote_sec)

        for i in range(0, len(winners), 2):
            if i + 1 < len(winners):
                cur.execute(
                    "INSERT INTO match(guild_id,round_index,left_id,right_id,end_utc) "
                    "VALUES(?,?,?,?,?)",
                    (gid, nr, winners[i], winners[i + 1], vote_end.isoformat())
                )
        con.commit()
        cur.execute(
            "UPDATE event SET round_index=?, entry_end_utc=?, state='voting' WHERE guild_id=?",
            (nr, vote_end.isoformat(), gid)
        )
        con.commit()
        if ch:
            await ch.send(embed=discord.Embed(
                title=f"🆚 Stylo — Round {nr} begins!",
                description=f"All matches posted. Voting closes {rel_ts(vote_end)}.",
                colour=EMBED_COLOUR
            ))
        await post_round_matches(ev, nr, vote_end, con, cur)

# ------------- Message listener (capture uploads + bump panels) -------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    # image capture into entrant.image_url if in ticket
    if message.attachments:
        con = db(); cur = con.cursor()
        cur.execute(
            "SELECT entrant.id AS entrant_id FROM ticket "
            "JOIN entrant ON entrant.id = ticket.entrant_id WHERE ticket.channel_id=?",
            (message.channel.id,)
        )
        row = cur.fetchone(); con.close()
        if row:
            img = next((a for a in message.attachments if (a.content_type or "").startswith("image/")), None)
            if img:
                con = db(); cur = con.cursor()
                cur.execute("UPDATE entrant SET image_url=? WHERE id=?", (img.url, row["entrant_id"]))
                con.commit(); con.close()
                try: await message.add_reaction("✅")
                except: pass

    # bump join/vote panels after chat flows
    try:
        con = db(); cur = con.cursor()
        cur.execute("SELECT * FROM event WHERE guild_id=? AND state IN ('entry','voting')", (message.guild.id,))
        ev = cur.fetchone(); con.close()
        if not ev: return
        if ev["main_channel_id"] != message.channel.id: return
        cid = message.channel.id
        count = stylo_chat_counters.get(cid, 0) + 1
        stylo_chat_counters[cid] = count
        if count < STYLO_CHAT_BUMP_LIMIT: return
        stylo_chat_counters[cid] = 0
        if ev["state"] == "entry":
            # resend compact join panel
            title = f"✨ Stylo: {ev['theme']}" if ev["theme"] else "✨ Stylo"
            dt = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
            em = discord.Embed(title=title,
                               description="Entries are **OPEN** ✨\nClick **Join** to submit your look.",
                               colour=EMBED_COLOUR)
            em.add_field(name="Closes", value=rel_ts(dt), inline=False)
            await message.channel.send(embed=em, view=build_join_view(True))
        elif ev["state"] == "voting":
            await bump_voting_panels(message.guild, message.channel, ev)
    finally:
        await bot.process_commands(message)

async def bump_voting_panels(guild: discord.Guild, ch: discord.TextChannel, ev_row: sqlite3.Row):
    """Post a small 'bump' voting panel only once per open match; never duplicates the main post."""
    if not (guild and ch and ev_row):
        return

    # Single event-wide chat thread URL (if it exists)
    try:
        thread_id = await ensure_event_chat_thread(guild, ch, ev_row)
        chat_url = chat_jump_url(guild, thread_id) if thread_id else None

    except Exception as e:
        print("[stylo] bump: ensure event chat failed:", e)
        chat_url = None

    con = db(); cur = con.cursor()
    try:
        # Get open matches that are still undecided
        cur.execute("""
            SELECT id, left_id, right_id, end_utc, msg_id
            FROM match
            WHERE guild_id=? AND round_index=? AND winner_id IS NULL
        """, (ev_row["guild_id"], ev_row["round_index"]))
        open_matches = cur.fetchall()
        if not open_matches:
            return

        for m in open_matches:
            # If the main message exists, do NOT bump (avoid double post look)
            if m["msg_id"]:
                # additionally ensure we don't have a stale bump saved for this match
                cur.execute("DELETE FROM bump_panel WHERE guild_id=? AND match_id=?",
                            (ev_row["guild_id"], m["id"]))
                con.commit()
                continue

            # If we already created a bump for this match, skip
            cur.execute("SELECT 1 FROM bump_panel WHERE guild_id=? AND match_id=? LIMIT 1",
                        (ev_row["guild_id"], m["id"]))
            if cur.fetchone():
                continue

            # Names
            cur.execute("SELECT name FROM entrant WHERE id=?", (m["left_id"],))
            Lname = (cur.fetchone() or {}).get("name", "Left")
            cur.execute("SELECT name FROM entrant WHERE id=?", (m["right_id"],))
            Rname = (cur.fetchone() or {}).get("name", "Right")

            end_dt = datetime.fromisoformat(m["end_utc"]).replace(tzinfo=timezone.utc)

            em = discord.Embed(
                title=f"🗳 Voting panel — Round {ev_row['round_index']}",
                description=f"**{Lname}** vs **{Rname}**\nCloses {rel_ts(end_dt)}",
                colour=EMBED_COLOUR
            )
            view = MatchView(m["id"], end_dt, Lname, Rname, chat_url=chat_url)

            try:
                sent = await ch.send(embed=em, view=view)
                # remember we already bumped this match so we won't do it again
                cur.execute("INSERT OR IGNORE INTO bump_panel(guild_id, match_id, msg_id) VALUES(?,?,?)",
                            (ev_row["guild_id"], m["id"], sent.id))
                con.commit()
                await asyncio.sleep(0.2)
            except Exception as e:
                print("[stylo] bump panel send failed:", e)
    finally:
        con.close()


# ------------- Commands -------------
@bot.tree.command(name="stylo", description="Start a Stylo challenge (admin only).")
async def stylo_cmd(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    try:
        await inter.response.send_modal(EntrantStartModal(inter))
    except Exception as e:
        try: await inter.followup.send(f"Couldn’t open modal: {e!r}", ephemeral=True)
        except: pass

class EntrantStartModal(discord.ui.Modal, title="Start Stylo Challenge"):
    theme = discord.ui.TextInput(label="Theme / Title", max_length=100)
    entry_hours = discord.ui.TextInput(label="Entry window (hours or m)", default="24")
    vote_hours  = discord.ui.TextInput(label="Vote window per round (hours or m)", default="24")
    def __init__(self, inter: discord.Interaction):
        super().__init__(); self._inter = inter
    async def on_submit(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("Guild missing.", ephemeral=True); return
        try:
            entry_sec = parse_duration_to_seconds(str(self.entry_hours), "h")
            vote_sec  = parse_duration_to_seconds(str(self.vote_hours), "h")
        except Exception:
            await inter.response.send_message("Bad duration. Use numbers + h/m (e.g. 2h, 30m).", ephemeral=True); return
        theme = str(self.theme).strip()
        now = datetime.now(timezone.utc); entry_end = now + timedelta(seconds=entry_sec)
        con = db(); cur = con.cursor()
        # reset
        cur.execute("DELETE FROM match WHERE guild_id=?", (inter.guild_id,))
        cur.execute("DELETE FROM ticket WHERE entrant_id IN (SELECT id FROM entrant WHERE guild_id=?)", (inter.guild_id,))
        cur.execute("DELETE FROM entrant WHERE guild_id=?", (inter.guild_id,))
        con.commit()
        cur.execute(
            "REPLACE INTO event(guild_id,theme,state,entry_end_utc,vote_hours,vote_seconds,round_index,main_channel_id,start_msg_id,round_thread_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (inter.guild_id, theme, "entry", entry_end.isoformat(), int(round(vote_sec/3600)), int(vote_sec), 0, inter.channel_id, None, None)
        )
        con.commit(); con.close()

        em = discord.Embed(title=f"✨ Stylo: {theme}" if theme else "✨ Stylo",
                           description="Entries are now **open**!\nClick **Join** to submit your look. Upload a square image in your ticket.",
                           colour=EMBED_COLOUR)
        em.add_field(name="Entries", value=f"Open for **{humanize_seconds(entry_sec)}**\nCloses {rel_ts(entry_end)}", inline=True)
        em.add_field(name="Voting", value=f"Each round runs **{humanize_seconds(vote_sec)}**", inline=True)

        await inter.response.defer(ephemeral=True)
        msg = await inter.followup.send(embed=em, view=build_join_view(True), wait=True)
        try: await msg.pin()
        except: pass
        con = db(); cur = con.cursor()
        cur.execute("UPDATE event SET start_msg_id=? WHERE guild_id=?", (msg.id, inter.guild_id))
        con.commit(); con.close()
        await inter.followup.send("Stylo opened. Join is live.", ephemeral=True)

@bot.tree.command(name="stylo_set_ticket_category", description="Set the category for entry tickets (admin only).")
@app_commands.describe(category="Category to create ticket channels")
async def stylo_set_ticket_category(inter: discord.Interaction, category: discord.CategoryChannel):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    me = inter.guild.me; perms = category.permissions_for(me); missing = []
    if not perms.view_channel: missing.append("View Channel")
    if not perms.manage_channels: missing.append("Manage Channels")
    if missing:
        await inter.response.send_message("I can’t use that category — missing: **" + ", ".join(missing) + "**.", ephemeral=True); return
    set_ticket_category_id(inter.guild_id, category.id)
    await inter.response.send_message(f"✅ Ticket category set to **{category.name}**", ephemeral=True)

@bot.tree.command(name="stylo_state", description="Show current Stylo state (ephemeral).")
async def stylo_state(inter: discord.Interaction):
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE guild_id=?", (inter.guild_id,)); ev = cur.fetchone(); con.close()
    if not ev:
        await inter.response.send_message("No event row.", ephemeral=True); return
    try:
        end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
        left = int((end - datetime.now(timezone.utc)).total_seconds())
    except: end, left = None, None
    lines = [
        f"state: **{ev['state']}**",
        f"round_index: **{ev['round_index']}**",
        f"entry_end_utc: **{ev['entry_end_utc']}**" + (f" (T-{left}s)" if left is not None else ""),
        f"vote_hours: **{ev['vote_hours']}**  vote_seconds: **{ev['vote_seconds']}**",
        f"main_channel_id: **{ev['main_channel_id']}**",
        f"start_msg_id: **{ev['start_msg_id']}**",
        f"round_thread_id: **{ev['round_thread_id']}**",
        f"DB_PATH: **{DB_PATH}**",
    ]
    await inter.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="stylo_finish_round_now", description="Force-finish current round (admin).")
async def stylo_finish_round_now(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    await inter.response.defer(ephemeral=True)
    now = datetime.now(timezone.utc)
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE guild_id=? AND state='voting'", (inter.guild_id,))
    ev = cur.fetchone()
    if not ev:
        con.close(); await inter.followup.send("No round in voting state.", ephemeral=True); return
    guild = inter.guild
    ch = guild.get_channel(ev["main_channel_id"]) if (guild and ev["main_channel_id"]) else (guild.system_channel if guild else None)
    cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
                (ev["guild_id"], ev["round_index"]))
    matches = cur.fetchall()
    vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
    any_revote = False

    for m in matches:
        L, R = m["left_votes"], m["right_votes"]
        cur.execute("SELECT name,image_url FROM entrant WHERE id=?", (m["left_id"],)); Lrow = cur.fetchone()
        cur.execute("SELECT name,image_url FROM entrant WHERE id=?", (m["right_id"],)); Rrow = cur.fetchone()
        Lname = Lrow["name"] if Lrow else "Left"
        Rname = Rrow["name"] if Rrow else "Right"
        Lurl = (Lrow["image_url"] or "").strip() if Lrow else ""
        Rurl = (Rrow["image_url"] or "").strip() if Rrow else ""
        if L == R:
            any_revote = True
            new_end = now + timedelta(seconds=vote_sec)
            cur.execute("UPDATE match SET left_votes=0,right_votes=0,end_utc=?,winner_id=NULL WHERE id=?",
                        (new_end.isoformat(), m["id"]))
            cur.execute("DELETE FROM voter WHERE match_id=?", (m["id"],))
            con.commit()
            if ch:
                view = MatchView(m["id"], new_end, Lname, Rname)
                if Lurl and Rurl:
                    card = await build_vs_card(Lurl, Rurl)
                    await ch.send(embed=discord.Embed(
                        title=f"🔁 Tie-break — {Lname} vs {Rname}",
                        description=f"Re-vote open until {rel_ts(new_end)}.",
                        colour=discord.Colour.orange()
                    ), file=discord.File(card, filename="tie.png"), view=view)
                else:
                    await ch.send(embed=discord.Embed(
                        title=f"🔁 Tie-break — {Lname} vs {Rname}",
                        description=f"Re-vote open until {rel_ts(new_end)}.",
                        colour=discord.Colour.orange()
                    ), view=view)
            continue
        winner_id = m["left_id"] if L > R else m["right_id"]
        cur.execute("UPDATE match SET winner_id=?, end_utc=? WHERE id=?", (winner_id, now.isoformat(), m["id"]))
        con.commit()
    if any_revote:
        cur.execute("SELECT MAX(end_utc) AS mx FROM match WHERE guild_id=? AND round_index=?",
                    (ev["guild_id"], ev["round_index"]))
        mx = cur.fetchone()["mx"]
        if mx:
            cur.execute("UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                        (mx, ev["guild_id"]))
            con.commit()
        con.close()
        await inter.followup.send("Round extended due to tie-breaks.", ephemeral=True)
        return
    await cleanup_bump_panels(guild, ch)
    await advance_to_next_round(ev, now, con, cur, guild, ch)
    con.close()
    await inter.followup.send("Round finished.", ephemeral=True)

# ------------- Scheduler -------------
@tasks.loop(seconds=10)
async def scheduler():
    now = datetime.now(timezone.utc)

    # ENTRY -> VOTING
    con = db(); cur = con.cursor()
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

        # collect entrants (only those who actually submitted an image)
        cur.execute(
            "SELECT * FROM entrant "
            "WHERE guild_id=? AND image_url IS NOT NULL AND TRIM(image_url)<>''",
            (ev["guild_id"],)
        )
        entrants = cur.fetchall()

        # no valid images at all
        if len(entrants) == 0:
            cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
            con.commit()
            if ch:
                await ch.send(
                    embed=discord.Embed(
                        title="✋ Stylo cancelled",
                        description="Entries closed but there were no valid looks submitted.",
                        colour=discord.Colour.red()
                    )
                )
            if guild:
                await cleanup_tickets_for_guild(guild)
            continue  # go to next event

        # only one valid image → instant champion, NO PAIRS, NO TIE-BREAK
        if len(entrants) == 1:
            only = entrants[0]
            try:
                cur.execute(
                    "UPDATE event SET state='closed' WHERE guild_id=?",
                    (ev["guild_id"],)
                )
            finally:
                con.commit()

            if ch:
                em = discord.Embed(
                    title=f"👑 Stylo Champion — {ev['theme']}",
                    description=f"Only one valid look was submitted on time.\n\nChampion: <@{only['user_id']}>",
                    colour=EMBED_COLOUR
                )
                em.set_image(url=only["image_url"])
                await ch.send(embed=em)

            if guild:
                await cleanup_tickets_for_guild(guild)
            continue  # stop here, don't make any matches

        # 2 or more valid images → normal pairing flow
        random.shuffle(entrants)
        pairs = []
        for i in range(0, len(entrants), 2):
            if i + 1 < len(entrants):
                pairs.append((entrants[i], entrants[i+1]))

        vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
        vote_end = now + timedelta(seconds=vote_sec)

        # --- PRE-FLAG EVENT TO PREVENT DOUBLE EXEC ---
        cur.execute("UPDATE event SET state='pre_voting' WHERE guild_id=?", (ev["guild_id"],))
        con.commit()

        # create Round 1 matches
        for L, R in pairs:
            cur.execute(
                "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                (ev["guild_id"], 1, L["id"], R["id"], vote_end.isoformat())
            )
        con.commit()

        # now officially flip to voting
        cur.execute(
            "UPDATE event SET state='voting', round_index=?, entry_end_utc=? WHERE guild_id=?",
            (1, vote_end.isoformat(), ev["guild_id"])
        )
        con.commit()

        # ---- DISABLE JOIN BUTTONS NOW ----
        if ch:
            if ev["start_msg_id"]:
                try:
                    start_msg = await ch.fetch_message(ev["start_msg_id"])
                    if start_msg and start_msg.embeds:
                        em = start_msg.embeds[0]
                        idx_entries = None
                        for idx, f in enumerate(em.fields):
                            if f.name.lower().startswith("entries"):
                                idx_entries = idx
                                break
                        if idx_entries is not None:
                            em.set_field_at(idx_entries, name="Entries", value="**Closed**", inline=True)
                        else:
                            em.add_field(name="Entries", value="**Closed**", inline=True)
                        view = build_join_view(False)
                        await start_msg.edit(embed=em, view=view)
                except Exception as ex:
                    print("[stylo] failed to disable Join on start msg:", ex)

            try:
                async for msg in ch.history(limit=120):
                    if not msg.components:
                        continue
                    new_view = discord.ui.View()
                    edited = False
                    for row in msg.components:
                        for comp in row.children:
                            if isinstance(comp, discord.ui.Button) and comp.custom_id == "stylo:join":
                                b = discord.ui.Button(
                                    style=comp.style,
                                    label=comp.label,
                                    custom_id=comp.custom_id,
                                    disabled=True
                                )
                                new_view.add_item(b)
                                edited = True
                    if edited:
                        try:
                            await msg.edit(view=new_view)
                        except Exception:
                            pass
            except Exception as ex:
                print("[stylo] sweep disable Join failed:", ex)
        # ---- /DISABLE JOIN BUTTONS ----

        if ch and guild:
            await ch.send(embed=discord.Embed(
                title="🆚 Stylo — Round 1 begins!",
                description=f"All matches posted. Voting closes {rel_ts(vote_end)}.",
                colour=EMBED_COLOUR
            ))
            try:
                await post_chat_floating_panel(guild, ch, ev)
            except Exception as e:
                print("[stylo] chat floating panel (r1) failed:", e)

        await post_round_matches(ev, 1, vote_end, con, cur)

    con.close()

    # ------------- VOTING END -> RESULTS/NEXT -------------
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE state='voting'")
    for ev in cur.fetchall():
        gid = ev["guild_id"]
        ridx = ev["round_index"]

        cur.execute(
            "SELECT MAX(end_utc) AS mx FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
            (gid, ridx)
        )
        mx = cur.fetchone()["mx"]

        if not mx:
            guild = bot.get_guild(gid)
            ch = guild.get_channel(ev["main_channel_id"]) if (guild and ev["main_channel_id"]) else (guild.system_channel if guild else None)
            await cleanup_bump_panels(guild, ch)
            await advance_to_next_round(ev, datetime.now(timezone.utc), con, cur, guild, ch)
            continue

        round_end = datetime.fromisoformat(mx).replace(tzinfo=timezone.utc)
        if now < round_end:
            continue

        guild = bot.get_guild(gid)
        ch = guild.get_channel(ev["main_channel_id"]) if (guild and ev["main_channel_id"]) else (guild.system_channel if guild else None)

        cur.execute(
            "SELECT * FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
            (gid, ridx)
        )
        ms = cur.fetchall()
        vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600

        any_revote = False
        for m in ms:
            L, R = m["left_votes"], m["right_votes"]

            cur.execute("SELECT name,image_url FROM entrant WHERE id=?", (m["left_id"],)); Lrow = cur.fetchone()
            cur.execute("SELECT name,image_url FROM entrant WHERE id=?", (m["right_id"],)); Rrow = cur.fetchone()
            Lname = Lrow["name"] if Lrow else "Left"
            Rname = Rrow["name"] if Rrow else "Right"
            Lurl = (Lrow["image_url"] or "").strip() if Lrow else ""
            Rurl = (Rrow["image_url"] or "").strip() if Rrow else ""

            if L == R:
                any_revote = True
                new_end = now + timedelta(seconds=vote_sec)
                cur.execute(
                    "UPDATE match SET left_votes=0,right_votes=0,end_utc=?,winner_id=NULL WHERE id=?",
                    (new_end.isoformat(), m["id"])
                )
                cur.execute("DELETE FROM voter WHERE match_id=?", (m["id"],))
                con.commit()

                if ch:
                    try:
                        file = None
                        if Lurl and Rurl:
                            card = await build_vs_card(Lurl, Rurl)
                            file = discord.File(card, filename="tie.png")
                        em = discord.Embed(
                            title=f"🔁 Tie-break — {Lname} vs {Rname}",
                            description=f"Re-vote open until {rel_ts(new_end)}.",
                            colour=discord.Colour.orange()
                        )
                        await ch.send(embed=em, view=MatchView(m["id"], new_end, Lname, Rname), file=file)
                    except Exception as e:
                        print("[stylo] tie announce failed:", e)
                continue

            winner_id = m["left_id"] if L > R else m["right_id"]
            cur.execute("UPDATE match SET winner_id=?, end_utc=? WHERE id=?", (winner_id, now.isoformat(), m["id"]))
            con.commit()

            if ch:
                try:
                    total = max(1, L + R)
                    pL = round((L / total) * 100, 1)
                    pR = round((R / total) * 100, 1)
                    cur.execute("SELECT user_id,image_url FROM entrant WHERE id=?", (winner_id,))
                    wrow = cur.fetchone()
                    winner_mention = f"<@{wrow['user_id']}>" if wrow and wrow["user_id"] else "the winner"
                    em = discord.Embed(
                        title=f"🏁 Result — {Lname} vs {Rname}",
                        description=(f"**{Lname}**: {L} ({pL}%)\n"
                                     f"**{Rname}**: {R} ({pR}%)\n\n"
                                     f"🏆 **Winner:** {winner_mention}"),
                        colour=discord.Colour.green()
                    )
                    file = None
                    wurl = (wrow["image_url"] or "").strip() if wrow else ""
                    if wurl:
                        data = await fetch_image_bytes(wurl)
                        if data:
                            file = discord.File(io.BytesIO(data), filename=f"winner_{m['id']}.png")
                            em.set_thumbnail(url=f"attachment://winner_{m['id']}.png")
                    await ch.send(embed=em, file=file) if file else await ch.send(embed=em)
                except Exception as e:
                    print("[stylo] result send error:", e)

        if any_revote:
            cur.execute(
                "SELECT MAX(end_utc) AS mx FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
                (gid, ridx)
            )
            mx2 = cur.fetchone()["mx"]
            if mx2:
                cur.execute("UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?", (mx2, gid))
                con.commit()
            continue

        await cleanup_bump_panels(guild, ch)
        await advance_to_next_round(ev, now, con, cur, guild, ch)

    con.close()

# ------------- Setup & Run -------------
@bot.event
async def setup_hook():
    # persistent Join button
    bot.add_view(build_join_view(True))
    # sync commands and start scheduler here (fixes NameError on on_ready)
    try:
        await bot.tree.sync()
        for g in bot.guilds:
            try:
                await bot.tree.sync(guild=discord.Object(id=g.id))
            except Exception as e:
                print("Guild sync err:", g.id, e)
    except Exception as e:
        print("Slash sync error:", e)
    if not scheduler.is_running():
        scheduler.start()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

if __name__ == "__main__":
    bot.run(TOKEN)

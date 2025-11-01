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

# how chatty they can be before we re-post the Stylo panel
STYLO_CHAT_BUMP_LIMIT = 10
stylo_chat_counters: dict[int, int] = {}  # key = channel_id -> count


INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---------------- DB helpers ----------------
def db():
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    # optional but nice:
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
        entry_end_utc    TEXT NOT NULL,          -- ISO
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
        side      TEXT NOT NULL,  -- 'L' or 'R'
        PRIMARY KEY (match_id, user_id),
        FOREIGN KEY(match_id) REFERENCES match(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id           INTEGER PRIMARY KEY,
        ticket_category_id INTEGER
    );
    """)
    con.commit()
    con.close()


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

# ---------------- helpers ----------------
async def send_stylo_status(
    guild: discord.Guild,
    ch: discord.TextChannel,
    ev,
    entries_open: bool,
    join_enabled: bool,
):
    """Post a fresh Stylo card at the bottom — works with sqlite.Row."""
    if not ch:
        return

    # ev might be sqlite3.Row or a dict we made on the fly
    theme = ev["theme"] if ("theme" in ev.keys()) else ev.get("theme", "Stylo")
    title = f"✨ Stylo: {theme}" if theme else "✨ Stylo"

    desc_lines = []
    if entries_open:
        end_iso = ev["entry_end_utc"]
        dt = datetime.fromisoformat(end_iso).replace(tzinfo=timezone.utc)
        desc_lines.append("Entries are **OPEN** ✨")
        desc_lines.append(f"Close {rel_ts(dt)}")
    else:
        desc_lines.append("Entries are **CLOSED** ✅")

    em = discord.Embed(
        title=title,
        description="\n".join(desc_lines),
        colour=EMBED_COLOUR,
    )
    view = build_join_view(join_enabled)

    try:
        await ch.send(embed=em, view=view)
    except Exception as e:
        print("[stylo] send_stylo_status failed:", e)


async def post_round_matches(ev, round_index: int, vote_end: datetime, con, cur):
    """Post all matches for a round.
    HARD RULE: for every match with msg_id IS NULL we MUST create a message with buttons.
    Images are best-effort only.
    """
    guild = bot.get_guild(ev["guild_id"])
    ch = guild.get_channel(ev["main_channel_id"]) if (guild and ev["main_channel_id"]) else (guild.system_channel if guild else None)
    if not (guild and ch):
        return

    # get all matches for this round that are not yet posted
    cur.execute(
        "SELECT * FROM match WHERE guild_id=? AND round_index=? AND msg_id IS NULL",
        (ev["guild_id"], round_index)
    )
    matches = cur.fetchall()

    for m in matches:
        try:
            # get entrants
            cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],))
            L = cur.fetchone()
            cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],))
            R = cur.fetchone()

            # fallback names if missing
            Lname = L["name"] if L else "Left"
            Rname = R["name"] if R else "Right"

            # try to recover image urls from tickets if missing
            Lurl = (L["image_url"] or "").strip() if L else ""
            Rurl = (R["image_url"] or "").strip() if R else ""
            if not Lurl and guild:
                Lurl = await fetch_latest_ticket_image_url(guild, m["left_id"]) or ""
            if not Rurl and guild:
                Rurl = await fetch_latest_ticket_image_url(guild, m["right_id"]) or ""

            # build base embed + view
            em = discord.Embed(
                title=f"Round {round_index} — {Lname} vs {Rname}",
                description="Tap a button to vote. One vote per person.",
                colour=EMBED_COLOUR
            )
            em.add_field(name="Live totals", value="Total votes: **0**", inline=False)
            em.add_field(name="Closes", value=rel_ts(vote_end), inline=False)
            view = MatchView(m["id"], vote_end, Lname, Rname)

            msg = None

            # 1) try nice composite card
            if Lurl and Rurl:
                try:
                    card = await build_vs_card(Lurl, Rurl)
                    file = discord.File(fp=card, filename="versus.png")
                    msg = await ch.send(embed=em, view=view, file=file)
                except Exception as e:
                    print(f"[stylo] VS card failed for match {m['id']}: {e!r}")

            # 2) if composite failed, try two separate images
            if msg is None:
                Lbytes = await fetch_image_bytes(Lurl) if Lurl else None
                Rbytes = await fetch_image_bytes(Rurl) if Rurl else None

                # always send the header FIRST so we at least have buttons
                header = await ch.send(embed=em, view=view)
                msg = header

                embeds = []
                files = []

                if Lbytes:
                    fL = discord.File(io.BytesIO(Lbytes), filename="left.png")
                    files.append(fL)
                    em_left = discord.Embed(title=Lname, colour=discord.Colour.dark_grey())
                    em_left.set_image(url="attachment://left.png")
                    embeds.append(em_left)
                else:
                    embeds.append(discord.Embed(
                        title=Lname,
                        description="No image found.",
                        colour=discord.Colour.dark_grey()
                    ))

                if Rbytes:
                    fR = discord.File(io.BytesIO(Rbytes), filename="right.png")
                    files.append(fR)
                    em_right = discord.Embed(title=Rname, colour=discord.Colour.dark_grey())
                    em_right.set_image(url="attachment://right.png")
                    embeds.append(em_right)
                else:
                    embeds.append(discord.Embed(
                        title=Rname,
                        description="No image found.",
                        colour=discord.Colour.dark_grey()
                    ))

                # only send extras if we actually have any
                if embeds:
                    if files:
                        await ch.send(embeds=embeds, files=files)
                    else:
                        await ch.send(embeds=embeds)

            # 3) best-effort thread
            thread_id = None
            try:
                thread = await msg.create_thread(
                    name=f"💬 {Lname} vs {Rname} — Chat",
                    auto_archive_duration=1440
                )
                await thread.send(embed=discord.Embed(
                    title="Supporter Chat",
                    description="Talk here — voting is on the parent post.",
                    colour=discord.Colour.dark_grey()
                ))
                thread_id = thread.id
            except Exception as e:
                print(f"[stylo] create thread failed for match {m['id']}: {e!r}")

            # 4) mark this match as posted
            cur.execute("UPDATE match SET msg_id=?, thread_id=? WHERE id=?", (msg.id, thread_id, m["id"]))
            con.commit()

            # little pause so Discord isn’t rushed
            await asyncio.sleep(0.25)

        except Exception as e:
            # even if EVERYTHING failed above, force a barebones message so voting is possible
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


async def fetch_latest_ticket_image_url(guild: discord.Guild, entrant_id: int) -> str | None:
    """Scan the entrant's ticket channel for the newest image (best-effort)."""
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
        # ✅ send the modal directly on the interaction
        await i.response.send_modal(EntrantModal(i))

    btn.callback = join_cb
    view.add_item(btn)
    return view

# ---------------- Voting UI ----------------
class MatchView(discord.ui.View):
    def __init__(self, match_id: int, end_utc: datetime, left_label: str, right_label: str):
        timeout = max(1, int((end_utc - datetime.now(timezone.utc)).total_seconds()))
        super().__init__(timeout=timeout)
        self.match_id = match_id
        # these two lines work now because both buttons exist below
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
                await interaction.response.send_message("You’ve already voted for this match. 👍", ephemeral=True)
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

        # update the message
        if interaction.message and interaction.message.embeds:
            em = interaction.message.embeds[0]
            if em.fields:
                em.set_field_at(0, name="Live totals", value=f"Total votes: **{total}**", inline=False)
            else:
                em.add_field(name="Live totals", value=f"Total votes: **{total}**", inline=False)
            await interaction.response.edit_message(embed=em, view=self)
        else:
            await interaction.response.edit_message(view=self)

        # banter
        if total >= 2:
            if pa >= 80:
                banter = "That’s a rinse. 🧽"
            elif pa >= 65:
                banter = "Crowd’s leaning that way 😏"
            elif 45 <= pa <= 55:
                banter = "Neck and neck, don’t blink 👀"
            else:
                banter = "You’re backing the underdog 🐶"
        else:
            banter = "First vote always feels powerful, init? 💅"

        await interaction.followup.send(f"Vote registered. ✅\n{banter}", ephemeral=True)

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

            cur.execute("DELETE FROM match   WHERE guild_id=?", (inter.guild_id,))
            cur.execute("DELETE FROM ticket  WHERE entrant_id IN (SELECT id FROM entrant WHERE guild_id=?)", (inter.guild_id,))
            cur.execute("DELETE FROM entrant WHERE guild_id=?", (inter.guild_id,))
            con.commit()

            # now write the new event
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
                    "Hit **Join** to submit your look. Your final image (square) must be posted in your ticket before entries close."
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
                value=f"Each round runs **{humanize_seconds(vote_sec)}**\nRound 1 closes {rel_ts(entry_end + timedelta(seconds=vote_sec))}",
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
    display_name = discord.ui.TextInput(label="Display name / alias", placeholder="MikeyMoon / Mike", max_length=50)
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

            # prevent duplicate ticket channels
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

            info = discord.Embed(
                title="📸 Submit your outfit image",
                description=(
                    "Upload **one** square (1:1) image here.\n"
                    "You can re-upload to replace it—your **latest image** before entries close is used.\n"
                    "This channel will be deleted when voting starts."
                ),
                colour=EMBED_COLOUR,
            )
            await ticket.send(content=inter.user.mention, embed=info)
            await inter.response.send_message("Ticket created — please upload your image there. ✅", ephemeral=True)

        except Exception as e:
            import traceback, sys, textwrap
            traceback.print_exc(file=sys.stderr)
            try:
                await inter.response.send_message(textwrap.shorten(f"Join failed: {e!r}", width=300), ephemeral=True)
            except discord.InteractionResponded:
                await inter.followup.send(textwrap.shorten(f"Join failed: {e!r}", width=300), ephemeral=True)

# ---------------- Message listener: capture image ----------------
@bot.event
async def on_message(message: discord.Message):
    # ignore bots / DMs
    if message.author.bot or not message.guild:
        return

    # --- 1) messages with NO attachments ---
    if not message.attachments:
        # let normal commands run
        await bot.process_commands(message)
        # maybe bump the stylo join panel
        await maybe_bump_stylo_panel(message)
        return

    # --- 2) messages WITH attachments ---
    con = db()
    cur = con.cursor()
    try:
        # is this message in a stylo ticket channel?
        cur.execute(
            "SELECT entrant.id AS entrant_id FROM ticket "
            "JOIN entrant ON entrant.id = ticket.entrant_id "
            "WHERE ticket.channel_id=?",
            (message.channel.id,),
        )
        row = cur.fetchone()
        if not row:
            # not a stylo ticket -> just process commands + maybe bump
            await bot.process_commands(message)
            await maybe_bump_stylo_panel(message)
            return

        # ---------- image detection ----------
        def is_image(att: discord.Attachment) -> bool:
            if att.content_type and att.content_type.startswith("image/"):
                return True
            name = (att.filename or "").lower().split("?")[0]
            ext = name.rsplit(".", 1)[-1] if "." in name else ""
            return ext in {
                "png",
                "jpg",
                "jpeg",
                "gif",
                "webp",
                "heic",
                "heif",
                "bmp",
                "tif",
                "tiff",
            }

        img_att = next((a for a in message.attachments if is_image(a)), None)
        if not img_att:
            await bot.process_commands(message)
            await maybe_bump_stylo_panel(message)
            return

        # ---------- read user's upload ----------
        img_bytes = await img_att.read()

        # ---------- re-upload as bot ----------
        bot_file = discord.File(io.BytesIO(img_bytes), filename=img_att.filename or "entry.png")
        bot_msg = await message.channel.send(
            content=(
                f"📸 Entry updated for <@{message.author.id}>.\n"
                f"Your most recent image will be used for Stylo."
            ),
            file=bot_file,
        )
        bot_url = bot_msg.attachments[0].url if bot_msg.attachments else img_att.url

        # ---------- save NEW url ----------
        cur.execute("UPDATE entrant SET image_url=? WHERE id=?", (bot_url, row["entrant_id"]))
        con.commit()

        # react on the user's message too (feedback)
        try:
            await message.add_reaction("✅")
        except:
            pass

    finally:
        # always close DB and always let commands through
        con.close()
        await bot.process_commands(message)
        await maybe_bump_stylo_panel(message)


async def maybe_bump_stylo_panel(message: discord.Message):
    # only for guild text channels
    if not message.guild or not isinstance(message.channel, discord.TextChannel):
        return

    # don't bump for bots
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

    # only bump in the actual stylo channel
    if ev["main_channel_id"] != message.channel.id:
        return

    cid = message.channel.id
    current = stylo_chat_counters.get(cid, 0) + 1
    stylo_chat_counters[cid] = current

    if current >= STYLO_CHAT_BUMP_LIMIT:
        stylo_chat_counters[cid] = 0  # reset

        entries_open = (ev["state"] == "entry")
        join_enabled = (ev["state"] == "entry")

        guild = message.guild
        ch = message.channel
        await send_stylo_status(guild, ch, ev, entries_open, join_enabled)

# ---------------- Commands ----------------
@bot.tree.command(name="stylo", description="Start a Stylo challenge (admin only).")
async def stylo_cmd(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True)
        return

    try:
        # ORIGINAL, correct way
        await inter.response.send_modal(StyloStartModal(inter))
    except discord.errors.NotFound:
        # Discord said: Unknown interaction (10062) -> tell user to retry
        try:
            await inter.followup.send(
                "That took a bit too long and Discord dropped it. Please run `/stylo` again.",
                ephemeral=True,
            )
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

@bot.tree.command(name="stylo_debug", description="Show Stylo status (admin only).")
async def stylo_debug(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True)
        return
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE guild_id=?", (inter.guild_id,))
    ev = cur.fetchone()
    if not ev:
        con.close()
        await inter.response.send_message("No active event.", ephemeral=True)
        return
    cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=?", (inter.guild_id,))
    total_entrants = cur.fetchone()["c"] or 0
    cur.execute(
        "SELECT COUNT(*) AS c FROM entrant WHERE guild_id=? AND image_url IS NOT NULL AND TRIM(image_url) <> ''",
        (inter.guild_id,),
    )
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
        f"**Entry end (UTC):** {entry_end.isoformat()}  |  **Now:** {now.isoformat()}\n"
    )
    await inter.response.send_message(msg, ephemeral=True)

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

    # pretend the round has ended
    cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
                (ev["guild_id"], ev["round_index"]))
    matches = cur.fetchall()
    winners = []
    vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
    any_revote = False

    for m in matches:
        L = m["left_votes"]
        R = m["right_votes"]

        # 🚩 NEW: if the match was never posted, post it now instead of tie-breaking
        if m["msg_id"] is None:
            try:
                new_end = now + timedelta(seconds=vote_sec)
                await post_round_matches(ev, ev["round_index"], new_end, con, cur)
                cur.execute(
                    "UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                    (new_end.isoformat(), ev["guild_id"])
                )
                con.commit()
            except Exception as ex:
                print(f"[stylo_finish] re-post missing match {m['id']} failed: {ex!r}")
            continue

        cur.execute("SELECT name, user_id, image_url FROM entrant WHERE id=?", (m["left_id"],))
        Lrow = cur.fetchone()
        cur.execute("SELECT name, user_id, image_url FROM entrant WHERE id=?", (m["right_id"],))
        Rrow = cur.fetchone()
        LN = (Lrow["name"] if Lrow else "Left")
        RN = (Rrow["name"] if Rrow else "Right")

        # ---- tie handling (same as we wanted) ----
        if L == R:
            if L == 0 and R == 0:
                # auto pick
                chosen_id = random.choice([m["left_id"], m["right_id"]])
                cur.execute("UPDATE match SET winner_id=?, end_utc=? WHERE id=?",
                            (chosen_id, now.isoformat(), m["id"]))
                con.commit()
                winners.append((m["id"], chosen_id, LN, RN, L, R))

                # announce
                if ch:
                    try:
                        cur.execute("SELECT name, user_id, image_url FROM entrant WHERE id=?", (chosen_id,))
                        wrow = cur.fetchone()
                        wname = wrow["name"] if wrow else "Unknown"
                        em = discord.Embed(
                            title=f"🏁 Result — {LN} vs {RN}",
                            description=(
                                "No votes were cast, so winner picked automatically.\n"
                                f"🏆 **Winner:** {wname}" + (f" (<@{wrow['user_id']}>)" if wrow and wrow["user_id"] else "")
                            ),
                            colour=discord.Colour.green()
                        )
                        if wrow and (wrow["image_url"] or "").strip():
                            data = await fetch_image_bytes(wrow["image_url"])
                            if data:
                                file = discord.File(io.BytesIO(data), filename=f"winner_{m['id']}.png")
                                em.set_thumbnail(url=f"attachment://winner_{m['id']}.png")
                                await ch.send(embed=em, file=file)
                            else:
                                await ch.send(embed=em)
                        else:
                            await ch.send(embed=em)
                    except Exception as ex:
                        print("[stylo_finish] auto tie announce err:", ex)
                continue

            # real tie with votes -> re-vote
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

                    em.add_field(
                        name="Closes",
                        value=rel_ts(new_end),
                        inline=False
                    )

                    view = MatchView(m["id"], new_end, LN, RN)
                    await msg.edit(embed=em, view=view)
                except:
                    pass

            if ch:
                try:
                    await ch.send(embed=discord.Embed(
                        title=f"🔁 Tie-break — {LN} vs {RN}",
                        description=f"Tied at {L}-{R}. Re-vote is open now and closes {rel_ts(new_end)}.",
                        colour=discord.Colour.orange()
                    ))
                except:
                    pass
            # go to next match
            continue

        # ---- normal winner ----
        winner_id = m["left_id"] if L > R else m["right_id"]
        cur.execute("UPDATE match SET winner_id=?, end_utc=? WHERE id=?",
                    (winner_id, now.isoformat(), m["id"]))
        con.commit()
        winners.append((m["id"], winner_id, LN, RN, L, R))

        total = max(1, L + R)
        pL = round((L / total) * 100, 1)
        pR = round((R / total) * 100, 1)

        if ch:
            try:
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

    # ----- if any re-vote, extend round and continue -----
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
        con.close()
        await inter.followup.send("Round extended due to tie-breaks. ✅", ephemeral=True)
        return

    # 🔴 NEW: if we got here and we have NO winners at all,
    # it means there was nothing to advance -> just close it
    if not winners:
        if ch and guild:
            try:
                await ch.set_permissions(guild.default_role, send_messages=True)
            except:
                pass
        cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
        con.commit()
        con.close()
        if ch:
            try:
                await ch.send(embed=discord.Embed(
                    title="⛔ Stylo ended",
                    description="No valid matches to advance, so this round was closed.",
                    colour=discord.Colour.red()
                ))
            except:
                pass
        await inter.followup.send("Round closed — no winners to advance. ✅", ephemeral=True)
        return

    # ----- unlock main chat because we're moving on -----
    if ch and guild:
        try:
            await ch.set_permissions(guild.default_role, send_messages=True)
        except:
            pass

    # CHAMPION?
    if len(winners) == 1:
        champ_id = winners[0][1]
        cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
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

        con.close()
        await inter.followup.send("Champion announced. ✅", ephemeral=True)
        return

    # ----- build next round from winners -----
    winner_ids = [w[1] for w in winners]
    placeholders = ",".join("?" for _ in winner_ids)
    cur.execute(f"SELECT * FROM entrant WHERE id IN ({placeholders})", winner_ids)
    next_entrants = cur.fetchall()
    random.shuffle(next_entrants)

    next_pairs = []
    for i in range(0, len(next_entrants), 2):
        if i + 1 < len(next_entrants):
            next_pairs.append((next_entrants[i], next_entrants[i + 1]))

    # 🔴 if for some reason we STILL have no pairs -> just close it
    if not next_pairs:
        if ch and guild:
            try:
                await ch.set_permissions(guild.default_role, send_messages=True)
            except:
                pass
            try:
                await ch.send(embed=discord.Embed(
                    title="⛔ Stylo ended",
                    description="Not enough looks to continue to the next round.",
                    colour=discord.Colour.red()
                ))
            except:
                pass
        cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
        con.commit()
        con.close()
        await inter.followup.send("No pairs for next round — Stylo closed. ✅", ephemeral=True)
        return

    new_round = ev["round_index"] + 1
    vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
    vote_end = now + timedelta(seconds=vote_sec)

    for L, R in next_pairs:
        cur.execute(
            "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
            (ev["guild_id"], new_round, L["id"], R["id"], vote_end.isoformat())
        )
    con.commit()

    cur.execute(
        "UPDATE event SET round_index=?, entry_end_utc=?, state='voting' WHERE guild_id=?",
        (new_round, vote_end.isoformat(), ev["guild_id"])
    )
    con.commit()

    if ch:
        await ch.send(embed=discord.Embed(
            title=f"🆚 Stylo — Round {new_round} begins!",
            description=(
                f"All matches posted. Voting closes {rel_ts(vote_end)}.\n"
                "Main chat is locked; use each match thread for hype."
            ),
            colour=EMBED_COLOUR
        ))
        try:
            await ch.set_permissions(guild.default_role, send_messages=False)
        except Exception as e:
            print("[stylo] Failed to lock main chat:", e)

        await post_round_matches(ev, new_round, vote_end, con, cur)

    con.close()
    await inter.followup.send("Round finished, next round posted. ✅", ephemeral=True)

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

# ---------------- ticket helpers for scheduler ----------------
async def lock_tickets_for_guild(guild: discord.Guild):
    """Make all Stylo tickets read-only (keep them for image recovery)."""
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
                    # block everyone from chatting
                    await ch.set_permissions(guild.default_role, send_messages=False)
                    # bot can still talk
                    await ch.set_permissions(guild.me, send_messages=True, attach_files=True, read_message_history=True)
                except:
                    pass
    finally:
        con.close()

# ---------------- Scheduler ----------------
@tasks.loop(seconds=20)
async def scheduler():
    now = datetime.now(timezone.utc)

    # ========== 1) ENTRY -> VOTING ==========
    try:
        con = db()
        cur = con.cursor()
        cur.execute("SELECT * FROM event WHERE state='entry'")
        for ev in cur.fetchall():
            entry_end = datetime.fromisoformat(ev["entry_end_utc"]).astimezone(timezone.utc)
            if now < entry_end:
                continue  # still accepting entries

            guild = bot.get_guild(ev["guild_id"])
            ch = (
                guild.get_channel(ev["main_channel_id"])
                if (guild and ev["main_channel_id"])
                else (guild.system_channel if guild else None)
            )

            # 1) backfill missing images from tickets (best-effort)
            if guild:
                cur.execute("SELECT id, image_url FROM entrant WHERE guild_id=?", (ev["guild_id"],))
                for e in cur.fetchall():
                    if not (e["image_url"] or "").strip():
                        try:
                            url = await fetch_latest_ticket_image_url(guild, e["id"])
                            if url:
                                cur.execute("UPDATE entrant SET image_url=? WHERE id=?", (url, e["id"]))
                                con.commit()
                        except Exception as ex:
                            print(f"[stylo] backfill image failed for entrant {e['id']}: {ex!r}")

            # 2) entrants with actual images
            cur.execute(
                "SELECT * FROM entrant WHERE guild_id=? AND image_url IS NOT NULL AND TRIM(image_url) <> ''",
                (ev["guild_id"],)
            )
            entrants = cur.fetchall()

            # 3) total entrants (even without image)
            cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=?", (ev["guild_id"],))
            total_entrants = cur.fetchone()["c"] or 0

            # 4) decide if we cancel or proceed
            if len(entrants) < 2:
                if total_entrants >= 2:
                    # we have people but some are slow -> use everyone
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
                                description="Entries closed but I didn’t get enough looks to start.",
                                colour=discord.Colour.red()
                            ))
                            if guild:
                                await ch.set_permissions(guild.default_role, send_messages=True)
                        except:
                            pass
                    continue

            # 5) build round 1
            random.shuffle(entrants)
            pairs = []
            for i in range(0, len(entrants), 2):
                if i + 1 < len(entrants):
                    pairs.append((entrants[i], entrants[i + 1]))
                # if odd -> leftover will be handled in voting handler (round 1 odd-fix)

            vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
            vote_end = now + timedelta(seconds=vote_sec)
            round_index = 1

            for L, R in pairs:
                cur.execute(
                    "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                    (ev["guild_id"], round_index, L["id"], R["id"], vote_end.isoformat())
                )
            con.commit()

            # 6) update event to voting
            cur.execute(
                "UPDATE event SET state='voting', round_index=?, entry_end_utc=?, main_channel_id=? WHERE guild_id=?",
                (round_index, vote_end.isoformat(), ev["main_channel_id"], ev["guild_id"])
            )
            con.commit()

            # 7) keep Join embed visible but disable the button
            if ch and ev["start_msg_id"]:
                try:
                    start_msg = await ch.fetch_message(ev["start_msg_id"])
                    if start_msg and start_msg.embeds:
                        em = start_msg.embeds[0]
                        # mark entries closed
                        if em.fields:
                            em.set_field_at(0, name="Entries", value="**Closed**", inline=True)
                        view = build_join_view(False)  # disabled button
                        await start_msg.edit(embed=em, view=view)
                        # IMPORTANT: keep it pinned so users still see it
                        try:
                            await start_msg.pin(reason="Stylo: keep Join visible always")
                        except:
                            pass
                except Exception as ex:
                    print("[stylo] failed to edit start msg on entry->voting:", ex)

            # 8) announce + lock chat
            if ch and guild:
                try:
                    await ch.send(embed=discord.Embed(
                        title="🆚 Stylo — Round 1 begins!",
                        description=f"All matches posted. Voting closes {rel_ts(vote_end)}.\nMain chat is now **locked** — use each match thread for hype 💬.",
                        colour=EMBED_COLOUR
                    ))
                    await ch.set_permissions(guild.default_role, send_messages=False)
                except Exception as e:
                    print("[stylo] Failed to announce/lock chat:", e)

            # 9) post round 1 matches
            await post_round_matches(ev, round_index, vote_end, con, cur)

            # 10) lock tickets (not delete yet)
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

    # ========== 2) VOTING END -> RESULTS / NEXT ROUND / CHAMPION ==========
    try:
        con = db()
        cur = con.cursor()
        cur.execute("SELECT * FROM event WHERE state='voting'")
        for ev in cur.fetchall():
            round_end = datetime.fromisoformat(ev["entry_end_utc"]).astimezone(timezone.utc)
            if now < round_end:
                continue  # round still open

            guild = bot.get_guild(ev["guild_id"])
            ch = (
                guild.get_channel(ev["main_channel_id"])
                if (guild and ev["main_channel_id"])
                else (guild.system_channel if guild else None)
            )

            # matches in this round that don't have a winner yet
            cur.execute(
                "SELECT * FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
                (ev["guild_id"], ev["round_index"])
            )
            matches = cur.fetchall()

            vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
            any_revote = False

            # 1) resolve every match
            for m in matches:
                L = m["left_votes"]
                R = m["right_votes"]

                # message never posted -> post now and extend
                if m["msg_id"] is None:
                    try:
                        new_end = now + timedelta(seconds=vote_sec)
                        await post_round_matches(ev, ev["round_index"], new_end, con, cur)
                        cur.execute(
                            "UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                            (new_end.isoformat(), ev["guild_id"])
                        )
                        con.commit()
                    except Exception as ex:
                        print(f"[stylo] scheduler re-post missing match {m['id']} failed: {ex!r}")
                    # skip the rest for this match this tick
                    continue

                # get names + images
                cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],))
                Lrow = cur.fetchone()
                cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],))
                Rrow = cur.fetchone()
                Lname = Lrow["name"] if Lrow else "Left"
                Rname = Rrow["name"] if Rrow else "Right"
                Lurl = (Lrow["image_url"] or "").strip() if Lrow else ""
                Rurl = (Rrow["image_url"] or "").strip() if Rrow else ""

                # TIE -> re-vote
                if L == R:
                    any_revote = True
                    new_end = now + timedelta(seconds=vote_sec)
                    cur.execute(
                        "UPDATE match SET left_votes=0, right_votes=0, end_utc=?, winner_id=NULL WHERE id=?",
                        (new_end.isoformat(), m["id"])
                    )
                    cur.execute("DELETE FROM voter WHERE match_id=?", (m["id"],))
                    con.commit()

                    # refresh original message
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
                            em.add_field(name="Closes", value=rel_ts(new_end), inline=False)
                            view = MatchView(m["id"], new_end, Lname, Rname)
                            await msg.edit(embed=em, view=view)
                        except Exception as e_edit:
                            print("[stylo] tie edit failed:", e_edit)

                    # re-show images for tie so it stays visible
                    if ch:
                        embeds = []
                        files = []
                        if Lurl:
                            Lbytes = await fetch_image_bytes(Lurl)
                            if Lbytes:
                                fL = discord.File(io.BytesIO(Lbytes), filename="tie_left.png")
                                files.append(fL)
                                eL = discord.Embed(title=Lname, colour=discord.Colour.dark_grey())
                                eL.set_image(url="attachment://tie_left.png")
                                embeds.append(eL)
                        else:
                            embeds.append(discord.Embed(title=Lname, description="No image found.", colour=discord.Colour.dark_grey()))

                        if Rurl:
                            Rbytes = await fetch_image_bytes(Rurl)
                            if Rbytes:
                                fR = discord.File(io.BytesIO(Rbytes), filename="tie_right.png")
                                files.append(fR)
                                eR = discord.Embed(title=Rname, colour=discord.Colour.dark_grey())
                                eR.set_image(url="attachment://tie_right.png")
                                embeds.append(eR)
                        else:
                            embeds.append(discord.Embed(title=Rname, description="No image found.", colour=discord.Colour.dark_grey()))

                        try:
                            if files:
                                await ch.send(embeds=embeds, files=files)
                            else:
                                await ch.send(embeds=embeds)
                        except:
                            pass

                        await ch.send(embed=discord.Embed(
                            title=f"🔁 Tie-break — {Lname} vs {Rname}",
                            description=f"Tied at {L}-{R}. Re-vote open until {rel_ts(new_end)}.",
                            colour=discord.Colour.orange()
                        ))

                    continue  # next match

                # NORMAL winner
                winner_id = m["left_id"] if L > R else m["right_id"]
                cur.execute(
                    "UPDATE match SET winner_id=?, end_utc=? WHERE id=?",
                    (winner_id, now.isoformat(), m["id"])
                )
                con.commit()

                # announce result immediately
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

            # 2) if any tie -> extend round and stop
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

            # 3) collect ALL winners of this round
            cur.execute(
                "SELECT winner_id FROM match WHERE guild_id=? AND round_index=?",
                (ev["guild_id"], ev["round_index"])
            )
            all_winners_this_round = [r["winner_id"] for r in cur.fetchall() if r["winner_id"]]

            # 4) round 1 odd-fix (leftover)
            leftover_ids = []
            if ev["round_index"] == 1:
                # who fought
                cur.execute(
                    "SELECT left_id, right_id FROM match WHERE guild_id=? AND round_index=?",
                    (ev["guild_id"], ev["round_index"])
                )
                fought = cur.fetchall()
                fought_ids = set()
                for r in fought:
                    fought_ids.add(r["left_id"])
                    fought_ids.add(r["right_id"])

                # all valid entrants
                cur.execute(
                    "SELECT id FROM entrant WHERE guild_id=? AND image_url IS NOT NULL AND TRIM(image_url) <> ''",
                    (ev["guild_id"],)
                )
                all_event_ids = {r["id"] for r in cur.fetchall()}

                leftover_ids = list(all_event_ids - fought_ids - set(all_winners_this_round))

                if len(leftover_ids) == 1 and len(all_winners_this_round) >= 1:
                    leftover_id = leftover_ids[0]
                    # try to create special in SAME round
                    cur.execute(
                        "SELECT 1 FROM match WHERE guild_id=? AND round_index=? AND (left_id=? OR right_id=?) LIMIT 1",
                        (ev["guild_id"], ev["round_index"], leftover_id, leftover_id)
                    )
                    already_has_special = cur.fetchone() is not None
                    if not already_has_special:
                        # find strongest loser
                        best_loser_id = None
                        best_loser_votes = -1
                        cur.execute(
                            "SELECT id, left_id, right_id, left_votes, right_votes, winner_id "
                            "FROM match WHERE guild_id=? AND round_index=?",
                            (ev["guild_id"], ev["round_index"])
                        )
                        for mrow in cur.fetchall():
                            if not mrow["winner_id"]:
                                continue
                            if mrow["winner_id"] == mrow["left_id"]:
                                loser_id = mrow["right_id"]
                                loser_votes = mrow["right_votes"]
                            else:
                                loser_id = mrow["left_id"]
                                loser_votes = mrow["left_votes"]
                            if loser_id == leftover_id:
                                continue
                            if loser_votes > best_loser_votes:
                                best_loser_votes = loser_votes
                                best_loser_id = loser_id

                        if best_loser_id is not None:
                            vote_end_2 = now + timedelta(seconds=vote_sec)
                            cur.execute(
                                "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                                (ev["guild_id"], ev["round_index"], leftover_id, best_loser_id, vote_end_2.isoformat())
                            )
                            con.commit()
                            cur.execute(
                                "UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                                (vote_end_2.isoformat(), ev["guild_id"])
                            )
                            con.commit()
                            if ch:
                                await ch.send(embed=discord.Embed(
                                    title="🆚 Stylo — Special Match",
                                    description="Odd number of looks, so the spare look battles the **strongest non-winner**.",
                                    colour=EMBED_COLOUR
                                ))
                            await post_round_matches(ev, ev["round_index"], vote_end_2, con, cur)
                            # wait for this special match to finish
                            continue
                        else:
                            # couldn't build special -> push leftover forward
                            all_winners_this_round.append(leftover_id)
                            leftover_ids = []

            # 5) unlock chat before we continue
            if ch and guild:
                try:
                    await ch.set_permissions(guild.default_role, send_messages=True)
                except:
                    pass

            # 6) CHAMPION?
            if len(all_winners_this_round) == 1 and not leftover_ids:
                champ_id = all_winners_this_round[0]
                cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
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

                # delete tickets at the real end
                if guild:
                    try:
                        await cleanup_tickets_for_guild(guild, reason="Stylo: finished - deleting tickets")
                    except:
                        pass

                continue

            # 7) NEXT ROUND
            if len(all_winners_this_round) >= 2:
                random.shuffle(all_winners_this_round)
                new_round = ev["round_index"] + 1
                vote_end = now + timedelta(seconds=vote_sec)

                for i in range(0, len(all_winners_this_round), 2):
                    if i + 1 < len(all_winners_this_round):
                        cur.execute(
                            "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                            (ev["guild_id"], new_round, all_winners_this_round[i], all_winners_this_round[i+1], vote_end.isoformat())
                        )
                con.commit()
                cur.execute(
                    "UPDATE event SET round_index=?, entry_end_utc=?, state='voting' WHERE guild_id=?",
                    (new_round, vote_end.isoformat(), ev["guild_id"])
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

                continue

            # 8) nothing to advance -> close
            if ch:
                try:
                    await ch.send(embed=discord.Embed(
                        title="⛔ Stylo ended",
                        description="No valid matches to advance.",
                        colour=discord.Colour.red()
                    ))
                except:
                    pass
            cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
            con.commit()

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
    # make old "Join" buttons work after restart
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

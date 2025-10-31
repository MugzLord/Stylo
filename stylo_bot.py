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

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ---------------- DB helpers ----------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
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
    return max(60, min(seconds, 60*60*24*10))  # 1m..10d

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.manage_guild or member.guild_permissions.administrator

def get_ticket_category_id(guild_id: int) -> int | None:
    con = db(); cur = con.cursor()
    cur.execute("SELECT ticket_category_id FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone(); con.close()
    return (row["ticket_category_id"] if row and row["ticket_category_id"] else None)

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

# ---------------- helpers ----------------
async def post_round_matches(ev, round_index: int, vote_end: datetime, con, cur):
    """Post all matches for a round with images (composite if possible, attached files as fallback)."""
    guild = bot.get_guild(ev["guild_id"])
    ch = guild.get_channel(ev["main_channel_id"]) if (guild and ev["main_channel_id"]) else (guild.system_channel if guild else None)
    if not (guild and ch):
        return

    cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND msg_id IS NULL",
                (ev["guild_id"], round_index))
    matches = cur.fetchall()

    for m in matches:
        try:
            # entrants
            cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],)); L = cur.fetchone()
            cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],)); R = cur.fetchone()
            if not L or not R:
                continue

            Lurl = (L["image_url"] or "").strip()
            Rurl = (R["image_url"] or "").strip()

            # last-chance recover (useful in R1)
            if not Lurl and guild:
                Lurl = await fetch_latest_ticket_image_url(guild, m["left_id"]) or ""
            if not Rurl and guild:
                Rurl = await fetch_latest_ticket_image_url(guild, m["right_id"]) or ""

            # header + buttons
            em = discord.Embed(
                title=f"Round {round_index} — {L['name']} vs {R['name']}",
                description="Tap a button to vote. One vote per person.",
                colour=EMBED_COLOUR
            )
            em.add_field(name="Live totals", value="Total votes: **0**\nSplit: **0% / 0%**", inline=False)
            view = MatchView(m["id"], vote_end, L["name"], R["name"])

            msg = None

            # 1) composite card
            if Lurl and Rurl:
                try:
                    card = await build_vs_card(Lurl, Rurl)
                    file = discord.File(fp=card, filename="versus.png")
                    msg = await ch.send(embed=em, view=view, file=file)
                except Exception as e:
                    print(f"[stylo] VS card failed for match {m['id']}: {e!r}")

            # 2) fallback: attach each image so they render
            if msg is None:
                Lbytes = await fetch_image_bytes(Lurl) if Lurl else None
                Rbytes = await fetch_image_bytes(Rurl) if Rurl else None

                em_left  = discord.Embed(title=L['name'], colour=discord.Colour.dark_grey())
                em_right = discord.Embed(title=R['name'], colour=discord.Colour.dark_grey())
                files = []

                if Lbytes:
                    fL = discord.File(io.BytesIO(Lbytes), filename="left.png")
                    files.append(fL)
                    em_left.set_image(url="attachment://left.png")
                else:
                    em_left.description = "No image saved."

                if Rbytes:
                    fR = discord.File(io.BytesIO(Rbytes), filename="right.png")
                    files.append(fR)
                    em_right.set_image(url="attachment://right.png")
                else:
                    em_right.description = "No image saved."

                header = await ch.send(embed=em, view=view)
                if files:
                    await ch.send(embeds=[em_left, em_right], files=files)
                else:
                    await ch.send(embeds=[em_left, em_right])
                msg = header

            # thread (best-effort)
            thread_id = None
            try:
                thread = await msg.create_thread(
                    name=f"💬 {L['name']} vs {R['name']} — Chat", auto_archive_duration=1440
                )
                await thread.send(embed=discord.Embed(
                    title="Supporter Chat",
                    description="Talk here! Votes are via buttons on the parent post above.",
                    colour=discord.Colour.dark_grey()
                ))
                thread_id = thread.id
            except Exception as e:
                print(f"[stylo] create thread failed: {e!r}")

            # keep EXACT indent here:
            cur.execute("UPDATE match SET msg_id=?, thread_id=? WHERE id=?", (msg.id, thread_id, m["id"]))
            con.commit()
            await asyncio.sleep(0.3)

        except Exception as e:
            print(f"[stylo] posting match {m['id']} (round {round_index}) failed: {e!r}")
            continue

async def fetch_latest_ticket_image_url(guild: discord.Guild, entrant_id: int) -> str | None:
    """Scan the entrant's ticket channel for the newest image (best-effort)."""
    con = db(); cur = con.cursor()
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
            if ctype_ok or ext in {"png","jpg","jpeg","gif","webp","heic","heif","bmp","tif","tiff"}:
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
    con = db(); cur = con.cursor()
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
        tile.paste(img, ((tile_w - img.width)//2, (target_h - img.height)//2))
        return tile

    canvas = Image.new("RGB", (width, target_h), (20, 20, 30))
    canvas.paste(make_tile(Lc), (0, 0))
    canvas.paste(make_tile(Rc), (tile_w + gap, 0))
    ImageDraw.Draw(canvas).rectangle([tile_w, 0, tile_w + gap, target_h], fill=(45,45,60))

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out

# ---------------- Join button ----------------
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

# ---------------- Voting UI ----------------
class MatchView(discord.ui.View):
    def __init__(self, match_id: int, end_utc: datetime, left_label: str, right_label: str):
        timeout = max(1, int((end_utc - datetime.now(timezone.utc)).total_seconds()))
        super().__init__(timeout=timeout)
        self.match_id = match_id
        self.btn_left.label = f"Vote {left_label}"
        self.btn_right.label = f"Vote {right_label}"

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
            pb = 100 - pa if total else 0
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
                em.set_field_at(0, name="Live totals", value=f"Total votes: **{total}**\nSplit: **{pa}% / {pb}%**", inline=False)
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

# ---------------- Start modal ----------------
class StyloStartModal(discord.ui.Modal, title="Start Stylo Challenge"):
    theme = discord.ui.TextInput(label="Theme / Title", max_length=100)
    entry_hours = discord.ui.TextInput(label="Entry window (hours)", default="24")
    vote_hours  = discord.ui.TextInput(label="Vote window per round (hours)", default="24")

    def __init__(self, inter: discord.Interaction):
        super().__init__(); self._origin = inter

    async def on_submit(self, inter: discord.Interaction):
        if not is_admin(inter.user):
            await inter.response.send_message("Admins only.", ephemeral=True); return
        try:
            try:
                await inter.response.defer(ephemeral=False)
            except discord.InteractionResponded:
                pass

            entry_sec = parse_duration_to_seconds(str(self.entry_hours), default_unit="h")
            vote_sec  = parse_duration_to_seconds(str(self.vote_hours),  default_unit="h")
            theme = str(self.theme).strip()
            if not theme:
                await inter.followup.send("Theme is required.", ephemeral=True); return

            now_utc = datetime.now(timezone.utc)
            entry_end = now_utc + timedelta(seconds=entry_sec)

            con = db(); cur = con.cursor()
            cur.execute(
                "REPLACE INTO event (guild_id, theme, state, entry_end_utc, vote_hours, vote_seconds, round_index, main_channel_id, start_msg_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (inter.guild_id, theme, "entry", entry_end.isoformat(),
                 int(round(vote_sec/3600)), int(vote_sec), 0, inter.channel_id, None)
            )
            con.commit(); con.close()

            em = discord.Embed(
                title=f"✨ Stylo: {theme}",
                description=("Entries are now **open**!\n"
                             "Hit **Join** to submit your look. Your final image (square) must be posted in your ticket before entries close."),
                colour=EMBED_COLOUR
            )
            em.add_field(name="Entries", value=f"Open for **{humanize_seconds(entry_sec)}**\nCloses {rel_ts(entry_end)}", inline=True)
            em.add_field(name="Voting",  value=f"Each round runs **{humanize_seconds(vote_sec)}**\nRound 1 closes {rel_ts(entry_end + timedelta(seconds=vote_sec))}", inline=True)

            sent = await inter.followup.send(embed=em, view=build_join_view(True), wait=True)
            try:
                await sent.pin(reason="Stylo: keep Join visible during entries")
            except:
                pass

            con = db(); cur = con.cursor()
            cur.execute("UPDATE event SET start_msg_id=? WHERE guild_id=?", (sent.id, inter.guild_id))
            con.commit(); con.close()
        except Exception as e:
            import traceback, sys, textwrap
            traceback.print_exc(file=sys.stderr)
            await inter.followup.send(textwrap.shorten(f"Start failed: {e!r}", width=300), ephemeral=True)

# ---------------- Join modal ----------------
class EntrantModal(discord.ui.Modal, title="Join Stylo"):
    display_name = discord.ui.TextInput(label="Display name / alias", placeholder="MikeyMoon / Mike", max_length=50)
    caption = discord.ui.TextInput(label="Caption (optional)", style=discord.TextStyle.paragraph, required=False, max_length=200)

    def __init__(self, inter: discord.Interaction):
        super().__init__(); self._origin = inter

    async def on_submit(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("Guild context missing.", ephemeral=True); return
        try:
            con = db(); cur = con.cursor()
            cur.execute("SELECT * FROM event WHERE guild_id=?", (inter.guild_id,))
            ev = cur.fetchone()
            if not ev or ev["state"] != "entry":
                con.close(); await inter.response.send_message("Entries are not open.", ephemeral=True); return

            entry_end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= entry_end:
                con.close(); await inter.response.send_message("Entries have just closed.", ephemeral=True); return

            name = str(self.display_name).strip()
            cap  = (str(self.caption).strip() if self.caption is not None else "")
            try:
                cur.execute("INSERT INTO entrant(guild_id, user_id, name, caption) VALUES(?,?,?,?)",
                            (inter.guild_id, inter.user.id, name, cap))
            except sqlite3.IntegrityError:
                cur.execute("UPDATE entrant SET name=?, caption=? WHERE guild_id=? AND user_id=?",
                            (name, cap, inter.guild_id, inter.user.id))
            con.commit()

            cur.execute("SELECT id FROM entrant WHERE guild_id=? AND user_id=?", (inter.guild_id, inter.user.id))
            entrant_id = cur.fetchone()["id"]

            # prevent duplicate ticket channels
            cur.execute("SELECT channel_id FROM ticket WHERE entrant_id=?", (entrant_id,))
            existing = cur.fetchone()
            if existing:
                already = inter.guild.get_channel(existing["channel_id"])
                if already:
                    con.close(); await inter.response.send_message(f"You already have a ticket: {already.mention}", ephemeral=True); return
                else:
                    cur.execute("DELETE FROM ticket WHERE entrant_id=?", (entrant_id,)); con.commit()

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
                default:  discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
                inter.user:discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True),
            }
            for r in admin_roles:
                overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True, read_message_history=True)

            ticket_name = f"stylo-entry-{inter.user.name}".lower()[:90]
            try:
                ticket = await guild.create_text_channel(ticket_name, overwrites=overwrites, reason="Stylo entry ticket", category=category)
            except discord.Forbidden:
                ticket = await guild.create_text_channel(ticket_name, overwrites=overwrites, reason="Stylo entry ticket (fallback)")

            cur.execute("INSERT OR REPLACE INTO ticket(entrant_id, channel_id) VALUES(?,?)", (entrant_id, ticket.id))
            con.commit(); con.close()

            info = discord.Embed(
                title="📸 Submit your outfit image",
                description=("Upload **one** square (1:1) image here.\n"
                             "You can re-upload to replace it—your **latest image** before entries close is used.\n"
                             "This channel will be deleted when voting starts."),
                colour=EMBED_COLOUR
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
    # Only handle guild messages, ignore bots
    if message.author.bot or not message.guild:
        return

    # If no attachments, just let commands run
    if not message.attachments:
        await bot.process_commands(message)
        return

    con = db(); cur = con.cursor()
    try:
        # Is this message in a Stylo ticket channel?
        cur.execute(
            "SELECT entrant.id AS entrant_id FROM ticket "
            "JOIN entrant ON entrant.id = ticket.entrant_id "
            "WHERE ticket.channel_id=?",
            (message.channel.id,),
        )
        row = cur.fetchone()
        if not row:
            # not a Stylo ticket, process normal commands
            await bot.process_commands(message)
            return

        # ---------- image detection ----------
        def is_image(att: discord.Attachment) -> bool:
            if att.content_type and att.content_type.startswith("image/"):
                return True
            name = (att.filename or "").lower().split("?")[0]
            ext  = name.rsplit(".", 1)[-1] if "." in name else ""
            return ext in {"png","jpg","jpeg","gif","webp","heic","heif","bmp","tif","tiff"}

        img_att = next((a for a in message.attachments if is_image(a)), None)
        if not img_att:
            await bot.process_commands(message)
            return

        # ---------- read user's upload ----------
        img_bytes = await img_att.read()

        # ---------- re-upload as bot (so deletions don't break URLs) ----------
        bot_file = discord.File(io.BytesIO(img_bytes), filename=img_att.filename or "entry.png")
        bot_msg = await message.channel.send(
            content=(
                f"📸 Entry updated for <@{message.author.id}>.\n"
                f"Your most recent image will be used for Stylo."
            ),
            file=bot_file
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
        con.close()
        await bot.process_commands(message)


# ---------------- Commands ----------------
@bot.tree.command(name="stylo", description="Start a Stylo challenge (admin only).")
async def stylo_cmd(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    await inter.response.send_modal(StyloStartModal(inter))

@bot.tree.command(name="stylo_set_ticket_category", description="Set the category for entry tickets (admin only).")
@app_commands.describe(category="Pick a category where ticket channels will be created.")
async def stylo_set_ticket_category(inter: discord.Interaction, category: discord.CategoryChannel):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
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
        await inter.response.send_message("Admins only.", ephemeral=True); return
    cat_id = get_ticket_category_id(inter.guild_id)
    if not cat_id:
        await inter.response.send_message("No ticket category set.", ephemeral=True); return
    cat = inter.guild.get_channel(cat_id)
    if isinstance(cat, discord.CategoryChannel):
        await inter.response.send_message(f"Current ticket category: **{cat.name}**", ephemeral=True)
    else:
        await inter.response.send_message("Stored ticket category no longer exists.", ephemeral=True)

@bot.tree.command(name="stylo_debug", description="Show Stylo status (admin only).")
async def stylo_debug(inter: discord.Interaction):
    if not is_admin(inter.user):
        await inter.response.send_message("Admins only.", ephemeral=True); return
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE guild_id=?", (inter.guild_id,))
    ev = cur.fetchone()
    if not ev:
        con.close(); await inter.response.send_message("No active event.", ephemeral=True); return
    cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=?", (inter.guild_id,))
    total_entrants = cur.fetchone()["c"] or 0
    cur.execute("SELECT COUNT(*) AS c FROM entrant WHERE guild_id=? AND image_url IS NOT NULL AND TRIM(image_url) <> ''", (inter.guild_id,))
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
    con = db(); cur = con.cursor()
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
        L = m["left_votes"]; R = m["right_votes"]
        cur.execute("SELECT name, user_id, image_url FROM entrant WHERE id=?", (m["left_id"],)); Lrow = cur.fetchone()
        cur.execute("SELECT name, user_id, image_url FROM entrant WHERE id=?", (m["right_id"],)); Rrow = cur.fetchone()
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
                            description=f"No votes were cast, so winner picked automatically.\n🏆 **Winner:** {wname}" + (f" (<@{wrow['user_id']}>)" if wrow and wrow["user_id"] else ""),
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
                    description=f"**{LN}**: {L} ({pL}%)\n**{RN}**: {R} ({pR}%)\n\n🏆 **Winner:** {winner_mention}",
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

    # if any match was re-voted, just extend that round
    if any_revote:
        # set event end to latest match end
        cur.execute("SELECT MAX(end_utc) AS mx FROM match WHERE guild_id=? AND round_index=?",
                    (ev["guild_id"], ev["round_index"]))
        mx = cur.fetchone()["mx"]
        if mx:
            cur.execute("UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                        (mx, ev["guild_id"]))
            con.commit()
        con.close()
        await inter.followup.send("Some matches were tied — re-votes opened. Others were processed.", ephemeral=True)
        return

    # no re-votes -> proceed to next round / champion
    # unlock main chat for a moment (your main scheduler also does this)
    if ch and guild:
        try:
            await ch.set_permissions(guild.default_role, send_messages=True)
        except:
            pass

    # champion if only 1 winner
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
        await inter.followup.send("Round finished and Champion announced. ✅", ephemeral=True)
        return

    # build next round from these winners
    winner_ids = [w[1] for w in winners]
    placeholders = ",".join("?" for _ in winner_ids)
    cur.execute(f"SELECT * FROM entrant WHERE id IN ({placeholders})", winner_ids)
    next_entrants = cur.fetchall()
    random.shuffle(next_entrants)

    next_pairs = []
    for i in range(0, len(next_entrants), 2):
        if i + 1 < len(next_entrants):
            next_pairs.append((next_entrants[i], next_entrants[i + 1]))

    new_round = ev["round_index"] + 1
    vote_end = now + timedelta(seconds=vote_sec)

    for L, R in next_pairs:
        cur.execute(
            "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
            (ev["guild_id"], new_round, L["id"], R["id"], vote_end.isoformat())
        )
    con.commit()

    cur.execute("UPDATE event SET round_index=?, entry_end_utc=?, state='voting' WHERE guild_id=?",
                (new_round, vote_end.isoformat(), ev["guild_id"]))
    con.commit()

    if ch:
        await ch.send(embed=discord.Embed(
            title=f"🆚 Stylo — Round {new_round} begins!",
            description=f"All matches posted. Voting closes {rel_ts(vote_end)}.\nMain chat is locked; use each match thread for hype.",
            colour=EMBED_COLOUR
        ))
        try:
            await ch.set_permissions(guild.default_role, send_messages=False)
        except Exception as e:
            print("[stylo_finish] failed to lock main chat:", e)

        # post matches for this new round
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

    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE guild_id=? AND state='voting'", (inter.guild_id,))
    ev = cur.fetchone()
    if not ev:
        con.close()
        await inter.response.send_message("No Stylo round currently in voting state.", ephemeral=True)
        return

    new_end = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    cur.execute("UPDATE event SET entry_end_utc=? WHERE guild_id=?", (new_end.isoformat(), inter.guild_id))
    con.commit(); con.close()

    await inter.response.send_message(f"⏱ Current round will now end in **{minutes} minutes**.", ephemeral=True)


# ---------------- Scheduler ----------------
@tasks.loop(seconds=20)
async def scheduler():
    now = datetime.now(timezone.utc)

    # ========== 1) ENTRY -> VOTING ==========
    try:
        con = db(); cur = con.cursor()
        cur.execute("SELECT * FROM event WHERE state='entry'")
        for ev in cur.fetchall():
            entry_end = datetime.fromisoformat(ev["entry_end_utc"]).astimezone(timezone.utc)
            if now < entry_end:
                continue

            guild = bot.get_guild(ev["guild_id"])

            # backfill missing images from tickets
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

            # only entrants with image
            cur.execute(
                "SELECT * FROM entrant WHERE guild_id=? AND image_url IS NOT NULL AND TRIM(image_url) <> ''",
                (ev["guild_id"],)
            )
            entrants = cur.fetchall()

            ch = (guild.get_channel(ev["main_channel_id"]) if (guild and ev["main_channel_id"])
                  else (guild.system_channel if guild else None))

            if len(entrants) < 2:
                # not enough -> close
                cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
                con.commit()
                if guild:
                    try:
                        await cleanup_tickets_for_guild(guild, reason="Stylo: entries ended (<2 images)")
                    except:
                        pass
                if ch:
                    try:
                        await ch.send(embed=discord.Embed(
                            title="⛔ Stylo cancelled",
                            description="Entries closed with fewer than two valid images.",
                            colour=discord.Colour.red()
                        ))
                    except:
                        pass
                continue

            # build round 1
            random.shuffle(entrants)
            pairs = []
            for i in range(0, len(entrants), 2):
                if i + 1 < len(entrants):
                    pairs.append((entrants[i], entrants[i + 1]))

            round_index = 1
            vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
            vote_end = now + timedelta(seconds=vote_sec)

            for L, R in pairs:
                cur.execute(
                    "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                    (ev["guild_id"], round_index, L["id"], R["id"], vote_end.isoformat())
                )
            con.commit()

            # move event to voting
            cur.execute(
                "UPDATE event SET state='voting', round_index=?, entry_end_utc=?, main_channel_id=? WHERE guild_id=?",
                (round_index, vote_end.isoformat(), ev["main_channel_id"], ev["guild_id"])
            )
            con.commit()

            # disable join + unpin
            if ch and ev["start_msg_id"]:
                try:
                    start_msg = await ch.fetch_message(ev["start_msg_id"])
                    if start_msg and start_msg.embeds:
                        je = start_msg.embeds[0]
                        if je.fields:
                            je.set_field_at(0, name="Entries", value="**Closed**", inline=True)
                        await start_msg.edit(embed=je, view=build_join_view(False))
                    try:
                        await start_msg.unpin(reason="Stylo: entries closed")
                    except:
                        pass
                except:
                    pass

            # announce + lock
            if ch:
                try:
                    await ch.send(embed=discord.Embed(
                        title=f"🆚 Stylo — Round {round_index} begins!",
                        description=f"All matches posted. Voting closes {rel_ts(vote_end)}.\n"
                                    "Main chat is now **locked** — use each match thread for hype 💬.",
                        colour=EMBED_COLOUR
                    ))
                    # lock
                    await ch.set_permissions(guild.default_role, send_messages=False)
                except Exception as e:
                    print(f"[stylo] Failed to announce/lock chat: {e!r}")

            # post round 1 matches
            await post_round_matches(ev, round_index, vote_end, con, cur)

            # delete tickets after posting
            if guild:
                try:
                    await cleanup_tickets_for_guild(guild, reason="Stylo: entries closed - deleting tickets")
                except:
                    pass

        con.close()
    except Exception as e:
        import traceback, sys
        print(f"[stylo] ERROR entry->voting: {e!r}")
        traceback.print_exc(file=sys.stderr)

    # ========== 2) VOTING END -> RESULTS / NEXT ROUND / CHAMPION ==========
    try:
        con = db(); cur = con.cursor()
        cur.execute("SELECT * FROM event WHERE state='voting'")
        for ev in cur.fetchall():
            round_end = datetime.fromisoformat(ev["entry_end_utc"]).astimezone(timezone.utc)
            if now < round_end:
                continue

            guild = bot.get_guild(ev["guild_id"])
            ch = (guild.get_channel(ev["main_channel_id"]) if (guild and ev["main_channel_id"])
                  else (guild.system_channel if guild else None))

            cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
                        (ev["guild_id"], ev["round_index"]))
            matches = cur.fetchall()

            winners = []
            vote_sec = ev["vote_seconds"] if ev["vote_seconds"] else int(ev["vote_hours"]) * 3600
            any_revote = False

            # ----- resolve matches -----
            for m in matches:
                L = m["left_votes"]; R = m["right_votes"]
                cur.execute("SELECT name FROM entrant WHERE id=?", (m["left_id"],)); LNrow = cur.fetchone()
                cur.execute("SELECT name FROM entrant WHERE id=?", (m["right_id"],)); RNrow = cur.fetchone()
                LN = (LNrow["name"] if LNrow else "Left"); RN = (RNrow["name"] if RNrow else "Right")

                if L == R:
                    # tie -> re-vote
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
                    continue

                # normal winner
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
                            description=f"**{LN}**: {L} ({pL}%)\n**{RN}**: {R} ({pR}%)\n\n🏆 **Winner:** {winner_mention}",
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
                        print("[stylo] result send error:", ex)

            # if any re-vote, extend round and skip
            if any_revote:
                cur.execute("SELECT MAX(end_utc) AS mx FROM match WHERE guild_id=? AND round_index=?",
                            (ev["guild_id"], ev["round_index"]))
                mx = cur.fetchone()["mx"]
                if mx:
                    cur.execute("UPDATE event SET entry_end_utc=?, state='voting' WHERE guild_id=?",
                                (mx, ev["guild_id"]))
                    con.commit()
                continue

            # ----- ODD-FIX: 1 leftover -> special match -----
            cur.execute(
                "SELECT id FROM entrant WHERE guild_id=? AND image_url IS NOT NULL AND TRIM(image_url) <> ''",
                (ev["guild_id"],)
            )
            all_valid_ids = {r["id"] for r in cur.fetchall()}

            cur.execute(
                "SELECT left_id, right_id FROM match WHERE guild_id=? AND round_index=?",
                (ev["guild_id"], ev["round_index"])
            )
            fought_ids = set()
            for r in cur.fetchall():
                fought_ids.add(r["left_id"]); fought_ids.add(r["right_id"])

            leftover_ids = list(all_valid_ids - fought_ids)
            created_special = False

            if len(leftover_ids) == 1 and len(winners) >= 1:
                leftover_id = leftover_ids[0]

                best_loser_id = None
                best_loser_votes = -1

                for (match_id, winner_id, LN, RN, L, R) in winners:
                    cur.execute("SELECT left_id, right_id FROM match WHERE id=?", (match_id,))
                    mrow = cur.fetchone()
                    if not mrow:
                        continue

                    if winner_id == mrow["left_id"]:
                        loser_id = mrow["right_id"]
                        loser_votes = R
                    else:
                        loser_id = mrow["left_id"]
                        loser_votes = L

                    if loser_id == leftover_id:
                        continue

                    if loser_votes > best_loser_votes:
                        best_loser_votes = loser_votes
                        best_loser_id = loser_id

                if best_loser_id is not None:
                    special_round = ev["round_index"] + 1
                    vote_end = now + timedelta(seconds=vote_sec)

                    cur.execute(
                        "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                        (ev["guild_id"], special_round, leftover_id, best_loser_id, vote_end.isoformat())
                    )
                    con.commit()

                    cur.execute(
                        "UPDATE event SET round_index=?, entry_end_utc=?, state='voting' WHERE guild_id=?",
                        (special_round, vote_end.isoformat(), ev["guild_id"])
                    )
                    con.commit()

                    if ch:
                        await ch.send(embed=discord.Embed(
                            title="🆚 Stylo — Special Match",
                            description="Odd number of looks, so the unpaired look is battling the strongest non-winner. Winner advances.",
                            colour=EMBED_COLOUR
                        ))

                    await post_round_matches(ev, special_round, vote_end, con, cur)
                    created_special = True

            if created_special:
                continue

            # unlock main chat before next round / closing
            if ch and guild:
                try:
                    await ch.set_permissions(guild.default_role, send_messages=True)
                except:
                    pass

            # ----- CHAMPION? -----
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
                continue

            # ----- NORMAL NEXT ROUND -----
            winner_ids = [w[1] for w in winners]
            random.shuffle(winner_ids)

            new_round = ev["round_index"] + 1
            vote_end = now + timedelta(seconds=vote_sec)

            for i in range(0, len(winner_ids), 2):
                if i + 1 < len(winner_ids):
                    cur.execute(
                        "INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                        (ev["guild_id"], new_round, winner_ids[i], winner_ids[i + 1], vote_end.isoformat())
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
                    description=f"All matches posted. Voting closes {rel_ts(vote_end)}.\n"
                                "Main chat is locked; use each match thread for hype.",
                    colour=EMBED_COLOUR
                ))
                try:
                    await ch.set_permissions(guild.default_role, send_messages=False)
                except Exception as e:
                    print("[stylo] Failed to lock main chat:", e)

                await post_round_matches(ev, new_round, vote_end, con, cur)

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

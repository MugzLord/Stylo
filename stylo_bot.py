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
INTENTS.message_content = False  # not needed
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

init_db()

# ---------- Permissions helper ----------
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

# ---------- Pillow VS card ----------
async def build_vs_card(left_url: str, right_url: str, width: int = 1200, gap: int = 24) -> io.BytesIO:
    async with aiohttp.ClientSession() as sess:
        async with sess.get(left_url) as r1: Lb = await r1.read()
        async with sess.get(right_url) as r2: Rb = await r2.read()

    L = Image.open(io.BytesIO(Lb)).convert("RGB")
    R = Image.open(io.BytesIO(Rb)).convert("RGB")
    target_h = int(width * 3 / 2)  # 2:3 portrait canvas
    tile_w = (width - gap) // 2

    def fit(img):
        return ImageOps.fit(img, (tile_w, target_h), method=Image.LANCZOS, centering=(0.5, 0.5))
    Lf, Rf = fit(L), fit(R)

    canvas = Image.new("RGB", (width, target_h), (20, 20, 30))
    canvas.paste(Lf, (0, 0))
    canvas.paste(Rf, (tile_w + gap, 0))

    draw = ImageDraw.Draw(canvas)
    x0 = tile_w
    draw.rectangle([x0, 0, x0 + gap, target_h], fill=(45, 45, 60))

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out

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
            await inter.response.send_message("Admins only.", ephemeral=True); return

        try:
            eh = max(1, min(240, int(str(self.entry_hours))))
            vh = max(1, min(240, int(str(self.vote_hours))))
        except ValueError:
            await inter.response.send_message("Use whole numbers for hours.", ephemeral=True); return

        theme = str(self.theme).strip()
        if not theme:
            await inter.response.send_message("Theme is required.", ephemeral=True); return

        entry_end = datetime.now(timezone.utc) + timedelta(hours=eh)

        con = db(); cur = con.cursor()
        cur.execute("REPLACE INTO event(guild_id, theme, state, entry_end_utc, vote_hours, round_index, main_channel_id) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (inter.guild_id, theme, "entry", entry_end.isoformat(), vh, 0, inter.channel_id))
        con.commit(); con.close()

        # Post Join embed (channel stays open during entry)
        join_em = discord.Embed(
            title=f"👗 Stylo — {theme}",
            description=(
                "Click **Join** to enter the challenge!\n"
                "A private ticket will open where you’ll upload **one outfit image**."
            ),
            colour=EMBED_COLOUR
        )
        join_em.add_field(name="Entries", value=f"Open for **{eh}h**", inline=True)
        join_em.add_field(name="Voting", value=f"Each round runs **{vh}h**", inline=True)
        join_em.set_footer(text="Channel remains open during entries. Voting phase locks main chat.")

        view = discord.ui.View(timeout=None)
        @discord.ui.button(style=discord.ButtonStyle.success, label="Join", custom_id="stylo:join")
        async def _join_button(btn_inter: discord.Interaction, _btn: discord.ui.Button):
            # Entrant modal
            if btn_inter.user.bot:
                return
            await btn_inter.response.send_modal(EntrantModal(btn_inter))

        # attach dynamic button to the view
        view.add_item(_join_button)  # type: ignore

        await inter.response.send_message(embed=join_em, view=view)

# ---------- Modal: Entrant info (name, caption) ----------
class EntrantModal(discord.ui.Modal, title="Join Stylo"):
    display_name = discord.ui.TextInput(label="Display name / alias", placeholder="JasmineMoon", max_length=50)
    caption = discord.ui.TextInput(label="Caption (optional)", style=discord.TextStyle.paragraph, required=False, max_length=200)

    def __init__(self, inter: discord.Interaction):
        super().__init__()
        self._origin = inter

    async def on_submit(self, inter: discord.Interaction):
        con = db(); cur = con.cursor()
        # Ensure event is in entry phase
        cur.execute("SELECT * FROM event WHERE guild_id=?", (inter.guild_id,))
        ev = cur.fetchone()
        if not ev or ev["state"] != "entry":
            await inter.response.send_message("Entries are not open.", ephemeral=True)
            con.close(); return

        # Upsert entrant
        name = str(self.display_name).strip()
        cap = str(self.caption).strip()
        try:
            cur.execute("INSERT INTO entrant(guild_id, user_id, name, caption) VALUES(?,?,?,?)",
                        (inter.guild_id, inter.user.id, name, cap))
        except sqlite3.IntegrityError:
            # already exists; update name/caption
            cur.execute("UPDATE entrant SET name=?, caption=? WHERE guild_id=? AND user_id=?",
                        (name, cap, inter.guild_id, inter.user.id))
        con.commit()

        # (Re)fetch entrant id
        cur.execute("SELECT id FROM entrant WHERE guild_id=? AND user_id=?", (inter.guild_id, inter.user.id))
        entrant_id = cur.fetchone()["id"]

        # Create private ticket channel (entrant + admins)
        guild = inter.guild
        default = guild.default_role
        admin_roles = [r for r in guild.roles if r.permissions.administrator]
        overwrites = {
            default: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            inter.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
        }
        for r in admin_roles:
            overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True)

        ticket_name = f"stylo-entry-{inter.user.name}".lower()[:90]
        category_id = get_ticket_category_id(guild.id)
        category = guild.get_channel(category_id) if category_id else None
        ticket = await guild.create_text_channel(
            ticket_name,
            overwrites=overwrites,
            reason="Stylo entry ticket",
            category=category
        )

        cur.execute("INSERT OR REPLACE INTO ticket(entrant_id, channel_id) VALUES(?,?)", (entrant_id, ticket.id))
        con.commit(); con.close()

        info = discord.Embed(
            title="📸 Submit your outfit image",
            description=(
                "Please upload **one** image for your entry in this channel.\n"
                "You may re-upload to replace it — the **last** image before entries close is used.\n"
                "Spam or unrelated messages may be removed."
            ),
            colour=EMBED_COLOUR
        )
        await ticket.send(content=inter.user.mention, embed=info)
        await inter.response.send_message("Ticket created — please upload your image there. ✅", ephemeral=True)

# ---------- Message listener: capture image in ticket ----------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    # Is this a ticket channel?
    con = db(); cur = con.cursor()
    cur.execute("SELECT entrant.id AS entrant_id, entrant.user_id FROM ticket "
                "JOIN entrant ON entrant.id = ticket.entrant_id "
                "WHERE ticket.channel_id=?", (message.channel.id,))
    row = cur.fetchone()
    if not row:
        con.close(); return

    # Only accept the entrant's images
    if message.author.id != row["user_id"]:
        con.close(); return
    if not message.attachments:
        con.close(); return

    # Take last image-like attachment
    img = None
    for a in message.attachments:
        if a.content_type and a.content_type.startswith("image/"):
            img = a.url
    if img:
        cur.execute("UPDATE entrant SET image_url=? WHERE id=?", (img, row["entrant_id"]))
        con.commit()
        con.close()
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
    else:
        con.close()

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
            if len(entrants) < 2:
                # Not enough entrants; close
                cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
                con.commit(); continue

            random.shuffle(entrants)
            # Pair up
            pairs = [(entrants[i], entrants[i+1]) for i in range(0, len(entrants) - len(entrants)%2, 2)]
            # If odd, last one gets a bye
            byes = []
            if len(entrants) % 2 == 1:
                byes.append(entrants[-1])

            round_index = 1
            vote_end = now + timedelta(hours=ev["vote_hours"])
            # Save matches
            for L, R in pairs:
                cur.execute("INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                            (ev["guild_id"], round_index, L["id"], R["id"], vote_end.isoformat()))
            con.commit()

            # Update event
            cur.execute("UPDATE event SET state='voting', round_index=?, entry_end_utc=?, main_channel_id=? WHERE guild_id=?",
                        (round_index, vote_end.isoformat(), ev["main_channel_id"], ev["guild_id"]))
            con.commit()

            # Announce and post all pairs
            guild = bot.get_guild(ev["guild_id"])
            ch = guild.get_channel(ev["main_channel_id"]) if guild else None
            if ch:
                await ch.send(embed=discord.Embed(
                    title=f"🆚 Stylo — Round {round_index} begins!",
                    description=f"All matches posted. Voting closes in **{ev['vote_hours']}h**.\nMain chat is locked; use each match thread for hype.",
                    colour=EMBED_COLOUR
                ))
                # Lock main channel chat
                default = guild.default_role
                try:
                    await ch.set_permissions(default, send_messages=False)
                except Exception:
                    pass

                # Fetch matches for posting
                cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND msg_id IS NULL",
                            (ev["guild_id"], round_index))
                matches = cur.fetchall()
                # Post each match
                for m in matches:
                    # Fetch entrants
                    cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],)); L = cur.fetchone()
                    cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],)); R = cur.fetchone()
                    card = await build_vs_card(L["image_url"], R["image_url"])
                    file = discord.File(fp=card, filename="versus.png")

                    em = discord.Embed(
                        title=f"Round {round_index} — {L['name']} vs {R['name']}",
                        description="Tap a button to vote. One vote per person.",
                        colour=EMBED_COLOUR
                    )
                    em.add_field(name="Live totals", value="Total votes: **0**\nSplit: **0% / 0%**", inline=False)
                    em.set_image(url="attachment://versus.png")

                    end_dt = datetime.fromisoformat(vote_end.isoformat()).replace(tzinfo=timezone.utc)
                    view = MatchView(m["id"], end_dt, L["name"], R["name"])

                    msg = await ch.send(embed=em, view=view, file=file)
                    # Create thread for chat
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
                    await asyncio.sleep(0.4)  # rate-limit friendly
            # done entry->voting
    con.close()

    # Handle voting end -> compute winners; maybe next round
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM event WHERE state='voting'")
    for ev in cur.fetchall():
        round_end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)  # stored vote end
        if now < round_end:
            continue

        guild = bot.get_guild(ev["guild_id"])
        ch = guild.get_channel(ev["main_channel_id"]) if guild else None

        # Close all matches of this round
        cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL",
                    (ev["guild_id"], ev["round_index"]))
        matches = cur.fetchall()
        winners = []
        for m in matches:
            # Disable buttons, lock thread
            if ch and m["msg_id"]:
                try:
                    msg = await ch.fetch_message(m["msg_id"])
                    # Disable view (create disabled view)
                    if msg.components:
                        # Quick way: edit with a disabled view from MatchView with past end time
                        view = MatchView(m["id"], now - timedelta(seconds=1), "Left", "Right")
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

            # Compute winner
            L = m["left_votes"]; R = m["right_votes"]
            winner_id = m["left_id"] if L > R else m["right_id"] if R > L else random.choice([m["left_id"], m["right_id"]])
            cur.execute("UPDATE match SET winner_id=? WHERE id=?", (winner_id, m["id"]))
            con.commit()
            winners.append(winner_id)

            # Post result
            cur.execute("SELECT name FROM entrant WHERE id=?", (m["left_id"],)); LN = cur.fetchone()["name"]
            cur.execute("SELECT name FROM entrant WHERE id=?", (m["right_id"],)); RN = cur.fetchone()["name"]
            total = L + R
            pL = round((L / total) * 100, 1) if total else 0.0
            pR = round((R / total) * 100, 1) if total else 0.0
            if ch:
                await ch.send(embed=discord.Embed(
                    title=f"🏁 Result — {LN} vs {RN}",
                    description=f"**{LN}**: {L} ({pL}%)\n**{RN}**: {R} ({pR}%)\n\n**Winner:** "
                                f"**{LN if winner_id==m['left_id'] else RN}**",
                    colour=discord.Colour.green()
                ))

        # Unlock main channel after round
        if ch:
            default = guild.default_role
            try:
                await ch.set_permissions(default, send_messages=True)
            except Exception:
                pass

        # If only one winner -> champion
        if len(winners) == 1:
            cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
            con.commit()
            # Announce champion
            cur.execute("SELECT name FROM entrant WHERE id=?", (winners[0],)); champ = cur.fetchone()["name"]
            if ch:
                await ch.send(embed=discord.Embed(
                    title=f"👑 Stylo Champion — {ev['theme']}",
                    description=f"Winner by public vote: **{champ}**",
                    colour=discord.Colour.gold()
                ))
            continue

        # Otherwise set up next round
        # Build next entrants from winners (must have images already)
        placeholders = ",".join("?" for _ in winners)
        cur.execute(f"SELECT * FROM entrant WHERE id IN ({placeholders})", winners)
        next_entrants = cur.fetchall()
        random.shuffle(next_entrants)
        pairs = [(next_entrants[i], next_entrants[i+1]) for i in range(0, len(next_entrants)-len(next_entrants)%2, 2)]
        byes = []
        if len(next_entrants) % 2 == 1:
            byes.append(next_entrants[-1])

        new_round = ev["round_index"] + 1
        vote_end = now + timedelta(hours=ev["vote_hours"])
        for L, R in pairs:
            cur.execute("INSERT INTO match(guild_id, round_index, left_id, right_id, end_utc) VALUES(?,?,?,?,?)",
                        (ev["guild_id"], new_round, L["id"], R["id"], vote_end.isoformat()))
        con.commit()

        # Move event cursor to voting next round and reuse main channel; re-lock chat
        cur.execute("UPDATE event SET round_index=?, entry_end_utc=?, state='voting' WHERE guild_id=?",
                    (new_round, vote_end.isoformat(), ev["guild_id"]))
        con.commit()

        if ch:
            await ch.send(embed=discord.Embed(
                title=f"🆚 Stylo — Round {new_round} begins!",
                description=f"All matches posted. Voting closes in **{ev['vote_hours']}h**.\nMain chat is locked; use each match thread for hype.",
                colour=EMBED_COLOUR
            ))
            default = guild.default_role
            try:
                await ch.set_permissions(default, send_messages=False)
            except Exception:
                pass

            cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND msg_id IS NULL",
                        (ev["guild_id"], new_round))
            matches = cur.fetchall()
            for m in matches:
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

                end_dt = datetime.fromisoformat(vote_end.isoformat()).replace(tzinfo=timezone.utc)
                view = MatchView(m["id"], end_dt, L["name"], R["name"])
                msg = await ch.send(embed=em, view=view, file=file)

                # Thread
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
    con.close()

@scheduler.before_loop
async def _wait_ready():
    await bot.wait_until_ready()

import os

LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

@bot.event
async def on_ready():
    ...
    if LOG_CHANNEL_ID:
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if channel:
            await channel.send("✨ Stylo updated to the latest version and is back online!")
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

if __name__ == "__main__":
    bot.run(TOKEN)

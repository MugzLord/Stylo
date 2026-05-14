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

# 🚨 FIX: Ensure the directory exists before SQLite tries to open the file
db_dir = os.path.dirname(os.path.abspath(DB_PATH))
if db_dir and not os.path.exists(db_dir):
    try:
        os.makedirs(db_dir, exist_ok=True)
        print(f"Directory created: {db_dir}")
    except Exception as e:
        print(f"Error creating directory {db_dir}: {e}")

EMBED_COLOUR = discord.Colour.from_rgb(224, 64, 255)

# ------------- Discord client -------------
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.guilds = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ------------- DB helpers -------------
def db():
    con = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def init_db():
    con = db(); cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS event (
      guild_id INTEGER PRIMARY KEY,
      theme TEXT NOT NULL,
      state TEXT NOT NULL,
      entry_end_utc TEXT NOT NULL,
      vote_hours INTEGER NOT NULL,
      vote_seconds INTEGER,
      round_index INTEGER NOT NULL DEFAULT 0,
      main_channel_id INTEGER,
      start_msg_id INTEGER,
      round_thread_id INTEGER
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
    """)
    con.close()

init_db()

# ------------- Utils -------------
def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.manage_guild or member.guild_permissions.administrator

def rel_ts(dt_utc: datetime) -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt_utc.timestamp())}:R>"

# ------------- Images -------------
async def build_vs_card(left_url: str, right_url: str, width: int = 1200, gap: int = 24) -> io.BytesIO:
    async with aiohttp.ClientSession() as s:
        async with s.get(left_url) as rl, s.get(right_url) as rr:
            Lb = await rl.read()
            Rb = await rr.read()
            
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
    
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    out.seek(0)
    return out

# ------------- Voting UI & Logic -------------
class MatchView(discord.ui.View):
    def __init__(self, match_id: int, end_utc: datetime, left_label: str, right_label: str, chat_url: str = None):
        super().__init__(timeout=None) # Persistent
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.success, label=f"Vote {left_label}", custom_id=f"stylo:v:L:{match_id}"))
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.danger, label=f"Vote {right_label}", custom_id=f"stylo:v:R:{match_id}"))
        if chat_url:
            self.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=chat_url, label="Theme Chat"))

async def handle_vote(interaction: discord.Interaction, side: str, match_id: int):
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM match WHERE id=?", (match_id,))
    m = cur.fetchone()
    
    if not m:
        con.close()
        return await interaction.response.send_message("Match not found.", ephemeral=True)
    
    end_dt = datetime.fromisoformat(m["end_utc"]).replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= end_dt:
        con.close()
        return await interaction.response.send_message("Voting has ended for this match.", ephemeral=True)

    try:
        cur.execute("INSERT INTO voter(match_id, user_id, side) VALUES(?,?,?)", (match_id, interaction.user.id, side))
        col = "left_votes" if side == "L" else "right_votes"
        cur.execute(f"UPDATE match SET {col}={col}+1 WHERE id=?", (match_id,))
        con.close()
        await interaction.response.send_message(f"Vote cast for {'Left' if side == 'L' else 'Right'}!", ephemeral=True)
    except sqlite3.IntegrityError:
        con.close()
        await interaction.response.send_message("You have already voted in this match.", ephemeral=True)

# ------------- Core Logic -------------
async def ensure_event_chat_thread(guild: discord.Guild, ch: discord.TextChannel, ev_row: sqlite3.Row) -> int | None:
    th_id = ev_row["round_thread_id"]
    if th_id:
        th = guild.get_thread(th_id)
        if th and not th.archived: return th.id

    th = await ch.create_thread(name=f"🗣 Theme Chat — {ev_row['theme']}"[:95], type=discord.ChannelType.public_thread)
    con = db(); cur = con.cursor()
    cur.execute("UPDATE event SET round_thread_id=? WHERE guild_id=?", (th.id, ev_row["guild_id"]))
    con.close()
    return th.id

async def post_round_matches(ev, round_index, vote_end, con, cur):
    guild = bot.get_guild(ev["guild_id"])
    ch = guild.get_channel(ev["main_channel_id"])
    if not ch: return
    
    th_id = await ensure_event_chat_thread(guild, ch, ev)
    chat_url = guild.get_thread(th_id).jump_url if th_id else None

    cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND msg_id IS NULL", (ev["guild_id"], round_index))
    for m in cur.fetchall():
        cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],))
        L = cur.fetchone()
        cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],))
        R = cur.fetchone()
        
        em = discord.Embed(title=f"Round {round_index}: {L['name']} vs {R['name']}", colour=EMBED_COLOUR)
        em.add_field(name="Ends", value=rel_ts(vote_end))
        view = MatchView(m["id"], vote_end, L["name"], R["name"], chat_url=chat_url)
        
        card = await build_vs_card(L["image_url"], R["image_url"])
        msg = await ch.send(embed=em, view=view, file=discord.File(card, "vs.png"))
        cur.execute("UPDATE match SET msg_id=? WHERE id=?", (msg.id, m["id"]))

async def advance_to_next_round(ev, now, con, cur, guild, ch):
    cur.execute("SELECT winner_id FROM match WHERE guild_id=? AND round_index=?", (ev["guild_id"], ev["round_index"]))
    winners = [r["winner_id"] for r in cur.fetchall() if r["winner_id"]]

    if len(winners) == 1:
        cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
        cur.execute("SELECT name, user_id, image_url FROM entrant WHERE id=?", (winners[0],))
        w = cur.fetchone()
        em = discord.Embed(title=f"👑 Champion: {w['name']}", description=f"The winner of **{ev['theme']}** is <@{w['user_id']}>!", color=discord.Color.gold())
        if w["image_url"]: em.set_image(url=w["image_url"])
        await ch.send(embed=em)
        return

    random.shuffle(winners)
    nr = ev["round_index"] + 1
    v_sec = ev["vote_seconds"] or (int(ev["vote_hours"]) * 3600)
    v_end = now + timedelta(seconds=v_sec)
    
    for i in range(0, len(winners), 2):
        if i+1 < len(winners):
            cur.execute("INSERT INTO match(guild_id,round_index,left_id,right_id,end_utc) VALUES(?,?,?,?,?)",
                        (ev["guild_id"], nr, winners[i], winners[i+1], v_end.isoformat()))
    
    cur.execute("UPDATE event SET round_index=?, entry_end_utc=?, state='voting' WHERE guild_id=?", (nr, v_end.isoformat(), ev["guild_id"]))
    await post_round_matches(ev, nr, v_end, con, cur)

# ------------- Scheduler -------------
@tasks.loop(seconds=30)
async def scheduler():
    now = datetime.now(timezone.utc)
    con = db(); cur = con.cursor()
    
    cur.execute("SELECT * FROM event WHERE state IN ('entry', 'voting')")
    for ev in cur.fetchall():
        end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
        if now < end: continue
        
        cur.execute("UPDATE event SET state='processing' WHERE guild_id=?", (ev["guild_id"],))
        guild = bot.get_guild(ev["guild_id"])
        ch = guild.get_channel(ev["main_channel_id"])

        if ev["state"] == "entry":
            cur.execute("SELECT * FROM entrant WHERE guild_id=? AND image_url IS NOT NULL", (ev["guild_id"],))
            ents = cur.fetchall()
            if len(ents) < 2:
                cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
                continue
            
            random.shuffle(ents)
            v_sec = ev["vote_seconds"] or (int(ev["vote_hours"]) * 3600)
            v_end = now + timedelta(seconds=v_sec)
            for i in range(0, len(ents), 2):
                if i+1 < len(ents):
                    cur.execute("INSERT INTO match(guild_id,round_index,left_id,right_id,end_utc) VALUES(?,?,?,?,?)",
                                (ev["guild_id"], 1, ents[i]["id"], ents[i+1]["id"], v_end.isoformat()))
            
            cur.execute("UPDATE event SET state='voting', round_index=1, entry_end_utc=? WHERE guild_id=?", (v_end.isoformat(), ev["guild_id"]))
            await post_round_matches(ev, 1, v_end, con, cur)
        
        elif ev["state"] == "voting":
            cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL", (ev["guild_id"], ev["round_index"]))
            for m in cur.fetchall():
                win_id = m["left_id"] if m["left_votes"] > m["right_votes"] else m["right_id"]
                if m["left_votes"] == m["right_votes"]: win_id = random.choice([m["left_id"], m["right_id"]])
                cur.execute("UPDATE match SET winner_id=? WHERE id=?", (win_id, m["id"]))
            await advance_to_next_round(ev, now, con, cur, guild, ch)

    con.close()

# ------------- Slash Commands -------------
@bot.tree.command(name="start", description="Start a new bracket event")
async def start(interaction: discord.Interaction, theme: str, channel: discord.TextChannel, entry_hours: int = 24, vote_hours: int = 24):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("You don't have permission to start events.", ephemeral=True)

    end_utc = datetime.now(timezone.utc) + timedelta(hours=entry_hours)
    con = db(); cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO event (guild_id, theme, state, entry_end_utc, vote_hours, main_channel_id) VALUES (?,?,?,?,?,?)",
                (interaction.guild_id, theme, 'entry', end_utc.isoformat(), vote_hours, channel.id))
    con.close()
    
    await interaction.response.send_message(f"🏆 Event **{theme}** started! Post your entries in {channel.mention}. Entries close {rel_ts(end_utc)}.")

@bot.tree.command(name="join", description="Submit your entry for the current event")
async def join(interaction: discord.Interaction, name: str, image_url: str):
    con = db(); cur = con.cursor()
    cur.execute("SELECT state FROM event WHERE guild_id=?", (interaction.guild_id,))
    ev = cur.fetchone()
    
    if not ev or ev["state"] != "entry":
        con.close()
        return await interaction.response.send_message("There is no active entry period.", ephemeral=True)
        
    try:
        cur.execute("INSERT INTO entrant (guild_id, user_id, name, image_url) VALUES (?,?,?,?)",
                    (interaction.guild_id, interaction.user.id, name, image_url))
        con.close()
        await interaction.response.send_message(f"Successfully joined with **{name}**!", ephemeral=True)
    except sqlite3.IntegrityError:
        con.close()
        await interaction.response.send_message("You have already joined this event.", ephemeral=True)

# ------------- Setup & Events -------------
@bot.event
async def setup_hook():
    if not scheduler.is_running():
        scheduler.start()
    await bot.tree.sync()
    print("Slash commands synced globally.")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        cid = interaction.data.get("custom_id", "")
        if cid.startswith("stylo:v:"):
            parts = cid.split(":")
            await handle_vote(interaction, parts[2], int(parts[3]))
            return 
    # Process slash commands
    await bot.process_application_commands(interaction)

@bot.event
async def on_ready():
    print(f"Stylo Online: {bot.user}")

if __name__ == "__main__":
    bot.run(TOKEN)

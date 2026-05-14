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

# 🚨 FIX: Force-create the /data directory if it doesn't exist yet
db_dir = os.path.dirname(os.path.abspath(DB_PATH))
if db_dir and not os.path.exists(db_dir):
    try:
        os.makedirs(db_dir, exist_ok=True)
        print(f"Successfully created database directory: {db_dir}")
    except Exception as e:
        print(f"CRITICAL: Failed to create directory {db_dir}. Error: {e}")

EMBED_COLOUR = discord.Colour.from_rgb(224, 64, 255)

STYLO_CHAT_BUMP_LIMIT = 10
stylo_chat_counters: dict[int, int] = {}

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
      state TEXT NOT NULL,                -- 'entry'|'voting'|'closed'|'processing'
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

    CREATE TABLE IF NOT EXISTS bump_panel(
      guild_id INTEGER NOT NULL,
      match_id INTEGER NOT NULL,
      msg_id INTEGER NOT NULL,
      PRIMARY KEY (guild_id, msg_id)
    );
    """)
    con.close()
init_db()

# ------------- Utils -------------
def rel_ts(dt_utc: datetime) -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
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
    return max(60, min(int(round(minutes * 60)), 60 * 60 * 24 * 10))

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.manage_guild or member.guild_permissions.administrator

def get_ticket_category_id(guild_id: int) -> int | None:
    con = db(); cur = con.cursor()
    cur.execute("SELECT ticket_category_id FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone(); con.close()
    return row["ticket_category_id"] if row else None

def set_ticket_category_id(guild_id: int, category_id: int | None):
    con = db(); cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO guild_settings(guild_id, ticket_category_id) VALUES(?,?)", (guild_id, category_id))
    con.close()

# ------------- Images -------------
async def fetch_image_bytes(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=10) as r:
                return await r.read() if r.status == 200 else None
    except: return None

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

# ------------- Voting UI & Logic -------------
class MatchView(discord.ui.View):
    def __init__(self, match_id: int, end_utc: datetime, left_label: str, right_label: str, chat_url: str = None):
        timeout = max(1, int((end_utc - datetime.now(timezone.utc)).total_seconds()))
        super().__init__(timeout=timeout)
        self.match_id = match_id
        
        # PERSISTENT IDS: Match ID is encoded in the custom_id string
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.success, label=f"Vote {left_label}", custom_id=f"stylo:v:L:{match_id}"))
        self.add_item(discord.ui.Button(style=discord.ButtonStyle.danger, label=f"Vote {right_label}", custom_id=f"stylo:v:R:{match_id}"))
        if chat_url:
            self.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=chat_url, label="Chat here"))

async def handle_vote(interaction: discord.Interaction, side: str, match_id: int):
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM match WHERE id=?", (match_id,))
    m = cur.fetchone()
    if not m:
        con.close(); return await interaction.response.send_message("Match not found.", ephemeral=True)
    
    end_dt = datetime.fromisoformat(m["end_utc"]).replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= end_dt:
        con.close(); return await interaction.response.send_message("Voting ended.", ephemeral=True)

    try:
        cur.execute("INSERT INTO voter(match_id, user_id, side) VALUES(?,?,?)", (match_id, interaction.user.id, side))
        col = "left_votes" if side == "L" else "right_votes"
        cur.execute(f"UPDATE match SET {col}={col}+1 WHERE id=?", (match_id,))
        con.close()
        
        # Simple feedback instead of editing complex embeds in global listener
        await interaction.response.send_message(f"Vote registered for {'Left' if side == 'L' else 'Right'}!", ephemeral=True)
    except sqlite3.IntegrityError:
        con.close()
        await interaction.response.send_message("You've already voted.", ephemeral=True)

# ------------- Core Logic -------------
async def ensure_event_chat_thread(guild: discord.Guild, ch: discord.TextChannel, ev_row: sqlite3.Row) -> int | None:
    if not (guild and ch and ev_row): return None
    th_id = ev_row["round_thread_id"]
    if th_id:
        th = guild.get_thread(th_id)
        if th and not th.archived: return th.id

    title = f"🗣 Theme Chat — {ev_row['theme']}"
    th = await ch.create_thread(name=title[:95], type=discord.ChannelType.public_thread)
    con = db(); cur = con.cursor()
    cur.execute("UPDATE event SET round_thread_id=? WHERE guild_id=?", (th.id, ev_row["guild_id"]))
    con.close()
    await th.send("Chat here about the theme. Voting posts stay clean.")
    return th.id

def chat_jump_url(guild: discord.Guild, thread_id: int | None) -> str | None:
    if not (guild and thread_id): return None
    th = guild.get_thread(thread_id)
    return th.jump_url if th else None

async def advance_to_next_round(ev, now, con, cur, guild, ch):
    gid = ev["guild_id"]
    cur_round = ev["round_index"]
    vote_sec = ev["vote_seconds"] or (int(ev["vote_hours"]) * 3600)

    cur.execute("SELECT winner_id FROM match WHERE guild_id=? AND round_index=?", (gid, cur_round))
    winners = [r["winner_id"] for r in cur.fetchall() if r["winner_id"]]

    if len(winners) == 1:
        # Final Winner logic
        cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (gid,))
        cur.execute("SELECT name, user_id, image_url FROM entrant WHERE id=?", (winners[0],))
        w = cur.fetchone()
        em = discord.Embed(title=f"👑 Champion — {ev['theme']}", description=f"Winner: **{w['name']}** (<@{w['user_id']}>)", color=discord.Color.gold())
        if w["image_url"]: em.set_image(url=w["image_url"])
        await ch.send(embed=em)
        return

    # Normal Pairings
    random.shuffle(winners)
    nr = cur_round + 1
    v_end = now + timedelta(seconds=vote_sec)
    for i in range(0, len(winners), 2):
        if i+1 < len(winners):
            cur.execute("INSERT INTO match(guild_id,round_index,left_id,right_id,end_utc) VALUES(?,?,?,?,?)",
                        (gid, nr, winners[i], winners[i+1], v_end.isoformat()))
    
    cur.execute("UPDATE event SET round_index=?, entry_end_utc=?, state='voting' WHERE guild_id=?", (nr, v_end.isoformat(), gid))
    await post_round_matches(ev, nr, v_end, con, cur)

async def post_round_matches(ev, round_index, vote_end, con, cur):
    guild = bot.get_guild(ev["guild_id"])
    ch = guild.get_channel(ev["main_channel_id"])
    if not ch: return
    
    th_id = await ensure_event_chat_thread(guild, ch, ev)
    url = chat_jump_url(guild, th_id)

    cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND msg_id IS NULL", (ev["guild_id"], round_index))
    for m in cur.fetchall():
        cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["left_id"],)); L = cur.fetchone()
        cur.execute("SELECT name, image_url FROM entrant WHERE id=?", (m["right_id"],)); R = cur.fetchone()
        
        em = discord.Embed(title=f"Round {round_index} — {L['name']} vs {R['name']}", colour=EMBED_COLOUR)
        em.add_field(name="Closes", value=rel_ts(vote_end))
        
        view = MatchView(m["id"], vote_end, L["name"], R["name"], chat_url=url)
        
        msg = None
        if L["image_url"] and R["image_url"]:
            card = await build_vs_card(L["image_url"], R["image_url"])
            msg = await ch.send(embed=em, view=view, file=discord.File(card, "vs.png"))
        else:
            msg = await ch.send(embed=em, view=view)
        
        cur.execute("UPDATE match SET msg_id=? WHERE id=?", (msg.id, m["id"]))

# ------------- Scheduler -------------
@tasks.loop(seconds=15)
async def scheduler():
    now = datetime.now(timezone.utc)
    con = db(); cur = con.cursor()
    
    # 1. Entry -> Voting
    cur.execute("SELECT * FROM event WHERE state='entry'")
    for ev in cur.fetchall():
        end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
        if now < end: continue
        
        # PREVENT DOUBLE RUN
        cur.execute("UPDATE event SET state='processing' WHERE guild_id=?", (ev["guild_id"],))
        
        cur.execute("SELECT * FROM entrant WHERE guild_id=? AND image_url IS NOT NULL", (ev["guild_id"],))
        ents = cur.fetchall()
        if len(ents) < 2:
            cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (ev["guild_id"],))
            continue
            
        # Initialize Round 1
        random.shuffle(ents)
        v_sec = ev["vote_seconds"] or (int(ev["vote_hours"]) * 3600)
        v_end = now + timedelta(seconds=v_sec)
        for i in range(0, len(ents), 2):
            if i+1 < len(ents):
                cur.execute("INSERT INTO match(guild_id,round_index,left_id,right_id,end_utc) VALUES(?,?,?,?,?)",
                            (ev["guild_id"], 1, ents[i]["id"], ents[i+1]["id"], v_end.isoformat()))
        
        cur.execute("UPDATE event SET state='voting', round_index=1, entry_end_utc=? WHERE guild_id=?", (v_end.isoformat(), ev["guild_id"]))
        await post_round_matches(ev, 1, v_end, con, cur)

    # 2. Voting -> Next Round
    cur.execute("SELECT * FROM event WHERE state='voting'")
    for ev in cur.fetchall():
        end = datetime.fromisoformat(ev["entry_end_utc"]).replace(tzinfo=timezone.utc)
        if now < end: continue
        
        cur.execute("UPDATE event SET state='processing' WHERE guild_id=?", (ev["guild_id"],))
        
        # Determine Winners (Simple majority, Tie-break = Random for now)
        cur.execute("SELECT * FROM match WHERE guild_id=? AND round_index=? AND winner_id IS NULL", (ev["guild_id"], ev["round_index"]))
        ms = cur.fetchall()
        for m in ms:
            win_id = m["left_id"] if m["left_votes"] > m["right_votes"] else m["right_id"]
            if m["left_votes"] == m["right_votes"]: win_id = random.choice([m["left_id"], m["right_id"]])
            cur.execute("UPDATE match SET winner_id=? WHERE id=?", (win_id, m["id"]))
            
        guild = bot.get_guild(ev["guild_id"])
        ch = guild.get_channel(ev["main_channel_id"])
        await advance_to_next_round(ev, now, con, cur, guild, ch)

    con.close()

# ------------- Setup -------------
@bot.event
async def setup_hook():
    # Persistent View Listener for Dynamic IDs
    @bot.event
    async def on_interaction(interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component:
            cid = interaction.data.get("custom_id", "")
            if cid.startswith("stylo:v:"):
                # Format: stylo:v:{L/R}:{match_id}
                parts = cid.split(":")
                await handle_vote(interaction, parts[2], int(parts[3]))
            elif cid == "stylo:join":
                # Handle the Join modal manually here or keep build_join_view
                pass

    await bot.tree.sync()
    if not scheduler.is_running(): scheduler.start()

@bot.event
async def on_ready():
    print(f"Stylo Online: {bot.user}")

if __name__ == "__main__":
    bot.run(TOKEN)

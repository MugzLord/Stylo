Skip to content
Chat history

You said:
can you generate this bot? What it is

A Discord fashion-battle (“Stylo”) bot that runs themed bracket rounds:

Entries phase → users open a private ticket channel to upload a square image.

Voting rounds → bot posts head-to-head match cards with buttons; live totals update on the embed.

Tie-breaks auto re-open the match. button will open for voting

Champion is announced with their image.


Key pieces

Tech: discord.py (slash + UI), sqlite3 (WAL), aiohttp, Pillow for image compositing, tasks.loop scheduler.

Env vars: DISCORD_TOKEN (required), STYLO_DB_PATH, CONFETTI_GIF_PATH, LOG_CHANNEL_ID.

Permissions: Admin-only to start/reset/configure; per-ticket channel overwrites for privacy.

Styling: Neon pink/purple embed colour.

Database schema (core tables)

event: one per guild – theme, state (entry/voting/closed), entry_end_utc, vote_hours, vote_seconds, round_index, main_channel_id, start_msg_id.

entrant: one per user per guild – display name, caption, image_url.

ticket: entrant’s private channel id.
ticket channel disappears once the 

match: per pairing – participants, end_utc, votes, winner_id, message/thread ids.

voter: one vote per user per match.

guild_settings: log_channel_id, ticket_category_id.

Commands & UI

/stylo (admin): Opens StyloStartModal (theme, entry window, vote window).

Join button on the start embed: opens EntrantModal (display name + caption) → creates a private ticket channel with instructions; reacts ✅ when image saved.

Match buttons: “Vote Left/Right” (one vote per user); embed shows Live totals with vote count and split.

/stylo_settings set_log_channel, /stylo_settings set_ticket_category, /stylo_settings show & show_ticket_category.

/stylo_debug (admin): quick status/diagnostics.

/stylo_reset (admin): wipes all Stylo data for testing.

Scheduler (every 20s)

Entry → Voting

When entry_end_utc passes and ≥2 images exist:
Delete, ticket channels

Shuffles entrants → pairs, sets round end_utc using vote_seconds (or vote_hours).

Posts each match with a side-by-side image (build_vs_card), buttons, and a supporter thread.

Disables Join, unpins the start embed, locks the main channel, and cleans up all ticket channels.

Voting end

Reads votes for each match.

Tie → resets that match’s votes, pushes a fresh end_utc, clears voters, re-enables buttons, announces tie-break, and pauses advancing the event (waits for all tie-breaks to finish).

Win → records winner_id, posts a results embed with percentages and (where available) winner image and @mention.

If only one winner remains → posts Champion embed (image + footer), sets state to closed.

Otherwise → builds next round from winners, posts new matches, relocks chat, and repeats.

Nice touches

Live countdown on the start embed: periodically edits the “Entries” and “Voting” info; disables Join at 00:00 and unpins.

Humanised durations (2h, 90m) and parsing (1.5h, 45m, etc.).

Category support for ticket channels with permission/limit checks.

Graceful error handling (no crashes on countdown; many try/except guards).

Notables / minor issues to watch

guild_settings table is declared twice in init_db() (harmless but redundant).

Voting-end logic appears twice in the scheduler block (a large “second pass” starting after con.close()), which risks duplicate handling—consolidate to one path.

publish_match_card() references cur without defining it (unused helper; would error if called).

compose_pair_image is probed via globals() but the actual pipeline uses build_vs_card.

Some result-announce code paths are duplicated; could be trimmed to one clean implementation.

Typical flow

Admin runs /stylo → sets theme, entry & vote durations.

Bot posts Join embed (pinned) with countdown.

Users press Join → submit image in private ticket.

Entries close → bot posts paired match cards with vote buttons + threads.

Round ends → ties revote; winners advance; Champion announced with image.

How to run

Set DISCORD_TOKEN (+ optional envs), install deps, run the script.

Bot syncs slash commands on on_ready, starts the scheduler, and (if log channel set) posts a “back online” message.

If you want, I can collapse the duplicated scheduler bits and remove the double table creation while keeping your structure intact.
ChatGPT said:

Thought for 9s
Stylo — Discord Fashion Battle Bot (stylo Bot
· python
                if main_channel:
                    try: await main_channel.send(embed=em)
                    except Exception: pass

        if not all_finished:
            con.close(); return

        # Advance bracket
        winners = [w for w in winners if w and w != 0]
        if len(winners) <= 1:
            # Champion!
            champion_id = winners[0] if winners else None
            cur.execute("UPDATE event SET state='closed' WHERE guild_id=?", (guild.id,))
            con.commit(); con.close()
            if main_channel and champion_id:
                # Champion embed with image + confetti
                cur3 = db_connect().cursor()
                cur3.execute("SELECT display_name, image_url FROM entrant WHERE guild_id=? AND user_id=?", (guild.id, champion_id))
                C = cur3.fetchone(); cur3.connection.close()
                em = discord.Embed(title="🏆 Stylo Champion!", description=f"**{C['display_name'] if C else champion_id}**", colour=EMBED_COLOUR)
                if C and C['image_url']:
                    em.set_thumbnail(url=C['image_url'])
                file = None
                if CONFETTI_GIF_PATH and os.path.exists(CONFETTI_GIF_PATH):
                    try:
                        file = discord.File(CONFETTI_GIF_PATH, filename="confetti.gif")
                        em.set_image(url="attachment://confetti.gif")
                    except Exception:
                        file = None
                try:
                    await main_channel.send(embed=em, file=file) if file else await main_channel.send(embed=em)
                except Exception:
                    pass
            return

        # Build next round
        next_round = round_index + 1
        random.shuffle(winners)
        pairs = [(winners[i], winners[i+1]) for i in range(0, len(winners)-1, 2)]
        if len(winners) % 2 == 1:
            pairs.append((winners[-1], 0))
        new_end = now + timedelta(seconds=vote_seconds)

        cur.execute("UPDATE event SET round_index=? WHERE guild_id=?", (next_round, guild.id))
        con.commit()

        if main_channel:
            match_id = 0
            for Lp, Rp in pairs:
                if Rp == 0:
                    cur.execute("INSERT OR REPLACE INTO match (guild_id, round_index, match_id, left_id, right_id, end_utc, winner_id) VALUES (?,?,?,?,?,?,?)",
                                (guild.id, next_round, match_id, Lp, Rp, new_end.isoformat(), Lp))
                    match_id += 1
                    continue
                msg_id, thread_id = await publish_match_card(main_channel, guild.id, next_round, match_id, Lp, Rp)
                cur.execute("INSERT OR REPLACE INTO match (guild_id, round_index, match_id, left_id, right_id, end_utc, message_id, thread_id) VALUES (?,?,?,?,?,?,?,?)",
                            (guild.id, next_round, match_id, Lp, Rp, new_end.isoformat(), msg_id, thread_id))
                match_id += 1
            con.commit()

    con.close()


# ---------------------- Bot lifecycle -----------------------
@bot.event
async def on_ready():
    if not bot.session:
        bot.session = aiohttp.ClientSession()
    # optional: log online
    for g in bot.guilds:
        if LOG_CHANNEL_ID:
            ch = g.get_channel(LOG_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send("Stylo bot back online ✨")
                except Exception:
                    pass
    print(f"Logged in as {bot.user}")


async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

All set! I’ve put a full, runnable stylo_bot.py in the canvas with:

Slash commands (/stylo, /stylo_reset, /stylo_settings ..., /stylo_debug)

Private ticket channels with ✅ reaction when an image is saved

Image compositing (side-by-side VS card) via Pillow

Voting buttons with live totals on the embed

Auto tie-break reopen, round advancement, and Champion announcement (with optional confetti GIF)

SQLite (WAL) schema exactly as you described

20-second scheduler handling entry→voting, match ends, ties, and next rounds

How to run
Create a Discord bot, invite with applications.commands and bot scopes (needs Manage Channels, Send Messages, Read Message History, Create Public Threads, Attach Files).

Set env vars:

DISCORD_TOKEN (required)

STYLO_DB_PATH (optional, default stylo.db)

CONFETTI_GIF_PATH (optional, path to a local confetti GIF)

LOG_CHANNEL_ID (optional, numeric channel ID)

Install deps:

pip install -U discord.py pillow aiohttp
Run:

python stylo_bot.py
Quick use
/stylo → fill modal (theme, entry window like 2h, vote window like 30m).

Press Join on the start embed → users get a private ticket to upload one square image.

Scheduler pairs entrants, posts match cards with Vote Left/Right, updates live totals, handles ties, advances rounds, and announces the Champion with their image.

Want any tweaks (e.g., allow vote switching, stricter square checks, per-server language, or a per-match timer display)? Tell me and I’ll update the canvas without touching your overall structure.





No file chosenNo file chosen
ChatGPT can make mistakes. Check important info. See Cookie Preferences.

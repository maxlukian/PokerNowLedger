#!/usr/bin/env python3
"""
Fetch full game logs from PokerNow and store them in Firestore.

Stores logs as subcollection chunks under each game document:
  games/{gameId}/log_chunks/{0, 1, 2, ...}
Each chunk holds up to 500 log entries.

Also updates the game document with:
  - logFetched: True
  - handCount: int
  - biggestPots: [top 10 pots with details]

Usage:
  python3 fetch_logs.py                  # fetch logs for all games missing logs
  python3 fetch_logs.py --force          # re-fetch all games even if already fetched
  python3 fetch_logs.py --game pglXYZ    # fetch a single game
"""

import json
import os
import re
import sys
import time
import urllib.request

import firebase_admin
from firebase_admin import credentials, firestore

# ── Firebase setup ──
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS",
                       os.path.join(os.path.dirname(__file__), "firebase-service-key.json"))
cred = credentials.Certificate(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()

CHUNK_SIZE = 500
POKERNOW_BASE = "https://www.pokernow.com/games"
REQUEST_DELAY = 3       # seconds between pages
RETRY_DELAY = 8         # seconds after rate limit
MAX_RETRIES = 5


def fetch_game_log(game_id):
    """Fetch all log entries for a game via paginated API."""
    all_logs = []
    cursor = None
    pages = 0

    while True:
        url = f"{POKERNOW_BASE}/{game_id}/log"
        if cursor:
            url += f"?before_at={cursor}"

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

        for attempt in range(MAX_RETRIES):
            try:
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read())
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    print(f"    Rate limited, waiting {RETRY_DELAY}s... (attempt {attempt+1})")
                    time.sleep(RETRY_DELAY)
                elif e.code == 404:
                    print(f"    404 - game log not available")
                    return None
                else:
                    print(f"    HTTP {e.code}, retrying...")
                    time.sleep(RETRY_DELAY)
        else:
            print(f"    Failed after {MAX_RETRIES} retries, stopping.")
            return None

        logs = data.get("logs", [])
        if not logs:
            break

        all_logs.extend(logs)
        cursor = logs[-1]["created_at"]
        pages += 1

        if pages % 20 == 0:
            print(f"    Page {pages}: {len(all_logs)} entries")

        if len(logs) < 50:
            break

        time.sleep(REQUEST_DELAY)

    print(f"    Done: {pages} pages, {len(all_logs)} entries")
    return all_logs


def parse_hand_data(logs):
    """Parse all hands from log entries. Returns structured hand data."""
    hands = {}
    current_hand_id = None

    # Hand strength ranking for bad beat detection
    HAND_RANKS = {
        "Royal Flush": 10, "Straight Flush": 9, "Four of a Kind": 8,
        "Full House": 7, "Flush": 6, "Straight": 5, "Three of a Kind": 4,
        "Two Pair": 3, "Pair": 2, "High Card": 1,
    }

    # Logs come newest-first, reverse for chronological order
    for entry in reversed(logs):
        msg = entry["msg"]
        at = entry.get("at", "")

        hand_start = re.search(r'-- starting hand #(\d+) \(id: (\w+)\)\s+(.*?)(?:\s*\(dealer:.*)?--', msg)
        if hand_start:
            current_hand_id = hand_start.group(2)
            hands[current_hand_id] = {
                "hand_num": int(hand_start.group(1)),
                "game_type": hand_start.group(3).strip(),
                "at": at,
                "entries": [],
                "pots": [],
                "shown_hands": [],
                "all_ins": [],
                "winners": set(),
                "community_cards": [],
                "street": "preflop",
                "players_in_hand": set(),
                "player_actions": {},  # player -> {preflop: [], flop: [], turn: [], river: []}
            }

            # Parse player stacks to get list of players dealt in
            # (next entry is usually "Player stacks: ...")

        if not current_hand_id or current_hand_id not in hands:
            continue

        hand = hands[current_hand_id]
        hand["entries"].append(entry)

        # Track player stacks (players dealt into the hand)
        stacks_match = re.search(r'Player stacks:', msg)
        if stacks_match:
            for m in re.finditer(r'"(.+?) @', msg):
                player = m.group(1)
                hand["players_in_hand"].add(player)
                if player not in hand["player_actions"]:
                    hand["player_actions"][player] = {"preflop": [], "flop": [], "turn": [], "river": []}

        # Track street transitions
        if msg.startswith("Flop:"):
            hand["street"] = "flop"
        elif msg.startswith("Turn:"):
            hand["street"] = "turn"
        elif msg.startswith("River:"):
            hand["street"] = "river"
            river_match = re.search(r'River: (.+)', msg)
            if river_match:
                hand["community_cards"] = river_match.group(1)

        # Track player actions (bets, calls, raises, checks, folds)
        action_match = re.search(r'"(.+?) @.+?" (bets|calls|raises|checks|folds)(.*)', msg)
        if action_match:
            player = action_match.group(1)
            action = action_match.group(2)
            if player not in hand["player_actions"]:
                hand["player_actions"][player] = {"preflop": [], "flop": [], "turn": [], "river": []}
            hand["player_actions"][player][hand["street"]].append(action)

        # Track blinds (these count as voluntary for big blind defense scenarios,
        # but standard VPIP excludes forced blinds)
        blind_match = re.search(r'"(.+?) @.+?" posts a (small|big) blind', msg)
        if blind_match:
            player = blind_match.group(1)
            if player not in hand["player_actions"]:
                hand["player_actions"][player] = {"preflop": [], "flop": [], "turn": [], "river": []}
            hand["player_actions"][player]["preflop"].append(f"post_{blind_match.group(2)}")

        # Track pot collections
        pot_match = re.search(r'"(.+?) @.+?" collected (\d+) from pot(.*)', msg)
        if pot_match:
            name = pot_match.group(1)
            amount = int(pot_match.group(2))
            detail = pot_match.group(3).strip()
            hand_name = ""
            hand_rank = 0
            if detail.startswith("with "):
                clean = re.sub(r'^with (hi hand with |low hand with )?', '', detail)
                hand_name = clean.split(" (combination")[0]
                for rank_name, rank_val in HAND_RANKS.items():
                    if rank_name.lower() in hand_name.lower():
                        hand_rank = rank_val
                        break
            hand["pots"].append({
                "player": name,
                "amount": amount,
                "detail": detail,
                "handName": hand_name,
                "handRank": hand_rank,
            })
            hand["winners"].add(name)

        # Track shown hands (even losers)
        show_match = re.search(r'"(.+?) @.+?" shows a (.+?)\.', msg)
        if show_match:
            hand["shown_hands"].append({
                "player": show_match.group(1),
                "cards": show_match.group(2),
            })

        # Track all-ins
        allin_match = re.search(r'"(.+?) @.+?" (calls|raises|bets) (\d+) and go all in', msg)
        if allin_match:
            hand["all_ins"].append({
                "player": allin_match.group(1),
                "action": allin_match.group(2),
                "amount": int(allin_match.group(3)),
            })

    return hands


def extract_biggest_pots(hands, limit=10):
    """Top N hands by total pot size."""
    pot_list = []
    for hand_id, hand in hands.items():
        total_pot = sum(p["amount"] for p in hand["pots"])
        if total_pot > 0:
            pot_list.append({
                "handId": hand_id,
                "handNum": hand["hand_num"],
                "totalPot": total_pot,
                "at": hand.get("at", ""),
                "winners": [{"player": p["player"], "amount": p["amount"],
                             "detail": p["detail"], "handName": p["handName"]}
                            for p in hand["pots"]],
            })
    pot_list.sort(key=lambda x: x["totalPot"], reverse=True)
    return pot_list[:limit]


def extract_bad_beats(hands, limit=10):
    """Find hands where a strong shown hand lost.
    A bad beat = player showed a hand ranked >= Two Pair but didn't win the pot."""
    bad_beats = []
    for hand_id, hand in hands.items():
        if not hand["shown_hands"] or not hand["pots"]:
            continue

        winners = hand["winners"]
        total_pot = sum(p["amount"] for p in hand["pots"])

        # Find the winning hand rank
        winning_rank = max((p["handRank"] for p in hand["pots"]), default=0)
        winning_detail = ""
        for p in hand["pots"]:
            if p["handRank"] == winning_rank and p["detail"]:
                winning_detail = p["detail"]
                break

        # Find losers who showed their hand
        for shown in hand["shown_hands"]:
            if shown["player"] not in winners:
                # This player showed but lost. Check if a winner had a named hand.
                # The loser's hand rank isn't in the log (only winners get "with X"),
                # so a bad beat is defined as: loser showed cards + winner won with
                # a hand where the pot was significant
                if total_pot >= 200 and winning_rank >= 2:
                    winner_info = hand["pots"][0]  # primary winner
                    bad_beats.append({
                        "handId": hand_id,
                        "handNum": hand["hand_num"],
                        "at": hand.get("at", ""),
                        "totalPot": total_pot,
                        "loser": shown["player"],
                        "loserCards": shown["cards"],
                        "winner": winner_info["player"],
                        "winnerHand": winner_info.get("handName", ""),
                        "winnerDetail": winner_info.get("detail", ""),
                        "communityCards": hand.get("community_cards", ""),
                    })

    # Sort by pot size (biggest bad beats first)
    bad_beats.sort(key=lambda x: x["totalPot"], reverse=True)
    return bad_beats[:limit]


def extract_all_in_showdowns(hands, limit=10):
    """Find the biggest all-in confrontations."""
    showdowns = []
    for hand_id, hand in hands.items():
        if len(hand["all_ins"]) < 2 and not (hand["all_ins"] and len(hand["shown_hands"]) >= 2):
            continue

        total_pot = sum(p["amount"] for p in hand["pots"])
        if total_pot == 0:
            continue

        showdowns.append({
            "handId": hand_id,
            "handNum": hand["hand_num"],
            "at": hand.get("at", ""),
            "totalPot": total_pot,
            "allIns": [{"player": a["player"], "amount": a["amount"]} for a in hand["all_ins"]],
            "winners": [{"player": p["player"], "amount": p["amount"],
                         "handName": p["handName"]} for p in hand["pots"]],
            "shownHands": [{"player": s["player"], "cards": s["cards"]} for s in hand["shown_hands"]],
            "communityCards": hand.get("community_cards", ""),
        })

    showdowns.sort(key=lambda x: x["totalPot"], reverse=True)
    return showdowns[:limit]


def normalize_game_type(game_type):
    """Normalize game type string to a short label."""
    gt = (game_type or "").lower()
    if "omaha" in gt and ("hi/lo" in gt or "8 or better" in gt or "hi-lo" in gt):
        return "PLO8"
    elif "omaha" in gt:
        return "PLO"
    elif "hold" in gt or "no limit" in gt or not gt:
        return "NLH"
    return "NLH"  # default


def _empty_stats():
    return {
        "handsDealt": 0, "vpipHands": 0,
        "sawFlop": 0, "sawTurn": 0, "sawRiver": 0,
        "wentToShowdown": 0, "handTypes": {},
    }


def _accumulate_stats(stats, hand, player, actions):
    """Accumulate one hand's stats into the stats dict."""
    stats["handsDealt"] += 1

    preflop_actions = actions.get("preflop", [])
    voluntary_preflop = [a for a in preflop_actions
                         if a in ("calls", "raises", "bets")]
    if voluntary_preflop:
        stats["vpipHands"] += 1

    if actions.get("flop") and "folds" not in actions["flop"]:
        stats["sawFlop"] += 1
    elif actions.get("flop"):
        stats["sawFlop"] += 1

    if actions.get("turn"):
        stats["sawTurn"] += 1

    if actions.get("river"):
        stats["sawRiver"] += 1

    if any(s["player"] == player for s in hand["shown_hands"]):
        stats["wentToShowdown"] += 1


def _stats_to_dict(stats):
    """Convert raw stats to serializable format with percentages."""
    hd = max(stats["handsDealt"], 1)
    return {
        "handsDealt": stats["handsDealt"],
        "vpipHands": stats["vpipHands"],
        "vpipPct": round(100 * stats["vpipHands"] / hd, 1),
        "sawFlop": stats["sawFlop"],
        "sawFlopPct": round(100 * stats["sawFlop"] / hd, 1),
        "sawTurn": stats["sawTurn"],
        "sawTurnPct": round(100 * stats["sawTurn"] / hd, 1),
        "sawRiver": stats["sawRiver"],
        "sawRiverPct": round(100 * stats["sawRiver"] / hd, 1),
        "wentToShowdown": stats["wentToShowdown"],
        "showdownPct": round(100 * stats["wentToShowdown"] / hd, 1),
        "handTypes": stats["handTypes"],
    }


def extract_player_stats(hands):
    """Extract per-player VPIP (overall + by game type + by street) and hand type distribution."""
    # overall stats
    player_stats = {}
    # per game type: player_stats_by_type[game_type][player] = stats
    player_stats_by_type = {}

    for hand_id, hand in hands.items():
        gt = normalize_game_type(hand.get("game_type", ""))

        for player, actions in hand["player_actions"].items():
            # Overall
            if player not in player_stats:
                player_stats[player] = _empty_stats()
            _accumulate_stats(player_stats[player], hand, player, actions)

            # Per game type
            if gt not in player_stats_by_type:
                player_stats_by_type[gt] = {}
            if player not in player_stats_by_type[gt]:
                player_stats_by_type[gt][player] = _empty_stats()
            _accumulate_stats(player_stats_by_type[gt][player], hand, player, actions)

        # Hand types won
        for pot in hand["pots"]:
            player = pot["player"]
            hand_name = pot["handName"]
            if not hand_name:
                hand_name = "Unknown/No Showdown"
            category = categorize_hand(hand_name)

            for target in [player_stats, player_stats_by_type.get(gt, {})]:
                if player in target:
                    ht = target[player]["handTypes"]
                    ht[category] = ht.get(category, 0) + 1

    # Convert to serializable format
    result = {}
    for player, stats in player_stats.items():
        result[player] = _stats_to_dict(stats)

    # Add per-game-type stats as nested dict
    by_type = {}
    for gt, players in player_stats_by_type.items():
        by_type[gt] = {}
        for player, stats in players.items():
            by_type[gt][player] = _stats_to_dict(stats)

    return {"overall": result, "byType": by_type}


def categorize_hand(hand_name):
    """Normalize a hand description to a standard category."""
    hn = hand_name.lower()
    if "royal flush" in hn:
        return "Royal Flush"
    elif "straight flush" in hn:
        return "Straight Flush"
    elif "four of a kind" in hn:
        return "Four of a Kind"
    elif "full house" in hn:
        return "Full House"
    elif "flush" in hn:
        return "Flush"
    elif "straight" in hn:
        return "Straight"
    elif "three of a kind" in hn:
        return "Three of a Kind"
    elif "two pair" in hn:
        return "Two Pair"
    elif "pair" in hn:
        return "Pair"
    elif "high card" in hn or "high" in hn:
        return "High Card"
    else:
        return "Unknown/No Showdown"


def store_game_log(game_id, logs, parsed):
    """Store log chunks and summary in Firestore."""
    game_ref = db.collection("games").document(game_id)

    # Delete existing log chunks if any
    existing_chunks = game_ref.collection("log_chunks").stream()
    for chunk_doc in existing_chunks:
        chunk_doc.reference.delete()

    # Store log entries in chunks of CHUNK_SIZE
    # Reverse so they're in chronological order (oldest first)
    chronological = list(reversed(logs))
    chunk_count = 0
    for i in range(0, len(chronological), CHUNK_SIZE):
        chunk = chronological[i:i + CHUNK_SIZE]
        game_ref.collection("log_chunks").document(str(chunk_count)).set({
            "entries": chunk,
            "index": chunk_count,
        })
        chunk_count += 1

    # Count hands
    hand_count = sum(1 for l in logs if "-- starting hand" in l["msg"])

    # Store playerStats in a separate subcollection doc to avoid index entry limits
    game_ref.collection("parsed_data").document("playerStats").set({
        "players": parsed["playerStats"],
    })

    # Update game document with summary (keep highlights on the main doc)
    game_ref.update({
        "logFetched": True,
        "logEntryCount": len(logs),
        "handCount": hand_count,
        "biggestPots": parsed["biggestPots"],
        "badBeats": parsed["badBeats"],
        "allInShowdowns": parsed["allInShowdowns"],
    })

    bp = len(parsed["biggestPots"])
    bb = len(parsed["badBeats"])
    ai = len(parsed["allInShowdowns"])
    ps_data = parsed["playerStats"]
    ps = len(ps_data.get("overall", ps_data)) if isinstance(ps_data, dict) else 0
    print(f"    Stored {chunk_count} chunks ({len(logs)} entries), {hand_count} hands")
    print(f"    Stats: {bp} biggest pots, {bb} bad beats, {ai} all-in showdowns, {ps} player stats")


def main():
    force = "--force" in sys.argv
    single_game = None
    for i, arg in enumerate(sys.argv):
        if arg == "--game" and i + 1 < len(sys.argv):
            single_game = sys.argv[i + 1]

    # Get all games from Firestore
    if single_game:
        games = [db.collection("games").document(single_game).get()]
        if not games[0].exists:
            print(f"Game {single_game} not found in Firestore")
            return
    else:
        games = list(db.collection("games").order_by("date").stream())

    print(f"Found {len(games)} games in Firestore\n")

    fetched = 0
    skipped = 0
    failed = 0

    for game_doc in games:
        game_id = game_doc.id
        data = game_doc.to_dict()
        date = data.get("date", "unknown")

        if not force and data.get("logFetched"):
            skipped += 1
            continue

        print(f"[{fetched + failed + 1}/{len(games) - skipped}] {game_id} ({date})")

        logs = fetch_game_log(game_id)
        if logs is None:
            failed += 1
            print(f"    FAILED - log not available\n")
            continue

        if len(logs) == 0:
            failed += 1
            print(f"    EMPTY - no log entries\n")
            continue

        hands = parse_hand_data(logs)
        parsed = {
            "biggestPots": extract_biggest_pots(hands),
            "badBeats": extract_bad_beats(hands),
            "allInShowdowns": extract_all_in_showdowns(hands),
            "playerStats": extract_player_stats(hands),
        }
        store_game_log(game_id, logs, parsed)
        fetched += 1

        # Longer delay between games to avoid rate limits
        if fetched + failed < len(games) - skipped:
            print(f"    Waiting 3s before next game...\n")
            time.sleep(3)

    print(f"\nDone: {fetched} fetched, {skipped} skipped (already had logs), {failed} failed")


if __name__ == "__main__":
    main()

import os
import re
import json
import time
import datetime
import asyncio
import inspect
import logging
import textwrap
import uuid

from dotenv import load_dotenv
from livekit.agents import (
    Agent, AgentServer, AgentSession, JobContext,
    TurnHandlingOptions, cli, inference, room_io,
)
from livekit.plugins import ai_coustics, openai

import agent_tools

try:
    from livekit.agents import StopResponse
except ImportError:
    try:
        from livekit.agents.voice import StopResponse
    except ImportError:
        StopResponse = None

logger = logging.getLogger("agent")
load_dotenv(".env.local")

LLM_MODEL = "openai/gpt-4o-mini"

# ---- domain loading: reads which domain is active, loads its config + KB ----
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOMAINS_DIR = os.path.join(PROJECT_ROOT, "domains")
ACTIVE_FILE = os.path.join(PROJECT_ROOT, "active_domain.txt")


def get_active_domain_id():
    try:
        with open(ACTIVE_FILE) as f:
            d = f.read().strip()
            if d:
                return d
    except FileNotFoundError:
        pass
    return "restaurant"


def load_domain(domain_id):
    cfg_path = os.path.join(DOMAINS_DIR, domain_id, "config.json")
    kb_path = os.path.join(DOMAINS_DIR, domain_id, "kb.txt")
    with open(cfg_path) as f:
        cfg = json.load(f)
    with open(kb_path) as f:
        kb = f.read()
    return cfg, kb


ACTIVE_DOMAIN_ID = get_active_domain_id()
DOMAIN_CFG, KB_TEXT = load_domain(ACTIVE_DOMAIN_ID)
DOMAIN = agent_tools.DomainConfig(DOMAIN_CFG, KB_TEXT)
logger.info(f"Loaded domain: {DOMAIN.display_name} ({ACTIVE_DOMAIN_ID})")


# ---- OpenAI-compatible servers that reject LiveKit's extra fields ---------
# LiveKit attaches "extra_content" to some assistant messages in the history.
# Groq rejects any request containing it (HTTP 400), which made Maya stall
# mid-call. This transport removes it on the wire; nothing else is changed.
_REJECTED_KEYS = ("extra_content",)


def _strip_payload(raw):
    try:
        body = json.loads(raw)
    except Exception:
        return None
    changed = False
    for m in body.get("messages", []) or []:
        if not isinstance(m, dict):
            continue
        for k in _REJECTED_KEYS:
            if k in m:
                m.pop(k)
                changed = True
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict):
                for k in _REJECTED_KEYS:
                    if k in tc:
                        tc.pop(k)
                        changed = True
    return json.dumps(body).encode() if changed else None


def _openai_compatible(model, base_url, api_key):
    """OpenAI-format LLM (Groq etc.) with the incompatible field stripped."""
    try:
        import httpx
        from openai import AsyncOpenAI

        class _StripExtraTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self._inner = httpx.AsyncHTTPTransport()

            async def handle_async_request(self, request):
                if request.method == "POST" and request.url.path.endswith("/chat/completions"):
                    try:
                        raw = await request.aread()
                        cleaned = _strip_payload(raw)
                        if cleaned is not None:
                            headers = request.headers.copy()
                            headers["Content-Length"] = str(len(cleaned))
                            request = httpx.Request(request.method, request.url, headers=headers,
                                                    content=cleaned, extensions=request.extensions)
                    except Exception as e:
                        logger.warning(f"strip transport skipped: {e}")
                return await self._inner.handle_async_request(request)

            async def aclose(self):
                await self._inner.aclose()

        client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=1,
                             http_client=httpx.AsyncClient(transport=_StripExtraTransport(),
                                                           timeout=httpx.Timeout(30.0, connect=10.0)))
        kw = {"reasoning_effort": "low"} if "gpt-oss" in model else {}
        llm = openai.LLM(model=model, client=client, **kw)
        logger.info(f"LLM: {model} via {base_url} (extra_content stripping on)")
        return llm
    except TypeError as e:
        logger.warning(f"plugin rejected client= ({e}); running without stripping")
        return openai.LLM(model=model, base_url=base_url, api_key=api_key)


def _make_llm():
    """LLM_PROVIDER in .env.local picks the brain:
       livekit (default) | gemini | openai | custom | auto (custom first, LiveKit backup)."""
    p = os.environ.get("LLM_PROVIDER", "livekit").lower()
    if p == "gemini":
        return openai.LLM(model=os.environ.get("GEMINI_MODEL", "models/gemini-2.5-flash"),
                          base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                          api_key=os.environ["GEMINI_API_KEY"])
    if p == "openai":
        return openai.LLM(model="gpt-4o-mini")
    if p in ("custom", "auto") and os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_MODEL_NAME"):
        primary = _openai_compatible(os.environ["LLM_MODEL_NAME"], os.environ["LLM_BASE_URL"],
                                     os.environ.get("LLM_API_KEY", "none"))
        if p == "custom":
            return primary
        try:
            from livekit.agents.llm import FallbackAdapter
            return FallbackAdapter([primary, inference.LLM(model=LLM_MODEL)])
        except Exception as e:
            logger.warning(f"fallback unavailable ({e}); using primary only")
            return primary
    return inference.LLM(model=LLM_MODEL)


# Noise cancellation runs a model on this Mac for every frame of audio. On a
# busy laptop it falls behind real time and the voice breaks up, so it is off
# unless NOISE_CANCEL=on is set in .env.local.
NOISE_CANCEL = os.environ.get("NOISE_CANCEL", "off").lower() in ("1", "on", "true", "yes")


def _turn_tuning():
    """Wait longer at pauses before deciding the caller has finished, and ignore
    one-word interruptions like 'Hello?'. Applied only where this LiveKit version
    supports each option, so it can never stop the agent from starting."""
    mn = float(os.environ.get("MIN_ENDPOINTING", "0.8"))
    mx = float(os.environ.get("MAX_ENDPOINTING", "4.0"))
    words = int(os.environ.get("MIN_INTERRUPTION_WORDS", "2"))
    secs = float(os.environ.get("MIN_INTERRUPTION_SECONDS", "0.8"))
    th_kw, sess_kw = {"turn_detection": inference.TurnDetector()}, {}
    try:
        th_params = inspect.signature(TurnHandlingOptions).parameters
    except Exception:
        th_params = {}
    try:
        s_params = inspect.signature(AgentSession.__init__).parameters
    except Exception:
        s_params = {}
    for key, val in (("min_endpointing_delay", mn), ("max_endpointing_delay", mx),
                     ("min_interruption_words", words), ("min_interruption_duration", secs)):
        if key in th_params:
            th_kw[key] = val
        elif key in s_params:
            sess_kw[key] = val
    try:
        th = TurnHandlingOptions(**th_kw)
    except Exception as e:
        logger.warning(f"turn tuning rejected ({e}); using defaults")
        th, sess_kw = TurnHandlingOptions(turn_detection=inference.TurnDetector()), {}
    applied = {k: v for k, v in th_kw.items() if k != "turn_detection"}
    applied.update(sess_kw)
    logger.info(f"turn tuning applied: {applied or 'none supported by this version'}")
    return th, sess_kw


def _session_limits():
    """Cap tool steps per turn so the model can never loop. Only passed if this
    LiveKit version supports the option, so older versions still start."""
    try:
        params = inspect.signature(AgentSession.__init__).parameters
    except Exception:
        return {}
    steps = int(os.environ.get("MAX_TOOL_STEPS", "3"))
    return {"max_tool_steps": steps} if "max_tool_steps" in params else {}


# A call must never run forever: hang up after a long silence or a time limit.
MAX_CALL_SECONDS = int(os.environ.get("MAX_CALL_SECONDS", "480"))
IDLE_HANGUP_SECONDS = int(os.environ.get("IDLE_HANGUP_SECONDS", "90"))


def _room_options():
    if NOISE_CANCEL:
        return room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S)))
    return room_io.RoomOptions()


# ---- deterministic screen control from the caller's own words -----------
# Only unambiguous nouns switch the business. Words like "room", "stay",
# "table" or "ticket" on their own are too common mid-booking to trust.
STRONG_DOMAIN_WORDS = {
    "restaurant": "restaurant", "restaurants": "restaurant",
    "hotel": "hotel", "hotels": "hotel",
    "hospital": "hospital", "hospitals": "hospital",
    "doctor": "hospital", "doctors": "hospital", "clinic": "hospital",
    "flight": "flight", "flights": "flight",
    "airline": "flight", "airlines": "flight", "plane": "flight",
}
COMPARE_RX = re.compile(r"\b(compare|comparison|cheapest|cheaper|best|nearest|closest|"
                        r"top rated|highest rated|which (one|is))\b")
LIST_RX = re.compile(r"\b(list|options|available|what do you have|what have you got|"
                     r"show me all|show all|any other|other|what else|more options|show more)\b")


def domain_from_speech(text):
    t = (text or "").lower()
    if re.search(r"\b(reserve|book|get)\b[^.?!]*\btable\b", t):
        return "restaurant"
    hits = [STRONG_DOMAIN_WORDS[w] for w in re.findall(r"[a-z]+", t) if w in STRONG_DOMAIN_WORDS]
    return hits[-1] if hits else None          # last one wins: "no, a flight"


EMERGENCY_RX = re.compile(r"chest pain|heart attack|can'?t breathe|breathing|bleed|faint|unconscious|"
                          r"stroke|seizure|fits|accident|emergency|collapsed|not breathing")
# anything that looks like booking details -> the LLM must answer
BOOKING_RX = re.compile(r"\b(yes|yeah|yep|confirm|book it|go ahead|name is|my name|phone|number|"
                        r"tomorrow|today|tonight|morning|evening|afternoon|monday|tuesday|wednesday|"
                        r"thursday|friday|saturday|sunday|am|pm|guests?|people|persons?|nights?|"
                        r"cancel|how much|change|instead of)\b|\d")
SYMPTOM_RX = re.compile(r"chest pain|heart attack|can'?t breathe|cannot breathe|not breathing|"
                        r"trouble breathing|breathing problem|heavy bleeding|bleeding (a lot|heavily|badly)|"
                        r"unconscious|fainted|collapsed|stroke|seizure|choking|severe (injury|pain|burn)|accident")
EMERGENCY_ASK_RX = re.compile(r"\bemergency\b.*\b(book|booking|appointment|help|ambulance|need|urgent|now|send)\b|"
                              r"\b(book|booking|need|help|send|get)\b.*\bemergency\b|\bambulance\b")
YES_RX = re.compile(r"\b(yes|yeah|yep|yup|confirm|confirmed|book it|go ahead|sure|okay|ok|"
                    r"please do|do it|that'?s right|correct|absolutely)\b")
NO_RX = re.compile(r"\b(no|nope|not|don'?t|wait|hold on|cancel|change|stop|instead|but|however|although)\b")
BYE_RX = re.compile(r"\b(bye|goodbye|good bye|that'?s all|end the call|hang up|nothing else|see you)\b")
TRAILING = {"and", "but", "or", "of", "on", "at", "in", "for", "to", "the", "a", "an", "my", "is",
            "was", "which", "with", "from", "about", "then", "so", "because", "like", "um", "uh",
            "er", "would", "be", "booking", "will", "i", "we", "our", "your", "as", "by", "that", "also"}


def looks_unfinished(text):
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.endswith((",", "-", "\u2026", "...")):
        return True
    if t.endswith((".", "?", "!")):
        return False                      # the recogniser marked it as a full sentence
    words = re.findall(r"[a-z']+", t)
    return bool(words) and words[-1] in TRAILING
DOCTORS_RX = re.compile(r"\b(doctors|specialists|departments|which doctor)\b")
_KB_CACHE = {}


def _kb_of(domain_id):
    if domain_id not in _KB_CACHE:
        try:
            _KB_CACHE[domain_id] = load_domain(domain_id)[1]
        except Exception:
            _KB_CACHE[domain_id] = ""
    return _KB_CACHE[domain_id]



# ============================================================================
# Scripted hotel booking: every question and read-back comes from the data.
# No model calls. Anything off-script falls back to the model safely.
# ============================================================================
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10, "a couple": 2, "couple": 2, "single": 1, "a": 1}
_NUM_RX = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|a couple|couple|single|a)"
_ORD = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"], 1)}
_MONTHS.update({"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
                "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12})
_UNITS = ["", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"]
_DAYWORD = {}
for _i, _w in enumerate(["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
                         "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth",
                         "seventeenth", "eighteenth", "nineteenth", "twentieth"], 1):
    _DAYWORD[_w] = _i
for _i in range(1, 10):
    _DAYWORD["twenty " + _UNITS[_i]] = 20 + _i
_DAYWORD.update({"thirtieth": 30, "thirty first": 31})
BOOK_START_RX = re.compile(r"\b(book|reserve)\b")


def _n(word):
    word = word.lower()
    return int(word) if word.isdigit() else _NUM.get(word)


def _parse_people(low):
    m = re.search(r"\bfor\s+" + _NUM_RX + r"\b(?!\s*(nights?|days?))", low) or \
        re.search(_NUM_RX + r"\s+(people|persons|guests|adults|of us)\b", low)
    v = _n(m.group(1)) if m else None
    return v if v and v > 0 else None


def _parse_nights(low):
    if re.search(r"\b(a|one) night\b|\bovernight\b", low):
        return 1
    m = re.search(_NUM_RX + r"\s+(nights?|days?)\b", low)
    v = _n(m.group(1)) if m else None
    return v if v and v > 0 else None


def _parse_date(low, today):
    if "day after tomorrow" in low:
        return today + datetime.timedelta(days=2)
    if "tomorrow" in low:
        return today + datetime.timedelta(days=1)
    if re.search(r"\b(today|tonight)\b", low):
        return today
    day = month = None
    for word, num in sorted(_DAYWORD.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"\b" + word + r"\b", low):
            day = num
            break
    m = re.search(r"\b(\d{1,2})(st|nd|rd|th)?\b", low)
    if day is None and m and re.search(r"\b(of|on|the)\b|" + "|".join(_MONTHS), low):
        day = int(m.group(1))
    for name, num in _MONTHS.items():
        if re.search(r"\b" + name + r"\b", low):
            month = num
            break
    if day:
        month = month or today.month
        try:
            d = datetime.date(today.year, month, day)
        except ValueError:
            return None
        if d < today:
            try:
                d = datetime.date(today.year + (1 if month < today.month or month == 12 else 0),
                                  month if month >= today.month or month == 12 else month, day)
            except ValueError:
                return None
        return d if d >= today else None
    for i, wd in enumerate(_WEEKDAYS):
        if re.search(r"\b" + wd + r"\b", low):
            ahead = (i - today.weekday()) % 7 or 7
            return today + datetime.timedelta(days=ahead)
    return None


def _extract_name(text):
    t = re.sub(r"(?i)\b(his|her|their|the)?\s*(second|third|fourth|fifth|other|first)?\s*"
               r"(guest'?s?|person'?s?|one'?s?)?\s*name\s*(is|'s)\b", " ", text)
    t = re.sub(r"(?i)\b(it'?s|it is|she is|he is|that'?s|this is|call (him|her)|"
               r"my (friend|wife|husband|brother|sister|mother|father|mom|dad|son|daughter|colleague)"
               r"( is)?)\b", " ", t)
    skip = {"and", "the", "is", "name", "yes", "okay", "ok", "please", "guest", "second", "person",
            "she", "he", "it", "her", "his", "sure", "so", "um", "uh"}
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", t) if w.lower() not in skip]
    return " ".join(w[:1].upper() + w[1:].lower() for w in words[:3]) or None


def _match_room(low, rooms):
    best, score = None, 0
    for r in rooms:
        words = [w for w in re.findall(r"[a-z]+", r["name"].lower()) if w not in ("room", "the")]
        hit = sum(1 for w in words if re.search(r"\b" + w + r"\b", low))
        if hit > score:
            best, score = r, hit
    return best


def _say_n(n):
    return {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}.get(n, str(n))


def _next_step(bk):
    rooms = bk["rooms"]
    if bk["people"] is None:
        return "people", "How many guests, including you?"
    if bk["incl"] is None:
        return "incl", f"{_say_n(bk['people']).capitalize()} guests. Does that include you?"
    need = bk["people"] - 1 if bk["incl"] else bk["people"]
    if len(bk["companions"]) < need:
        idx = len(bk["companions"]) + (1 if bk["incl"] else 0)
        return "name", f"What's the name of the {_ORD[idx]} guest?"
    if not bk["room"]:
        names = [r["name"] for r in rooms]
        choice = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
        return "room", f"Which room would you like: {choice}?"
    if not bk["date"]:
        return "date", "When are you checking in, and for how many nights?"
    if not bk["nights"]:
        return "nights", "For how many nights?"
    return "confirm", None


def _quote(bk, call_ctx):
    room, nights, people = bk["room"], bk["nights"], bk["people"]
    cap = room.get("capacity") or people
    rooms_needed = max(1, -(-people // cap))
    subtotal = room["price"] * nights * rooms_needed
    status = ((call_ctx.get("profile") or {}).get("status") or "").lower()
    kind = {"student": "student", "professional": "corporate"}.get(status)
    pct = agent_tools.listed_rate(DOMAIN.kb_text, bk["place"], kind, room["name"]) if kind else 0
    disc = round(subtotal * pct / 100)
    final = subtotal - disc
    call_ctx["last_total"] = final
    call_ctx["last_bill"] = {"items": [{"name": f"{room['name']} ({nights} night{'s' if nights > 1 else ''})",
                                        "unit_price": room["price"], "quantity": nights * rooms_needed}],
                             "subtotal": subtotal, "discount": disc, "service": 0, "tax": 0,
                             "final": final, "currency": DOMAIN.currency}
    when = bk["date"].strftime("%A %d %B").replace(" 0", " ")
    s = (f"{room['name']} at {agent_tools.short_name(bk['place'])}, {_say_n(rooms_needed)} room"
         f"{'s' if rooms_needed > 1 else ''} for {_say_n(nights)} night{'s' if nights > 1 else ''} "
         f"from {when}: {subtotal} rupees.")
    if pct:
        s += f" With your {pct} percent {kind} discount, it's {final} rupees."
    elif kind:
        s += f" There's no {kind} discount on this room."
    return s + " Shall I book it?"


def booking_turn(text, call_ctx):
    """One turn of the scripted booking. Returns Maya's line, or None to hand
    the turn to the model (the flow is then dropped)."""
    bk = call_ctx["bk"]
    low = text.lower()
    step = bk.get("step")
    if re.search(r"\b(cancel|never mind|forget it|stop)\b", low):
        call_ctx.pop("bk", None)
        return "No problem, I've cancelled that. Anything else?"
    yes = bool(YES_RX.search(low)) and not NO_RX.search(low)
    no = bool(re.search(r"\b(no|nope|not)\b", low))

    if step == "rating":
        m = re.search(r"\b([1-5]|one|two|three|four|five)\b", low)
        call_ctx.pop("bk", None)
        if not m:
            return None
        r = _n(m.group(1))
        agent_tools.set_rating(call_ctx.get("call_id", "local"), r)
        call_ctx["rating"] = r
        return f"Thank you for rating us {_say_n(r)}! Is there anything else I can help with?"

    if step == "confirm":
        if yes:
            prof = call_ctx.get("profile") or {}
            customer = prof.get("name") or (call_ctx.get("user_email") or "Guest").split("@")[0]
            d, n = bk["date"], bk["nights"]
            ref = agent_tools.commit_booking(
                DOMAIN, call_ctx, f"{agent_tools.short_name(bk['place'])} - {bk['room']['name']}",
                f"{d:%Y-%m-%d} check-in, {n} night{'s' if n > 1 else ''}",
                max(1, -(-bk["people"] // (bk["room"].get("capacity") or bk["people"]))),
                customer, prof.get("mobile") or "", f"{d:%Y-%m-%d}T14:00", ", ".join(bk["companions"]))
            bk["step"] = "rating"
            return (f"Booked! Your reference is {' '.join(ref.replace('-', ''))}. The confirmation and receipt are on "
                    f"their way. How would you rate your experience today, from one to five?")
        if no:
            call_ctx.pop("bk", None)
            return "Okay, I haven't booked it. What would you like to change?"
        return None          # a side question: the model answers, the booking stays ready

    # fill anything the caller mentioned, whatever the question was
    before = (bk["people"], bk["incl"], len(bk["companions"]), bk["room"], bk["date"], bk["nights"])
    if step == "name":
        nm = _extract_name(text)
        if nm:
            bk["companions"].append(nm)
    else:
        p = _parse_people(low)
        if step == "people" and p is None:
            m = re.search(r"\b" + _NUM_RX + r"\b", low)
            p = _n(m.group(1)) if m else None
            if p:
                bk["incl"] = True                       # the question said 'including you'
        if p:
            bk["people"] = p
        if re.search(r"\bnot including me\b|\bwithout me\b|\bexcluding me\b", low):
            bk["incl"] = False
        elif re.search(r"\bincluding (me|myself)\b|\b(me|myself) and\b|\bwith me\b", low):
            bk["incl"] = True
        elif step == "incl":
            if no and not yes:
                bk["incl"] = False
            elif yes:
                bk["incl"] = True
        r = _match_room(low, bk["rooms"])
        if r:
            bk["room"] = r
        d = _parse_date(low, datetime.date.today())
        if d:
            bk["date"] = d
        n = _parse_nights(low)
        if n:
            bk["nights"] = n
    after = (bk["people"], bk["incl"], len(bk["companions"]), bk["room"], bk["date"], bk["nights"])

    if after == before and step == "room" and re.search(r"\b(cheapest|cheaper|lowest|budget)\b", low):
        bk["room"] = min(bk["rooms"], key=lambda r: r["price"])
        after = (bk["people"], bk["incl"], len(bk["companions"]), bk["room"], bk["date"], bk["nights"])
    if after == before and (text.strip().endswith("?") or re.match(
            r"(?i)\s*(is|are|does|do|can|could|what|which|how|when|where|will|would)\b", text)):
        return None          # a side question: the model answers, the booking stays open
    if after == before:
        bk["misses"] = bk.get("misses", 0) + 1
        if bk["misses"] >= 2:
            call_ctx.pop("bk", None)
            return None
    else:
        bk["misses"] = 0
    step, question = _next_step(bk)
    bk["step"] = step
    if step == "confirm":
        return _quote(bk, call_ctx)
    return question


def start_booking(text, call_ctx, place_name):
    rooms = agent_tools.block_items(DOMAIN.kb_text, place_name)
    if not rooms:
        return None
    call_ctx["bk"] = {"domain": DOMAIN.id, "place": place_name, "rooms": rooms, "people": None,
                      "incl": None, "companions": [], "room": None, "date": None, "nights": None,
                      "step": "start"}
    call_ctx["focus_place"] = place_name
    logger.info(f"booking flow started: {place_name}")
    return booking_turn(text, call_ctx)


WAIT = "__WAIT__"


def route_turn(text, call_ctx):
    """Handle navigation straight from the knowledge files: update the screen and
    return a line for Maya to speak, with NO LLM call. Returns None when the LLM
    should answer (booking details, questions, emergencies)."""
    # Join a sentence the caller left unfinished with what they say next.
    text = (call_ctx.pop("pending_text", "") + " " + (text or "")).strip()
    low = text.lower()
    call_ctx["heard"] = (call_ctx.get("heard", "") + " " + low)[-4000:]
    call_ctx["said_bye"] = bool(BYE_RX.search(low))
    # The booking gate in create_booking reads this flag. A yes only counts once a
    # total has been read back; anything hesitant or negative clears it.
    said_yes = bool(YES_RX.search(low)) and not NO_RX.search(low) and not call_ctx["said_bye"]
    call_ctx["confirmed"] = said_yes and call_ctx.get("last_total") is not None
    if call_ctx["confirmed"]:
        logger.info(f"router: caller confirmed  ({text!r})")
    if not low.strip():
        return None
    # EMERGENCY: book the nearest 24-hour emergency department for the logged-in
    # caller and alert their emergency contact - no questions, no model call.
    if SYMPTOM_RX.search(low) or EMERGENCY_ASK_RX.search(low):
        call_ctx.pop("pending_text", None)
        logger.info(f"router: EMERGENCY  ({text!r})")
        return agent_tools.trigger_emergency(DOMAIN, call_ctx, text)
    if EMERGENCY_RX.search(low):
        return None                          # e.g. 'emergency department hours' -> model
    # 'did you send the confirmation?' -> answer from what actually happened
    if re.search(r"\bdid you\b.*\b(send|sent|email|mail|message|alert|notif)", low) or \
       re.search(r"\b(confirmation|alert)\b.*\b(sent|send)\b.*\?", low):
        ans = agent_tools.confirmation_answer(call_ctx)
        if ans:
            return ans
    if looks_unfinished(text):
        call_ctx["pending_text"] = text
        call_ctx["pending_since"] = time.monotonic()
        logger.info(f"router: waiting for the rest  ({text!r})")
        return WAIT
    # scripted hotel booking in progress?
    bk = call_ctx.get("bk")
    if bk and bk.get("domain") == DOMAIN.id:
        other_type = domain_from_speech(text)
        leaving = (bool(re.search(r"\bcompare\b|\bshow (me )?(all|the list)\b|\bother (hotels?|places|options)\b", low))
                   or (other_type is not None and other_type != DOMAIN.id)) \
            and bk.get("step") not in ("confirm", "rating")
        other = agent_tools.find_place(text, DOMAIN.kb_text)
        if leaving or (other and other["name"] != bk["place"]):
            call_ctx.pop("bk", None)
        else:
            line = booking_turn(text, call_ctx)
            if line:
                return line
            return None
    if DOMAIN.id == "hotel" and BOOK_START_RX.search(low):
        pl = agent_tools.find_place(text, DOMAIN.kb_text)
        name = pl["name"] if pl else call_ctx.get("focus_place")
        if name and agent_tools.block_items(DOMAIN.kb_text, name):
            if pl:
                agent_tools.write_view(DOMAIN.id, "detail", name)
            line = start_booking(text, call_ctx, name)
            if line:
                return line
    wants_llm = bool(BOOKING_RX.search(low))

    place, pdom = None, None
    b = agent_tools.find_place(text, DOMAIN.kb_text)
    if b:
        place, pdom = b, DOMAIN.id
    else:
        for d in agent_tools.DOMAIN_IDS:
            if d != DOMAIN.id:
                b = agent_tools.find_place(text, _kb_of(d))
                if b:
                    place, pdom = b, d
                    break

    target = pdom if place else domain_from_speech(text)
    if target:
        if target != DOMAIN.id:
            DOMAIN.switch_to(target)
            agent_tools.set_call_domain(call_ctx.get("call_id", "local"), target)
            call_ctx["domain_id"] = target
        agent_tools.write_active_domain(target)

    if place:
        agent_tools.write_view(DOMAIN.id, "detail", place["name"])
        call_ctx["focus_place"] = place["name"]
        logger.info(f"router: detail {place['name']}  ({text!r})")
        if wants_llm or re.search(r"\b(book|reserve|appointment for)\b", low):
            return None
        return agent_tools.say_detail(DOMAIN.id, place, DOMAIN.kb_text)

    if DOMAIN.id == "hospital" and DOCTORS_RX.search(low):
        agent_tools.write_view("hospital", "compare", "doctors")
        logger.info(f"router: doctors  ({text!r})")
        return None if wants_llm else "Every department and doctor is on your screen. Which specialty do you need?"

    if COMPARE_RX.search(low):
        agent_tools.write_view(DOMAIN.id, "compare")
        logger.info(f"router: compare  ({text!r})")
        return None if wants_llm else agent_tools.say_compare(DOMAIN.id, DOMAIN.kb_text, text)

    if LIST_RX.search(low):
        agent_tools.write_view(DOMAIN.id, "list")
        logger.info(f"router: list  ({text!r})")
        return None if wants_llm else agent_tools.say_list(DOMAIN.id, DOMAIN.kb_text)

    if target:
        agent_tools.write_view(target, "list")
        logger.info(f"router: open {target}  ({text!r})")
        return None if wants_llm else agent_tools.say_intro(target, DOMAIN.kb_text)
    return None


class Assistant(Agent):
    def __init__(self, tools, call_ctx=None) -> None:
        self._call_ctx = call_ctx if call_ctx is not None else {}
        super().__init__(
            llm=_make_llm(),
            instructions=f"Today is {datetime.date.today():%A, %d %B %Y}.\n" + textwrap.dedent(
                """\
                You are Maya, a fast, professional voice booking agent for restaurants,
                hospitals, hotels and airlines. Prices are in Indian rupees. Live call.

                VOICE: under 25 words per reply. Plain speech only, no lists, symbols or
                links. No filler like "sure thing" or "one moment". Get to the point.

                EMERGENCY FIRST: chest pain, trouble breathing, heavy bleeding, fainting,
                stroke signs, seizure or severe injury -> call emergency_alert at once and
                repeat its advice calmly, always including 108. Never diagnose.

                TOOL DISCIPLINE: call a tool only for what the caller just asked. Never
                search speculatively or ahead of time. If a tool fails, tell the caller
                instead of retrying it.

                NAVIGATION IS HANDLED FOR YOU: opening a business, listing, comparing and
                opening a named place are answered automatically, and the reply is already in
                the conversation. Continue from it; never repeat it.

                THE SCREEN FOLLOWS THE CALLER BY ITSELF when they say restaurant, hotel,
                hospital, doctor, flight or airline, or ask to list or compare. Do not call
                switch_domain for those words.

                FLOW:
                1. They name a business type or ask what is available: search_knowledge,
                   then name two or three options with price and rating. Do not ask for
                   dates, guests or names yet.
                2. They ask about ONE specific place: call show_detail with its name. Give
                   one key fact, say it is on their screen, and ask "Shall I book it?"
                3. Only after they choose to book: the caller is logged in, so you already
                   have their name and phone - never ask for them. Ask the date or time and how
                   many people. If others are joining, ask only those people's names and pass
                   them as companions. Leave customer empty to use the profile. Call calculate_total, passing discount_percent
                   with the listed student or corporate rate if it applies. Read back the
                   total and ask for a clear yes.
                4. CONFIRMATION GATE: call create_booking ONLY after a clear yes, confirm,
                   book it or go ahead. It books AND sends the confirmation. Pass start_iso
                   as an ISO date-time worked out from today's date, e.g. 2026-09-28T10:00. Then say:
                   "Booked. Your reference is" and the reference, and that the
                   confirmation is on its way.
                5. Ask for a rating from one to five and call capture_rating. Only when the
                   caller says goodbye, give a one-line farewell and call end_call_tool.

                LISTEN FIRST: callers pause mid-sentence. If their words look unfinished,
                say "go on" instead of acting. Never fill in details they did not say.
                create_booking refuses until it has a listed place, the caller's real name,
                a total you read back, and their yes after hearing it. If it refuses, ask
                for exactly what is missing.

                FACTS: never guess a price, name or availability. If a place is not
                listed, say so and name ones that are. For hospitals, name the doctor, fee
                and days. Ask student or working professional when it changes the price.
                """
            ),
            tools=tools,
        )

    async def on_enter(self) -> None:
        # Fixed greeting: instant, and costs no LLM tokens.
        self.session.say("Hi, I'm Maya. I can book a restaurant table, a doctor's appointment, "
                         "a hotel room or a flight. Which would you like?")

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        text = (getattr(new_message, "text_content", None) or "").strip()
        try:
            spoken = route_turn(text, self._call_ctx)
        except Exception as e:
            logger.warning(f"router failed, handing to LLM: {e}")
            return
        if spoken == WAIT and StopResponse is not None:
            raise StopResponse()                 # stay quiet; they're mid-sentence
        if spoken and spoken != WAIT and StopResponse is not None:
            logger.info(f"router answered without LLM: {spoken[:70]!r}")
            self.session.say(spoken)
            raise StopResponse()


server = AgentServer()


@server.rtc_session()
async def my_agent(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}

    agent_tools.init_db()
    call_id = "call-" + uuid.uuid4().hex[:8]
    call_ctx = {"call_id": call_id, "domain_id": DOMAIN.id}
    agent_tools.start_call(call_id, DOMAIN.id)

    tools = agent_tools.build_tools(DOMAIN, call_ctx)

    _TH, _SESS_TUNING = _turn_tuning()
    session = AgentSession(
        stt=inference.STT(model="deepgram/nova-3", language=os.environ.get("STT_LANGUAGE", "en")),
        tts=inference.TTS(model="cartesia/sonic-3", voice="9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"),
        turn_handling=_TH,
        # Off by default: it starts the LLM early, spending tokens even on turns
        # the router answers itself. PREEMPTIVE=on trades tokens for speed.
        preemptive_generation=os.environ.get("PREEMPTIVE", "off").lower() in ("1", "on", "true"),
        **_session_limits(),
        **_SESS_TUNING,
    )

    @session.on("user_input_transcribed")
    def _heard(ev):
        if getattr(ev, "is_final", False):
            call_ctx["last_heard"] = time.monotonic()

    async def _on_shutdown():
        agent_tools.end_call(call_id, tools_called=call_ctx.get("tools_called", []),
                             rating=call_ctx.get("rating"))
        logger.info(f"call {call_id} ended; tools: {call_ctx.get('tools_called')}")

    ctx.add_shutdown_callback(_on_shutdown)

    call_ctx["last_heard"] = time.monotonic()

    async def _watchdog():
        began = time.monotonic()
        while True:
            await asyncio.sleep(1.5)
            now = time.monotonic()
            # they stopped mid-sentence and went quiet: answer what we have
            if call_ctx.get("pending_text") and now - call_ctx.get("last_heard", now) > 3.5:
                call_ctx.pop("pending_text", None)
                logger.info("router: caller went quiet mid-sentence, replying")
                try:
                    session.generate_reply()
                except Exception as e:
                    logger.warning(f"pending reply failed: {e}")
                continue
            if now - began > MAX_CALL_SECONDS:
                reason = f"time limit of {MAX_CALL_SECONDS}s"
            elif now - call_ctx.get("last_heard", now) > IDLE_HANGUP_SECONDS:
                reason = f"{IDLE_HANGUP_SECONDS}s of silence"
            else:
                continue
            logger.info(f"auto-hangup: {reason}")
            try:
                await session.say("I'll end the call here. Thank you for calling, goodbye.",
                                  allow_interruptions=False)
                await asyncio.sleep(1.5)
            except Exception:
                pass
            try:
                ctx.shutdown(reason=reason)
            except Exception:
                await ctx.room.disconnect()
            return

    await session.start(
        agent=Assistant(tools, call_ctx),
        room=ctx.room,
        room_options=_room_options(),
    )

    await ctx.connect()
    asyncio.create_task(_watchdog())
    try:
        who = await asyncio.wait_for(ctx.wait_for_participant(), timeout=10)
        call_ctx["user_email"] = who.identity
        prof = agent_tools.load_profile(who.identity)
        if prof:
            call_ctx["profile"] = prof
            logger.info(f"caller: {prof.get('name')} <{who.identity}> "
                        f"emergency contact: {prof.get('emergency_name') or 'not set'}")
    except Exception as e:
        logger.warning(f"could not load caller profile: {e}")


if __name__ == "__main__":
    cli.run_app(server)

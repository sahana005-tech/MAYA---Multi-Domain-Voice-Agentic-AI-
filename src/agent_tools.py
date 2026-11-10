import os
import re
import datetime
import string
import json
import time
import uuid
import sqlite3
import logging
import threading
from contextlib import contextmanager

from livekit.agents import function_tool, RunContext
import messaging

logger = logging.getLogger("agent.tools")

DB_PATH = os.path.join(os.path.dirname(__file__), "voice_agent.db")

# --- multi-domain paths (added) ---------------------------------------
DOMAIN_IDS = ("restaurant", "hospital", "hotel", "flight")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOMAINS_DIR = os.path.join(PROJECT_ROOT, "domains")
ACTIVE_FILE = os.path.join(PROJECT_ROOT, "active_domain.txt")
VIEW_FILE = os.path.join(PROJECT_ROOT, "view_state.json")

# words that mean "show me everything", not "find one fact"
BROAD_WORDS = ("all", "list", "available", "availability", "options", "option",
               "what do you have", "compare", "comparison", "cheapest", "cheap",
               "best", "nearest", "closest", "near", "recommend", "suggest",
               "student", "which", "any", "other", "others", "more", "else",
               "rating", "rated", "review", "reviews", "budget",
               "do you have", "have you got", "hotels", "restaurants", "hospitals",
               "airlines", "flights", "doctors", "places", "show me", "tell me about",
               "anything", "somewhere")
STOP = {"the", "and", "for", "you", "your", "are", "can", "what", "with", "have",
        "has", "does", "how", "about", "there", "this", "that", "from", "would",
        "like", "want", "need", "tell", "show", "give", "please", "hotel",
        "hotels", "restaurant", "restaurants"}


NAME_STOP = {"the", "hotel", "restaurant", "hospital", "airline", "airlines",
             "clinic", "centre", "center", "resort", "open", "show", "one", "please"}


def business_names(kb_text):
    """Every listed business, taken from headings like 'HARBOUR VIEW RESIDENCY, FORT KOCHI - RATING 4.5 ...:'"""
    names = []
    for line in (kb_text or "").splitlines():
        t = line.strip()
        if t.endswith(":") and " - RATING" in t.upper():
            names.append(string.capwords(t.split(" - ")[0].split(",")[0].strip().lower()))
    return names


def match_business(query, kb_text):
    """Best listed business for what the caller said, or None if nothing fits."""
    words = [w for w in re.findall(r"[a-z0-9']+", (query or "").lower())
             if len(w) > 2 and w not in NAME_STOP]
    best, best_score = None, 0
    for n in business_names(kb_text):
        nw = set(re.findall(r"[a-z0-9']+", n.lower()))
        score = sum(1 for w in words if w in nw)
        if score > best_score:
            best, best_score = n, score
    return best


def emergency_hospitals(kb_text):
    """[(km, name), ...] for listings with a 24 hour emergency department, nearest first."""
    out = []
    for block in (kb_text or "").split("\n\n"):
        lines = block.strip().splitlines()
        if not lines or " - RATING" not in lines[0].upper():
            continue
        body = block.lower()
        if "emergency" in body and "24 hours" in body and "no emergency" not in body:
            head = lines[0]
            name = string.capwords(head.split(" - ")[0].split(",")[0].strip().lower())
            m = re.search(r"([\d.]+)\s*km", head, re.I)
            out.append((float(m.group(1)) if m else 999.0, name))
    return sorted(out)


EVENT_MINUTES = {"hospital": 30, "restaurant": 90, "hotel": 60, "flight": 90}


def calendar_link(title, start_iso, minutes, location, details):
    """One-tap 'Add to Google Calendar' link, pre-filled. No API or login needed."""
    from urllib.parse import quote
    url = ("https://calendar.google.com/calendar/render?action=TEMPLATE"
           f"&text={quote(title)}&details={quote(details)}&location={quote(location)}")
    if start_iso:
        try:
            st = datetime.datetime.fromisoformat(start_iso.strip().replace("Z", ""))
            en = st + datetime.timedelta(minutes=minutes)
            f = "%Y%m%dT%H%M%S"
            url += f"&dates={st.strftime(f)}/{en.strftime(f)}&ctz=Asia/Kolkata"
        except Exception:
            pass
    return url


def write_view(domain_id, mode, focus=""):
    """Tell the browser what to put on screen. The page polls this."""
    try:
        with open(VIEW_FILE, "w") as f:
            json.dump({"domain": domain_id, "mode": mode,
                       "focus": focus, "ts": time.time()}, f)
    except Exception as e:
        logger.warning(f"write_view failed: {e}")

# words a caller might actually say -> domain id
DOMAIN_ALIASES = {
    "restaurant": "restaurant", "restaurants": "restaurant", "food": "restaurant",
    "dinner": "restaurant", "lunch": "restaurant", "table": "restaurant",
    "dining": "restaurant", "eat": "restaurant", "meal": "restaurant",
    "hospital": "hospital", "hospitals": "hospital", "doctor": "hospital",
    "clinic": "hospital", "appointment": "hospital", "medical": "hospital",
    "health": "hospital", "consultation": "hospital",
    "hotel": "hotel", "hotels": "hotel", "room": "hotel", "rooms": "hotel",
    "stay": "hotel", "accommodation": "hotel", "lodging": "hotel",
    "flight": "flight", "flights": "flight", "plane": "flight",
    "ticket": "flight", "tickets": "flight", "airline": "flight",
    "fly": "flight", "flying": "flight", "air": "flight",
}


def resolve_domain(text):
    """Map whatever the caller said onto one of the four domain ids."""
    t = (text or "").strip().lower()
    if t in DOMAIN_ALIASES:
        return DOMAIN_ALIASES[t]
    for word in t.replace(",", " ").split():
        if word in DOMAIN_ALIASES:
            return DOMAIN_ALIASES[word]
    return None


def write_active_domain(domain_id):
    with open(ACTIVE_FILE, "w") as f:
        f.write(domain_id)


def set_call_domain(call_id, domain_id):
    with _conn() as c:
        c.execute("UPDATE calls SET domain_id=? WHERE id=?", (domain_id, call_id))


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, domain_id TEXT, status TEXT DEFAULT 'active',
                task_completed INTEGER DEFAULT 0, tools_called TEXT DEFAULT '[]',
                rating INTEGER, transcript TEXT DEFAULT '',
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, ended_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bookings (
                ref_id TEXT PRIMARY KEY, call_id TEXT, domain_id TEXT,
                service_id TEXT, datetime TEXT, quantity INTEGER,
                customer TEXT, total REAL, status TEXT DEFAULT 'confirmed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def start_call(call_id, domain_id):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO calls (id, domain_id, status) VALUES (?,?, 'active')",
                  (call_id, domain_id))


def mark_completed(call_id, tools_called):
    with _conn() as c:
        c.execute("UPDATE calls SET task_completed=1, status='ended', tools_called=?, ended_at=CURRENT_TIMESTAMP WHERE id=?",
                  (json.dumps(tools_called), call_id))


def end_call(call_id, tools_called, rating=None, transcript=""):
    with _conn() as c:
        c.execute("""UPDATE calls SET status='ended', tools_called=?, rating=?,
                     transcript=?, ended_at=CURRENT_TIMESTAMP WHERE id=?""",
                  (json.dumps(tools_called), rating, transcript, call_id))


def set_rating(call_id, rating):
    with _conn() as c:
        c.execute("UPDATE calls SET rating=? WHERE id=?", (rating, call_id))


def _log_tool(call_ctx, name):
    tc = call_ctx.setdefault("tools_called", [])
    if name not in tc:
        tc.append(name)


def _send_sms(text, to=None):
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_SMS_FROM")
    to = to or os.environ.get("TWILIO_SMS_TO")
    if not all([sid, token, frm, to]):
        return False, "twilio sms not configured"
    try:
        from twilio.rest import Client
        Client(sid, token).messages.create(body=text, from_=frm, to=to)
        return True, "sent"
    except Exception as e:
        return False, str(e)


class DomainConfig:
    def __init__(self, cfg: dict, kb_text: str):
        self.id = cfg.get("domain_id", "restaurant")
        self.display_name = cfg.get("display_name", self.id)
        self.currency = cfg.get("currency", "INR")
        self.pricing_rules = cfg.get("pricing_rules", {})
        self.confirmation_template = cfg.get(
            "confirmation_template", "Booking {ref_id} confirmed - {summary}")
        self.kb_text = kb_text

    def switch_to(self, domain_id):
        """Reload this object in place so tool closures see the new business."""
        with open(os.path.join(DOMAINS_DIR, domain_id, "config.json")) as f:
            cfg = json.load(f)
        with open(os.path.join(DOMAINS_DIR, domain_id, "kb.txt")) as f:
            kb = f.read()
        self.__init__(cfg, kb)
        return self


def build_tools(domain: DomainConfig, call_ctx: dict):

    @function_tool
    async def search_knowledge(context: RunContext, query: str=""):
        """Look up places, prices, ratings, reviews and policies.

        Args:
            query: e.g. 'all hotels', 'cheapest for a student', 'Harbour View rooms'
        """
        _log_tool(call_ctx, "search_knowledge")
        text = domain.kb_text or ""
        if not query:
            return "What would you like to know?"

        paras = [p.strip() for p in text.split("\n\n") if p.strip()
                 and "how to help a caller" not in p.lower()]
        q = query.lower()
        q_words = [w for w in re.findall(r"[a-z0-9]+", q)
                   if len(w) > 2 and w not in STOP]
        broad = any(k in q for k in BROAD_WORDS)

        scored = []
        for p in paras:
            pl = p.lower()
            score = sum(pl.count(w) for w in q_words)
            if broad and ("rating" in pl or "rupees" in pl):
                score += 2
            if score > 0:
                scored.append((score, p))
        scored.sort(key=lambda t: -t[0])

        if broad:
            # Compact overview: the comparison block plus the key lines of every
            # listing. Mentions every business at a fraction of the tokens.
            KEEP = ("rating", "student", "corporate", "spend", "rooms left",
                    "seats left", "best for", "average wait", "open ")
            top = []
            for p in paras:
                pl = p.lower()
                if "quick comparison" in pl:
                    top.insert(0, p)
                    continue
                lines = p.splitlines()
                if not lines or " - rating" not in lines[0].lower():
                    continue
                keep = [lines[0]] + [ln for ln in lines[1:]
                                     if any(k in ln.lower() for k in KEEP)
                                     or any(w in ln.lower() for w in q_words)]
                top.append("\n".join(keep))
            if not top:
                top = [p for _, p in scored[:4]] or paras[:4]
        else:
            top = [p for _, p in scored[:3]] or paras[:3]

        out = "\n\n".join(top)[:4500]
        logger.info(f"search_knowledge('{query}') broad={broad} -> {len(top)} blocks, {len(out)} chars")
        return out

    @function_tool
    async def quote_booking(context: RunContext, item: str, quantity: int = 1, nights: int = 1,
                            place: str = "", discount_type: str = ""):
        """Price a booking straight from the listing, applying any discount the caller
        qualifies for. Use this instead of searching for prices.

        Args:
            item: what is booked, e.g. 'Standard AC', 'Dermatology', 'Kochi', 'table'
            quantity: rooms, seats, people or dishes
            nights: hotel nights, otherwise 1
            place: the business; leave empty for the one on screen
            discount_type: 'student' or 'corporate' if the caller asks, else empty
        """
        _log_tool(call_ctx, "quote_booking")
        pl = place or call_ctx.get("focus_place", "")
        b = find_place(pl, domain.kb_text) if pl else None
        pname = b["name"] if b else ""
        if not pname:
            return "Ask the caller which place they want first."
        call_ctx["focus_place"] = pname
        items = block_items(domain.kb_text, pname)
        low = (item or "").lower()
        q = max(1, int(quantity or 1))
        if domain.id == "restaurant" and ("table" in low or not low):
            call_ctx["last_total"] = 0
            call_ctx["last_bill"] = {"items": [{"name": f"Table for {q}", "unit_price": 0, "quantity": 1}],
                                     "subtotal": 0, "discount": 0, "service": 0, "tax": 0, "final": 0,
                                     "currency": domain.currency}
            call_ctx["confirmed"] = False
            return (f"A table for {q} at {short_name(pname)} is free to reserve. Read back the date "
                    f"and time and ask for a clear yes.")
        best, score = None, 0
        for it in items:
            words = [w for w in re.findall(r"[a-z]+", it["name"].lower()) if w not in ("room", "the", "to")]
            hit = sum(1 for w in words if re.search(r"\b" + w + r"\b", low))
            if hit > score:
                best, score = it, hit
        if not best:
            return (f"There is no '{item}' at {short_name(pname)}. Options: "
                    + ", ".join(i["name"] for i in items) + ". Ask which one.")
        n = max(1, int(nights or 1)) if domain.id == "hotel" else 1
        sub = best["price"] * q * n
        kind = (discount_type or "").lower().strip()
        kind = "corporate" if kind in ("professional", "working", "company", "employee") else kind
        if kind not in ("student", "corporate"):
            status = ((call_ctx.get("profile") or {}).get("status") or "").lower()
            kind = {"student": "student", "professional": "corporate"}.get(status, "")
        pct = listed_rate(domain.kb_text, pname, kind, best["name"], qty=q, nights=n) if kind else 0
        disc = round(sub * pct / 100)
        final = sub - disc
        call_ctx["last_total"] = final
        call_ctx["last_bill"] = {"items": [{"name": best["name"] + (f" ({n} nights)" if n > 1 else ""),
                                            "unit_price": best["price"], "quantity": q * n}],
                                 "subtotal": sub, "discount": disc, "service": 0, "tax": 0,
                                 "final": final, "currency": domain.currency}
        call_ctx["confirmed"] = False
        s = f"{best['name']} at {short_name(pname)}: {sub} rupees"
        if pct:
            s += f", minus {pct} percent {kind} discount, total {final} rupees"
        elif kind:
            s += f" (no {kind} discount applies to this)"
        return s + ". Read this total to the caller and ask for a clear yes."

    @function_tool
    async def calculate_total(context: RunContext, items: str, modifiers: str = "",
                              discount_percent: float = 0):
        """Price a booking.

        Args:
            items: JSON list like [{"name":"Compact Single","unit_price":1450,"quantity":2}]
            modifiers: optional rule names, e.g. 'member_discount,tax'
            discount_percent: listed student or corporate rate as a number, else 0
        """
        _log_tool(call_ctx, "calculate_total")
        try:
            line_items = json.loads(items) if isinstance(items, str) else items
        except Exception:
            return "I couldn't read the items. Please tell me the dishes and quantities again."
        subtotal = sum(li.get("unit_price", 0) * li.get("quantity", 1) for li in line_items)
        rules = domain.pricing_rules or {}
        cur = domain.currency
        mods = [m.strip() for m in modifiers.split(",") if m.strip()]

        discount_amt = 0.0
        for mod in mods:
            if "discount" in mod and mod in rules:
                discount_amt += subtotal * float(rules[mod])
        try:
            pct = float(discount_percent or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if 0 < pct < 100:
            allowed = allowed_discounts(domain.kb_text, call_ctx.get("focus_place", ""))
            if int(round(pct)) not in allowed:
                logger.info(f"discount {pct}% refused; listed for this place: {sorted(allowed)}")
                call_ctx["discount_note"] = "No such discount is listed for this place, so none was applied."
                pct = 0.0
            else:
                call_ctx["discount_note"] = ""
        if 0 < pct < 100:
            discount_amt += subtotal * pct / 100.0
        after_discount = subtotal - discount_amt

        service_amt = 0.0
        for mod in mods:
            if "service" in mod and mod in rules:
                service_amt += after_discount * float(rules[mod])

        tax_amt = 0.0
        for mod in mods:
            if "tax" in mod and mod in rules:
                tax_amt += (after_discount + service_amt) * float(rules[mod])

        final = round(after_discount + service_amt + tax_amt, 2)
        call_ctx["last_total"] = final
        call_ctx["last_bill"] = {
            "items": [{"name": li.get("name", "item"),
                       "unit_price": li.get("unit_price", 0),
                       "quantity": li.get("quantity", 1)} for li in line_items],
            "subtotal": round(subtotal, 2),
            "discount": round(discount_amt, 2),
            "service": round(service_amt, 2),
            "tax": round(tax_amt, 2),
            "final": final,
            "currency": cur,
        }

        parts = [f"The subtotal is {round(subtotal,2)} {cur}."]
        if discount_amt > 0:
            parts.append(f"A discount of {round(discount_amt,2)} {cur} is applied, bringing it to {round(after_discount,2)} {cur}.")
        if service_amt > 0:
            parts.append(f"A service charge of {round(service_amt,2)} {cur} is added.")
        if tax_amt > 0:
            parts.append(f"Tax of {round(tax_amt,2)} {cur} is added.")
        parts.append(f"The final total is {final} {cur}.")
        if call_ctx.get("discount_note"):
            parts.append(call_ctx["discount_note"])
        call_ctx["confirmed"] = False            # a new total needs a new yes
        logger.info(f"calculate_total -> subtotal={subtotal} discount={discount_amt} service={service_amt} final={final}")
        return " ".join(parts)

    @function_tool
    async def create_booking(context: RunContext, service_id: str, datetime_str: str,
                             quantity: int, customer: str = "", phone: str = "",
                             start_iso: str = "", companions: str = ""):
        """Book AND send the confirmation. Only after the caller clearly said yes, book it, confirm or go ahead.

        Args:
            service_id: what is booked
            datetime_str: when
            quantity: number of people or rooms
            customer: leave empty to use the logged-in caller's profile name
            phone: leave empty to use the caller's profile phone
            start_iso: start as ISO date-time, e.g. 2026-09-27T10:00, if known
            companions: names of the other people joining, comma separated
        """
        idem = f"{call_ctx.get('call_id','local')}|{service_id}|{datetime_str}"
        cache = call_ctx.setdefault("bookings", {})
        if idem in cache:
            b = cache[idem]
            return f"That's already booked. Your reference is {b['ref_id']}."
        prof = call_ctx.get("profile") or {}
        if (not customer or customer.strip().lower() in FAKE_NAMES) and prof.get("name"):
            customer = prof["name"]
        if not phone and prof.get("mobile"):
            phone = prof["mobile"]
        prof_first = (prof.get("name") or "").strip().split(" ")[0].lower()
        # ---- hard gate: the model cannot book on its own initiative ----
        missing = []
        known = [short_name(b["name"]) for b in business_blocks(domain.kb_text)]
        svc_c = _compact(service_id) + _compact(call_ctx.get("focus_place", ""))
        if not any(_compact(k) and _compact(k) in svc_c for k in known):
            missing.append("which listed place it is for (we have: " + ", ".join(known) + ")")
        heard = (call_ctx.get("heard") or "").lower()
        first = (customer or "").strip().split(" ")[0].lower() if customer else ""
        if (not customer or customer.strip().lower() in FAKE_NAMES or len(first) < 2
                or (first not in heard and first != prof_first)):
            missing.append("the caller's real name, in their own words")
        if call_ctx.get("last_total") is None:
            missing.append("the total, worked out with calculate_total and read back")
        elif not call_ctx.get("confirmed"):
            missing.append("a clear yes from the caller AFTER hearing the total")
        if missing:
            logger.info(f"create_booking BLOCKED: {missing}")
            return ("Not booked yet. Still needed: " + "; ".join(missing) +
                    ". Ask the caller for this, one thing at a time. Do not invent anything.")
        call_ctx["confirmed"] = False            # one yes = one booking
        if companions and companions.strip():
            customer = f"{customer} + {companions.strip()}"
        _log_tool(call_ctx, "create_booking")
        ref_id = domain.id[:3].upper() + "-" + uuid.uuid4().hex[:5].upper()
        total = call_ctx.get("last_total")
        if phone:
            call_ctx["phone"] = phone
        with _conn() as c:
            c.execute("""INSERT INTO bookings
                         (ref_id, call_id, domain_id, service_id, datetime, quantity, customer, total)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (ref_id, call_ctx.get("call_id", "local"), domain.id,
                       service_id, datetime_str, quantity, customer, total))
        cache[idem] = {"ref_id": ref_id, "service_id": service_id,
                       "datetime": datetime_str, "quantity": quantity,
                       "customer": customer, "total": total, "start_iso": start_iso,
                       "place": call_ctx.get("focus_place", "")}
        call_ctx["last_booking"] = cache[idem]
        mark_completed(call_ctx.get("call_id", "local"), call_ctx.get("tools_called", []))
        logger.info(f"create_booking -> {ref_id}")
        write_view(domain.id, "booked", ref_id)      # banner: 'Booked, sending...'
        sent = _dispatch_confirmation()              # later writes 'sent' when done
        return (f"Booked. Reference {ref_id} for {service_id} at {datetime_str}. "
                + ("Confirmation is being sent by email and message." if sent else ""))

    def _confirmation_text():
        b = call_ctx.get("last_booking")
        if not b:
            return None, None
        cur = domain.currency
        lines = [f"{domain.display_name} - Booking Confirmation", ""]
        lines.append(f"Reference: {b['ref_id']}")
        lines.append(f"Name: {b.get('customer','')}")
        lines.append(f"Booking: {b['service_id']} for {b['quantity']} at {b['datetime']}")
        bill = call_ctx.get("last_bill")
        if bill:
            lines += ["", "Order details:"]
            for it in bill["items"]:
                lines.append(f"  {it['quantity']} x {it['name']} @ {it['unit_price']} {cur} = {round(it['unit_price']*it['quantity'],2)} {cur}")
            lines.append(f"Subtotal: {bill['subtotal']} {cur}")
            if bill["discount"] > 0:
                lines.append(f"Discount: -{bill['discount']} {cur}")
            if bill["service"] > 0:
                lines.append(f"Service charge: +{bill['service']} {cur}")
            if bill.get("tax", 0) > 0:
                lines.append(f"Tax: +{bill['tax']} {cur}")
            lines.append(f"Total: {bill['final']} {cur}")
        elif b.get("total"):
            lines.append(f"Total: {b['total']} {cur}")
        place = b.get("place") or domain.display_name
        link = calendar_link(f"{b['service_id']} - {place}", b.get("start_iso", ""),
                             EVENT_MINUTES.get(domain.id, 60), place,
                             f"Booking reference {b['ref_id']} via Maya.")
        lines += ["", "Add it to your Google Calendar:", link]
        lines += ["", "Thank you for choosing us. See you soon!"]
        return "\n".join(lines), f"{domain.display_name} Booking {b['ref_id']}"

    def _dispatch_confirmation():
        """Email + Telegram in a background thread. SMTP can take seconds, and
        doing it inline froze the whole audio pipeline, so Maya's voice broke."""
        msg, subject = _confirmation_text()
        if not msg:
            return False
        ref = (call_ctx.get("last_booking") or {}).get("ref_id", "")
        dom_id = domain.id

        def _send():
            status = "failed"
            try:
                results = messaging.send_confirmation_all(msg, subject=subject)
                if any(v[0] for v in results.values()):
                    status = "ok"
                    logger.info(f"confirmation sent ({ref})")
                else:
                    logger.warning(f"confirmation not delivered ({results})")
                    print(f"[CONFIRMATION] {msg}")
            except Exception as e:
                logger.warning(f"confirmation failed: {e}")
                print(f"[CONFIRMATION] {msg}")
            call_ctx["confirmation_status"] = status
            # tell the screen the whole booking is finished, so it can go home
            write_view(dom_id, "sent", f"{ref}|{status}")
        threading.Thread(target=_send, daemon=True).start()
        call_ctx["confirmation_sent"] = True
        return True

    call_ctx["_dispatch"] = _dispatch_confirmation

    @function_tool
    async def send_confirmation(context: RunContext):
        """Resend the confirmation, only if the caller asks for it again."""
        _log_tool(call_ctx, "send_confirmation")
        if not call_ctx.get("last_booking") and call_ctx.get("emergency_ref"):
            return confirmation_answer(call_ctx)
        if not _dispatch_confirmation():
            return "There is no booking to confirm yet."
        return "Confirmation sent again by email and message."

    @function_tool
    async def end_call_tool(context: RunContext):
        """End the call after the goodbye.
        """
        if not call_ctx.get("said_bye"):
            logger.info("end_call_tool BLOCKED: caller has not said goodbye")
            return "The caller has not said goodbye. Do not end the call; answer what they asked."
        _log_tool(call_ctx, "end_call")
        cid = call_ctx.get("call_id", "local")
        try:
            end_call(cid, tools_called=call_ctx.get("tools_called", []),
                     rating=call_ctx.get("rating"))
        except Exception:
            pass
        return "Call ended. Thank you and have a great day!"

    @function_tool
    async def capture_rating(context: RunContext, rating: int):
        """Record the caller's rating.

        Args:
            rating: 1 to 5
        """
        _log_tool(call_ctx, "capture_rating")
        r = max(1, min(5, int(rating)))
        set_rating(call_ctx.get("call_id", "local"), r)
        call_ctx["rating"] = r
        return f"Thank you, I've recorded your rating of {r}."

    @function_tool
    async def switch_domain(context: RunContext, business: str):
        """Switch business type when the caller describes it in unusual words.

        Args:
            business: what they said, e.g. 'somewhere to eat'
        """
        _log_tool(call_ctx, "switch_domain")
        target = resolve_domain(business)
        if not target:
            return ("I can help with a restaurant table, a doctor's appointment, "
                    "a hotel room, or a flight ticket. Which one would you like?")
        if target == domain.id:
            # Same business, but the screen may not be showing it — always signal it.
            write_active_domain(target)
            write_view(target, "list")
            logger.info(f"switch_domain -> {target} (same domain, screen refreshed)")
            return (f"{domain.display_name} is open on their screen now. "
                    f"Ask what they would like to book.")
        try:
            domain.switch_to(target)
            write_active_domain(target)
            set_call_domain(call_ctx.get("call_id", "local"), target)
            call_ctx["domain_id"] = target
            write_view(target, "list")
        except Exception as e:
            logger.warning(f"switch_domain failed: {e}")
            return "I could not open that just now. Which service did you want?"
        logger.info(f"switch_domain -> {target} ({domain.display_name})")
        return (f"Opening {domain.display_name} on your screen now. "
                f"Tell the caller it is open and ask what they would like to book.")

    @function_tool
    async def emergency_alert(context: RunContext, symptom: str, caller_name: str = ""):
        """Call at once for chest pain, breathing trouble, heavy bleeding, fainting, stroke signs, seizure or severe injury.

        Args:
            symptom: what the caller described
            caller_name: their name if known
        """
        _log_tool(call_ctx, "emergency_alert")
        line = trigger_emergency(domain, call_ctx, symptom)
        return ("URGENT. Say exactly this, calmly: " + line +
                " Do not ask for any details. Do not diagnose.")

    @function_tool
    async def show_options(context: RunContext):
        """Show every option on screen.
        """
        _log_tool(call_ctx, "show_options")
        write_view(domain.id, "list")
        return "The full list is on their screen now. Read out the main options."

    @function_tool
    async def show_comparison(context: RunContext, topic: str = ""):
        """Show a comparison table. Empty topic compares the businesses.

        Args:
            topic: optional, e.g. 'cardiology', 'biryani', 'Kochi'
        """
        _log_tool(call_ctx, "show_comparison")
        write_view(domain.id, "compare", (topic or "").strip())
        if topic:
            return (f"A table comparing {topic} across every listing is on their screen, "
                    f"cheapest first. Talk them through the best two or three.")
        return "The full comparison table is on their screen. Talk them through the top two or three."

    @function_tool
    async def show_detail(context: RunContext, name: str):
        """Open one named place on screen.

        Args:
            name: e.g. 'Harbour View'
        """
        _log_tool(call_ctx, "show_detail")
        found = match_business(name, domain.kb_text)
        if not found:
            options = ", ".join(business_names(domain.kb_text))
            logger.info(f"show_detail('{name}') -> no match")
            return (f"There is no place called {name} in the listings. Tell the caller "
                    f"plainly that it is not one of ours, then name the ones we do have: "
                    f"{options}. Do not open anything.")
        write_view(domain.id, "detail", found)
        logger.info(f"show_detail('{name}') -> {found}")
        return f"{found} is open on their screen now. Read out its main options and prices."

    @function_tool
    async def send_menu(context: RunContext):
        """Email the full price list.
        """
        _log_tool(call_ctx, "send_menu")
        try:
            import asyncio as _aio
            results = await _aio.to_thread(messaging.send_confirmation_all,
                domain.kb_text, subject=f"{domain.display_name} - services and prices")
            ok = any(v[0] for v in results.values())
        except Exception as e:
            logger.warning(f"send_menu failed: {e}")
            ok = False
        if not ok:
            return "I could not send it just now, but I can read the options out to you."
        return "I have emailed you the full list with prices."

    everything = [emergency_alert, switch_domain, show_options, show_comparison, show_detail,
                  search_knowledge, quote_booking, calculate_total, create_booking,
                  send_confirmation, capture_rating, send_menu, end_call_tool]
    if os.environ.get("SLIM_TOOLS", "on").lower() in ("1", "on", "true", "yes"):
        # The router already opens businesses, lists, switches and answers
        # 'did you send it?', so the model gets only what it needs: smaller requests.
        return [emergency_alert, show_comparison, show_detail, search_knowledge,
                quote_booking, calculate_total, create_booking, capture_rating, end_call_tool]
    return everything


# ======================================================================
# ---- deterministic answers straight from kb.txt (no LLM) ------------------

PLURAL = {"restaurant": "restaurants", "hospital": "hospitals", "hotel": "hotels", "flight": "airlines"}
# words too common in business names to identify a place on their own
GENERIC = {"the", "grand", "city", "house", "garden", "central", "kitchen", "court", "view",
           "bowl", "green", "care", "hotel", "resort", "lodge", "cafe", "residency", "inn",
           "hospital", "clinic", "centre", "center", "multi", "speciality", "multispeciality",
           "old", "air", "and", "eye", "ent", "copper", "spice", "palm", "transit", "harbour"}


STOP = {"the", "and", "of", "a", "an", "one", "to", "at", "in", "me", "about", "show", "tell", "please"}
KEEP_UPPER_OK = {"AND", "THE", "OF", "AIR", "INN", "EYE", "OLD"}


def titleize(raw):
    """'SUNRISE EYE AND ENT CENTRE' -> 'Sunrise Eye and ENT Centre', 'MG ROAD' -> 'MG Road'."""
    out = []
    for i, w in enumerate(raw.strip().split()):
        if w.upper() in ("AND", "OF") and i:
            out.append(w.lower())
        elif len(w) <= 3 and w.isalpha() and w.isupper() and w.upper() not in KEEP_UPPER_OK:
            out.append(w)
        else:
            out.append("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
    return " ".join(out)


def business_blocks(kb):
    out = []
    for block in (kb or "").split("\n\n"):
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if not lines or " - RATING" not in lines[0].upper():
            continue
        head = lines[0].rstrip(":")
        parts = head.split(" - ")
        name_place = parts[0].split(",")
        rating = re.search(r"rating\s*([\d.]+)", head, re.I)
        km = re.search(r"([\d.]+)\s*km", head, re.I)
        prices = []
        for l in lines[1:]:
            m = re.search(r"\s-\s([\d,]+)\s*rupees", l, re.I)
            if m and "add-on" not in l.lower():
                prices.append(int(m.group(1).replace(",", "")))
        spend = None
        for l in lines:
            m = re.search(r"spend\s+([\d,]+)\s+to\s+([\d,]+)", l, re.I)
            if m:
                spend = (int(m.group(1).replace(",", "")), int(m.group(2).replace(",", "")))
        out.append({
            "name": {"Indigo": "IndiGo", "Spicejet": "SpiceJet"}.get(titleize(name_place[0]), titleize(name_place[0])),
            "place": titleize(",".join(name_place[1:])),
            "rating": float(rating.group(1)) if rating else None,
            "km": float(km.group(1)) if km else None,
            "prices": prices, "spend": spend,
            "words": set(re.findall(r"[a-z0-9']+", name_place[0].lower())) - STOP,
        })
    return out


def quick_lines(kb):
    for block in (kb or "").split("\n\n"):
        if "quick comparison" in block.lower():
            return [l.strip() for l in block.splitlines()[1:] if ":" in l]
    return []


def find_place(text, kb):
    """A specific business named in the caller's words, or None. Strict on purpose:
    two name words, or one distinctive word (not 'grand', 'city', 'view' ...)."""
    said = set(re.findall(r"[a-z0-9']+", (text or "").lower())) - STOP
    said |= {w.rstrip("s") for w in said} | {w.replace("'", "") for w in said}
    best, best_score = None, 0
    for b in business_blocks(kb):
        words = b["words"] | {w.replace("'", "") for w in b["words"]}
        hit = words & said
        strong = [w for w in hit if w not in GENERIC and len(w) >= 4]
        score = len(hit) if len(hit) >= 2 else (1 if strong else 0)
        core = _compact(short_name(b["name"]))
        if len(core) >= 6 and core in _compact(text):
            score = max(score, 3)          # 'CityCare', 'Harborview residency
        if score > best_score:
            best, best_score = b, score
    return best


def _rupees(n):
    return f"{n:,} rupees".replace(",", ",")


SHORT_DROP = {"multi-speciality", "multispeciality", "hospital", "clinic", "centre",
              "center", "residency", "resort", "hotel"}


def short_name(name):
    words = name.split()
    while len(words) > 1 and words[-1].lower() in SHORT_DROP:
        words.pop()
    return " ".join(words)


def _join(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def say_intro(domain_id, kb):
    bs = business_blocks(kb)
    if not bs:
        return None
    kind = PLURAL.get(domain_id, "options")
    names = _join([short_name(b["name"]) for b in bs])
    return f"We have {len(bs)} {kind}: {names}. They're all on your screen. Which one would you like?"


def say_list(domain_id, kb):
    bs = business_blocks(kb)
    if not bs:
        return None
    names = [b["name"] for b in bs]
    joined = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
    return f"Here are all {len(bs)}: {joined}. Which one interests you?"


COMPARE_KEYS = [
    (r"cheap|budget|afford|lowest|price", "cheapest"),
    (r"student", "best student"),
    (r"corporate|business trip|work|office", "best corporate"),
    (r"close|near|distance|central", ("closest", "most central")),
    (r"best|top|rated|rating|review", "best rated"),
]


def say_compare(domain_id, kb, text):
    lines = quick_lines(kb)
    if not lines:
        return None
    t = (text or "").lower()
    picked = []
    for rx, keys in COMPARE_KEYS:
        if re.search(rx, t):
            keys = keys if isinstance(keys, tuple) else (keys,)
            for l in lines:
                if l.lower().startswith(keys) and l not in picked:
                    picked.append(l); break
    if not picked:                                   # plain "compare them"
        for key in ("best rated", "cheapest"):
            for l in lines:
                if l.lower().startswith(key) and l not in picked:
                    picked.append(l); break
    body = " ".join(l.rstrip(".") + "." for l in picked[:2])
    return f"{body} The full comparison is on your screen. Which one would you like?"


def say_detail(domain_id, b, kb=None):
    s = f"{b['name']}"
    if b["place"]:
        s += f" in {b['place']}"
    if b["rating"]:
        s += f", rated {b['rating']}"
    s += ". "
    if domain_id == "restaurant" and b["spend"]:
        s += f"Two people spend about {b['spend'][0]} to {b['spend'][1]} rupees. "
    elif b["prices"]:
        lo, hi = min(b["prices"]), max(b["prices"])
        unit = {"hotel": " a night", "hospital": " per consultation"}.get(domain_id, "")
        s += (f"From {lo} rupees{unit}. " if lo == hi or domain_id == "flight"
              else f"From {lo} to {hi} rupees{unit}. ")
    if domain_id == "hotel" and kb:
        rooms = [i["name"] for i in block_items(kb, b["name"])]
        if rooms:
            s += f"Rooms: {_join(rooms)}. "
        return s + "It's on your screen. Would you like to book a room?"
    return s + "It's on your screen. Shall I book it?"



# ---- matching and validation helpers ----------------------------------------
FAKE_NAMES = {"user", "caller", "guest", "customer", "unknown", "name", "sir", "madam", "test",
              "none", "john doe", "jane doe", "john", "jane"}


def _compact(s):
    """'Harbour View' / 'Harborview' / 'harbor-view' -> 'harborview'."""
    return re.sub(r"[^a-z]", "", (s or "").lower()).replace("our", "or")


def allowed_discounts(kb, place):
    """Percentages the file actually lists as student/corporate rates for this
    place (or anywhere in the domain if no place is open)."""
    out = set()
    target = _compact(short_name(place)) if place else ""
    for block in (kb or "").split("\n\n"):
        lines = block.strip().splitlines()
        if not lines or " - RATING" not in lines[0].upper():
            continue
        if target and target not in _compact(lines[0]):
            continue
        for ln in lines:
            for sentence in re.split(r"\.\s+", ln):
                t = sentence.lower()
                if ("student" in t or "corporate" in t) and not t.strip().startswith("no "):
                    for m in re.findall(r"(\d+)\s*percent", t):
                        out.add(int(m))
    return out



# ---- caller profile, location and emergencies -------------------------------
def load_profile(email):
    """The logged-in caller's saved details (users table), or {}."""
    if not email:
        return {}
    try:
        with _conn() as c:
            r = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return dict(r) if r else {}
    except Exception as e:
        logger.warning(f"load_profile failed: {e}")
        return {}


def latest_location(email, since):
    p = load_profile(email)
    lat, lng, at = p.get("last_lat"), p.get("last_lng"), p.get("last_loc_at")
    if lat is None or lng is None or not at or float(at) < since:
        return None
    return float(lat), float(lng)


def _notify_email(to, subject, body):
    try:
        fn = getattr(messaging, "send_email", None)
        if fn and to:
            res = fn(subject, body, to=to)
            return bool(res[0] if isinstance(res, (tuple, list)) else res)
    except Exception as e:
        logger.warning(f"emergency email failed: {e}")
    return False


def _notify_telegram(text):
    try:
        fn = getattr(messaging, "send_telegram", None)
        if fn:
            res = fn(text)
            return bool(res[0] if isinstance(res, (tuple, list)) else res)
    except Exception as e:
        logger.warning(f"emergency telegram failed: {e}")
    return False


def trigger_emergency(domain, call_ctx, symptom):
    """Book the nearest 24-hour emergency department for the logged-in caller and
    alert their emergency contact with their location. No questions asked.
    Returns the line Maya should say. Safe to call twice (second call just repeats)."""
    if call_ctx.get("emergency_line"):
        return call_ctx["emergency_line"]
    cid = call_ctx.get("call_id", "local")
    try:
        if domain.id != "hospital":
            domain.switch_to("hospital")
            call_ctx["domain_id"] = "hospital"
        write_active_domain("hospital")
        set_call_domain(cid, "hospital")
    except Exception as e:
        logger.warning(f"emergency switch failed: {e}")

    er = emergency_hospitals(domain.kb_text)
    km, hosp = er[0] if er else (None, "the nearest emergency department")
    prof = call_ctx.get("profile") or load_profile(call_ctx.get("user_email"))
    patient = prof.get("name") or "the caller"
    phone = prof.get("mobile") or "not on file"
    contact = (prof.get("emergency_name") or "").strip()
    ref = "ER-" + uuid.uuid4().hex[:5].upper()
    try:
        with _conn() as c:
            c.execute("""INSERT INTO bookings
                         (ref_id, call_id, domain_id, service_id, datetime, quantity, customer, total, status)
                         VALUES (?,?,?,?,?,?,?,?,?)""",
                      (ref, cid, "hospital", f"EMERGENCY - {symptom}"[:120], "immediately", 1,
                       patient, 0, "emergency"))
        mark_completed(cid, call_ctx.get("tools_called", []))
    except Exception as e:
        logger.warning(f"emergency booking failed: {e}")
    write_view("hospital", "emergency", hosp)
    started = time.time()

    def _notify():
        # give the browser a moment to send a fresh position
        loc = None
        for _ in range(10):
            loc = latest_location(call_ctx.get("user_email"), since=started - 5)
            if loc:
                break
            time.sleep(0.5)
        loc = loc or latest_location(call_ctx.get("user_email"), since=time.time() - 1800)
        where = (f"https://maps.google.com/?q={loc[0]:.6f},{loc[1]:.6f}" if loc
                 else "Location was not shared from the browser.")
        text = (f"EMERGENCY ALERT {ref}\n"
                f"{patient} reported: {symptom}\n"
                f"Booked at: {hosp}" + (f" ({km} km)" if km else "") + " - 24-hour emergency department\n"
                f"Patient phone: {phone}\n"
                f"Live location: {where}\n"
                f"Please contact them now. If they cannot travel safely, call 108 for an ambulance.")
        results = {
            "contact_email": _notify_email(prof.get("emergency_email"), f"EMERGENCY: {patient} needs help ({ref})",
                                           (f"Dear {contact},\n\n" if contact else "") + text +
                                           (f"\n\nEmergency contact on file: {contact} {prof.get('emergency_phone') or ''}" if contact else "")),
            "telegram": _notify_telegram(text + (f"\nEmergency contact: {contact} {prof.get('emergency_phone') or ''}" if contact else "")),
        }
        call_ctx["emergency_alerts"] = dict(results, location=bool(loc))
        logger.info(f"emergency {ref} alerts: {results} location={'yes' if loc else 'no'}")
        write_view("hospital", "emergency", hosp)

    threading.Thread(target=_notify, daemon=True).start()
    line = (f"Emergency booked at {hosp}" + (f", {km} kilometres away" if km else "") + ". " +
            (f"I'm alerting {contact} with your location now. " if contact and prof.get("emergency_email")
             else "I'm sending the alert now. ") +
            "If you can't travel safely, call 108 immediately.")
    call_ctx["emergency_line"] = line
    call_ctx["emergency_ref"] = ref
    logger.info(f"EMERGENCY {ref} -> {hosp} for {patient}")
    return line



# ---- scripted booking support -------------------------------------------------
def block_items(kb, place):
    """Priced options of one business: [{name, price, capacity, detail}]."""
    target = _compact(short_name(place or ""))
    for block in (kb or "").split("\n\n"):
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if not lines or " - RATING" not in lines[0].upper() or not target:
            continue
        if target not in _compact(lines[0]):
            continue
        out = []
        for l in lines[1:]:
            parts = [p.strip() for p in re.split(r"\s+-\s+", l)]
            if len(parts) >= 2:
                m = re.match(r"^([\d,]+)\s*rupees", parts[1], re.I)
                if m and "add-on" not in parts[0].lower():
                    cap = re.search(r"fits\s+(\d+)", l, re.I)
                    out.append({"name": parts[0], "price": int(m.group(1).replace(",", "")),
                                "capacity": int(cap.group(1)) if cap else None,
                                "detail": ", ".join(parts[2:])})
        return out
    return []


_WORDN = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "ten": 10}


def _cond_n(txt):
    return int(txt) if txt.isdigit() else _WORDN.get(txt, 0)


def listed_rate(kb, place, kind, item_name="", qty=None, nights=None):
    """The student / corporate percentage this place lists, honouring
    'off economy and standard'-style restrictions. 0 if none applies."""
    if kind not in ("student", "corporate"):
        return 0
    target = _compact(short_name(place or ""))
    for block in (kb or "").split("\n\n"):
        lines = block.strip().splitlines()
        if not lines or " - RATING" not in lines[0].upper() or target not in _compact(lines[0]):
            continue
        firsts = {i["name"].split()[0].lower() for i in block_items(kb, place)}
        for ln in lines:
            for sentence in re.split(r"\.\s+", ln):
                t = sentence.lower().strip()
                if kind not in t or t.startswith("no "):
                    continue
                m = re.search(r"(\d+)\s*percent", t)
                if not m:
                    continue
                named = firsts & set(re.findall(r"[a-z]+", t))
                if named and item_name and item_name.split()[0].lower() not in named:
                    return 0
                # conditions written in the file: 'for bookings of four or more',
                # 'for stays over three nights'
                c = re.search(r"(\d+|two|three|four|five|six|seven|eight|ten)\s+or more", t)
                if c and qty is not None and qty < _cond_n(c.group(1)):
                    return 0
                c = re.search(r"over\s+(\d+|two|three|four|five|six|seven)\s+nights?", t)
                if c and nights is not None and nights <= _cond_n(c.group(1)):
                    return 0
                return int(m.group(1))
    return 0


def confirmation_answer(call_ctx):
    """Truthful answer to 'did you send the confirmation?'."""
    if call_ctx.get("emergency_ref"):
        a = call_ctx.get("emergency_alerts")
        if a is None:
            return "The emergency alert is being sent right now."
        who = (call_ctx.get("profile") or {}).get("emergency_name") or "your emergency contact"
        if a.get("contact_email") or a.get("telegram"):
            loc = " with your location" if a.get("location") else ""
            return f"Yes, I've alerted {who}{loc}. If you can't travel safely, call 108."
        return "I couldn't send the alert, so please call 108 now."
    st = call_ctx.get("confirmation_status")
    ref = (call_ctx.get("last_booking") or {}).get("ref_id")
    if not ref:
        return None
    if st == "ok":
        return f"Yes, the confirmation for {ref} has been sent by email and message."
    if st == "failed":
        return f"Your booking {ref} is saved, but the confirmation couldn't be sent."
    return f"The confirmation for {ref} is being sent now."


def commit_booking(domain, call_ctx, service_id, datetime_str, quantity, customer,
                   phone="", start_iso="", companions=""):
    """Save a booking the caller confirmed in the scripted flow, then send the
    confirmation exactly as create_booking does."""
    ref_id = domain.id[:3].upper() + "-" + uuid.uuid4().hex[:5].upper()
    who = customer + (f" + {companions}" if companions else "")
    with _conn() as c:
        c.execute("""INSERT INTO bookings
                     (ref_id, call_id, domain_id, service_id, datetime, quantity, customer, total)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (ref_id, call_ctx.get("call_id", "local"), domain.id, service_id,
                   datetime_str, quantity, who, call_ctx.get("last_total")))
    b = {"ref_id": ref_id, "service_id": service_id, "datetime": datetime_str,
         "quantity": quantity, "customer": who, "total": call_ctx.get("last_total"),
         "start_iso": start_iso, "place": call_ctx.get("focus_place", "")}
    call_ctx.setdefault("bookings", {})[f"{call_ctx.get('call_id')}|{service_id}|{datetime_str}"] = b
    call_ctx["last_booking"] = b
    _log_tool(call_ctx, "create_booking")
    mark_completed(call_ctx.get("call_id", "local"), call_ctx.get("tools_called", []))
    write_view(domain.id, "booked", ref_id)
    d = call_ctx.get("_dispatch")
    if d:
        d()
    logger.info(f"create_booking -> {ref_id} (scripted flow)")
    return ref_id

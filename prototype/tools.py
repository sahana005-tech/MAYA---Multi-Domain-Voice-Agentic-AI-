"""
tools.py — the FOUR generic tools. This file is the intellectual core of the
project: these tools NEVER learn what domain they're in. A restaurant table,
a hospital slot, a hotel room-night, and a flight seat are all just
    service_id + quantity + datetime + notes.

If you ever feel tempted to add book_table() and book_appointment() as two
separate tools, STOP — that breaks the whole multi-domain claim. Force every
domain into the four signatures below.

Each tool returns a plain dict (JSON-serialisable) so the agent loop can hand
the result straight back to the LLM as a tool result.
"""

import uuid
import datetime as _dt

from db import record_booking

try:
    from rag import retrieve  # real RAG (Chroma). Optional at this stage.
except Exception:
    retrieve = None


# ---------- tool 1: read action (RAG) ----------

def search_knowledge(query: str, domain, call_ctx):
    """Look up domain info. The only 'read' tool."""
    if retrieve is not None:
        try:
            chunks = retrieve(domain.id, query, k=3)
            return {"results": chunks}
        except Exception as e:
            return {"results": [], "error": f"retrieval unavailable: {e}"}
    # Fallback before Chroma is wired: read raw KB files so the tool still works.
    import os, glob
    hits = []
    for path in glob.glob(os.path.join(domain.kb_dir, "*.md")):
        with open(path) as f:
            text = f.read()
        if any(w.lower() in text.lower() for w in query.split()):
            hits.append(text[:600])
    return {"results": hits[:3] or ["No specific info found; ask the caller to clarify."]}


# ---------- tool 2: pricing math ----------

def calculate_total(line_items: list, modifiers: list, domain, call_ctx):
    """
    line_items: [{"name": str, "unit_price": float, "quantity": int}, ...]
    modifiers:  ["member_discount", "service_charge", ...] — resolved against
                the domain's pricing_rules in config, NOT hardcoded here.
    """
    subtotal = sum(li.get("unit_price", 0) * li.get("quantity", 1) for li in line_items)
    total = subtotal
    applied = []
    rules = domain.pricing_rules or {}

    for mod in (modifiers or []):
        if mod == "member_discount" and "member_discount" in rules:
            d = subtotal * float(rules["member_discount"])
            total -= d
            applied.append({"modifier": mod, "amount": -round(d, 2)})
        elif mod == "service_charge" and "service_charge" in rules:
            c = subtotal * float(rules["service_charge"])
            total += c
            applied.append({"modifier": mod, "amount": round(c, 2)})
        elif mod in rules:  # generic flat/percent rule
            val = float(rules[mod])
            amt = subtotal * val if abs(val) < 1 else val
            total += amt
            applied.append({"modifier": mod, "amount": round(amt, 2)})

    return {
        "subtotal": round(subtotal, 2),
        "applied": applied,
        "total": round(total, 2),
        "currency": domain.currency,
    }


# ---------- tool 3: write action (booking) ----------

def create_booking(service_id: str, datetime_str: str, quantity: int,
                   customer: str, notes: str, domain, call_ctx, total=None):
    """
    THE write action. Persists a real row, returns a real reference id.
    Idempotent per call+service+datetime so an LLM double-fire can't create
    two bookings.
    """
    idem_key = f"{call_ctx.get('call_id','local')}|{service_id}|{datetime_str}"
    existing = call_ctx.setdefault("_bookings", {})
    if idem_key in existing:
        return existing[idem_key]  # already booked this exact thing this call

    ref_id = domain.id[:3].upper() + "-" + uuid.uuid4().hex[:6].upper()
    record_booking(
        ref_id=ref_id,
        call_id=call_ctx.get("call_id", "local"),
        domain_id=domain.id,
        service_id=service_id,
        datetime_str=datetime_str,
        quantity=quantity,
        customer=customer,
        total=total,
    )
    result = {
        "ref_id": ref_id,
        "service_id": service_id,
        "datetime": datetime_str,
        "quantity": quantity,
        "customer": customer,
        "total": total,
        "status": "confirmed",
    }
    existing[idem_key] = result
    call_ctx.setdefault("_tools_called", []).append("create_booking")
    return result


# ---------- tool 4: write action (confirmation) ----------

def send_confirmation(channel: str, to: str, ref_id: str, summary: str,
                      domain, call_ctx):
    """
    Fires the confirmation. channel is 'whatsapp' | 'telegram' | 'console'.
    The tool is channel-agnostic on purpose — Indian SMS needs DLT registration,
    so WhatsApp is the deployed channel; console is used for offline testing.
    """
    message = domain.confirmation_template.format(ref_id=ref_id, summary=summary)

    if channel == "console" or to in (None, "", "local"):
        print(f"[CONFIRMATION -> {to or 'console'}] {message}")
        return {"channel": "console", "delivered": True, "message": message}

    # Real senders are wired in messaging.py once keys exist. Import lazily so
    # this module works with zero credentials during early development.
    try:
        from messaging import send_message
        ok = send_message(channel, to, message)
        return {"channel": channel, "delivered": bool(ok), "message": message}
    except Exception as e:
        print(f"[CONFIRMATION fallback -> console] {message}  (reason: {e})")
        return {"channel": "console", "delivered": True, "message": message,
                "note": f"{channel} unavailable, logged to console"}


# ---------- tool registry (what the LLM sees) ----------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Look up information from this business's knowledge base (menu, services, prices, hours, policies).",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_total",
            "description": "Calculate a price total from line items and optional modifiers like member_discount or service_charge.",
            "parameters": {
                "type": "object",
                "properties": {
                    "line_items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "unit_price": {"type": "number"},
                                "quantity": {"type": "integer"},
                            },
                        },
                    },
                    "modifiers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["line_items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": "Create a confirmed booking/reservation/appointment. Returns a reference id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_id": {"type": "string", "description": "what is being booked (table, room, appointment, flight route)"},
                    "datetime_str": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "customer": {"type": "string"},
                    "notes": {"type": "string"},
                    "total": {"type": "number"},
                },
                "required": ["service_id", "datetime_str", "quantity", "customer"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_confirmation",
            "description": "Send the booking confirmation to the caller via whatsapp (or console in testing).",
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "enum": ["whatsapp", "telegram", "console"]},
                    "to": {"type": "string"},
                    "ref_id": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["channel", "to", "ref_id", "summary"],
            },
        },
    },
]

# name -> python function, for the agent loop to dispatch
TOOL_FUNCS = {
    "search_knowledge": search_knowledge,
    "calculate_total": calculate_total,
    "create_booking": create_booking,
    "send_confirmation": send_confirmation,
}


def dispatch(name: str, args: dict, domain, call_ctx):
    """Call a tool by name with the LLM-provided args, injecting domain+ctx."""
    fn = TOOL_FUNCS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    call_ctx.setdefault("_tools_called", [])
    if name not in call_ctx["_tools_called"]:
        call_ctx["_tools_called"].append(name)
    return fn(domain=domain, call_ctx=call_ctx, **args)

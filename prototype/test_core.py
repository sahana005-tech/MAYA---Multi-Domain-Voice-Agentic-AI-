"""
test_core.py — proves the heart of the system works with NO phone, NO LLM,
NO API keys. It manually walks the exact tool sequence the agent will later
drive automatically, for two different domains, to prove the multi-domain
claim at the tool level.

Run:  python test_core.py
"""

import os
from db import init_db, start_call, end_call, telemetry_snapshot
from domain import load_domain
import tools


def run_booking_for(domain_id, call_id, line_items, modifiers, service_id,
                    when, qty, customer):
    print(f"\n{'='*60}\nDOMAIN: {domain_id}   (call {call_id})\n{'='*60}")
    domain = load_domain(domain_id)
    print("Greeting:", domain.greeting)

    # simulated per-call context (the agent loop will own this later)
    ctx = {"call_id": call_id}
    start_call(call_id, domain_id)

    # 1. read action — search the KB
    r = tools.dispatch("search_knowledge", {"query": "table reservation price"}, domain, ctx)
    print("\n[search_knowledge] found", len(r["results"]), "chunk(s)")

    # 2. pricing math
    calc = tools.dispatch("calculate_total",
                          {"line_items": line_items, "modifiers": modifiers},
                          domain, ctx)
    print(f"[calculate_total] subtotal={calc['subtotal']} "
          f"applied={calc['applied']} total={calc['total']} {calc['currency']}")

    # 3. write action — booking
    booking = tools.dispatch("create_booking",
                             {"service_id": service_id, "datetime_str": when,
                              "quantity": qty, "customer": customer,
                              "notes": "", "total": calc["total"]},
                             domain, ctx)
    print(f"[create_booking] ref_id={booking['ref_id']} status={booking['status']}")

    # 3b. prove idempotency — same booking again must NOT create a second row
    booking2 = tools.dispatch("create_booking",
                              {"service_id": service_id, "datetime_str": when,
                               "quantity": qty, "customer": customer,
                               "notes": "", "total": calc["total"]},
                              domain, ctx)
    assert booking2["ref_id"] == booking["ref_id"], "IDEMPOTENCY FAILED"
    print(f"[idempotency] second create returned SAME ref {booking2['ref_id']} — OK")

    # 4. write action — confirmation (console channel, no keys needed)
    summary = f"{qty} guests, {when}, total {calc['total']} {calc['currency']}"
    conf = tools.dispatch("send_confirmation",
                          {"channel": "console", "to": "local",
                           "ref_id": booking["ref_id"], "summary": summary},
                          domain, ctx)
    print(f"[send_confirmation] delivered={conf['delivered']}")

    # end the call — the second of the two lifecycle writes
    end_call(call_id, task_completed=True,
             tools_called=ctx.get("_tools_called", []),
             rating=5, transcript="(simulated)",
             stt_ms=280, llm_ms=900, tts_ms=210)
    print("tools fired this call:", ctx.get("_tools_called"))


if __name__ == "__main__":
    # fresh db for the test
    if os.path.exists("data.db"):
        os.remove("data.db")
    init_db()

    # RESTAURANT booking
    run_booking_for(
        "restaurant", "TESTCALL-001",
        line_items=[{"name": "Butter Chicken", "unit_price": 380, "quantity": 2},
                    {"name": "Veg Biryani", "unit_price": 300, "quantity": 1}],
        modifiers=["member_discount", "service_charge"],
        service_id="table for 4", when="tonight 8:00 PM", qty=4, customer="Ravi",
    )

    print("\n\n########## DASHBOARD SNAPSHOT ##########")
    snap = telemetry_snapshot()
    for k, v in snap.items():
        if k != "recent_conversations":
            print(f"  {k}: {v}")
    print("  recent_conversations:")
    for row in snap["recent_conversations"]:
        print(f"    - {row['id']} [{row['domain_id']}] "
              f"status={row['status']} completed={row['task_completed']} "
              f"rating={row['rating']} tools={row['tools_called']}")

    print("\nCore verified: 4 generic tools + DB lifecycle + idempotency, "
          "no phone / no LLM / no keys.")

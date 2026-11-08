"""
domain.py — loads a single domain "pack" (config + which KB to use).

The ENTIRE difference between restaurant / hospital / hotel / flight is which
folder we load here. No backend code branches on domain_id. Swapping the demo
is: set DOMAIN=hospital, and this loader hands the rest of the system a
different persona, greeting, and pricing rule set.
"""

import os
import yaml

DOMAINS_DIR = os.path.join(os.path.dirname(__file__), "domains")


class Domain:
    def __init__(self, domain_id: str):
        self.id = domain_id
        cfg_path = os.path.join(DOMAINS_DIR, domain_id, "config.yaml")
        if not os.path.exists(cfg_path):
            raise ValueError(f"Unknown domain '{domain_id}' — no config at {cfg_path}")
        with open(cfg_path) as f:
            self.cfg = yaml.safe_load(f)
        self.kb_dir = os.path.join(DOMAINS_DIR, domain_id, "kb")

    @property
    def display_name(self):
        return self.cfg.get("display_name", self.id)

    @property
    def greeting(self):
        return self.cfg.get("greeting", "Hello, how can I help you today?")

    @property
    def booking_noun(self):
        return self.cfg.get("booking_noun", "booking")

    @property
    def currency(self):
        return self.cfg.get("currency", "INR")

    @property
    def pricing_rules(self):
        return self.cfg.get("pricing_rules", {})

    @property
    def confirmation_template(self):
        return self.cfg.get(
            "confirmation_template",
            "Booking {ref_id} confirmed - {summary}",
        )

    def system_prompt(self):
        """The persona handed to the LLM. Domain-specific text lives in config,
        never in code."""
        persona = self.cfg.get("persona", f"You are a helpful voice agent for {self.display_name}.")
        return (
            f"{persona}\n"
            f"You are on a live phone call. Keep replies short and natural — one or two sentences. "
            f"You can search the knowledge base, calculate totals, create a {self.booking_noun}, "
            f"and send a confirmation. When the caller wants to book, collect the needed details, "
            f"confirm the price, create the {self.booking_noun}, then send confirmation. "
            f"Prices are in {self.currency}."
        )


def load_domain(domain_id: str = None) -> Domain:
    domain_id = domain_id or os.environ.get("DOMAIN", "restaurant")
    return Domain(domain_id)


def list_domains():
    return sorted(
        d for d in os.listdir(DOMAINS_DIR)
        if os.path.isdir(os.path.join(DOMAINS_DIR, d))
    )

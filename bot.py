"""Vera challenge bot: deterministic context composer and HTTP harness API."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

VERSION = "1.0.0"
STARTED = time.time()
VALID_SCOPES = {"category", "merchant", "customer", "trigger"}
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, dict[str, Any]] = {}
sent_suppressions: set[str] = set()


def _first(mapping: dict, *keys: str, default=None):
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def _name(merchant: dict) -> str:
    identity = merchant.get("identity", {})
    return str(_first(identity, "name", default="there"))


def _owner(merchant: dict) -> str:
    identity = merchant.get("identity", {})
    owner = identity.get("owner_first_name")
    if owner:
        return str(owner)
    return _name(merchant).split()[1].strip("'’s,.") if _name(merchant).lower().startswith("dr.") and len(_name(merchant).split()) > 1 else _name(merchant)


def _pct(value: Any) -> str:
    try:
        return f"{abs(float(value)) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(value)


def _titlecase(value: Any) -> str:
    return str(value or "").replace("_", " ").replace("-", " ")


def _active_offer(merchant: dict) -> str | None:
    for offer in merchant.get("offers", []):
        if str(offer.get("status", "active")).lower() == "active" and offer.get("title"):
            return str(offer["title"])
    return None


def _matching_digest(category: dict, trigger: dict) -> dict | None:
    payload = trigger.get("payload", {})
    wanted = payload.get("top_item_id") or payload.get("digest_item_id")
    digest = category.get("digest", [])
    if wanted:
        for item in digest:
            if item.get("id") == wanted:
                return item
    return digest[0] if digest else None


def _customer_message(category: dict, merchant: dict, trigger: dict, customer: dict) -> dict:
    payload = trigger.get("payload", {})
    ident = customer.get("identity", {})
    cust_name = str(ident.get("name") or "there")
    biz = _name(merchant)
    consent = customer.get("consent", {})
    scopes = {str(s).lower() for s in consent.get("scope", [])}
    kind = trigger.get("kind", "")
    if not consent or not scopes:
        return {"body": "", "cta": "none", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", ""), "rationale": "No recorded outreach consent; customer message suppressed."}
    if kind == "appointment_tomorrow":
        allowed = bool(scopes & {"all", "appointment_reminders", "service_updates"})
    elif kind in ("recall_due", "customer_lapsed_soft", "customer_lapsed_hard"):
        allowed = bool(scopes & {"all", "recall_reminders", "service_updates"})
        if kind in ("customer_lapsed_soft", "customer_lapsed_hard"):
            allowed = allowed or bool(scopes & {"marketing", "promotional_offers", "winback_offers"})
    elif kind == "chronic_refill_due":
        allowed = bool(scopes & {"all", "refill_reminders", "recall_alerts", "service_updates"})
    elif kind == "winback_eligible":
        allowed = bool(scopes & {"all", "marketing", "winback_offers", "promotional_offers"})
    elif kind == "wedding_package_followup":
        allowed = bool(scopes & {"all", "marketing", "promotional_offers"})
    else:
        allowed = bool(scopes & {"all", "marketing", "service_updates"})
    if not allowed:
        return {"body": "", "cta": "none", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", ""), "rationale": "Consent scope does not cover this outreach; customer message suppressed."}
    slots = payload.get("available_slots", [])
    slot_labels = [s.get("label") for s in slots if isinstance(s, dict) and s.get("label")]
    offer = _active_offer(merchant)
    category_slug = merchant.get("category_slug", category.get("slug", ""))
    if kind in ("recall_due", "customer_lapsed_soft", "customer_lapsed_hard", "chronic_refill_due"):
        due = payload.get("due_date") or payload.get("refill_due_date")
        is_lapsed = kind in ("customer_lapsed_soft", "customer_lapsed_hard")
        if is_lapsed:
            service = "welcome back"
        elif category_slug == "dentists":
            service = "cleaning recall" if "recall" in kind else "follow-up"
        elif category_slug == "pharmacies":
            service = "refill reminder"
        else:
            service = "follow-up"
        if is_lapsed:
            detail = f" It's been {payload['days_since_last_visit']} days since your last visit." if payload.get("days_since_last_visit") is not None else f" We'd be glad to {service}."
        else:
            detail = f" Your {service} is due" + (f" on {due}" if due else "") + "."
        if offer:
            detail += f" Current offer: {offer}."
        if slot_labels:
            detail += " We have " + " or ".join(slot_labels[:2]) + "."
        detail += " Would you like us to help arrange a time?"
        body = f"Hi {cust_name}, {biz} here.{detail} Reply STOP if you don't want these reminders."
        cta = "open_ended"
    elif kind == "appointment_tomorrow":
        appointment = payload.get("appointment_time") or payload.get("appointment") or payload.get("slot")
        when = f" for {appointment}" if appointment else " tomorrow"
        body = f"Hi {cust_name}, a reminder from {biz}: your appointment is{when}. Please reply if you need to make a change. Reply STOP to opt out."
        cta = "open_ended"
    elif kind == "wedding_package_followup":
        wedding = payload.get("wedding_date")
        body = f"Hi {cust_name}, {biz} here. Your wedding date{f' ({wedding})' if wedding else ''} is coming up. Would you like to discuss the next step for your service? Reply STOP to opt out."
        cta = "open_ended"
    elif kind == "winback_eligible":
        offer = _active_offer(merchant)
        detail = f" We currently have {offer}." if offer else ""
        body = f"Hi {cust_name}, {biz} here. We'd be glad to welcome you back.{detail} Would you like to hear about options for your next visit? Reply STOP to opt out."
        cta = "open_ended"
    else:
        body = ""
        cta = "none"
    return {"body": body, "cta": cta, "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", ""), "rationale": f"Customer-facing {kind} message uses the recorded consent, merchant identity, and trigger details."}


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    """Compose one fact-grounded outbound message from structured contexts."""
    if trigger.get("scope") == "customer" or customer:
        if not customer:
            return {"body": "", "cta": "none", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", ""), "rationale": "Customer context is missing; no customer-facing message is composed."}
        return _customer_message(category, merchant, trigger, customer)
    name = _name(merchant)
    first = _owner(merchant)
    city = merchant.get("identity", {}).get("city")
    payload = trigger.get("payload", {}) or {}
    kind = str(trigger.get("kind", ""))
    performance = merchant.get("performance", {}) or {}
    aggregate = merchant.get("customer_aggregate", {}) or {}
    offers = _active_offer(merchant)
    item = _matching_digest(category, trigger)
    body = ""
    cta = "open_ended"
    rationale = f"{kind.replace('_', ' ').capitalize()} message grounded in the merchant and trigger context."

    if kind == "research_digest" and item:
        summary = item.get("summary") or item.get("title", "")
        segment = item.get("patient_segment")
        segment_phrase = ""
        if segment and segment in ("high_risk_adults", "high_risk_adult") and aggregate.get("high_risk_adult_count"):
            segment_phrase = f" Relevant to your {aggregate['high_risk_adult_count']} high-risk adults."
        trial = f" ({item['trial_n']:,}-person study)" if item.get("trial_n") else ""
        body = f"{first}, this week's {category.get('display_name', 'category')} digest: {item.get('title', summary)}{trial}.{segment_phrase} {summary} Source: {item.get('source', 'provided digest')}. Want me to pull the details and draft a shareable customer note?"
        rationale = f"Uses the supplied digest item and source, with merchant cohort context when available."
    elif kind == "regulation_change" and item:
        deadline = payload.get("deadline_iso") or item.get("date")
        body = f"{first}, a relevant update from {item.get('source', 'the supplied category digest')}: {item.get('title', item.get('summary', ''))}"
        if deadline:
            body += f" Effective {deadline}."
        body += " Would you like a short checklist based on this update?"
        rationale = "Compliance note cites only the supplied category item and deadline."
    elif kind in ("cde_opportunity", "trial_followup") and item:
        body = f"{first}, {item.get('title', 'a category learning opportunity')} ({item.get('source', 'category context')}). {item.get('summary', '')} Want the practical takeaways?"
    elif kind in ("perf_dip", "seasonal_perf_dip"):
        metric = str(payload.get("metric", "calls"))
        delta = payload.get("delta_pct", performance.get("delta_7d", {}).get(f"{metric}_pct"))
        window = payload.get("window", "7d")
        if delta is not None:
            body = f"{first}, your {metric} are down {_pct(delta)} over {window}"
            if payload.get("vs_baseline") is not None:
                body += f" (baseline {payload['vs_baseline']})"
            body += "."
        else:
            body = f"{first}, I noticed a dip in your {metric} in the latest performance snapshot."
        if kind == "seasonal_perf_dip" and payload.get("season_note"):
            body += f" The supplied seasonal note is {payload['season_note'].replace('_', ' ')}."
        body += " Want me to review the listing and suggest one change to test?"
        rationale = "Performance message uses the trigger delta and offers one concrete next step."
    elif kind == "perf_spike":
        metric = str(payload.get("metric", "views"))
        delta = payload.get("delta_pct", performance.get("delta_7d", {}).get(f"{metric}_pct"))
        body = f"{first}, your {metric} are up {_pct(delta)}" if delta is not None else f"{first}, your {metric} have increased in the latest snapshot"
        body += f" over {payload.get('window', '7d')}. Want me to pinpoint what may be driving it and turn it into a repeatable post?"
        rationale = "Celebrates the measured increase and asks permission for a low-effort follow-up."
    elif kind in ("renewal_due", "winback_eligible"):
        days = payload.get("days_remaining")
        if days is None:
            days = merchant.get("subscription", {}).get("days_remaining")
        if kind == "renewal_due":
            body = f"{first}, your {merchant.get('subscription', {}).get('plan', payload.get('plan', ''))} plan is due for renewal in {days} days" if days is not None else f"{first}, your plan renewal is coming up."
            if payload.get("renewal_amount"):
                body += f" The listed renewal amount is ₹{payload['renewal_amount']:,}."
            body += " Want me to share the renewal steps?"
        else:
            body = f"{first}, since expiry, the supplied snapshot shows {payload.get('lapsed_customers_added_since_expiry', 'additional')} more lapsed customers"
            if payload.get("perf_dip_pct") is not None:
                body += f" and a {_pct(payload['perf_dip_pct'])} performance dip"
            body += ". Want to review a simple reactivation plan?"
        rationale = "Commercial follow-up is anchored to the supplied renewal or winback figures."
    elif kind == "festival_upcoming":
        fest = payload.get("festival", "upcoming festival")
        date = payload.get("date")
        days = payload.get("days_until")
        timing = f" on {date}" if date else (f" in {days} days" if days is not None else "")
        body = f"{first}, {fest}{timing} is a chance to plan a timely update for {merchant.get('category_slug', 'your business')}"
        if offers:
            body += f"; your active offer is {offers}."
        else:
            body += "."
        body += " Want me to draft a category-fit post for your approval?"
    elif kind == "ipl_match_today":
        body = f"{first}, {payload.get('match', 'today’s match')} is at {payload.get('match_time_iso', 'the supplied match time')}"
        if payload.get("venue"):
            body += f" near {payload['venue']}"
        body += ". Want a quick match-night post draft using your current menu?"
    elif kind == "curious_ask_due":
        question_map = {"what_service_in_demand_this_week": "What service are customers asking for most this week?", "what_item_selling": "Which item is selling fastest this week?"}
        question = question_map.get(payload.get("ask_template"), "What are customers asking for most this week?")
        body = f"{first}, quick question: {question} I can turn your answer into a short local post."
        rationale = "A single relevant question starts a knowledge-led conversation without an unsupported claim."
    elif kind == "competitor_opened":
        distance = payload.get("distance_km")
        category_label = category.get("display_name", "business")
        competitor = payload.get("competitor_name") or payload.get("name")
        body = f"{first}, {competitor} has opened nearby" if competitor else f"{first}, a new {category_label.rstrip('s').lower()} has opened nearby"
        if distance is not None:
            body += f" ({distance} km away)"
        if payload.get("opened_date"):
            body += f" since {payload['opened_date']}"
        if payload.get("locality"):
            body += f" in {payload['locality']}"
        their_offer = payload.get("their_offer")
        own_offer = next((o.get("title") for o in merchant.get("offers", []) if o.get("status", "active").lower() == "active" and o.get("title")), None)
        if their_offer:
            body += f"; their listed offer is {their_offer}"
            if own_offer:
                body += f", while yours is {own_offer}"
        body += ". Want me to review the two listings and draft a clear update for yours?"
    elif kind == "review_theme_emerged":
        theme = _titlecase(payload.get("theme", "feedback"))
        count = payload.get("occurrences_30d")
        body = f"{first}, {theme.lower()} appeared in {count} reviews over the last 30 days" if count is not None else f"{first}, a review pattern around {theme.lower()} is emerging"
        if payload.get("common_quote"):
            body += f": “{payload['common_quote']}”"
        body += ". Want me to draft a response and one practical fix to consider?"
    elif kind == "milestone_reached":
        metric = _titlecase(payload.get("metric", "milestone")).lower()
        value = payload.get("value_now", payload.get("milestone_value"))
        body = f"{first}, a nice milestone: {value} {metric}" if value is not None else f"{first}, you have reached a new {metric} milestone"
        if payload.get("milestone_value") and payload.get("value_now") and payload.get("is_imminent"):
            body = f"{first}, you're at {payload['value_now']} {metric} — just {payload['milestone_value'] - payload['value_now']} to the {payload['milestone_value']} mark"
        body += ". Want a short thank-you post for customers?"
    elif kind == "dormant_with_vera":
        body = f"{first}, it's been {payload.get('days_silent', 14)} days since we last worked on your profile."
        if merchant.get("signals"):
            body += f" I can pick up with {str(merchant['signals'][0]).replace('_', ' ')}."
        body += " What would be most useful this week?"
    elif kind == "gbp_unverified":
        body = f"{first}, your Google Business Profile is still unverified. Verification can help keep the listing under your control. Want the setup steps?"
    elif kind == "active_planning_intent":
        topic = _titlecase(payload.get("intent_topic", "your plan"))
        body = f"{first}, picking up your {topic}: I can draft the first version now."
        if payload.get("merchant_last_message"):
            body += f" You said, “{payload['merchant_last_message']}.”"
        body += " Shall I put together the outline?"
        rationale = "Resumes the recorded planning intent and moves directly toward the requested work."
    elif kind in ("supply_alert", "summer_demand_shift", "category_seasonal"):
        fact = payload.get("alert") or payload.get("trend") or payload.get("season_note") or payload.get("title")
        body = f"{first}, {fact or 'there is a timely category signal in your trigger context'}."
        if offers:
            body += f" Your active offer is {offers}."
        body += " Want me to draft a practical update for this week?"
    else:
        fact = payload.get("title") or payload.get("summary") or payload.get("note")
        body = f"{first}, a {kind.replace('_', ' ')} update"
        if fact:
            body += f": {fact}"
        body += ". Want me to prepare a useful next step?"

    if not body.strip():
        return {"body": "", "cta": "none", "send_as": "vera", "suppression_key": trigger.get("suppression_key", ""), "rationale": "No supported composition is available for this trigger."}
    return {"body": re.sub(r"\s+", " ", body).strip(), "cta": cta, "send_as": "vera", "suppression_key": trigger.get("suppression_key", ""), "rationale": rationale}


def _load_context(scope: str, context_id: str) -> dict | None:
    return contexts.get((scope, context_id), {}).get("payload")


def _context_count() -> dict:
    out = {scope: 0 for scope in VALID_SCOPES}
    for scope, _ in contexts:
        out[scope] = out.get(scope, 0) + 1
    return out


def _handle_context(data: dict) -> tuple[int, dict]:
    scope = data.get("scope")
    if scope not in VALID_SCOPES:
        return 400, {"accepted": False, "reason": "invalid_scope", "details": "scope must be category, merchant, customer, or trigger"}
    context_id = data.get("context_id")
    version = data.get("version")
    payload = data.get("payload")
    if not isinstance(context_id, str) or not isinstance(version, int) or not isinstance(payload, dict):
        return 400, {"accepted": False, "reason": "malformed_context", "details": "context_id, integer version, and object payload are required"}
    key = (scope, context_id)
    current = contexts.get(key)
    if current and version < current["version"]:
        return 409, {"accepted": False, "reason": "stale_version", "current_version": current["version"]}
    if current and version == current["version"]:
        return 200, {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": current["stored_at"], "duplicate": True}
    stored_at = datetime.now(timezone.utc).isoformat()
    contexts[key] = {"version": version, "payload": payload, "stored_at": stored_at}
    return 200, {"accepted": True, "ack_id": f"ack_{context_id}_v{version}", "stored_at": stored_at}


def _handle_tick(data: dict) -> dict:
    actions = []
    now = data.get("now") or datetime.now(timezone.utc).isoformat()
    for trigger_id in data.get("available_triggers", [])[:20]:
        trigger = _load_context("trigger", trigger_id)
        if not trigger:
            continue
        merchant_id = trigger.get("merchant_id")
        merchant = _load_context("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue
        category = _load_context("category", merchant.get("category_slug"))
        if not category:
            continue
        customer_id = trigger.get("customer_id")
        customer = _load_context("customer", customer_id) if customer_id else None
        composed = compose(category, merchant, trigger, customer)
        suppression = composed.get("suppression_key") or trigger_id
        if suppression in sent_suppressions or not composed.get("body"):
            continue
        conversation_id = f"conv_{trigger_id}_{int(time.time() * 1000)}"
        action = {"conversation_id": conversation_id, "merchant_id": merchant_id, "customer_id": customer_id, "send_as": composed["send_as"], "trigger_id": trigger_id, "template_name": "vera_customer_update_v1" if customer_id else "vera_context_update_v1", "template_params": [composed["body"]], "body": composed["body"], "cta": composed["cta"], "suppression_key": suppression, "rationale": composed["rationale"]}
        actions.append(action)
        sent_suppressions.add(suppression)
        conversations[conversation_id] = {"merchant_id": merchant_id, "customer_id": customer_id, "trigger": trigger, "category": category, "merchant": merchant, "customer": customer, "turns": [{"role": "vera", "body": composed["body"]}], "sent_bodies": {composed["body"]}, "auto_reply_candidates": {}}
        if len(actions) >= 20:
            break
    return {"actions": actions}


AUTO_REPLY_PATTERNS = ("thank you for contacting", "thanks for contacting", "team will get back", "hamari team tak", "automated assistant", "this is an automated", "your message has been received", "office hours", "we will respond shortly")
OPT_OUT_PATTERNS = ("stop", "unsubscribe", "don't message", "do not message", "not interested", "no thanks", "band karo", "mat bhejo")
INTENT_PATTERNS = ("yes", "go ahead", "let's do it", "lets do it", "please do", "send it", "share it", "i want to join", "sign me up", "start", "update my", "publish", "draft it")


def respond(conversation_id: str, message: str, from_role: str = "merchant", turn_number: int | None = None) -> dict:
    state = conversations.setdefault(conversation_id, {"turns": [], "sent_bodies": set(), "auto_reply_candidates": {}})
    normalized = re.sub(r"\s+", " ", message.strip().lower())
    state["turns"].append({"role": from_role, "body": message})
    if not normalized or any(term in normalized for term in OPT_OUT_PATTERNS):
        return {"action": "end", "rationale": "Honors the opt-out or clear refusal immediately."}
    repeats = state.setdefault("auto_reply_candidates", {})
    count = repeats.get(normalized, 0) + 1
    repeats[normalized] = count
    auto_like = count >= 2 or any(pattern in normalized for pattern in AUTO_REPLY_PATTERNS)
    if auto_like:
        if state.get("auto_reply_followup_sent"):
            return {"action": "end", "rationale": "Repeated or recognizable canned auto-reply; stop after one brief check-in."}
        state["auto_reply_followup_sent"] = True
        body = "Thanks — I may have reached your business auto-reply. If the owner or manager would like help, they can reply here; I’ll leave it with you for now."
        state["sent_bodies"].add(body)
        state["turns"].append({"role": "vera", "body": body})
        return {"action": "send", "body": body, "cta": "none", "rationale": "Recognizes likely automation, makes one low-pressure handoff, then will exit if repeated."}
    if any(term in normalized for term in INTENT_PATTERNS):
        category = state.get("merchant", {}).get("category_slug", "")
        if any(term in normalized for term in ("join", "sign me up")):
            body = "Great — I’ll move straight to onboarding. Please share the best owner or manager contact and preferred callback time, and I’ll pass those details to the onboarding team."
        elif any(term in normalized for term in ("update my", "publish")):
            body = "Understood. I’ll move ahead with the profile update using the details already available. If a field needs confirmation, I’ll ask for that specific detail next."
        elif state.get("trigger", {}).get("kind") in ("research_digest", "cde_opportunity"):
            body = "Absolutely. I’ll pull the supplied source details and prepare a short, shareable customer note for your review."
        elif category == "restaurants":
            body = "On it. I’ll draft the first version using your current menu and the event details in your context, then you can review it before it goes live."
        else:
            body = "On it. I’ll prepare the first draft from the details already in your profile so you can review it."
        if body in state.get("sent_bodies", set()):
            return {"action": "end", "rationale": "Avoids repeating a message already sent in this conversation."}
        state.setdefault("sent_bodies", set()).add(body)
        state["turns"].append({"role": "vera", "body": body})
        return {"action": "send", "body": body, "cta": "open_ended", "rationale": "Recognizes explicit intent and moves directly to the requested action."}
    if any(q in normalized for q in ("what", "how much", "which", "when", "where", "can you")):
        body = "I can help with that. I only have the details in your current profile and trigger context, so tell me the specific item you mean and I’ll check what’s available."
        if body in state.get("sent_bodies", set()):
            return {"action": "end", "rationale": "No new supported information to add; avoids repeating itself."}
        state.setdefault("sent_bodies", set()).add(body)
        state["turns"].append({"role": "vera", "body": body})
        return {"action": "send", "body": body, "cta": "open_ended", "rationale": "Answers within available context and asks for a specific missing detail."}
    if turn_number and turn_number >= 5:
        return {"action": "end", "rationale": "Conversation reached the five-turn ceiling; close politely."}
    return {"action": "wait", "wait_seconds": 1800, "rationale": "Acknowledges the reply without adding pressure; wait for the merchant's next message."}


class Handler(BaseHTTPRequestHandler):
    server_version = "VeraChallenge/1.0"

    def _send(self, status: int, obj: dict):
        encoded = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/healthz":
            return self._send(200, {"status": "ok", "uptime_seconds": int(time.time() - STARTED), "contexts_loaded": _context_count()})
        if path == "/v1/metadata":
            return self._send(200, {"team_name": "Vera Context Crew", "team_members": [], "model": "deterministic-python", "approach": "trigger-routed, context-grounded composer with explicit conversation state", "version": VERSION})
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 500_000:
                return self._send(413, {"error": "payload_too_large"})
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "invalid_json"})
        if path == "/v1/context":
            status, response = _handle_context(data)
            return self._send(status, response)
        if path == "/v1/tick":
            return self._send(200, _handle_tick(data))
        if path == "/v1/reply":
            conv_id = data.get("conversation_id")
            if not isinstance(conv_id, str) or not isinstance(data.get("message"), str):
                return self._send(400, {"error": "conversation_id_and_message_required"})
            result = respond(conv_id, data["message"], data.get("from_role", "merchant"), data.get("turn_number"))
            return self._send(200, result)
        if path == "/v1/teardown":
            contexts.clear(); conversations.clear(); sent_suppressions.clear()
            return self._send(200, {"cleared": True})
        return self._send(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        return


def serve(host: str = "0.0.0.0", port: int = 8080):
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def _load_expanded() -> tuple[dict, dict, dict, dict]:
    root = Path(__file__).parent / "dataset" / "expanded"
    categories = {json.loads(p.read_text(encoding="utf-8"))["slug"]: json.loads(p.read_text(encoding="utf-8")) for p in (root / "categories").glob("*.json")}
    merchants = {}
    for path in (root / "merchants").glob("*.json"):
        obj = json.loads(path.read_text(encoding="utf-8")); merchants[obj["merchant_id"]] = obj
    customers = {}
    for path in (root / "customers").glob("*.json"):
        obj = json.loads(path.read_text(encoding="utf-8")); customers[obj["customer_id"]] = obj
    triggers = {}
    for path in (root / "triggers").glob("*.json"):
        obj = json.loads(path.read_text(encoding="utf-8")); triggers[obj["id"]] = obj
    return categories, merchants, customers, triggers


def build_submission(output: str | Path = "submission.jsonl") -> int:
    categories, merchants, customers, triggers = _load_expanded()
    pairs = json.loads((Path(__file__).parent / "dataset" / "expanded" / "test_pairs.json").read_text(encoding="utf-8"))["pairs"]
    lines = []
    for pair in pairs:
        trigger = triggers[pair["trigger_id"]]
        merchant = merchants[pair["merchant_id"]]
        category = categories[merchant["category_slug"]]
        customer = customers.get(pair.get("customer_id"))
        result = compose(category, merchant, trigger, customer)
        lines.append(json.dumps({"test_id": pair["test_id"], **result}, ensure_ascii=False))
    Path(output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run Vera challenge bot or build the canonical submission.")
    parser.add_argument("--build-submission", action="store_true")
    parser.add_argument("--output", default="submission.jsonl")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.build_submission:
        print(f"Wrote {build_submission(args.output)} canonical messages to {args.output}")
    else:
        serve(args.host, args.port)

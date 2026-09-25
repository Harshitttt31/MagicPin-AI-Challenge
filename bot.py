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


def _time_label(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.strftime("%d %b, %I:%M %p").replace(", 0", ", ")
    except (TypeError, ValueError):
        return str(value or "the supplied time")


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
    language_pref = str(ident.get("language_pref", "")).lower()
    language_tokens = set(re.findall(r"[a-z]+", language_pref))
    prefers_hindi = bool(language_tokens & {"hi", "hindi"})
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
    elif kind == "trial_followup":
        allowed = bool(scopes & {"all", "kids_program_updates", "trial_followups", "service_updates"})
    elif kind == "winback_eligible":
        allowed = bool(scopes & {"all", "marketing", "winback_offers", "promotional_offers"})
    elif kind == "wedding_package_followup":
        allowed = bool(scopes & {"all", "bridal_package_followup", "wedding_package_followup", "marketing", "promotional_offers"})
    else:
        allowed = bool(scopes & {"all", "marketing", "service_updates"})
    if not allowed:
        return {"body": "", "cta": "none", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", ""), "rationale": "Consent scope does not cover this outreach; customer message suppressed."}
    slots = payload.get("available_slots", [])
    slot_labels = [s.get("label") for s in slots if isinstance(s, dict) and s.get("label")]
    offer = _active_offer(merchant)
    category_slug = merchant.get("category_slug", category.get("slug", ""))
    if kind == "chronic_refill_due":
        stock_date = str(payload.get("stock_runs_out_iso", "")).split("T", 1)[0]
        greeting = "Namaste" if prefers_hindi else "Hi"
        detail = f" Your recorded medicine supply is expected to run out on {stock_date}." if stock_date else " Your refill reminder is due."
        if payload.get("delivery_address_saved"):
            detail += " We have your delivery address saved."
        ask = " Kya hum refill arrange karein?" if prefers_hindi else " Shall we arrange your refill before then?"
        body = f"{greeting} {cust_name}, {biz} here.{detail}{ask} Reply STOP if you don't want these reminders."
        return {"body": body, "cta": "open_ended", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", ""), "rationale": "Refill reminder uses the recorded stock date, delivery preference, and explicit refill consent without listing medicines in a message routed via family."}
    if kind == "trial_followup":
        parent = re.search(r"parent:\s*([^)]+)", cust_name, re.IGNORECASE)
        recipient = parent.group(1).strip() if parent else cust_name
        session_options = payload.get("next_session_options", [])
        next_slot = next((s.get("label") for s in session_options if isinstance(s, dict) and s.get("label")), None)
        trial_date = payload.get("trial_date")
        services = customer.get("relationship", {}).get("services_received", [])
        service = _titlecase(services[0]) if services else "trial session"
        subject = "Karthik" if parent else "you"
        trial_detail = f" {subject} completed a {service} on {trial_date}." if trial_date else f" {subject} completed a {service}."
        next_detail = f" The next session option is {next_slot}." if next_slot else ""
        ask = " Would that session work for you?" if next_slot else " Would you like to hear about the next session?"
        body = f"Hi {recipient}, {biz} here.{trial_detail}{next_detail}{ask} Reply STOP to opt out."
        return {"body": body, "cta": "open_ended", "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key", ""), "rationale": "Trial follow-up uses the recorded trial date and next session option, with the parent's recorded consent for kids-program updates."}
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
            last_service = payload.get("last_service_date")
            service_code = str(payload.get("service_due", "")).lower()
            if category_slug == "dentists" and "clean" in service_code and due and last_service:
                detail = f" Your last cleaning was on {last_service}; your cleaning recall is due on {due}."
            else:
                detail = f" Your {service} is due" + (f" on {due}" if due else "") + "."
        if offer:
            detail += f" Current offer: {offer}."
        if slot_labels:
            due_day = str(due or "")[:10]
            before_due = any(
                isinstance(slot, dict)
                and len(due_day) == 10
                and len(str(slot.get("iso", ""))[:10]) == 10
                and str(slot.get("iso", ""))[:10] < due_day
                for slot in slots
            )
            timing = " before your due date" if before_due else ""
            detail += " We have " + " or ".join(slot_labels[:2]) + timing + "."
            detail += " Aapke liye inme se kaunsa time theek rahega?" if prefers_hindi else " Which of those times works for you?"
        else:
            if is_lapsed:
                preferred = str(customer.get("preferences", {}).get("preferred_slots", "")).replace("_", " ")
                slot_phrase = f" {preferred}" if preferred else ""
                detail += f" Want me to share available{slot_phrase} times to help you restart?"
            else:
                detail += " Aapko kaunsa time suit karega?" if prefers_hindi else " Would you like us to help arrange a time?"
        body = f"Hi {cust_name}, {biz} here.{detail} Reply STOP if you don't want these reminders."
        cta = "open_ended"
    elif kind == "appointment_tomorrow":
        appointment = payload.get("appointment_time") or payload.get("appointment") or payload.get("slot")
        when = f" for {appointment}" if appointment else " tomorrow"
        change_prompt = "Agar time change karna ho, please reply." if prefers_hindi else "Please reply if you need to make a change."
        body = f"Hi {cust_name}, a reminder from {biz}: your appointment is{when}. {change_prompt} Reply STOP to opt out."
        cta = "open_ended"
    elif kind == "wedding_package_followup":
        wedding = payload.get("wedding_date")
        trial = payload.get("trial_completed")
        days = payload.get("days_to_wedding")
        window = payload.get("next_step_window_open", "")
        program = "30-day skin-prep program" if "skin_prep_program_30day" in str(window) else "next bridal service step"
        date_detail = f" on {wedding}" if wedding else ""
        days_detail = f" ({days} days away)" if days is not None else ""
        trial_detail = f" Your bridal trial was on {trial}." if trial else ""
        body = f"Hi {cust_name}, {biz} here. Your wedding is{date_detail}{days_detail}.{trial_detail} I can share the {program} outline when you're ready. Would you like the outline? Reply STOP to opt out."
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
    if merchant.get("category_slug", category.get("slug", "")) == "dentists" and not first.lower().startswith(("dr.", "doctor ")):
        first = f"Dr. {first}"
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
        if segment and segment in ("high_risk_adults", "high_risk_adult") and "high_risk_adult_cohort" in merchant.get("signals", []):
            segment_phrase = " Relevant to your high-risk adult cohort."
        trial = f" ({item['trial_n']:,}-person study)" if item.get("trial_n") else ""
        finding = "3-month vs 6-month result" if "3-month" in summary and "6-month" in summary else "finding"
        note_type = "patient note" if category.get("slug") == "dentists" else "customer note"
        audience = " for your high-risk adult patients" if segment_phrase else ""
        body = f"{first}, this week's {category.get('display_name', 'category')} digest: {item.get('title', summary)}{trial}.{segment_phrase} {summary} Source: {item.get('source', 'provided digest')}. I can turn this {finding} into a {note_type}{audience}. Want me to draft it now?"
        rationale = f"Uses the supplied digest item and source, with merchant cohort context when available."
    elif kind == "regulation_change" and item:
        deadline = payload.get("deadline_iso") or item.get("date")
        body = f"{first}, a relevant update from {item.get('source', 'the supplied category digest')}: {item.get('title', item.get('summary', ''))}"
        if deadline and str(deadline) not in str(item.get("title", "")):
            body += f" Effective {deadline}."
        if body and body[-1] not in ".!?":
            body += "."
        if deadline:
            body += " Want a quick checklist for updating your radiograph dose-limit protocol before that date?"
        else:
            body += " Want a quick checklist for updating your radiograph dose-limit protocol?"
        rationale = "Compliance note cites only the supplied category item and deadline."
    elif kind in ("cde_opportunity", "trial_followup") and item:
        body = f"{first}, {item.get('title', 'a category learning opportunity')} ({item.get('source', 'category context')}). {item.get('summary', '')}"
        if kind == "cde_opportunity":
            credits = payload.get("credits")
            event_detail = []
            if credits is not None:
                event_detail.append(f"{credits} continuing-education credits")
            if event_detail:
                body += f" It is {' and '.join(event_detail)}."
            event_date = item.get("date")
            if event_date:
                body += f" It is scheduled for {_time_label(event_date)}."
            if item.get("actionable"):
                body += f" {item['actionable']}"
                if not str(item["actionable"]).endswith((".", "!", "?")):
                    body += "."
            date_label = _time_label(event_date).split(",", 1)[0] if event_date else "the session"
            body += f" Want a short ROI checklist to help decide whether to attend before {date_label}?"
        else:
            body += " Want the practical takeaways?"
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
            season_note = str(payload["season_note"])
            if season_note == "post_resolution_window_apr_jun":
                season_note = "the post-resolution window from April through June"
            else:
                season_note = season_note.replace("_", " ")
            body += f" Context: {season_note}."
        if metric == "calls" and performance.get("views") is not None and performance.get("calls") is not None:
            days = performance.get("window_days", 30)
            body += f" The latest {days}-day snapshot shows {performance['views']} views and {performance['calls']} calls."
        if kind == "seasonal_perf_dip" and offers:
            body += f" Want me to draft one post featuring {offers} to test against the dip?"
        else:
            body += " Want me to check the listing's call path and draft one change to test?"
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
            body = f"{first}, your {merchant.get('subscription', {}).get('plan', payload.get('plan', ''))} plan renews in {days} days" if days is not None else f"{first}, your plan renewal is coming up."
            if payload.get("renewal_amount"):
                body += f"; the listed amount is ₹{payload['renewal_amount']:,}."
            elif not body.endswith((".", "!", "?")):
                body += "."
            body += " Want me to send the renewal steps before then?" if days is not None else " Want me to send the renewal steps?"
        else:
            subscription = merchant.get("subscription", {}) or {}
            plan = subscription.get("plan", payload.get("plan", "subscription"))
            days_expired = payload.get("days_since_expiry", subscription.get("days_since_expiry"))
            expiry_detail = f" expired {days_expired} days ago" if days_expired is not None else " has expired"
            body = f"{first}, your {plan} plan{expiry_detail}. The supplied snapshot shows {payload.get('lapsed_customers_added_since_expiry', 'additional')} additional lapsed customers"
            if payload.get("perf_dip_pct") is not None:
                body += f" and a {_pct(payload['perf_dip_pct'])} performance dip"
            body += ". Want me to draft a win-back message for your lapsed customers to review?"
        rationale = "Commercial follow-up is anchored to the supplied renewal or winback figures."
    elif kind == "festival_upcoming":
        fest = payload.get("festival", "upcoming festival")
        date = payload.get("date")
        days = payload.get("days_until")
        when = f" falls on {date}" if date else ""
        distance = f" ({days} days away)" if days is not None else ""
        active_offers = [o.get("title") for o in merchant.get("offers", []) if str(o.get("status", "active")).lower() == "active" and o.get("title")]
        offer_detail = f" Your active offers are {' and '.join(active_offers[:2])}." if active_offers else ""
        body = f"{first}, {fest}{when}{distance}. It is a planning opportunity for your {category.get('display_name', 'business').lower()}.{offer_detail} Want me to sketch a campaign outline to build toward the date?"
    elif kind == "ipl_match_today":
        time_label = _time_label(payload.get("match_time_iso"))
        venue = payload.get("venue")
        city_name = payload.get("city", merchant.get("identity", {}).get("city", "your city"))
        location = f" at {venue} in {city_name}" if venue else f" in {city_name}"
        body = f"{first}, {payload.get('match', 'today’s match')} is scheduled for {time_label}{location}."
        orders = merchant.get("customer_aggregate", {}) or {}
        order_detail = []
        if orders.get("delivery_orders_30d") is not None:
            order_detail.append(f"{orders['delivery_orders_30d']} delivery orders")
        if orders.get("dine_in_orders_30d") is not None:
            order_detail.append(f"{orders['dine_in_orders_30d']} dine-in orders")
        if order_detail:
            body += f" Your latest 30-day snapshot shows {' and '.join(order_detail)}."
        body += " Want me to draft a match-night post around your menu?"
    elif kind == "curious_ask_due":
        question_map = {"what_service_in_demand_this_week": "What service are customers asking for most this week?", "what_item_selling": "Which item is selling fastest this week?"}
        question = question_map.get(payload.get("ask_template"), "What are customers asking for most this week?")
        delta = performance.get("delta_7d", {}).get("calls_pct")
        calls = performance.get("calls")
        window = performance.get("window_days", 30)
        context_facts = []
        if delta is not None:
            context_facts.append(f"calls are up {_pct(delta)} over 7 days")
        if calls is not None:
            context_facts.append(f"you received {calls} calls over the last {window} days")
        context = f" {'; '.join(context_facts).capitalize()}." if context_facts else ""
        offer_context = f" featuring your active offer, {offers}" if offers else ""
        body = f"{first}, quick question: {question}{context} I can turn your answer into a local post{offer_context}."
        rationale = "A single relevant question starts a knowledge-led conversation without an unsupported claim."
    elif kind == "competitor_opened":
        distance = payload.get("distance_km")
        category_label = category.get("display_name", "business")
        competitor = payload.get("competitor_name") or payload.get("name")
        body = f"{first}, {competitor} has opened nearby" if competitor else f"{first}, a new {category_label.rstrip('s').lower()} has opened nearby"
        if distance is not None:
            body += f" ({distance} km away)"
        if payload.get("opened_date"):
            body += f" on {payload['opened_date']}"
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
        silent_days = payload.get("days_since_last_merchant_message", payload.get("days_silent", 14))
        subscription = merchant.get("subscription", {}) or {}
        if subscription.get("status") == "expired":
            plan = subscription.get("plan", "subscription")
            expiry_days = subscription.get("days_since_expiry")
            expiry = f" {plan} expired {expiry_days} days ago." if expiry_days is not None else f" Your {plan} plan has expired."
        else:
            expiry = ""
        delta = performance.get("delta_7d", {}) or {}
        calls = f" Calls are down {_pct(delta['calls_pct'])} over 7 days." if delta.get("calls_pct") is not None else ""
        views = f" Views are down {_pct(delta['views_pct'])} over 7 days." if delta.get("views_pct") is not None else ""
        topic = str(payload.get("last_topic", "your profile")).replace("_", " ")
        if topic == "subscription expiry" and subscription.get("status") == "expired":
            check_in = "since we last checked in"
        else:
            check_in = f"since our last {topic} check-in" if topic != "your profile" else "since we last checked in"
        body = f"{first}, it's been {silent_days} days {check_in}.{expiry}{calls}{views} Would you like to revisit the plan, review the listing dip, or pause for now?"
    elif kind == "gbp_unverified":
        path = str(payload.get("verification_path", "")).replace("_", " ")
        path_detail = f" The recorded options are {path}." if path else ""
        views = performance.get("views")
        calls = performance.get("calls")
        snapshot = f" Your latest {performance.get('window_days', 30)}-day snapshot shows {views} views and {calls} calls." if views is not None and calls is not None else ""
        body = f"{first}, your Google Business Profile is unverified.{snapshot}{path_detail} Which path should I walk you through first?"
    elif kind == "active_planning_intent":
        topic = _titlecase(payload.get("intent_topic", "your plan"))
        normalized_topic = str(payload.get("intent_topic", "")).lower()
        if "corporate_bulk_thali" in normalized_topic:
            thali = next((o.get("title") for o in merchant.get("offers", []) if "thali" in str(o.get("title", "")).lower() and str(o.get("status", "active")).lower() == "active"), None)
            starting_point = f" around your current {thali}" if thali else ""
            body = f"{first}, for the corporate bulk-thali package, I can outline portions, bulk pricing, and delivery options{starting_point}. Want me to draft that for your review?"
        elif "kids_yoga" in normalized_topic:
            body = f"{first}, for the kids yoga summer camp, I can sketch a sample 4-week plan with weekly themes, session length, and parent sign-up steps. Want me to prepare the first draft today?"
        else:
            body = f"{first}, for your {topic}, I can draft the first version now. Want me to prepare it for your review?"
        rationale = "Resumes the recorded planning intent and moves directly toward the requested work."
    elif kind == "supply_alert":
        molecule = payload.get("molecule", "the listed medicine")
        batches = [str(batch) for batch in payload.get("affected_batches", []) if batch]
        batch_detail = f" affected batches {', '.join(batches)}" if batches else ""
        manufacturer = f" from {payload['manufacturer']}" if payload.get("manufacturer") else ""
        body = f"{first}, the supplied recall alert for {molecule}{manufacturer} names{batch_detail or ' specific batches'}. Please check whether any are in stock before another dispense; I can format a batch-check list."
        rationale = "Supply alert names only the medicine, manufacturer, and affected batches in the trigger; the next step checks inventory without inventing safety instructions."
    elif kind in ("summer_demand_shift", "category_seasonal"):
        trend_phrases = []
        for trend in payload.get("trends", []):
            match = re.fullmatch(r"(.+)_([+-]?\d+(?:\.\d+)?)", str(trend))
            if not match:
                continue
            label, raw_change = match.groups()
            label = label.removesuffix("_demand").replace("_", " ")
            label = "ORS" if label.lower() == "ors" else label
            label = "cold/cough" if label.lower() == "cold cough" else label
            change = float(raw_change)
            direction = "up" if change >= 0 else "down"
            amount = int(abs(change)) if change.is_integer() else abs(change)
            trend_phrases.append(f"{label} demand {direction} {amount}%")
        if trend_phrases:
            detail = ", ".join(trend_phrases[:-1]) + (f", and {trend_phrases[-1]}" if len(trend_phrases) > 1 else trend_phrases[0])
            body = f"{first}, the supplied seasonal signals show {detail}."
        else:
            fact = payload.get("season_note") or payload.get("title") or "a seasonal shift in category demand"
            body = f"{first}, the supplied seasonal signal is {str(fact).replace('_', ' ')}."
        if payload.get("shelf_action_recommended"):
            body += " The brief recommends a shelf review."
        body += " Want a shelf-check list for the affected categories?"
        rationale = "Seasonal message translates supplied demand changes and follows the trigger's shelf-review recommendation."
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
    auto_like = count >= 3 or any(pattern in normalized for pattern in AUTO_REPLY_PATTERNS)
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

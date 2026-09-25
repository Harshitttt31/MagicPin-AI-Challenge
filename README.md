# Vera Merchant AI Assistant

**Public bot URL:** https://magicpin-ai-challenge-by-harshitgahlaut.onrender.com

## Approach

Vera is implemented as a deterministic, trigger-routed composer. Each message is grounded in the category, merchant, trigger, and optional customer context supplied by the judge. The composer uses specific facts and a single next step, follows category voice, and suppresses customer outreach when the recorded consent does not cover that message type.

Conversation handling recognizes opt-outs, common business auto-replies, and explicit intent. It exits after an opt-out or repeated auto-reply and advances directly to action when a merchant agrees.

## Run

Requires Python 3.10 or newer. Start the HTTP service with:

```bash
python bot.py --host 0.0.0.0 --port 8080
```

The service exposes:

- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`
- `GET /v1/healthz`
- `GET /v1/metadata`

It uses in-memory context and conversation state, so the process must remain running during the judge session.

## Tradeoffs

The deterministic rules are fast, reproducible, and auditable, but less flexible than a hosted language model. Messages use only facts present in the supplied contexts. Customer messages with insufficient consent are suppressed.

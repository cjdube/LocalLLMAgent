# Shopping

Tell Wren what you want and your budget — "I'd like a standing desk, budget
$300" — and she starts a background job that finds the best-priced options
that fit, then pushes you when it's done. Ask her for the result and you get up
to five picks with links, and a draft lower offer for any seller that takes
offers.

**It never buys and never contacts a seller.** The offer is text for you to
send yourself. Wren only reads.

## How a request flows

1. Chat: the model calls `start_shopping(description, max_price, must_haves)`.
   It is in `WRITE_TOOLS`, so you tap to start it — the card shows the budget
   the model parsed, which is the point of the tap.
2. `agent/tools/shopping.py:start_shopping` validates the budget, checks that at least one store has its keys, and queues a
   job in `config/bg_jobs.json` with `kind: "shopping"` and the arguments in
   `params`.
3. `tasks/bg_worker.py` sees the kind and calls `shopping.shop()` directly —
   **not** the `advance()` tool loop. The job is offered no tools at all.
4. The job's result is the full plain-text report; the push carries a one-line
   headline. Ask Wren "what did the shopping find?" and she reads it with
   `get_job_result`.

## The pipeline (`shop()`)

A fixed pipeline, same shape as `research.py`, because a small model left to
"go shopping" wanders ([model-constraints.md](model-constraints.md)). The model
does two bounded jobs, both `think=False`; Python does everything else.

| Step | Who | What |
| --- | --- | --- |
| Queries | model | 2–3 search phrases, one per line. Nothing parses → the description is used, WARNING logged. |
| Gather | Python | Each query against eBay and Best Buy, filtered to the budget; one Tavily search for review context. |
| Merge | Python | Dedupe, sort by total price (price + shipping), keep the cheapest 12. |
| Score | model | `n\|score\|reason` per numbered listing. It never sees a URL, seller or id ([opaque-identifiers.md](opaque-identifiers.md)). Fewer lines than listings → WARNING. |
| Rank | Python | Score ≥ 6, best score first, then cheapest; up to 5. Nothing scores ≥ 6 → the 3 cheapest, `ranking: "price_only"`, WARNING. |
| Haggle | Python | See below. |

A model-call failure (Ollama restarting) is not caught: it reaches the worker's
transient-retry path, so the job retries instead of failing.

## The negotiation kit

All Python, no model:

- **eBay listing that takes offers** (`BEST_OFFER`): a suggested offer 15% under
  the asking price, rounded down to a whole dollar ($5 steps at $100 and up),
  plus a ready-to-send message.
- **Best Buy item on sale**: how much is off the regular price.
- **Every Best Buy item**: a reminder that Best Buy price-matches major online
  retailers.
- **Shipping unknown**: a warning to check before committing.

## Sources

Official, free, key-based APIs only — the data sourcing policy in AGENTS.md. No
page scraping.

| Source | Key(s) | Note |
| --- | --- | --- |
| eBay Browse API | `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | Production keyset from developer.ebay.com. Client-credentials token, cached in memory for the run. Only fixed-price and best-offer listings — an auction price is not a price. |
| Best Buy Products API | `BESTBUY_API_KEY` | Free key from developer.bestbuy.com — sign up with a domain email; a free address (Gmail) is refused. Free for personal use; commercial use needs a partner agreement. The key rides in the query string, so every error goes through `http_error`, which redacts it. |
| Tavily | `TAVILY_API_KEY` | Review context only — never an offer. |

If **no** store has its keys, `start_shopping` refuses in chat and names the
keys to set. No job is queued, so no model call or Tavily search is spent.

A source with no key, or one that fails, is listed under "Source problems" in
the report and the job runs on what is left. No offers from any source → the
job is marked failed, and the stored error and the push both carry the source
problems, so "no offers found" never hides a store that was not asked.

Checked 2026-09-28: both shop APIs live, free with a key, personal use allowed.

## Safety

Listing titles are untrusted text from stores. In the job they reach only
`complete_text`, which is tool-free, so nothing they say can make the job act.
Every URL is checked with `tasks._urls.safe_url` before it is kept.
`start_shopping` is in `UNATTENDED_EXCLUDED_TOOLS`, so no background job can
start another.

## Try it from the shell

```bash
.venv/bin/python -m agent.tools._shop_sources --source ebay --query "usb-c hub" --max 40
.venv/bin/python -m agent.tools.shopping --description "noise cancelling headphones" --max-price 150
```

The second one calls the model — ask before running it while Ollama is busy.

## Not in v1

- Wren sending offers or messages to sellers herself.
- A price watch that re-runs a saved search and pushes on a drop.
- More sources (Walmart affiliate API; Amazon PA-API needs an Associates account).

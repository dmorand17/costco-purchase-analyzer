"""AI analysis engine using Strands Agents SDK + Bedrock Nova."""

import queue
import re
import threading
from collections.abc import Generator
from typing import Any

from strands import Agent, tool
from strands.models import BedrockModel

from services.db import get_presigned_url, list_price_drops, list_receipts

NOVA_LITE = "us.amazon.nova-2-lite-v1:0"

SYSTEM_PROMPT = """You are a Costco price adjustment specialist. Your job is to identify
items the user has purchased that are currently on sale, allowing them to request a price
adjustment (price match) at the Costco membership counter.

Costco's price adjustment policy: members can request an adjustment within 30 days of
purchase if the item goes on sale. This does not apply to items that already had a coupon
(TPD) applied at checkout.

Workflow:
1. Call find_potential_matches() to get pre-filtered candidates
2. Call get_receipt_items() if you need full receipt details or to check TPD status
3. Present results in two markdown tables:

**Table 1: Price Adjustment Opportunities**
| Receipt | Purchase Date | Item | Paid | Sale Price | Savings | Deal Source | Expires |
|---------|--------------|------|------|------------|---------|-------------|---------|

**Table 2: Already Discounted at Checkout (TPD)**
| Receipt | Item | TPD Price | Current Sale | Note |
|---------|------|-----------|--------------|------|

If no matches are found, say so clearly. Be concise."""


def _make_tools(
    receipt_ids: list[str] | None,
    date_from: str | None,
    date_to: str | None,
    sources: list[str] | None,
) -> list:
    @tool
    def find_potential_matches() -> str:
        """
        Pre-filter price drops against purchased items using item number and name overlap.
        Returns only deals where sale_price < paid_price, ranked by match quality.
        """
        receipts = list_receipts()
        if receipt_ids:
            receipts = [r for r in receipts if r["receipt_id"] in receipt_ids]
        if date_from:
            receipts = [r for r in receipts if r.get("receipt_date", "") >= date_from]
        if date_to:
            receipts = [r for r in receipts if r.get("receipt_date", "") <= date_to]

        drops = list_price_drops()
        if sources:
            drops = [d for d in drops if d.get("source") in sources]

        matches = []
        for receipt in receipts:
            for item in receipt.get("items", []):
                paid = float(item.get("price", 0))
                item_no = str(item.get("item_number", "")).strip()
                item_name = item.get("name", "").lower()

                for drop in drops:
                    sale = float(drop.get("sale_price", 0))
                    if sale <= 0 or sale >= paid:
                        continue

                    score = 0
                    drop_no = str(drop.get("item_number", "")).strip()
                    drop_name = drop.get("item_name", "").lower()

                    # Exact item number match
                    if item_no and drop_no and item_no == drop_no:
                        score += 10
                    # 5-digit prefix match
                    elif item_no and drop_no and item_no[:5] == drop_no[:5]:
                        score += 5
                    # Name keyword overlap
                    receipt_words = set(re.split(r"\W+", item_name)) - {"", "the", "a", "an"}
                    drop_words = set(re.split(r"\W+", drop_name)) - {"", "the", "a", "an"}
                    overlap = receipt_words & drop_words
                    if len(overlap) >= 2:
                        score += len(overlap)
                    elif len(overlap) == 1 and len(list(overlap)[0]) > 4:
                        score += 1

                    if score > 0:
                        matches.append(
                            {
                                "receipt_id": receipt["receipt_id"],
                                "receipt_date": receipt.get("receipt_date", ""),
                                "store": receipt.get("store", ""),
                                "item_name": item.get("name"),
                                "item_number": item_no,
                                "paid_price": paid,
                                "tpd": item.get("tpd", False),
                                "deal_item_name": drop.get("item_name"),
                                "sale_price": sale,
                                "savings": round(paid - sale, 2),
                                "source": drop.get("source"),
                                "promo_end": drop.get("promo_end", ""),
                                "link": drop.get("link", ""),
                                "score": score,
                            }
                        )

        matches.sort(key=lambda x: x["score"], reverse=True)
        return str(matches[:50]) if matches else "No potential matches found."

    @tool
    def get_receipt_items() -> str:
        """Return all purchased items from the selected receipts for detailed analysis."""
        receipts = list_receipts()
        if receipt_ids:
            receipts = [r for r in receipts if r["receipt_id"] in receipt_ids]
        if date_from:
            receipts = [r for r in receipts if r.get("receipt_date", "") >= date_from]
        if date_to:
            receipts = [r for r in receipts if r.get("receipt_date", "") <= date_to]
        result = []
        for r in receipts:
            for item in r.get("items", []):
                result.append(
                    {
                        "receipt_id": r["receipt_id"],
                        "receipt_date": r.get("receipt_date", ""),
                        "store": r.get("store", ""),
                        **item,
                    }
                )
        return str(result) if result else "No items found."

    @tool
    def get_current_price_drops() -> str:
        """Return all current price drops (use only if find_potential_matches misses something)."""
        drops = list_price_drops()
        if sources:
            drops = [d for d in drops if d.get("source") in sources]
        return str(drops[:100]) if drops else "No price drops in database."

    return [find_potential_matches, get_receipt_items, get_current_price_drops]


def run_analysis_stream(
    receipt_ids: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sources: list[str] | None = None,
) -> Generator[str, None, None]:
    """
    Run the analysis agent and yield SSE-formatted JSON strings.
    Each chunk is: data: {"type": "chunk"|"tool"|"done", ...}\\n\\n
    """
    result_queue: queue.Queue[dict | None] = queue.Queue()

    class _StreamCallback:
        def __call__(self, **kwargs):
            event = kwargs.get("event", {})
            # Text delta
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    result_queue.put({"type": "chunk", "text": delta["text"]})
            # Tool use start
            if "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                if "toolUse" in start:
                    result_queue.put({"type": "tool", "name": start["toolUse"].get("name", "")})

    import json as _json

    callback = _StreamCallback()
    full_text_parts: list[str] = []

    def _run():
        try:
            model = BedrockModel(model_id=NOVA_LITE, streaming=True)
            tools = _make_tools(receipt_ids, date_from, date_to, sources)
            agent = Agent(
                model=model,
                system_prompt=SYSTEM_PROMPT,
                tools=tools,
                callback_handler=callback,
            )
            response = agent("Analyze my Costco purchases for price adjustment opportunities.")
            full_text_parts.append(str(response))
        except Exception as exc:
            result_queue.put({"type": "error", "text": str(exc)})
        finally:
            result_queue.put(None)  # sentinel

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    collected = []
    while True:
        item = result_queue.get()
        if item is None:
            break
        if item["type"] == "chunk":
            collected.append(item["text"])
        yield f"data: {_json.dumps(item)}\n\n"

    full_markdown = "".join(collected)
    full_markdown = _inject_receipt_links(full_markdown)
    yield f"data: {_json.dumps({'type': 'done', 'text': full_markdown})}\n\n"


def run_analysis(
    receipt_ids: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    sources: list[str] | None = None,
) -> str:
    """Non-streaming analysis for the weekly AgentCore job."""
    model = BedrockModel(model_id=NOVA_LITE, streaming=False)
    tools = _make_tools(receipt_ids, date_from, date_to, sources)
    agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=tools)
    response = agent("Analyze my Costco purchases for price adjustment opportunities.")
    return _inject_receipt_links(str(response))


def _inject_receipt_links(markdown: str) -> str:
    """
    Replace bare item names in the first table column with links to their PDF.
    Requires receipt IDs to be embedded; used primarily in the streaming path.
    """
    # Placeholder — full link injection requires receipt_id context per row.
    # The agent includes receipt_id in its output; post-processing could parse and linkify.
    return markdown

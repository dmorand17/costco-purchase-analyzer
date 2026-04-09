"""Receipt parsing via Amazon Bedrock Nova models."""

import base64
import json
import os
import re
from typing import Any

import boto3

NOVA_LITE = "us.amazon.nova-2-lite-v1:0"
NOVA_PREMIER = "us.amazon.nova-premier-v1:0"

_bedrock = None


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    return _bedrock

PARSE_PROMPT = """You are a Costco receipt parser. Extract ALL line items from this receipt.

Return a JSON object with this exact structure:
{
  "store": "Costco Warehouse #<number> <city>",
  "receipt_date": "YYYY-MM-DD",
  "items": [
    {
      "item_number": "<5-8 digit number or empty string>",
      "name": "<product name>",
      "qty": <integer, default 1>,
      "price": <float, the final price paid>,
      "original_price": <float or null>,
      "tpd": false
    }
  ]
}

Rules:
- Include every item, including bulk/multi-packs
- TPD (Instant Savings / coupon) lines start with "TPD/" — set tpd=true on the preceding item,
  set original_price to the pre-discount price, and price to the post-discount price
- Negative prices on a line immediately following an item indicate a TPD discount
- Do NOT include tax lines, subtotal, total, or payment lines as items
- receipt_date must be ISO format YYYY-MM-DD
- Return ONLY valid JSON, no markdown, no explanation"""


def parse_receipt_pdf(pdf_bytes: bytes) -> dict[str, Any]:
    """Parse a Costco receipt PDF using Nova Lite (fast path)."""
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()
    response = _get_bedrock().converse(
        modelId=NOVA_LITE,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "document": {
                            "format": "pdf",
                            "name": "receipt",
                            "source": {"bytes": pdf_bytes},
                        }
                    },
                    {"text": PARSE_PROMPT},
                ],
            }
        ],
    )
    raw = response["output"]["message"]["content"][0]["text"]
    data = _extract_json(raw)
    return _post_process(data)


def reparse_receipt(pdf_bytes: bytes) -> dict[str, Any]:
    """Re-parse a receipt using Nova Premier with an image-based approach (high accuracy)."""
    import io

    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    mat = fitz.Matrix(300 / 72, 300 / 72)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")

    image_content = {
        "image": {
            "format": "png",
            "source": {"bytes": img_bytes},
        }
    }

    # Three separate prompts for better accuracy on complex receipts
    items_raw = _converse_premier(
        image_content,
        "List every purchased item name and item number, one per line as: <item_number>|<name>",
    )
    prices_raw = _converse_premier(
        image_content,
        "List every item price in the same order, one per line as: <price>|<tpd_discount_or_empty>",
    )
    meta_raw = _converse_premier(
        image_content,
        'Return JSON only: {"store": "...", "receipt_date": "YYYY-MM-DD"}',
    )

    items_lines = [l.strip() for l in items_raw.strip().splitlines() if "|" in l]
    price_lines = [l.strip() for l in prices_raw.strip().splitlines() if "|" in l]
    meta = _extract_json(meta_raw)

    parsed_items = []
    for i, item_line in enumerate(items_lines):
        parts = item_line.split("|", 1)
        item_number = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else ""
        price = 0.0
        original_price = None
        tpd = False
        if i < len(price_lines):
            p_parts = price_lines[i].split("|", 1)
            try:
                price = float(p_parts[0].strip().lstrip("$"))
            except ValueError:
                price = 0.0
            if len(p_parts) > 1 and p_parts[1].strip():
                try:
                    discount = float(p_parts[1].strip().lstrip("$-"))
                    original_price = round(price + discount, 2)
                    tpd = True
                except ValueError:
                    pass
        parsed_items.append(
            {
                "item_number": item_number,
                "name": name,
                "qty": 1,
                "price": price,
                "original_price": original_price,
                "tpd": tpd,
            }
        )

    data = {
        "store": meta.get("store", ""),
        "receipt_date": meta.get("receipt_date", ""),
        "items": parsed_items,
    }
    return _post_process(data)


def _converse_premier(image_content: dict, prompt: str) -> str:
    response = _get_bedrock().converse(
        modelId=NOVA_PREMIER,
        messages=[
            {
                "role": "user",
                "content": [image_content, {"text": prompt}],
            }
        ],
    )
    return response["output"]["message"]["content"][0]["text"]


def _extract_json(text: str) -> dict[str, Any]:
    """Strip markdown fences and parse JSON."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try extracting the first {...} block
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {"store": "", "receipt_date": "", "items": []}


def _post_process(data: dict[str, Any]) -> dict[str, Any]:
    """
    Merge TPD discount lines into their preceding item, normalize item numbers,
    and filter noise.
    """
    items: list[dict[str, Any]] = data.get("items", [])
    cleaned: list[dict[str, Any]] = []

    for item in items:
        name: str = str(item.get("name", "")).strip()
        price = item.get("price", 0.0)

        # Skip tax, total, payment lines
        skip_keywords = (
            "subtotal",
            "total",
            "tax",
            "hst",
            "gst",
            "pst",
            "visa",
            "mastercard",
            "debit",
            "cash",
            "change",
            "membership",
        )
        if any(kw in name.lower() for kw in skip_keywords) and not item.get(
            "item_number"
        ):
            continue

        # Merge TPD discount line into preceding item
        if name.startswith("TPD/") or (
            isinstance(price, (int, float)) and price < 0 and not name
        ):
            if cleaned:
                prev = cleaned[-1]
                discount = abs(float(price))
                orig = prev.get("original_price") or prev.get("price", 0.0)
                prev["original_price"] = round(float(orig), 2)
                prev["price"] = round(float(orig) - discount, 2)
                prev["tpd"] = True
            continue

        # Normalize item number: keep only 5-8 digit strings
        item_number = str(item.get("item_number", "")).strip()
        if item_number and not re.match(r"^\d{5,8}$", item_number):
            item_number = ""

        cleaned.append(
            {
                "item_number": item_number,
                "name": name,
                "qty": int(item.get("qty", 1) or 1),
                "price": round(float(price or 0), 2),
                "original_price": (
                    round(float(item["original_price"]), 2)
                    if item.get("original_price")
                    else None
                ),
                "tpd": bool(item.get("tpd", False)),
            }
        )

    data["items"] = cleaned
    return data

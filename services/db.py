"""DynamoDB and S3 data access layer."""

import hashlib
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr

RECEIPTS_TABLE = os.environ.get("DYNAMODB_RECEIPTS_TABLE", "CostcoReceipts")
PRICE_DROPS_TABLE = os.environ.get("DYNAMODB_PRICE_DROPS_TABLE", "CostcoPriceDrops")
S3_BUCKET = os.environ.get("S3_BUCKET", "")

_dynamodb = None
_s3 = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    return _dynamodb


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    return _s3


def _receipts_table():
    return _get_dynamodb().Table(RECEIPTS_TABLE)


def _price_drops_table():
    return _get_dynamodb().Table(PRICE_DROPS_TABLE)


# ── Receipts ───────────────────────────────────────────────────────────────────


def put_receipt(
    receipt_data: dict[str, Any],
    pdf_bytes: bytes,
) -> dict[str, Any]:
    """Store receipt in DynamoDB and PDF in S3. Deduplicates by MD5 hash."""
    pdf_hash = hashlib.md5(pdf_bytes).hexdigest()

    # Deduplication check
    existing = _receipts_table().scan(
        FilterExpression=Attr("pdf_hash").eq(pdf_hash)
    )
    if existing.get("Items"):
        return existing["Items"][0]

    receipt_id = str(uuid.uuid4())
    s3_key = f"receipts/{receipt_id}.pdf"

    _get_s3().put_object(Bucket=S3_BUCKET, Key=s3_key, Body=pdf_bytes, ContentType="application/pdf")

    item = {
        "receipt_id": receipt_id,
        "items": receipt_data.get("items", []),
        "receipt_date": receipt_data.get("receipt_date", ""),
        "store": receipt_data.get("store", ""),
        "upload_date": datetime.now(timezone.utc).isoformat(),
        "pdf_hash": pdf_hash,
        "s3_key": s3_key,
    }
    _receipts_table().put_item(Item=item)
    return item


def get_receipt(receipt_id: str) -> dict[str, Any] | None:
    result = _receipts_table().get_item(Key={"receipt_id": receipt_id})
    return result.get("Item")


def update_receipt(receipt_id: str, updates: dict[str, Any]) -> None:
    if not updates:
        return
    set_expr = ", ".join(f"#{k} = :{k}" for k in updates)
    names = {f"#{k}": k for k in updates}
    values = {f":{k}": v for k, v in updates.items()}
    _receipts_table().update_item(
        Key={"receipt_id": receipt_id},
        UpdateExpression=f"SET {set_expr}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def list_receipts() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    resp = _receipts_table().scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = _receipts_table().scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return sorted(items, key=lambda r: r.get("upload_date", ""), reverse=True)


def delete_receipt(receipt_id: str) -> None:
    receipt = get_receipt(receipt_id)
    if receipt and receipt.get("s3_key"):
        try:
            _get_s3().delete_object(Bucket=S3_BUCKET, Key=receipt["s3_key"])
        except Exception:
            pass
    _receipts_table().delete_item(Key={"receipt_id": receipt_id})


def clear_receipts() -> None:
    receipts = list_receipts()
    with _receipts_table().batch_writer() as batch:
        for r in receipts:
            if r.get("s3_key"):
                try:
                    _get_s3().delete_object(Bucket=S3_BUCKET, Key=r["s3_key"])
                except Exception:
                    pass
            batch.delete_item(Key={"receipt_id": r["receipt_id"]})


def get_pdf(receipt_id: str) -> bytes | None:
    receipt = get_receipt(receipt_id)
    if not receipt or not receipt.get("s3_key"):
        return None
    resp = _get_s3().get_object(Bucket=S3_BUCKET, Key=receipt["s3_key"])
    return resp["Body"].read()


def get_presigned_url(s3_key: str, expiry: int = 604800) -> str:
    """Return a presigned URL valid for ``expiry`` seconds (default 7 days)."""
    return _get_s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key},
        ExpiresIn=expiry,
    )


# ── Price Drops ────────────────────────────────────────────────────────────────


def put_price_drops(items: list[dict[str, Any]]) -> int:
    """Bulk-upsert price drop items. Returns count inserted."""
    if not items:
        return 0
    with _price_drops_table().batch_writer() as batch:
        for item in items:
            if "item_id" not in item:
                item["item_id"] = str(uuid.uuid4())
            item.setdefault("scanned_date", datetime.now(timezone.utc).isoformat())
            batch.put_item(Item=item)
    return len(items)


def list_price_drops() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    resp = _price_drops_table().scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = _price_drops_table().scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return sorted(items, key=lambda d: d.get("scanned_date", ""), reverse=True)


def delete_price_drop(item_id: str) -> None:
    _price_drops_table().delete_item(Key={"item_id": item_id})


def clear_price_drops() -> None:
    drops = list_price_drops()
    with _price_drops_table().batch_writer() as batch:
        for d in drops:
            batch.delete_item(Key={"item_id": d["item_id"]})

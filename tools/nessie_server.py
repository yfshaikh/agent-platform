"""MCP tool server for Capital One's Nessie sandbox bank.

This year's Nessie keeps records but never changes balances, and transfers have no payee.
So Nessie is used as the ledger: moving money writes a withdrawal on the sender and a deposit
on the recipient, and a balance is the opening balance plus completed deposits minus withdrawals.

NESSIE_MOCK=1 serves the same tools from memory, in case Nessie is down during the demo.
The API key travels in the URL, so URLs are never logged or put in error messages.
"""
import datetime
import os
import re
import uuid

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from common import serve

NESSIE_URL = "https://api.nessieisreal.com"
MOCK = os.environ.get("NESSIE_MOCK") == "1"
mock_records = {}  # path -> list of objects, used only in mock mode


async def nessie(method, path, body=None):
    """Call Nessie (or the in-memory mock) and return the JSON body; errors never include the URL."""
    if MOCK:
        return mock_nessie(method, path, body)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(method, NESSIE_URL + path, params={"key": os.environ["NESSIE_API_KEY"]}, json=body)
    except httpx.HTTPError as error:
        raise RuntimeError(f"Nessie unreachable ({type(error).__name__})") from None
    if response.status_code == 404:
        return []  # Nessie answers 404 for "none found" on some lists
    if response.status_code >= 400:
        raise RuntimeError(f"Nessie returned {response.status_code}: {response.text[:200]}")
    return response.json()


def mock_nessie(method, path, body):
    """Tiny in-memory stand-in for the few Nessie endpoints this server uses."""
    if method == "POST":
        created = {**body, "_id": uuid.uuid4().hex}
        if path.endswith("/accounts"):
            created["customer_id"] = path.split("/")[2]
            mock_records.setdefault(f"/accounts/{created['_id']}", []).append(created)
        mock_records.setdefault(path, []).append(created)
        return {"code": 201, "objectCreated": created}
    records = mock_records.get(path, [])
    return records[0] if path.count("/") == 2 and path.startswith("/accounts/") and records else records


def today():
    """Today's date in Nessie's format."""
    return datetime.date.today().isoformat()


async def balance_of(account_id):
    """Opening balance + completed deposits - completed withdrawals."""
    account = await nessie("GET", f"/accounts/{account_id}")
    if not account:
        raise ValueError(f"no account {account_id}")
    deposits = await nessie("GET", f"/accounts/{account_id}/deposits")
    withdrawals = await nessie("GET", f"/accounts/{account_id}/withdrawals")
    moved_in = sum(record["amount"] for record in deposits if record.get("status") == "completed")
    moved_out = sum(record["amount"] for record in withdrawals if record.get("status") == "completed")
    return round(account["balance"] + moved_in - moved_out, 2), account


mcp = MCPServer("nessie-bank")


@mcp.tool(annotations=ToolAnnotations(title="List a customer's accounts", read_only_hint=True))
async def get_customer_accounts(customer_id: str) -> list[dict]:
    """List a bank customer's accounts with their nicknames and current balances."""
    accounts = await nessie("GET", f"/customers/{customer_id}/accounts")
    return [{"account_id": account["_id"], "nickname": account.get("nickname"), "type": account.get("type"),
             "balance": (await balance_of(account["_id"]))[0]} for account in accounts]


@mcp.tool(annotations=ToolAnnotations(title="Get account balance", read_only_hint=True))
async def get_balance(account_id: str) -> dict:
    """Get the current balance of one bank account."""
    balance, account = await balance_of(account_id)
    return {"account_id": account_id, "nickname": account.get("nickname"), "balance": balance}


@mcp.tool(annotations=ToolAnnotations(title="List recent money movements", read_only_hint=True))
async def list_transfers(account_id: str) -> list[dict]:
    """List recent money in and out of an account, newest first (at most 20)."""
    deposits = await nessie("GET", f"/accounts/{account_id}/deposits")
    withdrawals = await nessie("GET", f"/accounts/{account_id}/withdrawals")
    movements = [{"direction": "in", **record} for record in deposits] + [{"direction": "out", **record} for record in withdrawals]
    movements.sort(key=lambda record: record.get("creation_date") or record.get("transaction_date") or "", reverse=True)
    return [{key: record.get(key) for key in ("direction", "amount", "description", "transaction_date", "status")}
            for record in movements[:20]]


TOWN_TAG = re.compile(r"\(([0-9a-f]{6})\)$")  # town accounts are nicknamed "<member> (<town id>)"


def check_same_town(*accounts):
    """Simulation copy only: money may move only between accounts of one Scam City town, never real customers' accounts."""
    if os.environ.get("SIMULATION") != "1":
        return
    tags = {(match.group(1) if (match := TOWN_TAG.search(account.get("nickname") or "")) else None) for account in accounts}
    if None in tags or len(tags) != 1:
        raise ValueError("the simulation bank only moves money between accounts of the same town")


@mcp.tool(annotations=ToolAnnotations(title="Transfer money", read_only_hint=False, destructive_hint=True))
async def transfer_money(from_account_id: str, to_account_id: str, amount: float, description: str) -> dict:
    """Move money from one account to another. Irreversible: the platform requires a human approval for every call."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    if from_account_id == to_account_id:
        raise ValueError("cannot transfer to the same account")
    balance, source = await balance_of(from_account_id)
    if amount > balance:
        raise ValueError(f"insufficient funds: balance is {balance}")
    _, recipient = await balance_of(to_account_id)  # fails clearly if the recipient doesn't exist, before any money moves
    check_same_town(source, recipient)
    record = {"medium": "balance", "transaction_date": today(), "status": "completed", "amount": amount}
    withdrawal = await nessie("POST", f"/accounts/{from_account_id}/withdrawals",
                              {**record, "description": f"Transfer to {to_account_id}: {description}"})
    deposit = await nessie("POST", f"/accounts/{to_account_id}/deposits",
                           {**record, "description": f"Transfer from {from_account_id}: {description}"})
    return {"status": "completed", "amount": amount, "from_account_id": from_account_id, "to_account_id": to_account_id,
            "withdrawal_id": withdrawal["objectCreated"]["_id"], "deposit_id": deposit["objectCreated"]["_id"],
            "new_balance": round(balance - amount, 2)}


@mcp.tool(annotations=ToolAnnotations(title="Pay a bill", read_only_hint=False, destructive_hint=True))
async def pay_bill(account_id: str, payee: str, amount: float) -> dict:
    """Pay a bill to a named payee (for example "Austin Energy") from an account. Irreversible."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    balance, account = await balance_of(account_id)
    if amount > balance:
        raise ValueError(f"insufficient funds: balance is {balance}")
    check_same_town(account)
    withdrawal = await nessie("POST", f"/accounts/{account_id}/withdrawals", {
        "medium": "balance", "transaction_date": today(), "status": "completed", "amount": amount, "description": f"Bill: {payee}"})
    return {"status": "completed", "payee": payee, "amount": amount, "withdrawal_id": withdrawal["objectCreated"]["_id"],
            "new_balance": round(balance - amount, 2)}


if __name__ == "__main__":
    # Must match the Tool object's "sensitive" classes. The simulation copy (Scam City) skips the grant check;
    # the operator and a NetworkPolicy keep it reachable only from simulation namespaces.
    serve(mcp, sensitive=set() if os.environ.get("SIMULATION") == "1" else {"transfer_money", "pay_bill"})

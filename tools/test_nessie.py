"""Offline check of the Nessie tool's money logic, using its in-memory mock.

Run: NESSIE_MOCK=1 uv run python test_nessie.py
"""
import asyncio
import os

assert os.environ.get("NESSIE_MOCK") == "1", "run with NESSIE_MOCK=1 so no real bank calls are made"
import nessie_server as bank  # noqa: E402  (imported after the mock check on purpose)


async def main():
    """Open two accounts, move money, and check balances, history and the safety checks."""
    customer = (await bank.nessie("POST", "/customers", {"first_name": "Test"}))["objectCreated"]["_id"]
    alice, bob = [
        (await bank.nessie("POST", f"/customers/{customer}/accounts", {"type": "Checking", "nickname": nickname, "balance": 100}))["objectCreated"]["_id"]
        for nickname in ("alice", "bob")
    ]
    result = await bank.transfer_money(alice, bob, 30, "lunch")
    assert result["new_balance"] == 70, result
    assert (await bank.get_balance(alice))["balance"] == 70
    assert (await bank.get_balance(bob))["balance"] == 130
    assert (await bank.pay_bill(bob, "Austin Energy", 10))["new_balance"] == 120
    history = await bank.list_transfers(bob)
    assert {movement["direction"] for movement in history} == {"in", "out"}, history
    accounts = await bank.get_customer_accounts(customer)
    assert sorted(account["balance"] for account in accounts) == [70, 120], accounts
    for bad_call in (lambda: bank.transfer_money(alice, bob, 1000, "too much"),
                     lambda: bank.transfer_money(alice, bob, -5, "negative"),
                     lambda: bank.transfer_money(alice, alice, 5, "self")):
        try:
            await bad_call()
            raise AssertionError("should have been refused")
        except ValueError:
            pass
    assert (await bank.get_balance(alice))["balance"] == 70  # refused transfers moved nothing
    print("nessie ledger checks passed")


asyncio.run(main())

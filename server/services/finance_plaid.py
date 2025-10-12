async def get_money_snapshot() -> dict:
    return {
        "accounts": [{"name": "Checking", "balance": 1243.17}],
        "spend_today": 18.50,
        "spend_week": 96.30,
        "budget_daily": 30.00,
        "budget_left_today": 30.00 - 18.50,
    }

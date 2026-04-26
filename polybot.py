import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs,
    MarketOrderArgs,
    OrderType,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client.order_builder.constants import BUY, SELL


# ── Configuration ──────────────────────────────────────────────────────────────

load_dotenv()

PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS")

SIGNATURE_TYPE = 0

TRADE_SIZE = 0.5   # 50% of available balance per trade
MARKETS_LIMIT = 100
MAX_POSITIONS = 3

MIN_USDC_TRADE = 1  # minimum $1 per trade (prevents tiny/invalid orders)

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

client = ClobClient(
    CLOB_API,
    key=PRIVATE_KEY,
    chain_id=137,
    signature_type=SIGNATURE_TYPE,
    funder=FUNDER_ADDRESS,
)

creds = client.derive_api_key()
client.set_api_creds(creds)


# ── Helpers ───────────────────────────────────────────────────────────────────

def ts():
    return datetime.now().strftime("%H:%M:%S")


def usdc_to_base(amount_usdc: float) -> int:
    """Convert USDC → base units (6 decimals)"""
    return int(amount_usdc * 1e6)


# ── API Layer ─────────────────────────────────────────────────────────────────


def get_markets(**filters):
    params = {
        "limit": MARKETS_LIMIT,
        "active": True,
        "closed": False,
    }
    params.update(filters)
    return requests.get(f"{GAMMA_API}/markets", params=params).json()


def get_balance():
    balance = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    return int(balance["balance"]) / 1e6


def get_price(token_id):
    return {
        "midpoint": float(client.get_midpoint(token_id)["mid"]),
        "best_ask": float(client.get_price(token_id, side="BUY")["price"]),
        "best_bid": float(client.get_price(token_id, side="SELL")["price"]),
        "spread": float(client.get_spread(token_id)["spread"]),
    }


def get_positions(address=None):
    addr = address or FUNDER_ADDRESS
    positions = requests.get(f"{DATA_API}/positions", params={"user": addr}).json()
    print(f"{ts()} - {len(positions)} open positions")
    return positions


def place_order(token_id, side, amount, price=None):
    if amount <= 0:
        raise ValueError("Order amount is zero — refusing to send")

    if price is None:
        order = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=side,
            order_type=OrderType.FOK
        )
        signed = client.create_market_order(order)
        return client.post_order(signed, OrderType.FOK)
    else:
        order = OrderArgs(
            token_id=token_id,
            price=price,
            size=amount,
            side=side
        )
        signed = client.create_order(order)
        return client.post_order(signed, OrderType.GTC)


# ── Strategy ──────────────────────────────────────────────────────────────────


def find_markets():
    min_price = 0.15
    max_price = 0.40
    min_volume = 10000

    markets = get_markets(tag_id=21)

    candidates = []

    for m in markets:
        prices = json.loads(m.get("outcomePrices", "[]"))
        volume = float(m.get("volume24hr", 0))

        if len(prices) >= 2:
            yes_price = float(prices[0])

            if min_price <= yes_price <= max_price and volume >= min_volume:
                token_ids = json.loads(m["clobTokenIds"])
                m["yes_token_id"] = token_ids[0]
                m["no_token_id"] = token_ids[1]
                candidates.append(m)

    candidates.sort(key=lambda m: float(m.get("volume24hr", 0)), reverse=True)

    print(f"{ts()} - Found {len(candidates)} markets matching strategy")
    return candidates


def should_trade(price_data):
    return price_data["spread"] < 0.05


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    print(f"{ts()} - Scanning markets...")
    markets = find_markets()

    if not markets:
        print(f"{ts()} - No markets found")
        return

    positions = get_positions()
    held_tokens = {p["asset"] for p in positions}

    for market in markets:
        print(f"\n{ts()} - --- {market['question']} ---")

        if len(held_tokens) >= MAX_POSITIONS:
            print(f"{ts()} - Max positions reached")
            break

        if market["yes_token_id"] in held_tokens or market["no_token_id"] in held_tokens:
            print(f"{ts()} - Already in market")
            continue

        price_data = get_price(market["yes_token_id"])
        print(f"{ts()} - YES price: {price_data['best_ask']:.2f} | Spread: {price_data['spread']:.2f}")

        if not should_trade(price_data):
            print(f"{ts()} - Spread too wide")
            continue

        # 🔑 Recalculate balance EACH trade
        balance = get_balance()
        trade_usdc = balance * TRADE_SIZE

        if trade_usdc < MIN_USDC_TRADE:
            print(f"{ts()} - Trade too small (${trade_usdc:.2f}), skipping")
            continue

        amount = usdc_to_base(trade_usdc)

        print(f"{ts()} - Balance: ${balance:.2f}")
        print(f"{ts()} - Trade size: ${trade_usdc:.2f} ({amount} base units)")

        try:
            print(f"{ts()} - Placing order...")
            place_order(market["yes_token_id"], BUY, amount)
            held_tokens.add(market["yes_token_id"])
            print(f"{ts()} - ✓ Trade executed")

        except Exception as e:
            print(f"{ts()} - ❌ Trade failed: {e}")

    print(f"\n{ts()} - Done.")


if __name__ == "__main__":
    main()

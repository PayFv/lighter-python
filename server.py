#!/usr/bin/env python3
"""
Fast Withdraw - Instant withdrawal from Lighter L2 to Arbitrum
"""

import asyncio
import json
import os
from decimal import Decimal

from dotenv import load_dotenv
from eth_account import Account  # type: ignore
from eth_account.messages import encode_defunct  # type: ignore

from flask import Flask, request, jsonify
import lighter

# Load environment variables from .env file
# load_dotenv()

app = Flask(__name__)

# Configuration from environment variables
BASE_URL = os.getenv('BASE_URL', 'https://api.lighter.xyz')
# API_KEY_PRIVATE_KEY = os.getenv('API_KEY_PRIVATE_KEY', '')
# ACCOUNT_INDEX = int(os.getenv('ACCOUNT_INDEX', '0'))
# API_KEY_INDEX = int(os.getenv('API_KEY_INDEX', '0'))
# ETH_PRIVATE_KEY = os.getenv('ETH_PRIVATE_KEY', '')
# WITHDRAW_ADDRESS = os.getenv('WITHDRAW_ADDRESS', '')

async def process_withdraw(amount_usdc: float,
    api_key_private_key: str,
    account_index: int,
    api_key_index: int,
    eth_private_key: str,
    to: str):
    """Process fast withdraw with given amount"""
    # Initialize clients
    api_client = lighter.ApiClient(configuration=lighter.Configuration(host=BASE_URL))
    client = lighter.SignerClient(
        url=BASE_URL,
        # private_key=api_key_private_key,
        account_index=account_index,
        api_private_keys = {api_key_index: api_key_private_key},
        # account_index=account_index,
        # api_key_index=api_key_index,
    )

    try:
        err = client.check_client()
        if err:
            raise Exception(f"API key verification failed: {err}")

        auth_token, err = client.create_auth_token_with_expiry(api_key_index=api_key_index)
        if err:
            raise Exception(f"Auth token failed: {err}")

        info_api = lighter.InfoApi(api_client)
        tx_api = lighter.TransactionApi(api_client)

        # Get fast withdraw pool
        params = api_client.param_serialize(
            method='GET',
            resource_path='/api/v1/fastwithdraw/info',
            query_params=[('account_index', account_index)],
            header_params={'Authorization': auth_token}
        )
        response = await api_client.call_api(*params)
        await response.read()
        data = response.data
        assert data is not None
        pool_info = json.loads(data.decode('utf-8'))

        if pool_info.get('code') != 200:
            raise Exception(f"Pool info failed: {pool_info.get('message')}")

        to_account = pool_info['to_account_index']
        print(f"Pool: {to_account}, Limit: {pool_info.get('withdraw_limit')}")

        # Get fee and nonce
        fee_info = await info_api.transfer_fee_info(
            account_index=account_index,
            to_account_index=to_account,
            auth=auth_token
        )
        nonce_info = await tx_api.next_nonce(
            account_index=account_index,
            api_key_index=api_key_index
        )

        # Build memo (20-byte address + 12 zeros)
        addr_hex = to.lower().removeprefix("0x")
        addr_bytes = bytes.fromhex(addr_hex)
        if len(addr_bytes) != 20:
            raise ValueError(f"Invalid address length: {len(addr_bytes)}")
        memo_list = list(addr_bytes + b"\x00" * 12)

        # Sign L1 message
        usdc_int = int(Decimal(str(amount_usdc)) * Decimal(10**6))
        nonce = nonce_info.nonce
        fee = fee_info.transfer_fee_usdc

        print(f"Withdrawing {amount_usdc} USDC (int: {usdc_int}) to {to} from Lighter L2 account {account_index}")
        print(f"Nonce: {nonce}, Fee: {fee}")
        
        def hex16(n):
            return format(n & 0xFFFFFFFFFFFFFFFF, '016x')

        memo_hex = ''.join(format(b, '02x') for b in memo_list)
        # Chain ID for mainnet is 304 (0x130), for testnet is 300 (0x12c)
        chain_id_hex = "0000000000000130" if "mainnet" in BASE_URL else "000000000000012c"
        
        l1_msg = f"""Transfer

nonce: 0x{hex16(nonce)}
from: 0x{hex16(account_index)} (route 0x0000000000000000)
api key: 0x{hex16(api_key_index)}
to: 0x{hex16(to_account)} (route 0x0000000000000000)
asset: 0x0000000000000003
amount: 0x{hex16(usdc_int)}
fee: 0x{hex16(fee)}
chainId: 0x{chain_id_hex}
memo: {memo_hex}
Only sign this message for a trusted client!"""

        print(f"L1 Msg:\n{l1_msg}")
        acct = Account.from_key(eth_private_key)
        l1_sig = "0x" + acct.sign_message(encode_defunct(text=l1_msg)).signature.hex()

        # Sign L2 (use dummy memo workaround for SDK limitation)
        _, temp_tx, _, err = client.sign_transfer(
            eth_private_key=eth_private_key,
            to_account_index=to_account,
            asset_id=3,
            route_from=0,
            route_to=0,
            usdc_amount=usdc_int,
            fee=fee,
            # memo='X' * 32,
            memo = memo_hex,
            nonce=nonce,
            api_key_index=api_key_index
        )
        if err:
            raise Exception(f"L2 signing failed: {err}")

        print(f"Temp TX: {temp_tx}")
        # Replace memo and L1 signature
        assert temp_tx is not None
        tx_info = json.loads(temp_tx)
        tx_info["Memo"] = memo_list
        tx_info["L1Sig"] = l1_sig
        
        # Submit fast withdraw
        params = api_client.param_serialize(
            method='POST',
            resource_path='/api/v1/fastwithdraw',
            post_params=[
                ('tx_info', json.dumps(tx_info)),
                ('to_address', to)
            ],
            header_params={
                'Authorization': auth_token,
                'Content-Type': 'application/x-www-form-urlencoded'
            }
        )
        response = await api_client.call_api(*params)
        await response.read()
        data = response.data
        assert data is not None
        result = json.loads(data.decode('utf-8'))
        
        return result
    finally:
        await client.close()
        await api_client.close()


@app.route("/", methods=['GET'])
def hello():
    return jsonify({"message": "Fast Withdraw API", "status": "ready"})


@app.route("/withdraw", methods=['POST'])
def withdraw():
    """
    Fast withdraw endpoint
    Expects JSON: {"amount": 10.5}
    Returns API response
    """
    try:
        data = request.get_json()
        if not data or 'amount' not in data:
            return jsonify({"error": "Missing 'amount' parameter"}), 400
        
        amount = float(data['amount'])
        if amount <= 0:
            return jsonify({"error": "Amount must be positive"}), 400
        
        api_key_private_key = data['api_key_private_key']
        account_index = int(data['account_index'])
        api_key_index = int(data['api_key_index'])
        eth_private_key = data['eth_private_key']
        to_address = data['to_address']

        # api_key_private_key = request.headers.get("X-API-Key-Private-Key")
        # account_index = int(request.headers.get("X-Account-Index", 0))
        # api_key_index = int(request.headers.get("X-API-Key-Index", 0))
        # eth_private_key = request.headers.get("X-ETH-Private-Key")
        # to_address = request.headers.get("X-To-Address")

        # Run async function in new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(process_withdraw(
                amount,
                api_key_private_key,
                account_index,
                api_key_index,
                eth_private_key,
                to_address
            ))
            return jsonify(result)
        finally:
            loop.close()
            
    except ValueError as e:
        return jsonify({"error": f"Invalid amount: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
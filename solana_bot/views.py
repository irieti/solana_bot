# views.py
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import asyncio
import aiohttp
import json
from asgiref.sync import async_to_sync
import threading
from collections import defaultdict
from datetime import datetime
import requests
import time
import os
from dotenv import load_dotenv
import random

load_dotenv()

# Configuration
url = os.getenv("URL_API")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
chat_ids_str = os.getenv("chat_ids")
if chat_ids_str:
    chat_ids = [int(id.strip()) for id in chat_ids_str.split(",")]
else:
    chat_ids = []
current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# Store active loops
active_loops = {}
loop_statuses = defaultdict(lambda: False)
loop_updates = {}


def save_largest_accounts(data, mint_address):
    file_path = f"largest_accounts_{mint_address[:6]}.json"
    all_data = {}

    # Добавляем или обновляем данные для текущего mint
    all_data[mint_address] = data

    with open(file_path, "w") as f:
        json.dump(all_data, f, indent=2)


async def fetch_page_with_retry(session, page, semaphore, mint):
    max_retries = 5
    base_delay = 1

    for attempt in range(max_retries):
        try:
            async with semaphore:
                payload = {
                    "jsonrpc": "2.0",
                    "id": f"page-{page}",
                    "method": "getTokenAccounts",
                    "params": {
                        "mint": mint,
                        "limit": 1000,
                        "page": page,
                    },
                }

                async with session.post(url, json=payload) as response:
                    if response.status == 429:
                        delay = base_delay * (2**attempt) + random.random()
                        print(
                            f"Rate limited on page {page}, attempt {attempt + 1}. Waiting {delay:.2f}s"
                        )
                        await asyncio.sleep(delay)
                        continue

                    if response.status != 200:
                        print(f"Error fetching page {page}: {response.status}")
                        return []

                    data = await response.json()
                    token_accounts = data.get("result", {}).get("token_accounts", [])

                    if not token_accounts:
                        print(f"No more accounts found at page {page}")
                        return []

                    print(f"Successfully fetched page {page}")
                    return token_accounts

        except Exception as e:
            print(f"Error on page {page}, attempt {attempt + 1}: {str(e)}")
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt) + random.random()
                await asyncio.sleep(delay)
            else:
                print(f"Max retries reached for page {page}")
                return []

    return []


async def find_largest_holders(
    mint, loop_time, stop_flag, previous_total_balance, balance_changes
):
    print(f"find_largest_holders for mint {mint} has started")
    previous_total_balance = 0
    max_pages = 1000  # Increased from 10 to 1000
    semaphore = asyncio.Semaphore(5)  # Reduced from 20 to 5
    all_accounts = []
    balance_changes = []
    reversed_balance_changes = []

    while True:
        if stop_flag.is_set():
            print(f"Stopping find_largest_holders for mint {mint}")
            break

        all_accounts = []
        stop_fetching = False

        async with aiohttp.ClientSession() as session:
            # Process pages in smaller chunks
            chunk_size = 50
            for chunk_start in range(1, max_pages + 1, chunk_size):
                if stop_flag.is_set():
                    break

                chunk_end = min(chunk_start + chunk_size, max_pages + 1)
                tasks = []

                for page in range(chunk_start, chunk_end):
                    if stop_fetching:
                        break
                    tasks.append(fetch_page_with_retry(session, page, semaphore, mint))

                # Process chunk results
                chunk_results = await asyncio.gather(*tasks)
                empty_pages = 0

                for accounts in chunk_results:
                    if accounts:
                        all_accounts.extend(accounts)
                    else:
                        empty_pages += 1

                # If we get multiple empty pages in a chunk, assume we've reached the end
                if empty_pages > chunk_size // 2:
                    stop_fetching = True
                    break

                # Add a small delay between chunks
                await asyncio.sleep(1)

        cur_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"Current time: {cur_time}")

        # Sort accounts by amount in descending order
        all_accounts.sort(key=lambda x: x["amount"], reverse=True)
        top_1000_accounts = all_accounts[:1000]

        # Save the current top 1000 accounts
        save_largest_accounts(top_1000_accounts, mint)
        print(
            f"Top 1000 largest accounts for mint {mint} saved to largest_accounts.json"
        )

        try:
            with open("wallets.json", "r") as f:
                wallets_data = json.load(f)
        except Exception as e:
            print(f"Error loading wallets.json: {e}")
            return

        # Create a set of wallet addresses for O(1) lookup
        wallets_to_check = {wallet["wallet"].lower() for wallet in wallets_data}

        # Create matching wallets list with case-insensitive comparison
        matching_wallets = []
        for account in top_1000_accounts:
            owner_address = account["owner"].lower()
            if owner_address in wallets_to_check:
                matching_wallets.append(
                    {
                        "wallet": account["owner"],  # Keep original case
                        "amount": account["amount"],
                    }
                )

        total_amount = sum(wallet["amount"] for wallet in matching_wallets)
        formatted_total_amount = f"{(total_amount/1000000) / 10000000:,.4f}"

        matching_wallets.sort(key=lambda wallet: wallet["amount"], reverse=True)
        wallets_dict = {wallet["wallet"]: wallet["name"] for wallet in wallets_data}

        print(f"Total balance of matching wallets for mint {mint}: {total_amount}")

        for wallet in matching_wallets:
            wallet["name"] = wallets_dict.get(wallet["wallet"], "Unknown")

        wallet_details = "\n".join(
            [
                f"{wallet['name']}, {(wallet['amount']/1000000) / 10000000:,.4f}%"
                for wallet in matching_wallets
            ]
        )

        if total_amount != previous_total_balance:
            if total_amount < previous_total_balance:
                update_balance(
                    mint=mint,
                    formatted_total_amount=formatted_total_amount,
                    wallet_details=wallet_details,
                    reversed_balance_changes=reversed_balance_changes,
                    balance_changes=balance_changes,
                )
                print(
                    f"Balance changed for mint {mint}! Previous total: {previous_total_balance}, New total: {total_amount}"
                )
                message = (
                    f"{cur_time}\n"
                    f"<b>{mint}</b>\n"
                    f"{reversed_balance_changes}\n"
                    f"<b>🔴 {formatted_total_amount}<br>\n"
                    f"{wallet_details}"
                )

                balance_changes.append("🔴")
                reversed_balance_changes = list(reversed(balance_changes))
            elif total_amount > previous_total_balance:
                print(
                    f"Balance changed for mint {mint}! Previous total: {previous_total_balance}, New total: {formatted_total_amount}"
                )
                update_balance(
                    mint=mint,
                    formatted_total_amount=formatted_total_amount,
                    wallet_details=wallet_details,
                    reversed_balance_changes=reversed_balance_changes,
                    balance_changes=balance_changes,
                )
                message = (
                    f"{cur_time}\n"
                    f"<b>{mint}</b>\n"
                    f"{reversed_balance_changes}\n"
                    f"<b>🟢 {formatted_total_amount}</b>\n"
                    f"{wallet_details}"
                )

                balance_changes.append("🟢")
                reversed_balance_changes = list(reversed(balance_changes))
        else:
            print(f"Balance has not changed for mint {mint}.")
            update_balance(
                mint=mint,
                formatted_total_amount=formatted_total_amount,
                wallet_details=wallet_details,
                reversed_balance_changes=reversed_balance_changes,
                balance_changes=balance_changes,
            )
            message = (
                f"{cur_time}\n"
                f"<b>{mint}</b>\n"
                f"{reversed_balance_changes}\n"
                f"<b>🟠 {formatted_total_amount}</b>\n"
                f"{wallet_details}"
            )
            balance_changes.append("🟠")
            reversed_balance_changes = list(reversed(balance_changes))

        previous_total_balance = total_amount

        if len(balance_changes) > 10:
            balance_changes.pop(0)

        print(f"Waiting for the next cycle for mint {mint}...")
        send_telegram_message(message, parse_mode="HTML")

        if stop_flag.is_set():
            print(f"Loop for mint {mint} stopped by the user.")
            break

        await asyncio.sleep(loop_time)


def send_telegram_message(message, parse_mode="HTML"):
    for chat_id in chat_ids:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": chat_id, "text": message, "parse_mode": parse_mode}
            response = requests.post(url, data=payload)
            if response.status_code != 200:
                print(
                    f"Failed to send notification to {chat_id}. Status code: {response.status_code}"
                )
        except Exception as e:
            print(f"Error sending telegram message: {e}")


class TokenTracker:
    def __init__(self, mint, loop_time):
        self.mint = mint
        self.loop_time = int(loop_time)  # Уникальное время ожидания для каждого mint
        self.stop_flag = threading.Event()
        self.balance_changes = []

    def start(self):
        """Запускает поток для отслеживания изменений баланса для этого mint с уникальным loop_time."""
        self.thread = threading.Thread(target=self._run_loop)
        self.thread.daemon = True
        self.thread.start()
        active_loops[self.mint] = self  # Добавляем активный цикл для данного mint

    def stop(self):
        """Останавливает поток и удаляет его из активных."""
        self.stop_flag.set()
        self.thread.join()
        del active_loops[self.mint]  # Удаляем цикл из активных

    def _run_loop(self):
        """Цикл, который будет обновлять данные для каждого mint с учетом индивидуального loop_time."""
        previous_total_balance = 0

        while not self.stop_flag.is_set():
            asyncio.run(
                find_largest_holders(
                    self.mint,
                    self.loop_time,  # Используем индивидуальное loop_time для каждого mint
                    self.stop_flag,
                    previous_total_balance,
                    self.balance_changes,
                )
            )
            self.stop_flag.wait(
                self.loop_time
            )  # Ожидание с уникальным временем для данного mint

    def save_balance_change(self, change_type):
        """Метод для сохранения изменений баланса."""
        self.balance_changes.append(change_type)
        if len(self.balance_changes) > 10:
            self.balance_changes.pop(0)

    def get_balance_changes(self):
        """Метод для получения последних изменений баланса."""
        return self.balance_changes


# Views
def index(request):
    context = {
        "all_mints": [
            "9L5BqqVA3EJ1XrGrS4HaXssivEewfme3DWoXCTBEVE9e"
        ],  # Add your mint addresses
        "running_loops": [
            {"mint": mint, "loop_time": tracker.loop_time}
            for mint, tracker in active_loops.items()
        ],
    }
    return render(request, "index.html", context)


@require_http_methods(["GET"])
def get_loop_updates(request):
    try:
        return JsonResponse(
            {
                "status": "success",
                "updates": loop_updates,
                "timestamp": datetime.now().isoformat(),
            }
        )
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


def update_balance(
    mint,
    formatted_total_amount,
    wallet_details,
    reversed_balance_changes,
    balance_changes,
):
    try:
        global loop_updates
        cur_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Update the loop_updates dictionary
        loop_updates[mint] = {
            "time": cur_time,
            "mint": mint,
            "formatted_total_amount": formatted_total_amount,  # Ensure consistent type
            "wallet_details": str(wallet_details),  # Ensure string format
            "reversed_balance_changes": reversed_balance_changes,
            "sign": balance_changes[-1],
        }

        # Optional: Limit the size of loop_updates to prevent memory issues
        if len(loop_updates) > 1000:  # Adjust limit as needed
            oldest_key = min(loop_updates.keys(), key=lambda k: loop_updates[k]["time"])
            del loop_updates[oldest_key]

        return True
    except Exception as e:
        print(f"Error updating balance: {e}")
        return False


@csrf_exempt
def start_loop(request):
    if request.method == "POST":
        data = json.loads(request.body)
        mint = data.get("mint")
        loop_time = data.get("loop_time")

        if mint and loop_time:
            tracker = TokenTracker(mint, loop_time)
            tracker.start()

            return JsonResponse({"status": "success", "mint": mint})

    return JsonResponse({"status": "error"})


@csrf_exempt
def stop_loop(request):
    if request.method == "POST":
        data = json.loads(request.body)
        mint = data.get("mint")

        if mint in active_loops:
            tracker = active_loops[mint]
            tracker.stop()
            return JsonResponse({"status": "success", "mint": mint})

    return JsonResponse({"status": "error"})


@csrf_exempt
def get_active_loops(request):
    loops = [
        {"mint": mint, "loop_time": tracker.loop_time}
        for mint, tracker in active_loops.items()
    ]
    return JsonResponse({"loops": loops})

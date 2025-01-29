# views.py
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
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


def save_largest_accounts(data, mint_address):
    file_path = f"largest_accounts_{mint_address[:6]}.json"
    all_data = {}

    # Добавляем или обновляем данные для текущего mint
    all_data[mint_address] = data

    with open(file_path, "w") as f:
        json.dump(all_data, f, indent=2)


async def fetch_page(session, page, mint):
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
        if response.status != 200:
            print(f"Error fetching page {page}: {response.status}")
            return []
        data = await response.json()
        token_accounts = data.get("result", {}).get("token_accounts", [])
        return token_accounts


async def fetch_page_with_semaphore(session, page, semaphore, mint):
    async with semaphore:
        return await fetch_page(session, page, mint)


async def find_largest_holders(
    mint, loop_time, stop_flag, previous_total_balance, balance_changes
):
    print(f"find_largest_holders for mint {mint} has started")
    previous_total_balance = 0
    max_pages = 10
    semaphore = asyncio.Semaphore(20)
    all_accounts = []
    balance_changes = []
    reversed_balance_changes = []

    while True:
        all_accounts = []
        async with aiohttp.ClientSession() as session:
            tasks = []

            for page in range(1, max_pages + 1):
                tasks.append(fetch_page_with_semaphore(session, page, semaphore, mint))

            results = await asyncio.gather(*tasks)

            for accounts in results:
                if accounts:
                    all_accounts.extend(accounts)

        cur_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"Current time: {cur_time}")

        all_accounts.sort(key=lambda x: x["amount"], reverse=True)
        top_1000_accounts = all_accounts[:1000]

        # Сохраняем данные для текущего mint
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

        wallets_to_check = [wallet["wallet"] for wallet in wallets_data]
        accounts_dict = {
            account["owner"]: account["amount"] for account in top_1000_accounts
        }

        # Формируем список matching_wallets, где проверяем кошельки по ключу mint
        matching_wallets = [
            {"wallet": wallet, "amount": accounts_dict[wallet]}
            for wallet in wallets_to_check
            if wallet in accounts_dict
        ]

        total_amount = sum(wallet["amount"] for wallet in matching_wallets)
        formatted_total_amount = f"{(total_amount/1000000) / 1000000000:,.4f}"

        matching_wallets.sort(key=lambda wallet: wallet["amount"], reverse=True)
        wallets_dict = {wallet["wallet"]: wallet["name"] for wallet in wallets_data}

        print(f"Total balance of matching wallets for mint {mint}: {total_amount}")

        for wallet in matching_wallets:
            wallet["name"] = wallets_dict.get(wallet["wallet"], "Unknown")

        wallet_details = "\n".join(
            [
                f"{wallet['name']}, {(wallet['amount']/1000000) / 1000000000:,.4f}%"
                for wallet in matching_wallets
            ]
        )

        if total_amount != previous_total_balance:
            if total_amount < previous_total_balance:
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

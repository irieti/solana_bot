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
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
)
from django.views import View
from telegram import Update
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
import logging
import traceback
import re
import pandas as pd
import json
import logging


logger = logging.getLogger("telegram_bot")

load_dotenv()

# Configuration
url = os.getenv("URL_API")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
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
token_name = {}

token_decimals = {}

bot = Bot(token=os.getenv("TOKEN"))

application = Application.builder().token(TOKEN).build()


def set_webhook():
    try:
        # Delete existing webhook
        delete_url = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=true"
        delete_response = requests.get(delete_url)
        logger.info(f"Delete webhook response: {delete_response.text}")

        # Set new webhook
        url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
        response = requests.post(url, data={"url": WEBHOOK_URL})

        logger.info(f"Webhook setup response: {response.text}")

        if response.status_code == 200:
            logger.info("Webhook set successfully!")

            # Verify webhook
            info_url = f"https://api.telegram.org/bot{TOKEN}/getWebhookInfo"
            info_response = requests.get(info_url)
            logger.info(f"Webhook info: {info_response.text}")

            return True
        else:
            logger.error(
                f"Failed to set webhook. Status code: {response.status_code}, Response: {response.text}"
            )
            return False

    except Exception as e:
        logger.error(f"Exception during webhook setup: {str(e)}", exc_info=True)
        return False


webhook_state = {"active": False}


WEBHOOK_STATE_FILE = "webhook_state.json"


def load_webhook_state():
    if os.path.exists(WEBHOOK_STATE_FILE):
        with open(WEBHOOK_STATE_FILE, "r") as f:
            return json.load(f)
    return {"active": False}


def save_webhook_state(state):
    with open(WEBHOOK_STATE_FILE, "w") as f:
        json.dump(state, f)


webhook_state = load_webhook_state()


@csrf_exempt
@require_http_methods(["GET"])
def webhook_status(request):
    """Возвращает текущий статус вебхука"""
    return JsonResponse({"active": webhook_state["active"]})


@csrf_exempt
@require_http_methods(["POST"])
def toggle_webhook(request):
    """Переключает состояние вебхука"""
    try:
        data = json.loads(request.body)
        webhook_state["active"] = data.get("active", False)
        save_webhook_state(webhook_state)  # Сохраняем состояние
        return JsonResponse({"status": "success", "active": webhook_state["active"]})
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


@csrf_exempt
async def webhook(request):
    if not webhook_state["active"]:
        return JsonResponse({"status": "webhook_inactive"}, status=200)
    try:
        if request.method == "POST":
            json_str = request.body.decode("utf-8")

            update = Update.de_json(json.loads(json_str), bot)

            message_text = update.message.text
            print(f"Message recieved: {message_text}")

            token_match = re.search(r"INFO\s*([A-Za-z0-9]{44})", message_text)

            if token_match:
                match_token = token_match.group(1)
                print(f"TOKEN: {match_token}")
                start_loop_with_token(match_token)
            else:
                print("TOKEN NOT FOUND")

            await application.update_queue.put(update)

            return JsonResponse({"status": "ok"})
        else:
            return JsonResponse({"status": "failed"}, status=400)

    except Exception as e:
        print(f"Ошибка в webhook: {e}")
        print(traceback.format_exc())
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


def get_token_name(mint):
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={
            "jsonrpc": "2.0",
            "id": "test",
            "method": "getAsset",
            "params": {"id": mint},
        },
    )

    data = response.json()

    name = data["result"]["content"]["metadata"]["name"]
    token_decimals[mint] = data["result"]["token_info"]["decimals"]

    print(f"Name: {name}")
    return name


def save_largest_accounts(data, mint_address):
    all_data = {}

    # Добавляем или обновляем данные для текущего mint
    all_data[mint_address] = data


async def fetch_page_with_retry(session, page, semaphore, mint):
    max_retries = 3
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
                        delay = 0.1
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
    formatted_previous_total_amount = 0
    max_pages = 1000  # Increased from 10 to 1000
    semaphore = asyncio.Semaphore(5)  # Reduced from 20 to 5
    all_accounts = []
    balance_changes = []
    reversed_balance_changes = []
    sign = ""
    formatted_balance_changes = []
    previous_sign = ""

    while not stop_flag.is_set():
        print(f"Fetching data for {mint}...")
        if stop_flag.is_set():
            print(f"Stopping async task for {mint}")
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

                if empty_pages > chunk_size // 2:
                    stop_fetching = True
                    break

                if stop_flag.is_set():
                    print(f"Stopping find_largest_holders for mint {mint}")
                    break
                await asyncio.sleep(0.5)

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
        num_matching_wallets = len(matching_wallets)

        total_amount = sum(wallet["amount"] for wallet in matching_wallets)
        if token_decimals[mint] == 6:
            formatted_total_amount = (
                f"{((total_amount/1000000) / 1000000000) * 100:,.4f}%"
            )
        elif token_decimals[mint] == 9:
            formatted_total_amount = (
                f"{((total_amount/1000000000) / 1000000000) * 100:,.4f}%"
            )

        wallets_dict = {wallet["wallet"]: wallet["name"] for wallet in wallets_data}

        # Сортируем словарь по значению (имени)

        print(f"Total balance of matching wallets for mint {mint}: {total_amount}")

        wallet_details_list = []

        for wallet in matching_wallets:
            wallet["name"] = wallets_dict.get(wallet["wallet"], "Unknown")

            divisor = 10**6 if token_decimals[mint] == 6 else 10**9
            wallet_amount = (
                f"{((wallet['amount'] / divisor) / 1_000_000_000) * 100:,.4f}%"
            )

            wallet_details_list.append(f"{wallet['name']}, {wallet_amount}")
            wallet_details_list.sort()

        wallet_details = "<br>".join(wallet_details_list)

        if total_amount != previous_total_balance:
            if total_amount < previous_total_balance:
                sign = "🔴"
                update_balance(
                    mint=mint,
                    formatted_total_amount=formatted_total_amount,
                    num_matching_wallets=num_matching_wallets,
                    wallet_details=wallet_details,
                    formatted_balance_changes=formatted_balance_changes,
                    sign=sign,
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

                balance_changes.append(
                    f"{num_matching_wallets} {previous_sign} {formatted_previous_total_amount}%"
                )
                reversed_balance_changes = list(reversed(balance_changes))
                formatted_balance_changes = "</br>".join(
                    map(str, reversed_balance_changes)
                )

            elif total_amount > previous_total_balance:
                print(
                    f"Balance changed for mint {mint}! Previous total: {previous_total_balance}, New total: {formatted_total_amount}"
                )
                sign = "🟢"
                update_balance(
                    mint=mint,
                    formatted_total_amount=formatted_total_amount,
                    num_matching_wallets=num_matching_wallets,
                    wallet_details=wallet_details,
                    formatted_balance_changes=formatted_balance_changes,
                    sign=sign,
                )
                message = (
                    f"{cur_time}\n"
                    f"<b>{mint}</b>\n"
                    f"{reversed_balance_changes}\n"
                    f"<b>🟢 {formatted_total_amount}</b>\n"
                    f"{wallet_details}"
                )

                balance_changes.append(
                    f"{num_matching_wallets} {previous_sign} {formatted_previous_total_amount}%"
                )
                reversed_balance_changes = list(reversed(balance_changes))
                formatted_balance_changes = "</br>".join(
                    map(str, reversed_balance_changes)
                )
        else:
            print(f"Balance has not changed for mint {mint}.")
            sign = "⚪️"
            update_balance(
                mint=mint,
                formatted_total_amount=formatted_total_amount,
                num_matching_wallets=num_matching_wallets,
                wallet_details=wallet_details,
                formatted_balance_changes=formatted_balance_changes,
                sign=sign,
            )
            message = (
                f"{cur_time}\n"
                f"<b>{mint}</b>\n"
                f"{reversed_balance_changes}\n"
                f"<b>🟠 {formatted_total_amount}</b>\n"
                f"{wallet_details}"
            )
            balance_changes.append(
                f"{num_matching_wallets} {previous_sign} {formatted_previous_total_amount}%"
            )
            reversed_balance_changes = list(reversed(balance_changes))
            formatted_balance_changes = "</br>".join(map(str, reversed_balance_changes))

        previous_total_balance = total_amount
        previous_sign = sign
        formatted_previous_total_amount = (
            f"{(previous_total_balance/1000000) / 10000000:,.4f}"
        )

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
    def __init__(self, mint, loop_time, name=None):
        self.mint = mint
        self.loop_time = int(loop_time)
        self.stop_flag = threading.Event()
        self.balance_changes = []
        self.name = name
        self.thread = None

    def start(self):
        """Запускает поток для отслеживания изменений баланса."""
        if (
            self.mint not in active_loops
        ):  # Проверяем, не запущен ли уже трекер для этого минта
            self.thread = threading.Thread(target=self._run_loop)
            self.thread.daemon = True
            self.thread.start()
            active_loops[self.mint] = self
            print(f"Started tracking {self.mint}. Active loops: {len(active_loops)}")

    def stop(self):
        """Принудительно останавливает процесс."""
        if self.mint in active_loops:
            print(f"Stopping {self.mint}...")
            self.stop_flag.set()  # Устанавливаем флаг остановки

            if self.task:
                print(f"Cancelling async task for {self.mint}...")
                self.task.cancel()  # Останавливаем `asyncio` таск

            if self.thread and self.thread.is_alive():
                print(f"Waiting for thread {self.mint} to stop...")
                self.thread.join(timeout=5)  # Ожидаем завершения потока

            del active_loops[self.mint]
            print(f"Stopped tracking {self.mint}. Active loops: {len(active_loops)}")

    def _run_loop(self):
        """Цикл обновления данных."""
        previous_total_balance = 0
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        self.task = loop.create_task(
            find_largest_holders(
                self.mint,
                self.loop_time,
                self.stop_flag,
                previous_total_balance,
                self.balance_changes,
            )
        )
        try:
            loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            print(f"Task for {self.mint} cancelled.")
        finally:
            loop.close()

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
        "all_mints": ["9L5BqqVA3EJ1XrGrS4HaXssivEewfme3DWoXCTBEVE9e"],
        "running_loops": [
            {"mint": mint, "loop_time": tracker.loop_time}
            for mint, tracker in active_loops.items()
        ],
    }
    set_webhook()

    return render(request, "index.html", context)


@require_http_methods(["GET"])
def get_loop_updates(request):
    return JsonResponse(
        {
            "status": "success",
            "updates": loop_updates,
            "timestamp": datetime.now().isoformat(),
        }
    )


def update_balance(
    mint,
    formatted_total_amount,
    num_matching_wallets,
    wallet_details,
    formatted_balance_changes,
    sign,
):
    try:
        global loop_updates
        cur_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Update the loop_updates dictionary
        loop_updates[mint] = {
            "time": cur_time,
            "mint": str(mint)[:6],
            "name": token_name.get(mint, "Unknown"),
            "formatted_total_amount": formatted_total_amount,
            "num_matching_wallets": num_matching_wallets,
            "wallet_details": str(wallet_details),  # Ensure string format
            "formatted_balance_changes": formatted_balance_changes,
            "sign": sign,
        }
        print(f"{loop_updates}")

        # Optional: Limit the size of loop_updates to prevent memory issues
        if len(loop_updates) > 1000:  # Adjust limit as needed
            oldest_key = min(loop_updates.keys(), key=lambda k: loop_updates[k]["time"])
            del loop_updates[oldest_key]

        return True
    except Exception as e:
        print(f"Error updating balance: {e}")
        return False


@csrf_exempt
def start_loop_with_token(match_token):
    mint = match_token
    loop_time = 2

    name = get_token_name(mint)
    token_name[mint] = name
    print(f"name: {name}")
    tracker = TokenTracker(mint, loop_time, name)
    tracker.start()


@csrf_exempt
def start_loop(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            mint = data.get("mint")
            loop_time = data.get("loop_time")

            if mint and loop_time:
                name = get_token_name(mint)
                token_name[mint] = name
                print(f"Starting tracker for {mint} with name: {name}")

                # Остановить существующий трекер, если есть
                if mint in active_loops:
                    active_loops[mint].stop()

                tracker = TokenTracker(mint, loop_time, name)
                tracker.start()
                return JsonResponse(
                    {
                        "status": "success",
                        "mint": mint,
                        "active_loops": len(active_loops),
                    }
                )

            return JsonResponse(
                {"status": "error", "message": "Missing mint or loop_time"}
            )
        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)})

    return JsonResponse({"status": "error", "message": "Invalid method"})


@csrf_exempt
def stop_loop(request):
    if request.method == "POST":
        data = json.loads(request.body)
        mint = data.get("mint")

        if mint in active_loops:
            tracker = active_loops[mint]
            tracker.stop()
            print("Current active_loops after stop:", active_loops.keys())

            return JsonResponse({"status": "success", "mint": mint})

    return JsonResponse({"status": "error"})


@csrf_exempt
def get_active_loops(request):
    try:
        loops_data = [
            {
                "mint": mint,
                "loop_time": tracker.loop_time,
                "name": tracker.name or "Unknown",
                "mint_part": str(mint)[:6],
            }
            for mint, tracker in active_loops.items()
            if tracker.thread and tracker.thread.is_alive()
        ]

        return JsonResponse(
            {
                "status": "success",
                "loops": loops_data,
                "count": len(loops_data),
                "timestamp": datetime.now().isoformat(),
            }
        )
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})


def create_loops_response():
    return JsonResponse(
        {
            "status": "success",
            "loops": [
                {
                    "mint": mint,
                    "loop_time": tracker.loop_time,
                    "name": tracker.name,
                    "mint_part": str(mint)[:6],
                }
                for mint, tracker in active_loops.items()
            ],
            "timestamp": datetime.now().isoformat(),
        }
    )


WALLET_FILE = "wallets.json"


def load_wallets():
    """Загружает кошельки из wallet.json"""
    try:
        with open(WALLET_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


@csrf_exempt
def save_wallets(wallet_data):
    """Сохраняет обновленный список кошельков"""
    with open(WALLET_FILE, "w", encoding="utf-8") as f:
        json.dump(wallet_data, f, indent=4, ensure_ascii=False)


@csrf_exempt
def new_wallet(request):
    """Добавляет новый кошелек в wallet.json"""
    if request.method == "POST":
        wallet_address = request.POST.get("wallet")  # Получаем адрес кошелька из формы
        wallet_name = request.POST.get("name")

        if not wallet_address:
            return JsonResponse(
                {"status": "error", "message": "No wallet address provided"}, status=400
            )

        wallets = load_wallets()

        # Проверяем, нет ли уже такого адреса
        if any(w["wallet"] == wallet_address for w in wallets):
            return JsonResponse(
                {"status": "error", "message": "Wallet already exists"}, status=400
            )

        # Добавляем новый адрес
        new_wallet_entry = {"wallet": wallet_address, "name": wallet_name}
        wallets.append(new_wallet_entry)

        save_wallets(wallets)

        return JsonResponse(
            {"status": "success", "message": "Wallet added", "wallet": new_wallet_entry}
        )

    return JsonResponse(
        {"status": "error", "message": "Invalid request method"}, status=405
    )


def get_wallets(request):
    return JsonResponse(load_wallets(), safe=False)


def delete_wallet(request, index):
    wallets = load_wallets()
    try:
        index = int(index)
        if 0 <= index < len(wallets):
            del wallets[index]
            save_wallets(wallets)
            return JsonResponse({"message": "Wallet deleted"})
        else:
            return JsonResponse({"error": "Error, please try again"}, status=400)
    except ValueError:
        return JsonResponse({"error": "Error, please try again"}, status=400)


@csrf_exempt
def add_wallets_excel(request):
    if request.method == "POST" and request.FILES.get("file"):
        file = request.FILES["file"]

        try:
            df = pd.read_excel(file)

            df.columns = df.columns.str.strip()

            if "wallet" in df.columns and "name" in df.columns:
                wallets_data = df[["wallet", "name"]].to_dict(orient="records")

                with open("wallets.json", "w") as json_file:
                    json.dump(wallets_data, json_file, indent=4)

                return JsonResponse(
                    {"message": "Wallets have been updated"}, status=200
                )
            else:
                return JsonResponse(
                    {
                        "error": "File must contain 'wallet' and 'name'",
                        "columns": list(df.columns),
                    },
                    status=400,
                )

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Error occured"}, status=400)

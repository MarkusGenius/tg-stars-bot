import asyncio
import hashlib
import logging
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import urlencode

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
from flask import Flask, request, jsonify

# ========= ЗАМЕНИ НА СВОИ =========
BOT_TOKEN = "8309652807:AAGm9d0lWcUcqonxFOgXruXpHDxE2ClUwfI"
BOT_USERNAME = "Zvezda_TON_bot"

MERCHANT_ID = "64994"
SECRET_WORD_1 = "/p7a$bkbbVurXI]"  # для ссылки оплаты
SECRET_WORD_2 = "9Mx,aLBqz(5Vc6?"  # для подписи вебхука

SUBSCRIPTION_RUB = 200
STAR_TO_RUB_RATE = 1.19  # 1⭐ = 4 ₽

# URL воркера (обязательно для авто‑покупки)
WORKER_URL = "http://<VPS_IP>:8000"  # пример: "http://1.2.3.4:8000"
WORKER_SECRET = "CHANGE_ME_SHARED_SECRET"

PORT = int(os.getenv("PORT", "8080"))
# ==================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
app = Flask(__name__)

bot_loop: asyncio.AbstractEventLoop = None

class OrderStates(StatesGroup):
    waiting_for_recipient = State()
    waiting_for_payment = State()

orders_db = {}          # order_id -> order dict
pending_by_user = {}    # user_id -> order dict

def check_username_format(username: str) -> bool:
    u = username.replace("@", "")
    return re.match(r"^[a-zA-Z][a-zA-Z0-9_]{4,31}$", u) is not None

def calculate_stars_cost(stars_count: int) -> int:
    return int(stars_count * STAR_TO_RUB_RATE)

def generate_payment_link(amount_rub: int, order_id: str, user_id: int, username: str) -> str:
    sign = hashlib.md5(f"{MERCHANT_ID}:{amount_rub}:{SECRET_WORD_1}:{order_id}".encode()).hexdigest()
    params = {
        "m": MERCHANT_ID,
        "oa": str(amount_rub),
        "o": order_id,
        "s": sign,
        "us_user_id": str(user_id),
        "us_username": username or "",
    }
    return f"https://pay.freekassa.ru/?{urlencode(params)}"

def verify_payment_signature(data: dict) -> bool:
    try:
        merchant_id = data.get("MERCHANT_ID")
        amount = data.get("AMOUNT")
        merchant_order_id = data.get("MERCHANT_ORDER_ID")
        sign = data.get("SIGN", "")
        expected = hashlib.md5(f"{merchant_id}:{amount}:{SECRET_WORD_2}:{merchant_order_id}".encode()).hexdigest()
        return sign.upper() == expected.upper()
    except Exception as e:
        logging.error(f"verify_payment_signature error: {e}")
        return False

def request_auto_purchase(order: dict) -> str:
    """Отправляет заказ воркеру. Возвращает job_id."""
    r = requests.post(
        f"{WORKER_URL}/api/purchase",
        json={"order_id": order["order_id"], "recipient": order["recipient"], "quantity": order["stars_count"]},
        headers={"X-Auth": WORKER_SECRET},
        timeout=20
    )
    r.raise_for_status()
    job_id = r.json().get("job_id")
    if not job_id:
        raise RuntimeError("worker did not return job_id")
    return job_id

def poll_worker_until_done(order_id: str, job_id: str):
    """Фоновая проверка статуса job у воркера. По завершении — уведомляем пользователя."""
    try:
        while True:
            time.sleep(5)
            try:
                r = requests.get(f"{WORKER_URL}/api/status/{job_id}", timeout=15)
                r.raise_for_status()
                status = r.json()
            except Exception as e:
                logging.warning(f"poll error for {order_id}: {e}")
                continue

            state = status.get("status")
            if state in ("done", "failed"):
                order = orders_db.get(order_id)
                if not order:
                    return
                if state == "done":
                    order["status"] = "completed"
                    order["completed_at"] = datetime.now().isoformat()
                    if bot_loop and bot_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bot.send_message(
                                order["user_id"],
                                f"🎉 Спасибо за покупку!\n\n"
                                f"⭐ {order['stars_count']:,} звёзд успешно отправлены пользователю @{order['recipient']}.\n"
                                f"Будем ждать вас снова!"
                            ),
                            bot_loop
                        )
                else:
                    err = status.get("error", "unknown error")
                    order["status"] = "failed"
                    if bot_loop and bot_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bot.send_message(
                                order["user_id"],
                                "😔 Техническая ошибка при покупке звёзд. Мы уже разбираемся."
                            ),
                            bot_loop
                        )
                return
    except Exception as e:
        logging.error(f"poll_worker_until_done fatal for {order_id}: {e}")

async def notify_user_paid(order: dict) -> None:
    try:
        await bot.send_message(
            order["user_id"],
            "✅ Оплата прошла! Ожидайте 5–10 минут — мы покупаем звёзды автоматически."
        )
    except Exception as e:
        logging.error(f"notify_user_paid error: {e}")

# ============== Flask endpoints ==============

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.form.to_dict()
        logging.info(f"Free‑Kassa webhook: {data}")

        if not verify_payment_signature(data):
            logging.error("Invalid signature")
            return "ERROR", 400

        order_id = data.get("MERCHANT_ORDER_ID")
        amount = float(data.get("AMOUNT", "0"))
        user_id_from_cb = int(data.get("us_user_id", "0") or 0)

        order = orders_db.get(order_id)
        if not order:
            logging.error("Order not found")
            return "ERROR", 404

        if user_id_from_cb != order["user_id"]:
            logging.error("User mismatch")
            return "ERROR", 400

        if amount < order["total_cost"]:
            logging.error(f"Underpayment: {amount} < {order['total_cost']}")
            return "ERROR", 400

        # помечаем оплачено
        order["status"] = "paid"
        order["paid_at"] = datetime.now().isoformat()

        # уведомляем пользователя
        if bot_loop and bot_loop.is_running():
            asyncio.run_coroutine_threadsafe(notify_user_paid(order), bot_loop)

        # отправляем заказ воркеру
        job_id = request_auto_purchase(order)
        order["job_id"] = job_id
        order["status"] = "purchasing"

        # запускаем фоновый опрос статуса
        t = threading.Thread(target=poll_worker_until_done, args=(order_id, job_id), daemon=True)
        t.start()

        return "YES"
    except Exception as e:
        logging.error(f"webhook error: {e}")
        return "ERROR", 500

@app.route("/success.html")
def success_page():
    return f"""
    <!doctype html>
    <html><head>
      <meta charset="utf-8" />
      <meta http-equiv="refresh" content="3;url=https://t.me/{BOT_USERNAME}?start=success" />
      <meta name="enot" content="ad293e60" />
      <title>Оплата успешна</title>
    </head>
    <body style="font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif; text-align:center; padding:40px;">
      <div style="font-size:48px">✅</div>
      <h2>Оплата прошла успешно</h2>
      <p>Сейчас вернём вас в бота…</p>
      <a href="https://t.me/{BOT_USERNAME}?start=success">Перейти в бота</a>
    </body></html>
    """

@app.route("/failed.html")
def failed_page():
    return f"""
    <!doctype html>
    <html><head>
      <meta charset="utf-8" />
      <meta http-equiv="refresh" content="3;url=https://t.me/{BOT_USERNAME}?start=failed" />
      <title>Ошибка оплаты</title>
    </head>
    <body style="font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif; text-align:center; padding:40px;">
      <div style="font-size:48px">❌</div>
      <h2>Оплата не прошла</h2>
      <p>Попробуйте ещё раз или обратитесь в поддержку</p>
      <a href="https://t.me/{BOT_USERNAME}?start=failed">Перейти в бота</a>
    </body></html>
    """

@app.route("/")
def index():
    return jsonify({"status": "running", "service": "telegram-stars-bot", "version": "1.0"})

@app.route("/health")
def health():
    return jsonify({"status": "healthy"})

# ============== Bot handlers ==============

@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message, state: FSMContext):
    args = message.get_args()
    if args == "success":
        await message.answer("✅ Оплата прошла успешно! Мы уже покупаем звёзды.")
        return
    if args == "failed":
        await message.answer("❌ Оплата не прошла. Попробуйте ещё раз.")
        return

    if not message.from_user.username:
        await message.answer(
            "❌ У вас отсутствует username в Telegram!\n"
            "Установите его в настройках и вернитесь с /start."
        )
        return

    await state.finish()
    await message.answer(
        "🌟 Добро пожаловать!\n\n"
        "Здесь вы можете приобрести звёзды по чистому курсу — всё автоматически.\n"
        f"💱 Текущий курс: 1⭐ = {STAR_TO_RUB_RATE} ₽\n\n"
        "📝 Напишите: @username количество\nПример: @durov 50"
    )
    await OrderStates.waiting_for_recipient.set()

@dp.message_handler(state=OrderStates.waiting_for_recipient)
async def handle_recipient(message: types.Message, state: FSMContext):
    try:
        parts = message.text.strip().split()
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Пример: @durov 50")
            return

        recipient = parts[0].replace("@", "")
        try:
            stars = int(parts[1])
        except ValueError:
            await message.answer("❌ Количество звёзд должно быть целым числом")
            return

        if stars <= 0 or stars > 10000:
            await message.answer("❌ Количество должно быть 1..10000")
            return

        if not check_username_format(recipient):
            await message.answer("❌ Некорректный username. Проверьте формат.")
            return

        stars_cost = calculate_stars_cost(stars)
        total_cost = SUBSCRIPTION_RUB + stars_cost

        order_id = f"order_{message.from_user.id}_{int(datetime.now().timestamp())}"
        order = {
            "order_id": order_id,
            "user_id": message.from_user.id,
            "username": message.from_user.username,
            "recipient": recipient,
            "stars_count": stars,
            "subscription_fee": SUBSCRIPTION_RUB,
            "stars_cost": stars_cost,
            "total_cost": total_cost,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        }
        orders_db[order_id] = order
        pending_by_user[message.from_user.id] = order

        pay_link = generate_payment_link(total_cost, order_id, message.from_user.id, message.from_user.username or "")

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("💳 Оплатить (СБП/карта)", url=pay_link))
        kb.add(types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}"))

        await message.answer(
            "Отлично! Необходимо оплатить подписку и звёзды.\n"
            f"— Подписка: {SUBSCRIPTION_RUB} ₽\n"
            f"— Звёзды: {stars_cost} ₽\n"
            f"— Итого к оплате: {total_cost} ₽",
            reply_markup=kb
        )
        await OrderStates.waiting_for_payment.set()
    except Exception as e:
        logging.error(f"handle_recipient error: {e}")
        await message.answer("❌ Произошла ошибка. Попробуйте ещё раз.")

@dp.callback_query_handler(lambda c: c.data.startswith("cancel_"))
async def cancel_order(callback_query: types.CallbackQuery, state: FSMContext):
    order_id = callback_query.data.replace("cancel_", "")
    orders_db.pop(order_id, None)
    pending_by_user.pop(callback_query.from_user.id, None)
    await state.finish()
    await callback_query.message.edit_text("❌ Заказ отменён. Нажмите /start для нового заказа.")
    await callback_query.answer("Отменено")

@dp.message_handler()
async def fallback(message: types.Message):
    await message.answer("👋 Нажмите /start, чтобы начать.")

def start_bot():
    global bot_loop
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    executor.start_polling(dp, skip_updates=True)

def start_web():
    app.run(host="0.0.0.0", port=PORT, debug=False)

if __name__ == "__main__":
    t = threading.Thread(target=start_bot, daemon=True)
    t.start()
    logging.info("🚀 Telegram Stars Bot started")
    start_web()

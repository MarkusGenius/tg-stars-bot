import asyncio
import hashlib
import hmac
import logging
import time
from datetime import datetime
import threading
import re

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
from flask import Flask, request, jsonify

# =============================================================================
# НАСТРОЙКИ — ВСЕ ПРЯМО В КОДЕ
# =============================================================================
BOT_TOKEN = "8309652807:AAGm9d0lWcUcqonxFOgXruXpHDxE2ClUwfI"
ADMIN_ID = 8127196287

# Lava (обычный кошелёк)
LAVA_WALLET_ID = "R10230965"
LAVA_SECRET_KEY = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1aWQiOiIwYTk1MjRjZS03N2U2LWYyZGEtYmUyZi04MGE3MmFiYzlkNjgiLCJ0aWQiOiJlOTU2MzZhNS1iMTAxLWIyODEtNGRkNC0xNjFmNTFlODVkMzEifQ.pkKulnSczEvZJ-UdTB7jbEEaIDmTp4qrF8I2C6IG1BM"

# Курс
STAR_TO_RUB_RATE = 1.2

# Домен для редиректов (success/fail)
DOMAIN = "https://tg-stars-bot-production-c736.up.railway.app/"

# Порт Flask (для Railway удобно 8080)
PORT = 8080
# =============================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
app = Flask(__name__)

class OrderStates(StatesGroup):
    waiting_for_recipient = State()
    waiting_for_payment = State()

orders_db = {}

def generate_lava_signature(data_string: str, secret_key: str) -> str:
    sign_string = data_string + secret_key
    return hashlib.sha256(sign_string.encode('utf-8')).hexdigest()

def verify_lava_signature(data: dict, received_signature: str, secret_key: str) -> bool:
    try:
        sign_data = []
        for key in sorted(data.keys()):
            if key != 'signature':
                sign_data.append(str(data[key]))
        sign_string = ':'.join(sign_data)
        expected_signature = generate_lava_signature(sign_string, secret_key)
        return hmac.compare_digest(received_signature, expected_signature)
    except Exception:
        return False

def check_username_exists(username: str) -> bool:
    try:
        clean_username = username.replace('@', '')
        return bool(re.match(r'^[a-zA-Z][a-zA-Z0-9_]{4,31}$', clean_username))
    except Exception:
        return False

def calculate_cost(stars_count: int) -> float:
    return round(stars_count * STAR_TO_RUB_RATE, 2)

def create_lava_invoice(amount: float, order_id: str, user_id: int) -> str:
    # строка подписи для примера (у Lava фактические поля могут отличаться — подстройте под свой кабинет)
    sign_string = f"{amount}:{order_id}:{LAVA_WALLET_ID}"
    signature = generate_lava_signature(sign_string, LAVA_SECRET_KEY)
    comment = f"Stars {orders_db[order_id]['stars_count']} for @{orders_db[order_id]['recipient']}"
    payment_url = (
        f"https://lava.ru/pay/{LAVA_WALLET_ID}"
        f"?amount={amount}"
        f"&order_id={order_id}"
        f"&comment={comment[:100]}"
        f"&success_url={DOMAIN}/success"
        f"&fail_url={DOMAIN}/failed"
        f"&signature={signature}"
    )
    return payment_url

# =============================================================================
# FLASK ROUTES (webhook от Lava)
# =============================================================================
@app.route('/webhook/lava', methods=['POST'])
def lava_webhook():
    try:
        data = request.get_json(silent=True) or request.form.to_dict()
        if not data:
            return jsonify({"error": "Empty data"}), 400

        signature = data.get('signature')
        if not signature or not verify_lava_signature(data, signature, LAVA_SECRET_KEY):
            return jsonify({"error": "Invalid signature"}), 400

        order_id = data.get('order_id')
        status = (data.get('status') or '').lower()
        amount = float(data.get('amount', 0) or 0)

        if order_id in orders_db and status in ['success', 'paid', 'complete']:
            order = orders_db[order_id]
            if amount + 1e-6 >= order['cost'] * 0.95:
                order['status'] = 'paid'
                order['paid_at'] = datetime.now().isoformat()
                order['paid_amount'] = amount

                def notify_async():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(notify_payment_success(order['user_id'], order_id))
                    loop.close()

                threading.Thread(target=notify_async, daemon=True).start()

        return jsonify({"status": "ok"})
    except Exception as e:
        logging.exception(e)
        return jsonify({"error": "Internal error"}), 500

@app.route('/success')
def success_page():
    return "Оплата прошла успешно. Вернитесь в бота."

@app.route('/failed')
def failed_page():
    return "Оплата не была завершена. Попробуйте снова."

@app.route('/')
def index():
    return jsonify({"status": "running", "orders": len(orders_db)})

# =============================================================================
# TELEGRAM HANDLERS
# =============================================================================
async def notify_payment_success(user_id: int, order_id: str):
    try:
        order = orders_db.get(order_id)
        if not order:
            return

        await bot.send_message(
            user_id,
            "✅ Оплата прошла успешно!\nЗаказ передан на обработку. Ожидайте 5–10 минут."
        )

        fragment_url = f"https://fragment.com/stars/buy?recipient={order['recipient']}&quantity={order['stars_count']}"
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton("✅ Отправил звёзды", callback_data=f"process_{order_id}"),
            types.InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_admin_{order_id}")
        )

        admin_text = (
            f"💳 Оплата подтверждена\n\n"
            f"ID: {order_id}\n"
            f"От: @{order['username']} ({order['user_id']})\n"
            f"Кому: @{order['recipient']}\n"
            f"Звёзд: {order['stars_count']}\n"
            f"Оплачено: {order['cost']} руб.\n\n"
            f"Ссылка: {fragment_url}\n"
            f"После отправки звёзд нажми кнопку ниже."
        )
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=keyboard, disable_web_page_preview=False)
    except Exception as e:
        logging.exception(e)

@dp.message_handler(commands=['start'])
async def start_handler(message: types.Message, state: FSMContext):
    await state.finish()
    if not message.from_user.username:
        await message.answer("У вас не установлен username в Telegram. Установите его и повторите /start.")
        return

    await message.answer(
        f"Добро пожаловать!\nКурс: 1⭐ = {STAR_TO_RUB_RATE} руб.\n\n"
        "Формат заказа: @username количество\nПример: @durov 100"
    )
    await OrderStates.waiting_for_recipient.set()

@dp.message_handler(state=OrderStates.waiting_for_recipient)
async def process_recipient(message: types.Message, state: FSMContext):
    try:
        parts = message.text.strip().split()
        if len(parts) != 2:
            await message.answer("Неверный формат. Пример: @durov 100")
            return

        recipient = parts[0].replace('@', '')
        if not check_username_exists(recipient):
            await message.answer("Некорректный username. Пример: @durov")
            return

        stars_count = int(parts[1])
        if stars_count <= 0 or stars_count > 10000:
            await message.answer("Количество должно быть от 1 до 10000.")
            return

        cost = calculate_cost(stars_count)
        order_id = f"order_{message.from_user.id}_{int(time.time())}"

        orders_db[order_id] = {
            "user_id": message.from_user.id,
            "username": message.from_user.username,
            "recipient": recipient,
            "stars_count": stars_count,
            "cost": cost,
            "order_id": order_id,
            "status": "pending",
            "created_at": datetime.now().isoformat()
        }

        pay_url = create_lava_invoice(cost, order_id, message.from_user.id)
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("💳 Оплатить", url=pay_url),
            types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}")
        )

        await message.answer(
            f"Заказ создан:\nID: {order_id}\nКому: @{recipient}\nЗвёзд: {stars_count}\nК оплате: {cost} руб.",
            reply_markup=kb
        )
        await OrderStates.waiting_for_payment.set()
    except ValueError:
        await message.answer("Количество должно быть целым числом.")
    except Exception as e:
        logging.exception(e)
        await message.answer("Ошибка. Попробуйте ещё раз.")

@dp.callback_query_handler(lambda c: c.data.startswith('cancel_'), state='*')
async def cancel_order(callback_query: types.CallbackQuery, state: FSMContext):
    order_id = callback_query.data.replace('cancel_', '')
    orders_db.pop(order_id, None)
    await state.finish()
    await callback_query.message.edit_text("Заказ отменён.")
    await callback_query.answer("Отменено")

@dp.callback_query_handler(lambda c: c.data.startswith('process_'))
async def process_order(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("Доступ запрещён", show_alert=True)
        return

    order_id = callback_query.data.replace('process_', '')
    order = orders_db.get(order_id)
    if not order:
        await callback_query.answer("Заказ не найден", show_alert=True)
        return
    if order['status'] != 'paid':
        await callback_query.answer("Заказ ещё не оплачен", show_alert=True)
        return

    order['status'] = 'completed'
    order['completed_at'] = datetime.now().isoformat()

    try:
        await bot.send_message(
            order['user_id'],
            f"🎉 {order['stars_count']}⭐ доставлены пользователю @{order['recipient']}. Спасибо за покупку!"
        )
    except Exception:
        pass

    await callback_query.message.edit_text(
        f"Готово. Заказ {order_id} завершён.\n"
        f"@{order['username']} → @{order['recipient']}, {order['stars_count']}⭐, {order['cost']} руб."
    )
    await callback_query.answer("Завершено")

@dp.callback_query_handler(lambda c: c.data.startswith('cancel_admin_'))
async def cancel_order_admin(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("Доступ запрещён", show_alert=True)
        return

    order_id = callback_query.data.replace('cancel_admin_', '')
    order = orders_db.pop(order_id, None)
    if order:
        try:
            await bot.send_message(order['user_id'], "Ваш заказ отменён. Если списали деньги — напишите в поддержку.")
        except Exception:
            pass
        await callback_query.message.edit_text(f"Заказ {order_id} отменён.")
    else:
        await callback_query.message.edit_text("Заказ не найден.")
    await callback_query.answer("Отменено")

@dp.message_handler(commands=['help'])
async def help_handler(message: types.Message):
    await message.answer("Формат: @username количество. Пример: @durov 100")

@dp.message_handler()
async def default_handler(message: types.Message, state: FSMContext):
    cur = await state.get_state()
    if cur is None:
        await message.answer("Нажмите /start и следуйте инструкции.")

# =============================================================================
# ЗАПУСК
# =============================================================================
def run_flask():
    try:
        logging.info(f"Starting Flask on port {PORT}")
        app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
    except Exception as e:
        logging.exception(e)

async def _test_bot():
    me = await bot.get_me()
    logging.info(f"Bot connected: @{me.username}")

def run_bot():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_test_bot())
    executor.start_polling(dp, skip_updates=True)

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    time.sleep(1)
    run_bot()

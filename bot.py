import asyncio
import hashlib
import logging
import os
from datetime import datetime
from urllib.parse import urlencode
import re
import threading

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
from flask import Flask, request, jsonify

# ==========================
# ЗАМЕНИТЕ ЭТИ КОНСТАНТЫ
# ==========================
BOT_TOKEN = "8477520204:AAGoSYaKCC4_s_OWvAdcdJVY4JbrM3r3Zhk"
ADMIN_ID = 8127196287
BOT_USERNAME = "Zvezda_TON_bot"

MERCHANT_ID = "20064a4e69197f528804edb0ee1e57c5"
SECRET_WORD_1 = "kk54FvRhT}*{PBm"  # для формирования ссылки оплаты
SECRET_WORD_2 = "={XJytOz_=Jte&t"  # для проверки подписи уведомления

# Фиксированная стоимость "подписки" (заказ-инициатор) — 200 ₽
FIXED_SUBSCRIPTION_RUB = 200

# Отображаемый курс (для информации пользователю)
STAR_TO_RUB_RATE = 4.0

# Порт для веб-сервера (Railway задает PORT в окружении)
PORT = int(os.getenv("PORT", "8080"))

# ==========================

logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

app = Flask(__name__)

# Глобальный event loop бота (нужен для уведомлений из Flask-потока)
bot_loop: asyncio.AbstractEventLoop = None

class OrderStates(StatesGroup):
	waiting_for_recipient = State()
	waiting_for_payment = State()

# Память в процессе (замените БД в проде)
orders_db = {}
pending_payments = {}

def check_username_exists(username: str) -> bool:
	"""Формальная проверка валидности username по шаблону.
	Фактическую проверку существования пользователя бот сделать не может (ограничение API).
	"""
	try:
		clean = username.replace("@", "")
		return re.match(r"^[a-zA-Z][a-zA-Z0-9_]{4,31}$", clean) is not None
	except Exception:
		return False

def generate_payment_link(amount_rub: int, order_id: str, user_id: int, username: str) -> str:
	"""Ссылка на Free-Kassa. Пользователь сможет оплатить СБП.
	Если нужен предвыбор метода СБП — уточните у поддержки Free‑Kassa параметр (например, i=...).
	"""
	sign = hashlib.md5(f"{MERCHANT_ID}:{amount_rub}:{SECRET_WORD_1}:{order_id}".encode()).hexdigest()
	params = {
		"m": MERCHANT_ID,
		"oa": str(amount_rub),
		"o": order_id,
		"s": sign,
		"us_user_id": str(user_id),
		"us_username": username or "",
		# "i": "SBP_METHOD_ID",  # если Free‑Kassa дает id метода для автоподстановки — добавьте
	}
	return f"https://pay.freekassa.ru/?{urlencode(params)}"

def verify_payment_signature(data: dict) -> bool:
	"""Проверка подписи уведомления Free‑Kassa (SECRET_WORD_2)."""
	try:
		merchant_id = data.get("MERCHANT_ID")
		amount = data.get("AMOUNT")
		merchant_order_id = data.get("MERCHANT_ORDER_ID")
		sign = data.get("SIGN", "")

		expected_sign = hashlib.md5(
			f"{merchant_id}:{amount}:{SECRET_WORD_2}:{merchant_order_id}".encode()
		).hexdigest()

		return sign.upper() == expected_sign.upper()
	except Exception as e:
		logging.error(f"Ошибка проверки подписи: {e}")
		return False

async def notify_payment_success(user_id: int, order_id: str) -> None:
	"""Уведомление пользователя и админа о поступлении оплаты 200 ₽ и передача ссылки на Fragment для админа."""
	try:
		order = orders_db.get(order_id)
		if not order:
			return

		# Пользователю
		await bot.send_message(
			user_id,
			"✅ Оплата прошла! Ожидайте в течение 5–10 минут!\n"
			"Ваш заказ передан в обработку."
		)

		# Кнопки админа
		keyboard = types.InlineKeyboardMarkup()
		keyboard.add(
			types.InlineKeyboardButton("✅ Обработать заказ", callback_data=f"process_{order_id}")
		)
		keyboard.add(
			types.InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_admin_{order_id}")
		)

		# Ссылка на покупку Stars на Fragment (админ покупает вручную)
		fragment_url = f"https://fragment.com/stars/buy?recipient={order['recipient']}&quantity={order['stars_count']}"

		admin_text = (
			f"🔔 Новый оплаченный заказ!\n\n"
			f"📋 ID заказа: <code>{order_id}</code>\n"
			f"👤 Заказчик: @{order['username']}\n"
			f"🎯 Получатель: @{order['recipient']}\n"
			f"⭐ Количество звёзд: {order['stars_count']}\n"
			f"💰 Оплачено: {FIXED_SUBSCRIPTION_RUB} руб.\n"
			f"📅 Время оплаты: {order.get('paid_at', 'N/A')}\n\n"
			f"🔗 Ссылка на Fragment для покупки:\n"
			f"<code>{fragment_url}</code>\n\n"
			f"После покупки нажмите “Обработать заказ”."
		)

		await bot.send_message(ADMIN_ID, admin_text, reply_markup=keyboard, parse_mode="HTML")

	except Exception as e:
		logging.error(f"Ошибка при уведомлении об оплате: {e}")

# ==========================
# Flask маршруты (для Free‑Kassa и статусов)
# ==========================

@app.route("/webhook", methods=["POST"])
def webhook():
	"""Callback от Free‑Kassa: проверяем подпись, сумму (=200), соответствие user_id и отмечаем заказ."""
	try:
		data = request.form.to_dict()
		logging.info(f"Webhook Free‑Kassa: {data}")

		if not verify_payment_signature(data):
			logging.error("Неверная подпись платежа")
			return "ERROR", 400

		order_id = data.get("MERCHANT_ORDER_ID")
		amount = float(data.get("AMOUNT", "0"))
		user_id_from_cb = int(data.get("us_user_id", "0"))

		if amount < FIXED_SUBSCRIPTION_RUB:
			logging.error(f"Сумма меньше требуемой: {amount}")
			return "ERROR", 400

		if order_id not in orders_db:
			logging.error("Заказ не найден")
			return "ERROR", 404

		order = orders_db[order_id]

		# Привязываем к тому же пользователю, который создал заказ
		if user_id_from_cb != order["user_id"]:
			logging.error("user_id из callback не совпадает с автором заказа")
			return "ERROR", 400

		order["status"] = "paid"
		order["paid_at"] = datetime.now().isoformat()

		# Уведомление из потока Flask в цикл бота
		if bot_loop and bot_loop.is_running():
			asyncio.run_coroutine_threadsafe(
				notify_payment_success(order["user_id"], order_id),
				bot_loop
			)
		else:
			logging.error("Цикл бота не запущен — не могу отправить уведомления")

		return "YES"
	except Exception as e:
		logging.error(f"Ошибка в webhook: {e}")
		return "ERROR", 500

@app.route("/success.html")
def success_page():
	return f"""
	<!DOCTYPE html>
	<html>
	<head>
		<meta charset="UTF-8" />
		<meta http-equiv="refresh" content="3;url=https://t.me/{BOT_USERNAME}?start=success" />
		<title>Оплата успешна</title>
		<style>
			body {{ font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif; text-align:center; padding:48px; }}
			.success {{ color:#2ecc71; font-size:42px; margin-bottom:12px; }}
		</style>
	</head>
	<body>
		<div class="success">✅</div>
		<h1>Оплата прошла успешно!</h1>
		<p>Перенаправление в бота через 3 секунды...</p>
		<a href="https://t.me/{BOT_USERNAME}?start=success">Перейти в бота сейчас</a>
	</body>
	</html>
	"""

@app.route("/failed.html")
def failed_page():
	return f"""
	<!DOCTYPE html>
	<html>
	<head>
		<meta charset="UTF-8" />
		<meta http-equiv="refresh" content="3;url=https://t.me/{BOT_USERNAME}?start=failed" />
		<title>Ошибка оплаты</title>
		<style>
			body {{ font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif; text-align:center; padding:48px; }}
			.error {{ color:#e74c3c; font-size:42px; margin-bottom:12px; }}
		</style>
	</head>
	<body>
		<div class="error">❌</div>
		<h1>Ошибка оплаты</h1>
		<p>Попробуйте еще раз или обратитесь в поддержку</p>
		<p>Перенаправление в бота через 3 секунды...</p>
		<a href="https://t.me/{BOT_USERNAME}?start=failed">Перейти в бота сейчас</a>
	</body>
	</html>
	"""

@app.route("/")
def index():
	return jsonify({"status": "running", "message": "Telegram Stars Bot is running!", "version": "1.0"})

@app.route("/health")
def health():
	return jsonify({"status": "healthy"})

# ==========================
# Telegram бот
# ==========================

@dp.message_handler(commands=["start"])
async def start_handler(message: types.Message, state: FSMContext):
	args = message.get_args()

	if args == "success":
		await message.answer("✅ Оплата прошла успешно! Ожидайте обработку заказа администратором.")
		return
	elif args == "failed":
		await message.answer("❌ Оплата не прошла. Попробуйте еще раз или обратитесь в поддержку.")
		return

	# Проверка наличия username
	if not message.from_user.username:
		await message.answer(
			"❌ У вас отсутствует username в Telegram!\n\n"
			"Пожалуйста, установите его в настройках и попробуйте снова:\n\n"
			"📱 Как установить username:\n"
			"1) Откройте настройки Telegram\n"
			"2) Нажмите на своё имя\n"
			"3) Введите username в поле «Имя пользователя»\n"
			"4) Вернитесь в бота и нажмите /start",
		)
		return

	await state.finish()

	welcome_text = (
		"🌟 Добро пожаловать в нашего бота!\n\n"
		"Здесь вы можете приобрести звёзды по чистому курсу или подарить их своему другу!\n\n"
		f"💱 Текущий курс: 1 звезда = {STAR_TO_RUB_RATE} руб."
	)
	await message.answer(welcome_text)

	await OrderStates.waiting_for_recipient.set()
	await message.answer(
		"📝 Напиши юзернейм получателя и сколько звёзд ты хочешь ему отправить?\n"
		"В таком виде: <юзернейм> <целое кол-во звёзд>\n\n"
		"Пример: @durov 50"
	)

@dp.message_handler(state=OrderStates.waiting_for_recipient)
async def process_recipient(message: types.Message, state: FSMContext):
	try:
		parts = message.text.strip().split()
		if len(parts) != 2:
			await message.answer("❌ Неверный формат. Пример: @durov 50")
			return

		recipient_username = parts[0].replace("@", "")
		stars_count = int(parts[1])

		if stars_count <= 0:
			await message.answer("❌ Количество звёзд должно быть больше 0")
			return

		if stars_count > 10000:
			await message.answer("❌ Максимально за раз: 10 000 звёзд")
			return

		if not check_username_exists(recipient_username):
			await message.answer(
				"❌ Некорректный username.\n"
				"Должен начинаться с буквы, 5–32 символа, буквы/цифры/нижние подчёркивания."
			)
			return

		order_id = f"order_{message.from_user.id}_{int(datetime.now().timestamp())}"

		order_data = {
			"user_id": message.from_user.id,
			"username": message.from_user.username,
			"recipient": recipient_username,
			"stars_count": stars_count,
			"order_id": order_id,
			"status": "pending",
			"created_at": datetime.now().isoformat()
		}

		orders_db[order_id] = order_data
		pending_payments[message.from_user.id] = order_data

		# Генерируем ссылку на оплату 200 ₽ (СБП доступен в кассе Free‑Kassa)
		payment_link = generate_payment_link(FIXED_SUBSCRIPTION_RUB, order_id, message.from_user.id, message.from_user.username or "")

		keyboard = types.InlineKeyboardMarkup()
		keyboard.add(types.InlineKeyboardButton("💳 Оплатить (СБП)", url=payment_link))
		keyboard.add(types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}"))

		await message.answer(
			"Отлично! Необходимо оплатить подписку в размере 200 рублей.",
			reply_markup=keyboard
		)
		await OrderStates.waiting_for_payment.set()

	except ValueError:
		await message.answer("❌ Количество звёзд должно быть целым числом")
	except Exception as e:
		logging.error(f"Ошибка при обработке заказа: {e}")
		await message.answer("❌ Произошла ошибка. Попробуйте ещё раз.")

@dp.callback_query_handler(lambda c: c.data.startswith("cancel_"))
async def cancel_order(callback_query: types.CallbackQuery, state: FSMContext):
	order_id = callback_query.data.replace("cancel_", "")

	if order_id in orders_db:
		del orders_db[order_id]

	if callback_query.from_user.id in pending_payments:
		del pending_payments[callback_query.from_user.id]

	await state.finish()
	await callback_query.message.edit_text("❌ Заказ отменён. Нажмите /start для нового заказа")
	await callback_query.answer("Заказ отменён")

@dp.callback_query_handler(lambda c: c.data.startswith("process_"))
async def process_order(callback_query: types.CallbackQuery):
	"""Админ подтверждает, что купил Stars на Fragment."""
	if callback_query.from_user.id != ADMIN_ID:
		await callback_query.answer("❌ Доступ запрещён", show_alert=True)
		return

	order_id = callback_query.data.replace("process_", "")
	if order_id not in orders_db:
		await callback_query.answer("❌ Заказ не найден", show_alert=True)
		return

	order = orders_db[order_id]
	order["status"] = "completed"
	order["completed_at"] = datetime.now().isoformat()

	# Уведомляем пользователя
	try:
		text = (
			f"🎉 Спасибо за покупку!\n\n"
			f"⭐ {order['stars_count']:,} звёзд отправлены пользователю @{order['recipient']}.\n\n"
			f"Будем ждать вас снова!"
		)
		await bot.send_message(order["user_id"], text)
	except Exception as e:
		logging.error(f"Ошибка уведомления пользователя: {e}")

	# Обновляем сообщение админа
	completion_text = (
		f"✅ Заказ {order_id} обработан.\n"
		f"Заказчик уведомлён. Время: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
	)

	await callback_query.message.edit_text(completion_text)
	await callback_query.answer("✅ Заказ обработан")

@dp.callback_query_handler(lambda c: c.data.startswith("cancel_admin_"))
async def cancel_order_admin(callback_query: types.CallbackQuery):
	if callback_query.from_user.id != ADMIN_ID:
		await callback_query.answer("❌ Доступ запрещён", show_alert=True)
		return

	order_id = callback_query.data.replace("cancel_admin_", "")
	if order_id not in orders_db:
		await callback_query.answer("❌ Заказ не найден", show_alert=True)
		return

	order = orders_db[order_id]
	try:
		await bot.send_message(
			order["user_id"],
			"😔 Извините, заказ был отменён по техническим причинам.\n\n"
			"💰 Средства будут возвращены в течение 1–3 рабочих дней."
		)
	except Exception:
		pass

	del orders_db[order_id]

	await callback_query.message.edit_text(f"❌ Заказ {order_id} отменён админом")
	await callback_query.answer("Заказ отменён")

@dp.message_handler(commands=["orders"])
async def show_orders(message: types.Message):
	if message.from_user.id != ADMIN_ID:
		return

	if not orders_db:
		await message.answer("📭 Активных заказов нет")
		return

	text = "📋 Активные заказы:\n\n"
	for oid, o in orders_db.items():
		status_emoji = {"pending": "⏳", "paid": "💰", "completed": "✅"}.get(o["status"], "❓")
		text += (
			f"{status_emoji} {oid}\n"
			f"@{o['username']} → @{o['recipient']}\n"
			f"⭐ {o['stars_count']:,} | статус: {o['status']}\n"
			f"📅 {o.get('created_at', 'N/A')[:16]}\n\n"
		)
	await message.answer(text)

@dp.message_handler(commands=["stats"])
async def show_stats(message: types.Message):
	if message.from_user.id != ADMIN_ID:
		return

	total_orders = len(orders_db)
	paid_orders = len([o for o in orders_db.values() if o["status"] in ["paid", "completed"]])
	completed_orders = len([o for o in orders_db.values() if o["status"] == "completed"])

	stats_text = (
		f"📊 Статистика:\n\n"
		f"Всего заказов: {total_orders}\n"
		f"Оплаченных: {paid_orders}\n"
		f"Выполненных: {completed_orders}\n"
		f"Курс: {STAR_TO_RUB_RATE} руб./звезда"
	)
	await message.answer(stats_text)

@dp.message_handler()
async def handle_other_messages(message: types.Message):
	await message.answer(
		"👋 Привет! Нажмите /start для начала.\n"
		"🌟 Здесь вы можете купить Telegram Stars по лучшему курсу!"
	)

# ==========================
# Запуск
# ==========================

def start_bot():
	global bot_loop
	bot_loop = asyncio.new_event_loop()
	asyncio.set_event_loop(bot_loop)
	executor.start_polling(dp, skip_updates=True)

def start_web():
	app.run(host="0.0.0.0", port=PORT, debug=False)

if __name__ == "__main__":
	# Бот — в отдельном потоке
	bot_thread = threading.Thread(target=start_bot, daemon=True)
	bot_thread.start()

	logging.info("🚀 Telegram Stars Bot запущен!")

	# Flask — в главном потоке (требуется для Railway)
	start_web()

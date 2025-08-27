import asyncio
import hashlib
import hmac
import logging
import json
import time
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
from flask import Flask, request, jsonify
import threading
import requests
import re
import uuid

# =============================================================================
# НАСТРОЙКИ - ЗАМЕНИТЕ НА СВОИ ЗНАЧЕНИЯ
# =============================================================================

# Telegram Bot
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
ADMIN_ID = 8127196287  # Замените на ваш Telegram ID

# Lava настройки (обычный кошелек)
LAVA_WALLET_ID = "R10230965"    # ID вашего кошелька (например: R123456789)
LAVA_SECRET_KEY = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1aWQiOiIwYTk1MjRjZS03N2U2LWYyZGEtYmUyZi04MGE3MmFiYzlkNjgiLCJ0aWQiOiJmOGZmNGYxMC1iMmI4LWE0OGUtYWM5Yi03N2VjYTZiOGM4Y2EifQ.mIHVVhObUQVWcsCYNdTBRsc4slHON0-DLpj6kVFDi6Y"  # Секретный ключ из настроек аккаунта

# Курс звезды к рублю
STAR_TO_RUB_RATE = 1.2

# Домен для webhook (Railway предоставит после деплоя)
DOMAIN = "http://tg-stars-bot-production-c736.up.railway.app/"

# Порт для Flask
PORT = 8080

# =============================================================================

# Проверяем обязательные настройки
if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    logging.error("Не задан BOT_TOKEN!")
    exit(1)

# Настройка логирования
logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Flask приложение
app = Flask(__name__)

# Состояния FSM
class OrderStates(StatesGroup):
    waiting_for_recipient = State()
    waiting_for_payment = State()

# База данных заказов
orders_db = {}

def generate_lava_signature(data_string, secret_key):
    """Генерирует подпись для Lava API (обычный кошелек)"""
    sign_string = data_string + secret_key
    return hashlib.sha256(sign_string.encode('utf-8')).hexdigest()

def verify_lava_signature(data, received_signature, secret_key):
    """Проверяет подпись от Lava для обычного кошелька"""
    # Формируем строку для проверки подписи
    sign_data = []
    for key in sorted(data.keys()):
        if key != 'signature':
            sign_data.append(f"{data[key]}")
    
    sign_string = ':'.join(sign_data)
    expected_signature = generate_lava_signature(sign_string, secret_key)
    
    return hmac.compare_digest(received_signature, expected_signature)

def check_username_exists(username):
    """Проверяет корректность формата username"""
    try:
        clean_username = username.replace('@', '')
        if re.match(r'^[a-zA-Z][a-zA-Z0-9_]{4,31}$', clean_username):
            return True
        return False
    except:
        return False

def calculate_cost(stars_count):
    """Рассчитывает стоимость звезд в рублях"""
    return round(stars_count * STAR_TO_RUB_RATE, 2)

def create_lava_invoice(amount, order_id, user_id):
    """Создает ссылку для оплаты через Lava (обычный кошелек)"""
    try:
        # Для обычного кошелька используем прямую ссылку
        # Параметры для ссылки
        params = {
            'amount': amount,
            'order_id': order_id,
            'wallet_to': LAVA_WALLET_ID,
            'success_url': f"{DOMAIN}/success",
            'fail_url': f"{DOMAIN}/failed",
            'comment': f"Покупка {orders_db[order_id]['stars_count']} звезд для @{orders_db[order_id]['recipient']}"
        }
        
        # Создаем строку для подписи
        sign_string = f"{amount}:{order_id}:{LAVA_WALLET_ID}"
        signature = generate_lava_signature(sign_string, LAVA_SECRET_KEY)
        
        # Формируем URL для оплаты
        payment_url = (
            f"https://lava.ru/pay/{LAVA_WALLET_ID}"
            f"?amount={amount}"
            f"&order_id={order_id}"
            f"&comment={params['comment'][:100]}"
            f"&success_url={params['success_url']}"
            f"&fail_url={params['fail_url']}"
            f"&signature={signature}"
        )
        
        logging.info(f"Создана ссылка для оплаты: {payment_url}")
        return payment_url
            
    except Exception as e:
        logging.error(f"Ошибка при создании ссылки Lava: {e}")
        return None

# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route('/webhook/lava', methods=['POST'])
def lava_webhook():
    """Обработка уведомлений от Lava (обычный кошелек)"""
    try:
        # Получаем данные из POST запроса
        if request.content_type == 'application/json':
            data = request.get_json()
        else:
            data = request.form.to_dict()
        
        logging.info(f"Получено уведомление от Lava: {data}")
        
        # Получаем подпись
        signature = data.get('signature')
        if not signature:
            logging.error("Отсутствует подпись в webhook от Lava")
            return jsonify({"error": "Missing signature"}), 400
        
        # Проверяем подпись
        if not verify_lava_signature(data, signature, LAVA_SECRET_KEY):
            logging.error("Неверная подпись от Lava")
            return jsonify({"error": "Invalid signature"}), 400
        
        # Извлекаем данные платежа
        order_id = data.get('order_id')
        status = data.get('status')
        amount = float(data.get('amount', 0))
        
        logging.info(f"Webhook данные: order_id={order_id}, status={status}, amount={amount}")
        
        # Обрабатываем успешный платеж
        if order_id and order_id in orders_db and status in ['success', 'paid', 'complete']:
            order = orders_db[order_id]
            
            # Проверяем сумму
            if amount >= (order['cost'] * 0.95):  # 95% от суммы заказа
                order['status'] = 'paid'
                order['paid_at'] = datetime.now().isoformat()
                order['paid_amount'] = amount
                
                logging.info(f"Заказ {order_id} помечен как оплаченный")
                
                # Уведомляем о платеже
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(notify_payment_success(order['user_id'], order_id))
                    loop.close()
                except Exception as e:
                    logging.error(f"Ошибка при уведомлении: {e}")
            else:
                logging.warning(f"Неподходящая сумма: получено {amount}, ожидалось {order['cost']}")
        elif not order_id:
            logging.error("Отсутствует order_id в webhook")
        elif order_id not in orders_db:
            logging.warning(f"Заказ {order_id} не найден в базе")
        else:
            logging.info(f"Статус платежа: {status} (не обрабатывается)")
        
        return jsonify({"status": "ok"})
    
    except Exception as e:
        logging.error(f"Ошибка в Lava webhook: {e}")
        return jsonify({"error": "Internal error"}), 500

@app.route('/webhook/lava', methods=['GET'])
def lava_webhook_get():
    """Обработка GET запросов от Lava (для проверки)"""
    return jsonify({"status": "webhook_active", "message": "Lava webhook endpoint"})

@app.route('/success')
def success_page():
    """Страница успешной оплаты"""
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Оплата успешна</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
                text-align: center; 
                padding: 20px; 
                background: linear-gradient(135deg, #4CAF50 0%, #45a049 100%);
                color: white;
                margin: 0;
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                justify-content: center;
            }
            .container { 
                background: rgba(255,255,255,0.1); 
                padding: 40px 20px; 
                border-radius: 20px;
                backdrop-filter: blur(10px);
                box-shadow: 0 8px 32px rgba(0,0,0,0.3);
                max-width: 500px;
                margin: 0 auto;
            }
            .success { font-size: 64px; margin-bottom: 20px; }
            h1 { margin: 20px 0; font-size: 28px; }
            p { font-size: 18px; margin: 10px 0; }
            a { 
                color: white; 
                text-decoration: none; 
                font-weight: bold;
                background: rgba(255,255,255,0.2);
                padding: 12px 24px;
                border-radius: 8px;
                display: inline-block;
                margin-top: 20px;
                transition: background 0.3s;
            }
            a:hover {
                background: rgba(255,255,255,0.3);
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="success">✅</div>
            <h1>Оплата прошла успешно!</h1>
            <p>Ваш заказ принят в обработку</p>
            <p>Ожидайте уведомления в боте</p>
            <a href="https://t.me/YOUR_BOT_USERNAME">Перейти в бота</a>
        </div>
    </body>
    </html>
    '''

@app.route('/failed')
def failed_page():
    """Страница неуспешной оплаты"""
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Ошибка оплаты</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
                text-align: center; 
                padding: 20px; 
                background: linear-gradient(135deg, #f44336 0%, #d32f2f 100%);
                color: white;
                margin: 0;
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                justify-content: center;
            }
            .container { 
                background: rgba(255,255,255,0.1); 
                padding: 40px 20px; 
                border-radius: 20px;
                backdrop-filter: blur(10px);
                box-shadow: 0 8px 32px rgba(0,0,0,0.3);
                max-width: 500px;
                margin: 0 auto;
            }
            .error { font-size: 64px; margin-bottom: 20px; }
            h1 { margin: 20px 0; font-size: 28px; }
            p { font-size: 18px; margin: 10px 0; }
            a { 
                color: white; 
                text-decoration: none; 
                font-weight: bold;
                background: rgba(255,255,255,0.2);
                padding: 12px 24px;
                border-radius: 8px;
                display: inline-block;
                margin-top: 20px;
                transition: background 0.3s;
            }
            a:hover {
                background: rgba(255,255,255,0.3);
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="error">❌</div>
            <h1>Ошибка оплаты</h1>
            <p>Платеж не был завершен</p>
            <p>Попробуйте еще раз</p>
            <a href="https://t.me/YOUR_BOT_USERNAME">Перейти в бота</a>
        </div>
    </body>
    </html>
    '''

@app.route('/')
def index():
    """Главная страница"""
    return jsonify({
        "status": "running",
        "message": "Telegram Stars Bot с Lava кошельком",
        "version": "2.0",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/health')
def health():
    """Проверка состояния"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "orders_count": len(orders_db),
        "active_orders": len([o for o in orders_db.values() if o['status'] == 'pending']),
        "paid_orders": len([o for o in orders_db.values() if o['status'] == 'paid']),
        "completed_orders": len([o for o in orders_db.values() if o['status'] == 'completed'])
    })

# =============================================================================
# TELEGRAM BOT HANDLERS
# =============================================================================

async def notify_payment_success(user_id, order_id):
    """Уведомляет о успешной оплате"""
    try:
        order = orders_db.get(order_id)
        if not order:
            return
        
        # Уведомляем пользователя
        await bot.send_message(
            user_id,
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            "🔄 Ваш заказ передан в обработку\n"
            "⏰ Ожидайте 5-10 минут\n"
            "📱 Получите уведомление о завершении",
            parse_mode='HTML'
        )
        
        # Создаем кнопки для админа
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton(
                "✅ Отправил звёзды", 
                callback_data=f"process_{order_id}"
            ),
            types.InlineKeyboardButton(
                "❌ Отменить заказ", 
                callback_data=f"cancel_admin_{order_id}"
            )
        )
        
        # Создаем ссылку для Fragment
        fragment_url = f"https://fragment.com/stars/buy?recipient={order['recipient']}&quantity={order['stars_count']}"
        
        # Уведомляем админа
        admin_message = (
            f"🔔 <b>НОВЫЙ ОПЛАЧЕННЫЙ ЗАКАЗ!</b>\n\n"
            f"📋 <b>ID:</b> <code>{order_id}</code>\n"
            f"👤 <b>От:</b> @{order['username']} ({order['user_id']})\n"
            f"🎯 <b>Кому:</b> @{order['recipient']}\n"
            f"⭐ <b>Звёзд:</b> {order['stars_count']:,}\n"
            f"💰 <b>Оплачено:</b> {order['cost']} руб.\n"
            f"📅 <b>Время:</b> {order.get('paid_at', 'N/A')[:16].replace('T', ' ')}\n\n"
            f"🚀 <b>ИНСТРУКЦИЯ:</b>\n"
            f"1️⃣ Откройте: <a href='{fragment_url}'>Fragment.com</a>\n"
            f"2️⃣ Подтвердите покупку в кошельке\n"
            f"3️⃣ Нажмите '✅ Отправил звёзды'\n\n"
            f"📱 <b>Прямая ссылка:</b>\n<code>{fragment_url}</code>"
        )
        
        await bot.send_message(
            ADMIN_ID,
            admin_message,
            reply_markup=keyboard,
            parse_mode='HTML',
            disable_web_page_preview=False
        )
        
    except Exception as e:
        logging.error(f"Ошибка при уведомлении об оплате: {e}")

@dp.message_handler(commands=['start'])
async def start_handler(message: types.Message, state: FSMContext):
    """Обработчик команды /start"""
    # Проверяем username
    if not message.from_user.username:
        await message.answer(
            "❌ <b>У вас отсутствует username в Telegram!</b>\n\n"
            "📝 <b>Как его установить:</b>\n"
            "1️⃣ Настройки → Имя пользователя\n"
            "2️⃣ Введите желаемый username\n"
            "3️⃣ Вернитесь и нажмите /start\n\n"
            "⚠️ Username обязателен для работы с ботом!",
            parse_mode='HTML'
        )
        return
    
    await state.finish()
    
    welcome_text = (
        "🌟 <b>Добро пожаловать в StarsSeller!</b>\n\n"
        "💫 Покупайте и дарите звёзды Telegram по лучшему курсу!\n\n"
        f"💱 <b>Курс:</b> 1 ⭐ = {STAR_TO_RUB_RATE} руб.\n"
        f"⚡ <b>Комиссия:</b> 0%\n"
        f"🚀 <b>Скорость:</b> 5-10 минут\n"
        f"💳 <b>Оплата:</b> Карты, СБП, QIWI, ЮMoney\n\n"
        f"✨ <b>Начнем?</b>"
    )
    
    await message.answer(welcome_text, parse_mode='HTML')
    
    await OrderStates.waiting_for_recipient.set()
    await message.answer(
        "📝 <b>Введите данные для заказа:</b>\n\n"
        "📋 <b>Формат:</b> <code>@username количество</code>\n"
        "📌 <b>Пример:</b> <code>@durov 100</code>\n\n"
        "💡 <b>Где:</b>\n"
        "• <code>@username</code> - получатель звёзд\n"
        "• <code>количество</code> - целое число от 1 до 10,000\n\n"
        "⚠️ Username должен существовать в Telegram!",
        parse_mode='HTML'
    )

@dp.message_handler(state=OrderStates.waiting_for_recipient)
async def process_recipient(message: types.Message, state: FSMContext):
    """Обработка получателя и количества звёзд"""
    try:
        parts = message.text.strip().split()
        if len(parts) != 2:
            await message.answer(
                "❌ <b>Неверный формат!</b>\n\n"
                "📋 <b>Правильно:</b> <code>@username количество</code>\n"
                "📌 <b>Пример:</b> <code>@durov 100</code>",
                parse_mode='HTML'
            )
            return
        
        recipient_username = parts[0].replace('@', '')
        stars_count = int(parts[1])
        
        # Проверки
        if stars_count <= 0:
            await message.answer("❌ Количество звёзд должно быть больше 0!")
            return
            
        if stars_count > 10000:
            await message.answer("❌ Максимальное количество звёзд за раз: 10,000")
            return
        
        if stars_count < 1:
            await message.answer("❌ Минимальное количество звёзд: 1")
            return
            
        if not check_username_exists(recipient_username):
            await message.answer(
                "❌ <b>Некорректный username!</b>\n\n"
                "📝 <b>Требования к username:</b>\n"
                "• Начинается с буквы\n"
                "• 5-32 символа\n"
                "• Только буквы, цифры, подчеркивания\n\n"
                "💡 <b>Примеры:</b> <code>@durov</code>, <code>@telegram</code>",
                parse_mode='HTML'
            )
            return
        
        # Рассчитываем стоимость
        cost = calculate_cost(stars_count)
        
        # Создаем заказ
        order_id = f"order_{message.from_user.id}_{int(time.time())}"
        
        order_data = {
            'user_id': message.from_user.id,
            'username': message.from_user.username,
            'recipient': recipient_username,
            'stars_count': stars_count,
            'cost': cost,
            'order_id': order_id,
            'status': 'pending',
            'created_at': datetime.now().isoformat()
        }
        
        orders_db[order_id] = order_data
        
        # Создаем счет в Lava
        payment_url = create_lava_invoice(cost, order_id, message.from_user.id)
        
        if not payment_url:
            await message.answer(
                "❌ <b>Ошибка создания счета</b>\n\n"
                "Попробуйте позже или обратитесь к администратору"
            )
            return
        
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            types.InlineKeyboardButton("💳 Оплатить", url=payment_url),
            types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}")
        )
        
        order_text = (
            f"✅ <b>Заказ создан!</b>\n\n"
            f"📋 <b>Детали заказа:</b>\n"
            f"🆔 <code>{order_id}</code>\n"
            f"👤 <b>От:</b> @{message.from_user.username}\n"
            f"🎯 <b>Кому:</b> @{recipient_username}\n"
            f"⭐ <b>Звёзд:</b> {stars_count:,}\n"
            f"💰 <b>К оплате:</b> {cost} рублей\n\n"
            f"💳 <b>Способы оплаты:</b>\n"
            f"• 💳 Банковские карты\n"
            f"• 🏦 СБП (быстрые платежи)\n"
            f"• 💼 QIWI кошелек\n"
            f"• 💛 ЮMoney\n\n"
            f"⚡ <b>Нажмите 'Оплатить' для перехода к оплате</b>"
        )
        
        await message.answer(order_text, reply_markup=keyboard, parse_mode='HTML')
        await OrderStates.waiting_for_payment.set()
        
    except ValueError:
        await message.answer("❌ Количество звёзд должно быть целым числом!")
    except Exception as e:
        await message.answer("❌ Произошла ошибка. Попробуйте еще раз.")
        logging.error(f"Ошибка при обработке заказа: {e}")

@dp.callback_query_handler(lambda c: c.data.startswith('cancel_'), state='*')
async def cancel_order(callback_query: types.CallbackQuery, state: FSMContext):
    """Отмена заказа пользователем"""
    order_id = callback_query.data.replace('cancel_', '')
    
    if order_id in orders_db:
        del orders_db[order_id]
    
    await state.finish()
    await callback_query.message.edit_text(
        "❌ <b>Заказ отменен</b>\n\n"
        "💡 Для создания нового заказа нажмите /start",
        parse_mode='HTML'
    )
    await callback_query.answer("Заказ отменен")

@dp.callback_query_handler(lambda c: c.data.startswith('process_'))
async def process_order(callback_query: types.CallbackQuery):
    """Обработка заказа админом"""
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("❌ Доступ запрещен", show_alert=True)
        return
    
    order_id = callback_query.data.replace('process_', '')
    
    if order_id not in orders_db:
        await callback_query.answer("❌ Заказ не найден", show_alert=True)
        return
    
    order = orders_db[order_id]
    
    if order['status'] != 'paid':
        await callback_query.answer("❌ Заказ не оплачен", show_alert=True)
        return
    
    order['status'] = 'completed'
    order['completed_at'] = datetime.now().isoformat()
    
    # Уведомляем пользователя
    try:
        success_text = (
            f"🎉 <b>Звёзды доставлены!</b>\n\n"
            f"✅ <b>{order['stars_count']:,} звёзд</b> успешно отправлены "
            f"пользователю <b>@{order['recipient']}</b>\n\n"
            f"⚡ Звёзды уже доступны получателю\n"
            f"🕒 Обработано: {datetime.now().strftime('%d.%m.%Y в %H:%M')}\n\n"
            f"🌟 <b>Спасибо за покупку!</b>\n"
            f"💫 Будем рады видеть вас снова"
        )
        
        await bot.send_message(order['user_id'], success_text, parse_mode='HTML')
        
    except Exception as e:
        logging.error(f"Ошибка уведомления пользователя: {e}")
    
    # Обновляем сообщение админа
    completion_text = (
        f"✅ <b>ЗАКАЗ ОБРАБОТАН</b>\n\n"
        f"📋 <b>ID:</b> <code>{order_id}</code>\n"
        f"👤 <b>Заказчик:</b> @{order['username']}\n"
        f"🎯 <b>Получатель:</b> @{order['recipient']}\n"
        f"⭐ <b>Звёзд:</b> {order['stars_count']:,}\n"
        f"💰 <b>Сумма:</b> {order['cost']} руб.\n"
        f"✅ <b>Статус:</b> Завершен\n"
        f"⏰ <b>Обработано:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    
    await callback_query.message.edit_text(completion_text, parse_mode='HTML')
    await callback_query.answer("✅ Заказ обработан, клиент уведомлен!")

@dp.callback_query_handler(lambda c: c.data.startswith('cancel_admin_'))
async def cancel_order_admin(callback_query: types.CallbackQuery):
    """Отмена заказа админом"""
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("❌ Доступ запрещен", show_alert=True)
        return
    
    order_id = callback_query.data.replace('cancel_admin_', '')
    
    if order_id not in orders_db:
        await callback_query.answer("❌ Заказ не найден", show_alert=True)
        return
    
    order = orders_db[order_id]
    
    try:
        await bot.send_message(
            order['user_id'],
            "😔 <b>Заказ отменен</b>\n\n"
            "К сожалению, заказ был отменен по техническим причинам.\n\n"
            "💰 <b>Возврат средств:</b>\n"
            "Средства будут возвращены автоматически в течение 1-3 рабочих дней\n\n"
            "📞 При вопросах обращайтесь к администратору",
            parse_mode='HTML'
        )
    except Exception as e:
        logging.error(f"Ошибка уведомления о отмене: {e}")
    
    del orders_db[order_id]
    
    await callback_query.message.edit_text(
        f"❌ <b>Заказ отменен</b>\n\n"
        f"📋 ID: <code>{order_id}</code>\n"
        f"👤 Клиент: @{order['username']}\n"
        f"💰 Сумма: {order['cost']} руб.\n"
        f"⏰ Отменен: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        parse_mode='HTML'
    )
    await callback_query.answer("Заказ отменен, клиент уведомлен")

@dp.message_handler(commands=['orders'])
async def show_orders(message: types.Message):
    """Показать заказы (только админ)"""
    if message.from_user.id != ADMIN_ID:
        return
    
    if not orders_db:
        await message.answer("📭 <b>Активных заказов нет</b>", parse_mode='HTML')
        return
    
    # Группируем заказы по статусам
    pending_orders = [o for o in orders_db.values() if o['status'] == 'pending']
    paid_orders = [o for o in orders_db.values() if o['status'] == 'paid']
    completed_orders = [o for o in orders_db.values() if o['status'] == 'completed']
    
    orders_text = f"📋 <b>АКТИВНЫЕ ЗАКАЗЫ ({len(orders_db)})</b>\n\n"
    
    if paid_orders:
        orders_text += "💰 <b>ТРЕБУЮТ ОБРАБОТКИ:</b>\n"
        for order in paid_orders[-5:]:  # Показываем последние 5
            orders_text += (
                f"🔥 <code>{order['order_id']}</code>\n"
                f"👤 @{order['username']} → @{order['recipient']}\n"
                f"⭐ {order['stars_count']:,} | 💰 {order['cost']} руб.\n\n"
            )
    
    if pending_orders:
        orders_text += "⏳ <b>ОЖИДАЮТ ОПЛАТЫ:</b>\n"
        for order in pending_orders[-3:]:  # Показываем последние 3
            orders_text += (
                f"⏰ <code>{order['order_id']}</code>\n"
                f"👤 @{order['username']} → @{order['recipient']}\n"
                f"⭐ {order['stars_count']:,} | 💰 {order['cost']} руб.\n\n"
            )
    
    if completed_orders:
        orders_text += f"✅ <b>ЗАВЕРШЕНО:</b> {len(completed_orders)}\n\n"
    
    await message.answer(orders_text[:4000], parse_mode='HTML')

@dp.message_handler(commands=['stats'])
async def show_stats(message: types.Message):
    """Статистика (только админ)"""
    if message.from_user.id != ADMIN_ID:
        return
    
    total_orders = len(orders_db)
    pending_orders = len([o for o in orders_db.values() if o['status'] == 'pending'])
    paid_orders = len([o for o in orders_db.values() if o['status'] == 'paid'])
    completed_orders = len([o for o in orders_db.values() if o['status'] == 'completed'])
    
    total_revenue = sum([o['cost'] for o in orders_db.values() if o['status'] in ['paid', 'completed']])
    total_stars = sum([o['stars_count'] for o in orders_db.values() if o['status'] == 'completed'])
    
    # Статистика за сегодня
    today = datetime.now().date()
    today_orders = [o for o in orders_db.values() if o.get('created_at', '')[:10] == str(today)]
    today_revenue = sum([o['cost'] for o in today_orders if o['status'] in ['paid', 'completed']])
    
    stats_text = (
        f"📊 <b>СТАТИСТИКА БОТА</b>\n\n"
        f"📈 <b>ОБЩАЯ:</b>\n"
        f"📋 Всего заказов: {total_orders}\n"
        f"⏳ Ожидают оплаты: {pending_orders}\n"
        f"💰 Требуют обработки: {paid_orders}\n"
        f"✅ Завершено: {completed_orders}\n\n"
        f"💵 <b>ФИНАНСЫ:</b>\n"
        f"💰 Общая выручка: {total_revenue:,.0f} руб.\n"
        f"📅 За сегодня: {today_revenue:,.0f} руб.\n"
        f"⭐ Звёзд отправлено: {total_stars:,}\n\n"
        f"⚡ <b>СРЕДНИЕ ПОКАЗАТЕЛИ:</b>\n"
        f"💱 Средний чек: {(total_revenue/max(1, completed_orders)):,.0f} руб.\n"
        f"⭐ Звёзд в заказе: {(total_stars/max(1, completed_orders)):,.0f}\n\n"
        f"🕐 Обновлено: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    
    await message.answer(stats_text, parse_mode='HTML')

@dp.message_handler(commands=['help'])
async def help_handler(message: types.Message):
    """Справка"""
    if message.from_user.id == ADMIN_ID:
        help_text = (
            "🔧 <b>КОМАНДЫ АДМИНИСТРАТОРА:</b>\n\n"
            "📋 <code>/orders</code> - Активные заказы\n"
            "📊 <code>/stats</code> - Статистика бота\n"
            "❓ <code>/help</code> - Эта справка\n\n"
            "🔄 <b>ПРОЦЕСС ОБРАБОТКИ:</b>\n"
            "1️⃣ Клиент оплачивает заказ\n"
            "2️⃣ Вы получаете уведомление\n"
            "3️⃣ Переходите по ссылке на Fragment\n"
            "4️⃣ Подтверждаете покупку\n"
            "5️⃣ Нажимаете '✅ Отправил звёзды'\n"
            "6️⃣ Клиент получает уведомление\n\n"
            "⚠️ <b>ВАЖНО:</b> Обрабатывайте заказы быстро!"
        )
    else:
        help_text = (
            "❓ <b>СПРАВКА ПО БОТУ</b>\n\n"
            "🌟 <b>Как купить звёзды:</b>\n"
            "1️⃣ Нажмите /start\n"
            "2️⃣ Введите: <code>@username количество</code>\n"
            "3️⃣ Оплатите удобным способом\n"
            "4️⃣ Дождитесь доставки (5-10 мин)\n\n"
            f"💰 <b>Курс:</b> 1 ⭐ = {STAR_TO_RUB_RATE} руб.\n"
            f"💳 <b>Оплата:</b> Карты, СБП, QIWI, ЮMoney\n"
            f"⚡ <b>Комиссия:</b> 0%\n\n"
            f"📝 <b>Пример заказа:</b>\n"
            f"<code>@durov 100</code> = {calculate_cost(100)} руб.\n\n"
            f"❓ <b>Вопросы?</b> Обратитесь к администратору"
        )
    
    await message.answer(help_text, parse_mode='HTML')

@dp.message_handler(commands=['clear'])
async def clear_completed_orders(message: types.Message):
    """Очистка завершенных заказов (только админ)"""
    if message.from_user.id != ADMIN_ID:
        return
    
    completed_count = len([o for o in orders_db.values() if o['status'] == 'completed'])
    
    if completed_count == 0:
        await message.answer("✅ Завершенных заказов для очистки нет")
        return
    
    # Удаляем завершенные заказы старше 24 часов
    now = datetime.now()
    cleared_count = 0
    
    for order_id, order in list(orders_db.items()):
        if order['status'] == 'completed' and order.get('completed_at'):
            try:
                completed_time = datetime.fromisoformat(order['completed_at'])
                if (now - completed_time).total_seconds() > 86400:  # 24 часа
                    del orders_db[order_id]
                    cleared_count += 1
            except:
                continue
    
    await message.answer(
        f"🧹 <b>Очистка завершена</b>\n\n"
        f"Удалено {cleared_count} завершенных заказов старше 24 часов"
    )

@dp.message_handler(content_types=['text'])
async def handle_text(message: types.Message, state: FSMContext):
    """Обработка произвольных сообщений"""
    current_state = await state.get_state()
    
    if current_state is None:
        await message.answer(
            "👋 Привет! Для начала работы нажмите /start\n"
            "❓ Помощь: /help"
        )

# =============================================================================
# ЗАПУСК ПРИЛОЖЕНИЯ
# =============================================================================

def run_flask():
    """Запуск Flask в отдельном потоке"""
    app.run(host='0.0.0.0', port=PORT, debug=False)

def run_bot():
    """Запуск бота"""
    executor.start_polling(dp, skip_updates=True)

if __name__ == '__main__':
    logging.info("Запуск Telegram Stars Bot...")
    
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    logging.info(f"Flask запущен на порту {PORT}")
    logging.info(f"Webhook URL: {DOMAIN}")
    
    # Запускаем бота
    try:
        run_bot()
    except KeyboardInterrupt:
        logging.info("Бот остановлен")
    except Exception as e:
        logging.error(f"Ошибка запуска бота: {e}")

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Blueprint
from pymongo import MongoClient
from bson import ObjectId
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
import time
import logging
from datetime import datetime
import uuid
import qrcode
import base64
from io import BytesIO
import requests
from flask import session
import math


cryptowalletbank_bp = Blueprint('cryptowalletbank', __name__)


# Инициализация Flask и MongoDB
app = Flask(__name__)
app.secret_key = 'your_secret_key'



client = MongoClient("mongodb://localhost:27017/")
db = client['neirospace']
users_collection = db['users']
sessions_collection = db['sessions']
messages_collection = db['messages']
wallet_collection = db['wallets']
transactions_collection = db['transactions']
crypto_collection = db['cryptocurrencies']
buyback_collection = db['buybacks']
investment_rounds_collection = db['investment_rounds']

wallet_bp = Blueprint('wallet', __name__, url_prefix='/wallet')



# Начальные параметры инвестиционного раунда
INITIAL_SUPPLY = 100
INITIAL_PRICE = 1.0
MULTIPLIER = 3
PRICE_INCREASE = 1.25


def create_new_investment_round():
    """Создает новый инвестиционный раунд."""
    last_round = investment_rounds_collection.find_one(sort=[("round_number", -1)])
    
    if last_round:
        new_round_number = last_round['round_number'] + 1
        new_supply = last_round['supply'] * MULTIPLIER
        new_price = round(last_round['price'] * PRICE_INCREASE, 2)
    else:
        new_round_number = 1
        new_supply = INITIAL_SUPPLY
        new_price = INITIAL_PRICE
    
    new_round = {
        "round_number": new_round_number,
        "supply": new_supply,
        "price": new_price,
        "sold_tokens": 0,
        "created_at": datetime.utcnow()
    }
    investment_rounds_collection.insert_one(new_round)
    return new_round


def get_current_investment_round():
    """Возвращает текущий инвестиционный раунд или создает новый, если текущий завершен."""
    current_round = investment_rounds_collection.find_one(sort=[("round_number", -1)])
    if not current_round or current_round['sold_tokens'] >= current_round['supply']:
        return create_new_investment_round()
    return current_round


@app.route('/')
def index():
    return render_template('cryptobank.html')


BITCOIN_WALLET = "bc1qduwye5myj34yc6xs7nazjzxegs6lgy2tc07jfg"

def generate_qr_code(data):
    """Генерирует QR-код"""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)

        img = qr.make_image(fill="black", back_color="white")
        img_io = BytesIO()
        img.save(img_io, format="PNG")
        img_io.seek(0)

        return base64.b64encode(img_io.getvalue()).decode()
    except Exception as e:
        print(f"Ошибка генерации QR-кода: {e}")
        return None


@cryptowalletbank_bp.route('/buy_tokens', methods=['POST'])
def buy_tokens():
    """Создание транзакции и генерация QR-кода."""
    try:
        data = request.json
        if not data or "amount" not in data:
            return jsonify({"error": "Некорректные данные"}), 400

        amount = int(data["amount"])
        if amount <= 0:
            return jsonify({"error": "Количество токенов должно быть положительным"}), 400

        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "Вы не авторизованы"}), 401

        current_round = get_current_investment_round()  # Используем обновленную логику для проверки раунда
        if not current_round:
            return jsonify({"error": "Нет активного инвестиционного раунда"}), 400

        remaining_tokens = current_round['supply'] - current_round['sold_tokens']
        if amount > remaining_tokens:
            return jsonify({"error": f"Доступно только {remaining_tokens} токенов"}), 400

        total_price = amount * current_round['price']

        # Берем текущий курс BTC/USD
        btc_usd_rate = get_btc_usd_rate()
        if btc_usd_rate == 0:
            return jsonify({"error": "Не удалось получить курс BTC/USD"}), 500

        # Вычисляем BTC сумму на основе общей стоимости
        btc_amount = total_price / btc_usd_rate  # Сумма в BTC

        # ГАРАНТИРУЕМ, что сумма не меньше $3
        min_usd = 3  # Минимальная сумма в USD
        min_btc = min_usd / btc_usd_rate  # Переводим в BTC
        btc_amount = max(btc_amount, min_btc)  # Берем максимум

        transaction_id = str(uuid.uuid4())

        # Генерация платежного URI
        payment_uri = f"bitcoin:{BITCOIN_WALLET}?amount={btc_amount}"
        qr_code_base64 = generate_qr_code(payment_uri)

        if not qr_code_base64:
            return jsonify({"error": "Ошибка генерации QR-кода"}), 500

        # Создание транзакции в MongoDB
        transaction_data = {
            "transaction_id": transaction_id,
            "user_id": user_id,
            "amount": amount,
            "price": current_round['price'],
            "total_price": total_price,  # В долларах
            "btc_amount": btc_amount,  # В BTC
            "btc_usd_rate": btc_usd_rate,
            "type": "purchase",
            "round_number": current_round['round_number'],
            "status": "noconfirmed",
            "date": datetime.utcnow(),
            "qr_code": qr_code_base64
        }

        transactions_collection.insert_one(transaction_data)

        return jsonify({
            "success": True,
            "transaction_id": transaction_id,
            "qr_code": qr_code_base64,
            "payment_uri": payment_uri,
            "btc_amount": btc_amount,
            "btc_usd_rate": btc_usd_rate,
            "total_price_usd": total_price  # Добавляем цену в долларах
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

BLOCKSTREAM_API_URL = f"https://blockstream.info/api/address/{BITCOIN_WALLET}/txs"

def get_transaction_history():
    """Получает историю транзакций через Blockstream API"""
    try:
        response = requests.get(BLOCKSTREAM_API_URL, timeout=10)
        response.raise_for_status()
        transactions_data = response.json()

        transactions = []
        for tx in transactions_data:
            tx_hash = tx.get("txid", "N/A")
            time = tx.get("status", {}).get("block_time", "Unknown")
            outputs = tx.get("vout", [])

            amount_btc = sum(output["value"] for output in outputs) / 1e8  # Сатоши -> BTC

            transactions.append({
                "date": datetime.utcfromtimestamp(time).strftime('%Y-%m-%d %H:%M:%S') if time != "Unknown" else "Unknown",
                "recipient": BITCOIN_WALLET,
                "amount": amount_btc,
                "tx_hash": tx_hash
            })

        return transactions
    except Exception as e:
        print(f"Ошибка получения истории транзакций: {e}")
        return []
    

def check_transaction(transaction_id, btc_amount):
    """Проверка транзакции на наличие оплаты"""
    transactions = get_transaction_history()
    for tx in transactions:
        if tx["recipient"] == BITCOIN_WALLET and tx["amount"] >= btc_amount:
            return True  # Транзакция найдена
    return False



def get_btc_usd_rate():
    """Получает текущий курс BTC/USD с помощью CoinGecko API"""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        return data["bitcoin"]["usd"]
    except requests.exceptions.RequestException as e:
        print(f"Ошибка при получении курса BTC/USD: {e}")
        return 0  # Возвращаем 0 в случае ошибки



@cryptowalletbank_bp.route('/confirm_transaction', methods=['POST'])
def auto_confirm_transactions():
    try:
        # Находим все неподтвержденные транзакции
        transactions = transactions_collection.find({"status": "noconfirmed"})
        
        for transaction in transactions:
            transaction_id = transaction["transaction_id"]
            btc_amount = round(transaction["total_price"] / get_btc_usd_rate(), 8)
            
            # Проверяем транзакцию на кошельке
            if check_transaction(transaction_id, btc_amount):
                user_id = transaction["user_id"]
                amount = transaction["amount"]

                # Получаем или создаем кошелек пользователя
                user_wallet = wallet_collection.find_one({"user_id": user_id})
                if not user_wallet:
                    wallet_collection.insert_one({"user_id": user_id, "tokens": 0, "transactions": []})
                    user_wallet = {"tokens": 0, "transactions": []}

                # Обновляем баланс пользователя
                new_balance = user_wallet["tokens"] + amount
                wallet_collection.update_one(
                    {"user_id": user_id},
                    {"$set": {"tokens": new_balance}, "$push": {"transactions": transaction_id}}
                )

                # Обновляем статус транзакции на "confirmed"
                transactions_collection.update_one(
                    {"transaction_id": transaction_id},
                    {"$set": {"status": "confirmed"}}
                )

                # Обновляем количество проданных токенов в раунде
                investment_rounds_collection.update_one(
                    {"round_number": transaction["round_number"]},
                    {"$inc": {"sold_tokens": amount}}
                )
    except Exception as e:
        print(f"Ошибка в автоматической проверке транзакций: {e}")

# Инициализация планировщика
scheduler = BackgroundScheduler()
scheduler.add_job(auto_confirm_transactions, 'interval', seconds=10)  # Каждые 10 секунд
scheduler.start()

@app.route('/confirm_transaction', methods=['POST'])
def confirm_transaction():
    """Подтверждение транзакции и начисление токенов."""
    try:
        data = request.json
        transaction_id = data.get("transaction_id")

        if not transaction_id:
            return jsonify({"error": "Отсутствует ID транзакции"}), 400

        # Найдем транзакцию по ID и статусу "noconfirmed"
        transaction = transactions_collection.find_one({"transaction_id": transaction_id, "status": "noconfirmed"})
        if not transaction:
            return jsonify({"error": "Транзакция не найдена или уже подтверждена"}), 400

        # Получаем сумму в BTC, необходимую для подтверждения
        btc_amount = round(transaction["total_price"] / get_btc_usd_rate(), 8)

        # Проверяем, есть ли транзакция на кошельке
        if check_transaction(transaction_id, btc_amount):
            user_id = transaction["user_id"]
            amount = transaction["amount"]

            # Получаем или создаем кошелек пользователя
            user_wallet = wallet_collection.find_one({"user_id": user_id})
            if not user_wallet:
                wallet_collection.insert_one({"user_id": user_id, "tokens": 0, "transactions": []})
                user_wallet = {"tokens": 0, "transactions": []}

            # Обновляем баланс пользователя
            new_balance = user_wallet["tokens"] + amount
            wallet_collection.update_one(
                {"user_id": user_id},
                {"$set": {"tokens": new_balance}, "$push": {"transactions": transaction_id}}
            )

            # Обновляем статус транзакции на "confirmed"
            transactions_collection.update_one(
                {"transaction_id": transaction_id},
                {"$set": {"status": "confirmed"}}
            )

            # Обновляем количество проданных токенов в раунде
            investment_rounds_collection.update_one(
                {"round_number": transaction["round_number"]},
                {"$inc": {"sold_tokens": amount}}
            )

            return jsonify({"success": True, "new_balance": new_balance}), 200
        else:
            return jsonify({"error": "Транзакция не найдена или еще не подтверждена на кошельке"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)





@app.route('/sell_tokens', methods=['POST'])
def sell_tokens():
    """Продажа токенов пользователем."""
    try:
        data = request.json
        if not data or "amount" not in data:
            return jsonify({"error": "Некорректные данные"}), 400
        
        amount = int(data["amount"])
        if amount <= 0:
            return jsonify({"error": "Количество токенов должно быть положительным"}), 400
        
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "Вы не авторизованы"}), 401
        
        user_wallet = wallet_collection.find_one({"user_id": user_id})
        if not user_wallet or user_wallet['tokens'] < amount:
            return jsonify({"error": "Недостаточно токенов"}), 400
        
        current_round = get_current_investment_round()
        total_price = amount * current_round['price']
        new_balance = user_wallet['tokens'] - amount
        
        wallet_collection.update_one(
            {"user_id": user_id},
            {"$set": {"tokens": new_balance}}
        )
        
        transaction = {
            "user_id": user_id,
            "amount": amount,
            "price": current_round['price'],
            "total_price": total_price,
            "type": "sell",
            "date": datetime.utcnow()
        }
        transactions_collection.insert_one(transaction)
        
        buyback_collection.insert_one({
            "user_id": user_id,
            "amount": amount,
            "price": current_round['price'],
            "profit": total_price,
            "date": datetime.utcnow()
        })
        
        return jsonify({"success": True, "tokens": new_balance, "fiat": total_price}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500





@cryptowalletbank_bp.route('/api/get_investment_status', methods=['GET'])
def get_investment_status():
    """Возвращает информацию о текущем инвестиционном раунде и прогрессе продаж."""
    current_round = get_current_investment_round()
    if not current_round:
        return jsonify({"error": "Нет данных о текущем раунде"}), 500

    total_tokens = current_round["supply"]
    sold_tokens = current_round["sold_tokens"]
    remaining_tokens = total_tokens - sold_tokens
    percent_remaining = round((remaining_tokens / total_tokens) * 100, 2)
    
    # Получаем цену токена для текущего и следующего раунда
    current_price = current_round["price"]
    next_price = round(current_price * PRICE_INCREASE, 2)

    return jsonify({
        "success": True,
        "round_number": current_round["round_number"],
        "remaining_tokens": remaining_tokens,
        "percent_remaining": percent_remaining,
        "current_price": current_price,
        "next_price": next_price
    })

@cryptowalletbank_bp.route('/get_recent_transactions', methods=['GET'])
def get_recent_transactions():
    """Получаем последние 5 транзакций с блокчейн-кошелька."""
    try:
        transactions = get_transaction_history()[:5]  # Ограничиваем до 5 последних транзакций
        return jsonify({"success": True, "transactions": transactions}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@cryptowalletbank_bp.route('/check_transaction_status', methods=['POST'])
def check_transaction_status():
    try:
        data = request.json
        transaction_id = data.get("transaction_id")
        
        if not transaction_id:
            return jsonify({"error": "Отсутствует ID транзакции"}), 400

        # Найдем транзакцию по ID и статусу "noconfirmed"
        transaction = transactions_collection.find_one({"transaction_id": transaction_id, "status": "noconfirmed"})
        if not transaction:
            return jsonify({"error": "Транзакция не найдена или уже подтверждена"}), 400

        # Получаем сумму в BTC, необходимую для подтверждения
        btc_amount = round(transaction["total_price"] / get_btc_usd_rate(), 8)

        # Проверяем, есть ли транзакция на кошельке
        if check_transaction(transaction_id, btc_amount):
            user_id = transaction["user_id"]
            amount = transaction["amount"]

            # Получаем или создаем кошелек пользователя
            user_wallet = wallet_collection.find_one({"user_id": user_id})
            if not user_wallet:
                wallet_collection.insert_one({"user_id": user_id, "tokens": 0, "transactions": []})
                user_wallet = {"tokens": 0, "transactions": []}

            # Обновляем баланс пользователя
            new_balance = user_wallet["tokens"] + amount
            wallet_collection.update_one(
                {"user_id": user_id},
                {"$set": {"tokens": new_balance}, "$push": {"transactions": transaction_id}}
            )

            # Обновляем статус транзакции на "confirmed"
            transactions_collection.update_one(
                {"transaction_id": transaction_id},
                {"$set": {"status": "confirmed", "confirmation_time": datetime.utcnow()}}
            )

            # Обновляем количество проданных токенов в раунде
            investment_rounds_collection.update_one(
                {"round_number": transaction["round_number"]},
                {"$inc": {"sold_tokens": amount}}
            )

            return jsonify({"success": True, "new_balance": new_balance}), 200
        else:
            return jsonify({"error": "Транзакция не найдена или еще не подтверждена на кошельке"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500





if __name__ == '__main__':
    app.run(debug=True)

import os
from flask import Flask, request
import requests

# токен бота берём из переменной окружения
TOKEN = os.getenv("TELEGRAM_TOKEN")
URL = f"https://api.telegram.org/bot{TOKEN}/"

app = Flask(__name__)

# функция отправки сообщения
def send_message(chat_id, text):
    requests.post(URL + "sendMessage", data={"chat_id": chat_id, "text": text})

# обработка вебхука
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json()

    if "message" in update and "text" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"]["text"]
        send_message(chat_id, f"Ты написал: {text}")

    return "ok", 200

# проверка что сервер жив
@app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

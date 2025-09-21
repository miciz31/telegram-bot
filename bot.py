import telebot

# Твой токен
TOKEN = "8419381170:AAHKGCEs2ay4t9Vr3EU30U4CkCuUNI-s3eo"
bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Привет! Бот работает ✅")

@bot.message_handler(func=lambda message: True)
def echo(message):
    bot.reply_to(message, f"Ты написал: {message.text}")

print("Бот запущен...")
bot.infinity_polling()

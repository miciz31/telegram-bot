import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# Логирование
logging.basicConfig(level=logging.INFO)

# Берём токен из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден! Укажи его в переменных окружения.")

# Создаём бота и диспетчер
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("✅ Бот запущен и готов к работе!")

@dp.message(Command("ping"))
async def cmd_ping(message: types.Message):
    await message.answer("pong")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

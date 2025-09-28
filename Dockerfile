# Используем официальный Python-образ
FROM python:3.11-slim

# Устанавливаем зависимости для работы pip
RUN apt-get update && apt-get install -y build-essential

# Задаем рабочую директорию
WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Запускаем бота
CMD ["python", "bot.py"]

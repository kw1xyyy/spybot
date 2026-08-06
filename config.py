import os

# Токен берём из переменных окружения (так безопаснее и удобнее на хостинге)
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Не указан BOT_TOKEN! Добавь его в переменные окружения.")
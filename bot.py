import requests
import os

# Замените на ваши значения или используйте переменные окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("https://t.me/ZakatDayZ_agent")

def check_bot():
    # 1. Проверка валидности токена (getMe)
    url_me = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    try:
        resp = requests.get(url_me, timeout=10)
        data = resp.json()
        if data.get("ok"):
            print(f"✅ Бот найден: @{data['result']['username']}")
        else:
            print(f"❌ Ошибка токена: {data}")
            return
    except Exception as e:
        print(f"❌ Ошибка соединения: {e}")
        return

    # 2. Отправка тестового сообщения (sendMessage)
    url_send = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": "🚀 Тест работоспособности пройден!"}
    
    try:
        resp = requests.post(url_send, json=payload, timeout=10)
        if resp.json().get("ok"):
            print("✅ Тестовое сообщение отправлено. Проверьте чат.")
        else:
            print(f"❌ Не удалось отправить: {resp.text}")
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")

if __name__ == "__main__":
    check_bot()

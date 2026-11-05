import aiohttp


class NitradoAPI:
    def __init__(self, token: str, server_id: int):
        self.token = token
        self.server_id = server_id
        self.headers = {"Authorization": f"Bearer {token}"}
        # Исправлен официальный эндпоинт Nitrado API
        self.url = f"https://nitrado.net{server_id}/gameservers"

    async def get_server_status(self) -> dict | None:
        """Получает актуальную информацию о сервере DayZ"""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(self.url, headers=self.headers, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("data", {}).get("gameserver", {})
                    
                    # Логируем код ошибки (например, 401 — неверный токен, 404 — неверный server_id)
                    print(f"Ошибка Nitrado API: статус {response.status}")
                    return None
            except Exception as e:
                print(f"Ошибка подключения к Nitrado API: {e}")
                return None

import aiohttp

class NitradoAPI:
    def __init__(self, token: str, server_id: int):
        self.token = token
        self.server_id = server_id
        self.headers = {"Authorization": f"Bearer {token}"}
        self.url = f"https://nitrado.net{server_id}/gameservers"

    async def get_server_status(self) -> dict | None:
        """Получает актуальную информацию о сервере DayZ"""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(self.url, headers=self.headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("data", {}).get("gameserver", {})
                    return None
            except Exception:
                return None


from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    name: str
    quantity: int
    price: int

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


class GameAPI:
    """Демонстрационная заглушка игрового API.

    Замените методы на вызовы REST API/RCON. Не доверяйте данным,
    пришедшим от клиента: проверяйте игрока и предметы на сервере.
    """

    async def validate_nick(self, nick: str) -> bool:
        # TODO: проверить ник через API игрового сервера.
        return 3 <= len(nick) <= 24 and nick.replace("_", "").isalnum()

    async def give_item(self, nick: str, item_name: str, quantity: int = 1) -> bool:
        # TODO: выдать предмет игроку через REST API/RCON.
        return True

    async def get_items_at_drop_zone(self) -> list[Item]:
        # TODO: получить реальные предметы из зоны сдачи.
        # Демо-данные позволяют проверить сценарий без игрового сервера.
        return [
            Item("Бинт", 2, 25),
            Item("Консервы", 1, 40),
        ]

    async def remove_items_from_drop_zone(self, items: list[Item]) -> bool:
        # TODO: удалить принятые предметы из зоны на игровом сервере.
        return True

    async def server_status(self) -> tuple[int, list[str]]:
        # TODO: получить онлайн и список ников.
        return 3, ["SurvivorOne", "Nomad_77", "Raven"]

    async def restart_server(self) -> bool:
        # TODO: отправить запрос на рестарт. Ограничьте доступ администраторами.
        return True

    async def banned_players(self) -> list[str]:
        # TODO: запросить список блокировок.
        return []

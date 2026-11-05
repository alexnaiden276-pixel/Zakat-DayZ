import aiohttp
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple, Any


@dataclass(frozen=True)
class Item:
    name: str
    quantity: int
    price: int


class GameAPI:
    """Демонстрационная заглушка игрового API.

    Замените методы на вызовы REST API/RCON. Не доверяйте данным,
    пришедшим от клиента: проверяйте игрока и предметы на сервере.
    """

    def __init__(self, token: str, server_id: int, session: aiohttp.ClientSession):
        self.token: str = token
        self.server_id: int = server_id
        self.headers: Dict[str, str] = {"Authorization": f"Bearer {token}"}
        self.session: aiohttp.ClientSession = session
        # ИСПРАВЛЕНО: Корректный URL к API Nitrado
        self.url: str = f"https://nitrado.net{server_id}/gameservers"

    async def get_server_status(self) -> Optional[Dict[str, Any]]:
        """Получает актуальную информацию о сервере DayZ"""
        try:
            async with self.session.get(self.url, headers=self.headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("data", {}).get("gameserver", {})
                return None
        except Exception:
            return None

    async def validate_nick(self, nick: str) -> bool:
        # TODO: проверить ник через API игрового сервера.
        return 3 <= len(nick) <= 24 and nick.replace("_", "").isalnum()

    async def give_item(self, nick: str, item_name: str, quantity: int = 1) -> bool:
        # TODO: выдать предмет игроку через REST API/RCON.
        return True

    async def get_items_at_drop_zone(self) -> List[Item]:
        # TODO: получить реальные предметы из зоны сдачи.
        # Демо-данные позволяют проверить сценарий без игрового сервера.
        return [
            Item("Бинт", 2, 25),
            Item("Консервы", 1, 40),
        ]

    async def remove_items_from_drop_zone(self, items: List[Item]) -> bool:
        # TODO: удалить принятые предметы из зоны на игровом сервере.
        return True

    async def server_status(self) -> Tuple[int, List[str]]:
        # TODO: получить онлайн и список ников.
        return 3, ["SurvivorOne", "Nomad_77", "Raven"]

    async def restart_server(self) -> bool:
        # TODO: отправить запрос на рестарт. Ограничьте доступ администраторами.
        return True

    async def banned_players(self) -> List[str]:
        # TODO: запросить список блокировок.
        return []

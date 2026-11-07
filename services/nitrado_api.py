import logging
import re
import aiohttp

logger = logging.getLogger(__name__)

class NitradoAPI:
    def __init__(self, token: str, server_id: int) -> None:
        self.token = token
        self.server_id = server_id
        self.headers = {"Authorization": f"Bearer {token}"}
        self.url = f"https://api.nitrado.net/services/{server_id}/gameservers"
        self._timeout = aiohttp.ClientTimeout(total=10)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=self._timeout,
            )
        return self._session

    async def get_server_status(self) -> dict | None:
        """Получает актуальную информацию о сервере DayZ."""
        session = await self._get_session()
        try:
            async with session.get(self.url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("data", {}).get("gameserver", {})

                logger.warning(
                    "Ошибка Nitrado API (server_id=%s): статус %s",
                    self.server_id,
                    response.status,
                )
                return None
        except aiohttp.ClientError as e:
            logger.error("Ошибка сети при запросе к Nitrado API: %s", e)
            return None
        except TimeoutError:
            logger.error("Таймаут подключения к Nitrado API")
            return None

    async def close(self) -> None:
        """Закрывает HTTP-сессию при остановке бота."""
        if self._session and not self._session.closed:
            await self._session.close()

        async def find_player_id_in_logs(self, psn_nick: str) -> str | None:
        """
        Диагностическая версия: выводит структуру папок сервера в консоль бота.
        """
        session = await self._get_session()
        
        # Проверяем сначала корень файловой системы
        list_url = f"https://api.nitrado.net/services/{self.server_id}/gameservers/file_server/list"
        
        # Попробуем сделать запрос без параметров, чтобы увидеть корневые папки
        try:
            async with session.get(list_url) as response:
                if response.status == 200:
                    data = await response.json()
                    entries = data.get("data", {}).get("entries", [])
                    folder_names = [f.get("name") for f in entries]
                    logger.info("=== КОРЕНЬ СЕРВЕРА (БЕЗ DIR): %s ===", folder_names)
                else:
                    logger.error("Ошибка корня API: %s", response.status)
        except Exception as e:
            logger.error("Ошибка при тесте корня: %s", e)

        # Теперь проверяем то, что выдает Nitrado по дефолтному пути
        params = {"dir": "dayzstandalone/config"}
        try:
            async with session.get(list_url, params=params) as response:
                data = await response.json()
                entries = data.get("data", {}).get("entries", [])
                all_files = [f.get("name") for f in entries]
                logger.info("=== СОДЕРЖИМОЕ ЗАПРОШЕННОЙ ПАПКИ: %s ===", all_files)
                
                # Дополнительно проверяем альтернативный консольный путь 'config'
                async with session.get(list_url, params={"dir": "config"}) as alt_resp:
                    alt_data = await alt_resp.json()
                    alt_entries = alt_data.get("data", {}).get("entries", [])
                    logger.info("=== СОДЕРЖИМОЕ АЛЬТЕРНАТИВНОЙ ПАПКИ 'config': %s ===", [f.get("name") for f in alt_entries])

            logger.warning("Диагностика завершена. Проверьте логи консоли выше.")
            return None

        except Exception as e:
            logger.error("Исключение при диагностике: %s", e)
            return None


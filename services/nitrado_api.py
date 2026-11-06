import logging
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
    import re

# ... ваш существующий код класса NitradoAPI ...

    async def find_player_id_in_logs(self, psn_nick: str) -> str | None:
        """
        Скачивает свежий .adm лог сервера DayZ и ищет полный ID игрока по PSN нику.
        """
        # 1. Получаем список файлов в папке profiles (где лежат админ-логи DayZ)
        list_url = f"https://nitrado.net{self.server_id}/gameserver/file_server/list"
        params = {"dir": "profiles"}
        
        # Предполагается, что у вас в классе есть self.headers с токеном авторизации
        # Если сессия хранится в self.session (aiohttp.ClientSession):
        async with self.session.get(list_url, headers=self.headers, params=params) as response:
            if response.status != 200:
                return None
            data = await response.json()
            
            entries = data.get("data", {}).get("entries", [])
            # Ищем файлы, заканчивающиеся на .adm
            adm_files = [f for f in entries if f.get("name", "").endswith(".adm")]
            
            if not adm_files:
                return None
                
            # Сортируем файлы: самые свежие будут первыми
            adm_files.sort(key=lambda x: x.get("modified", 0), reverse=True)
            latest_log_path = adm_files[0].get("path")

        # 2. Запрашиваем ссылку на скачивание этого лога
        download_url = f"https://nitrado.net{self.server_id}/gameserver/file_server/download"
        async with self.session.get(download_url, headers=self.headers, params={"file": latest_log_path}) as response:
            if response.status != 200:
                return None
            download_data = await response.json()
            direct_url = download_data.get("data", {}).get("url")

        # 3. Скачиваем содержимое лога
        async with self.session.get(direct_url) as response:
            if response.status != 200:
                return None
            log_content = await response.text(encoding="utf-8", errors="ignore")

        # 4. Ищем регулярным выражением строку подключения
        # Пример: Player "SonyGamer99" (id=v_Yh67...dfg89) connected
        pattern = rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)\s+connected'
        match = re.search(pattern, log_content, re.IGNORECASE)
        
        if match:
            return match.group(1)  # Возвращаем полный ID (UID)
            
        return None


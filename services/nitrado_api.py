import logging
import re
import aiohttp

logger = logging.getLogger(__name__)

class NitradoAPI:
    def __init__(self, token: str, server_id: int) -> None:
        self.token = token
        self.server_id = server_id
        self.headers = {"Authorization": f"Bearer {token}"}
        self.url = f"https://nitrado.net{server_id}/gameservers"
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
        Скачивает свежий .adm лог сервера DayZ PlayStation и ищет полный ID игрока по PSN нику.
        """
        session = await self._get_session()
        
        # Задаем правильный путь для консолей PlayStation
        list_url = f"https://nitrado.net{self.server_id}/gameservers/file_server/list"
        params = {"dir": "dayzstandalone/config"}
        
        try:
            # 1. Получаем список файлов
            async with session.get(list_url, params=params) as response:
                if response.status != 200:
                    logger.error("Не удалось получить список файлов Nitrado. Статус: %s", response.status)
                    return None
                data = await response.json()
                
                entries = data.get("data", {}).get("entries", [])
                
                # Фильтруем файлы с расширением .adm
                adm_files = [f for f in entries if f.get("name", "").lower().endswith(".adm")]
                
                if not adm_files:
                    logger.warning("Файлы логов .adm не найдены в папке dayzstandalone/config.")
                    return None
                    
                # Сортируем: самые свежие файлы по дате модификации будут первыми
                adm_files.sort(key=lambda x: x.get("modified", 0), reverse=True)
                latest_log_path = adm_files[0].get("path")

            # 2. Получаем ссылку на скачивание файла лога
            download_url = f"https://nitrado.net{self.server_id}/gameservers/file_server/download"
            async with session.get(download_url, params={"file": latest_log_path}) as response:
                if response.status != 200:
                    logger.error("Не удалось получить ссылку на скачивание лога. Статус: %s", response.status)
                    return None
                download_data = await response.json()
                
                # Извлекаем прямую ссылку из структуры ответа Nitrado
                direct_url = download_data.get("data", {}).get("token", {}).get("url")
                
                if not direct_url:
                    logger.error("Токен скачивания не найден в ответе API Nitrado.")
                    return None

            # 3. Скачиваем содержимое лога по прямой ссылке
            async with session.get(direct_url) as response:
                if response.status != 200:
                    logger.error("Не удалось скачать файл лога напрямую. Статус: %s", response.status)
                    return None
                log_content = await response.text(encoding="utf-8", errors="ignore")

            # 4. Ищем регулярным выражением строку подключения игрока
            pattern = rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)\s+connected'
            match = re.search(pattern, log_content, re.IGNORECASE)
            
            if match:
                return match.group(1)  # Возвращаем полный ID (UID)
                
            logger.info("Игрок %s не найден в скачанном логе.", psn_nick)
            return None

        except Exception as e:
            logger.error("Исключение при поиске ID игрока в логах Nitrado: %s", e)
            return None

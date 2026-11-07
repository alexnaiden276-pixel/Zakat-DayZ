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
        self._timeout = aiohttp.ClientTimeout(total=15)
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
                    self.server_id, response.status
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

    async def find_player_id_in_logs(self, psn_nick: str, folder: str = "/profiles") -> str | None:
        """
        Скачивает свежий .adm лог сервера DayZ PlayStation и ищет полный ID игрока по PSN нику.
        Автоматически проверяет корень сервера, если указанная папка пуста.
        """
        session = await self._get_session()
        list_url = f"https://api.nitrado.net/services/{self.server_id}/gameservers/file_server/list"
        download_url = f"https://api.nitrado.net/services/{self.server_id}/gameservers/file_server/download"
        
        pattern = re.compile(rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)\s+connected', re.IGNORECASE)

        try:
            # 1. Запрашиваем список файлов из папки
            async with session.get(list_url, params={"dir": folder}) as response:
                if response.status != 200:
                    logger.error("Не удалось получить список файлов Nitrado из '%s'. Статус: %s", folder, response.status)
                    return None
                data = await response.json()
            
            entries = data.get("data", {}).get("entries", [])
            
            # Если выбранная папка пуста, автоматически смотрим КОРЕНЬ сервера для диагностики
            if not entries and folder != "":
                logger.warning("Папка '%s' пуста. Проверяем корневой каталог сервера...", folder)
                async with session.get(list_url, params={"dir": ""}) as response:
                    if response.status == 200:
                        root_data = await response.json()
                        root_entries = root_data.get("data", {}).get("entries", [])
                        root_names = [f.get("name", "Unknown") for f in root_entries]
                        logger.info("СТРУКТУРА КОРНЯ СЕРВЕРА: Обнаружено объектов: %s. Список: %s", len(root_entries), root_names)
                    else:
                        logger.error("Не удалось получить даже корневой каталог. Статус: %s", response.status)
                return None

            # Выводим содержимое папки, если оно есть
            all_names = [f.get("name", "Unknown") for f in entries]
            logger.info("Nitrado API вернул объектов: %s в папке '%s'. Список: %s", len(entries), folder, all_names[:20])
            
            adm_files = [f for f in entries if f.get("name", "").lower().endswith(".adm")]
            
            if not adm_files:
                logger.warning("Файлы логов .adm не найдены в папке %s.", folder)
                return None

            # Сортируем и берем самый свежий лог
            adm_files.sort(key=lambda x: x.get("modified", 0), reverse=True)
            latest_log_path = adm_files[0].get("path")
            logger.info("Успешно выбран свежий лог: %s", adm_files[0].get("name"))

            # 2. Получаем pre-signed ссылку на скачивание
            async with session.get(download_url, params={"file": latest_log_path}) as response:
                if response.status != 200:
                    logger.error("Не удалось получить ссылку на скачивание лога. Статус: %s", response.status)
                    return None
                download_data = await response.json()
            
            direct_url = download_data.get("data", {}).get("token", {}).get("url")
            if not direct_url:
                logger.error("Токен скачивания не найден в ответе API Nitrado.")
                return None

            # 3. Скачиваем и обрабатываем лог ПОСТРОЧНО
            async with session.get(direct_url) as response:
                if response.status != 200:
                    logger.error("Не удалось скачать файл лога напрямую. Статус: %s", response.status)
                    return None
                
                async for line_bytes in response.content:
                    line = line_bytes.decode(encoding="utf-8", errors="replace")
                    match = pattern.search(line)
                    if match:
                        player_id = match.group(1)
                        logger.info("Игрок %s найден с ID: %s", psn_nick, player_id)
                        return player_id

            logger.info("Игрок %s не найден в скачанном логе.", psn_nick)
            return None

        except Exception as e:
            logger.error("Исключение при поиске ID игрока в логах Nitrado: %s", e, exc_info=True)
            return None

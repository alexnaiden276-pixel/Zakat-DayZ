import logging
import re
import aiohttp

logger = logging.getLogger(__name__)

class NitradoAPI:
    def __init__(self, token: str, server_id: int) -> None:
        self.token = token
        self.server_id = server_id
        self.headers = {"Authorization": f"Bearer {token}"}
        # Базовый URL для проверки статуса
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
        except Exception as e:
            logger.error("Ошибка при запросе статуса сервера: %s", e)
            return None

    async def close(self) -> None:
        """Закрывает HTTP-сессию при остановке бота."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def find_player_id_in_logs(self, psn_nick: str) -> str | None:
        """
        Ищет ID игрока в .adm логах, автоматически перебирая все возможные
        папки для DayZ PlayStation на Nitrado.
        """
        session = await self._get_session()
        # СТРОГО КОРРЕКТНЫЕ ССЫЛКИ ПО ДОКУМЕНТАЦИИ NITRADO API
        list_url = f"https://api.nitrado.net/services/{self.server_id}/gameservers/file_server/list"
        download_url = f"https://api.nitrado.net/services/{self.server_id}/gameservers/file_server/download"
        
        pattern = re.compile(rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)\s+connected', re.IGNORECASE)

        # Список папок, которые мы поочередно проверим на сервере
        possible_folders = ["/profiles", "", "/dayzps/config", "/config", "/dayzps"]
        entries = []
        chosen_folder = ""

        try:
            # Циклом перебираем папки, пока Nitrado API не вернет файлы
            for folder in possible_folders:
                logger.info("Проверка директории Nitrado API: '%s'", folder)
                async with session.get(list_url, params={"dir": folder}) as response:
                    if response.status == 200:
                        data = await response.json()
                        found_entries = data.get("data", {}).get("entries", [])
                        if found_entries:
                            entries = found_entries
                            chosen_folder = folder
                            break
                    else:
                        logger.warning("Папка '%s' вернула HTTP статус %s", folder, response.status)

            if not entries:
                logger.error(
                    "Nitrado API вернул 0 объектов во ВСЕХ возможных папках. "
                    "Убедитесь, что сервер запущен и логи физически создались в панели."
                )
                return None

            # Выводим структуру найденной папки
            all_names = [f.get("name", "Unknown") for f in entries]
            logger.info("Успешно прочитана папка '%s'. Найдено объектов: %s. Файлы: %s", chosen_folder, len(entries), all_names[:15])
            
            # Фильтруем .adm логи
            adm_files = [f for f in entries if f.get("name", "").lower().endswith(".adm")]
            
            if not adm_files:
                logger.warning("Файлы логов .adm не найдены в активной папке %s.", chosen_folder)
                return None

            # Сортируем файлы логов по дате изменения (свежие — первые)
            adm_files.sort(key=lambda x: x.get("modified", 0), reverse=True)
            latest_log_path = adm_files[0].get("path")
            logger.info("Выбран самый свежий лог: %s", adm_files[0].get("name"))

            # 2. Запрос pre-signed ссылки на скачивание лога
            async with session.get(download_url, params={"file": latest_log_path}) as response:
                if response.status != 200:
                    logger.error("Не удалось получить ссылку на скачивание лога. Статус: %s", response.status)
                    return None
                download_data = await response.json()
            
            direct_url = download_data.get("data", {}).get("token", {}).get("url")
            if not direct_url:
                logger.error("Токен скачивания отсутствует в ответе Nitrado.")
                return None

            # 3. Построчное чтение файла напрямую через стрим
            async with session.get(direct_url) as response:
                if response.status != 200:
                    logger.error("Не удалось скачать лог-файл напрямую. Статус: %s", response.status)
                    return None
                
                async for line_bytes in response.content:
                    line = line_bytes.decode(encoding="utf-8", errors="replace")
                    match = pattern.search(line)
                    if match:
                        player_id = match.group(1)
                        logger.info("Игрок %s успешно найден! ID: %s", psn_nick, player_id)
                        return player_id

            logger.info("Игрок %s не найден внутри файла лога.", psn_nick)
            return None

        except Exception as e:
            logger.error("Критическое исключение при разборе логов Nitrado: %s", e, exc_info=True)
            return None

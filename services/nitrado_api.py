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
        self.url = f"https://nitrado.net{server_id}/gameservers"
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
                logger.warning("Ошибка Nitrado API (server_id=%s): статус %s", self.server_id, response.status)
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
        Ищет ID игрока, выполняя глубокий обход папок сервера Nitrado,
        чтобы гарантированно найти скрытую директорию с .adm логами.
        """
        session = await self._get_session()
        
        # СТРОГО ИСПРАВЛЕННЫЕ И ПОЛНЫЕ ССЫЛКИ К NITRADO API
        list_url = f"https://nitrado.net{self.server_id}/gameservers/file_server/list"
        download_url = f"https://nitrado.net{self.server_id}/gameservers/file_server/download"
        
        pattern = re.compile(rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)\s+connected', re.IGNORECASE)

        # Очередь для сканирования папок (начнем с корня сервера)
        folders_to_scan = [""]
        scanned_folders = set()
        adm_files = []

        try:
            logger.info("ЗАПУСК АВТОМАТИЧЕСКОГО ПОИСКА ПАПКИ ЛОГОВ...")
            
            # Сканируем не более 30 папок в глубину, чтобы не спамить API
            while folders_to_scan and len(scanned_folders) < 30:
                current_folder = folders_to_scan.pop(0)
                if current_folder in scanned_folders:
                    continue
                
                scanned_folders.add(current_folder)
                
                async with session.get(list_url, params={"dir": current_folder}) as response:
                    if response.status != 200:
                        continue
                    
                    data = await response.json()
                    entries = data.get("data", {}).get("entries", [])
                    
                    if not entries:
                        continue

                    for item in entries:
                        item_name = item.get("name", "")
                        item_path = item.get("path", "")
                        
                        # Если это папка — добавляем её в очередь на проверку
                        if item.get("ch_dir") is True or item.get("type") == "dir":
                            # Защита: пропускаем тяжелые папки с модами или текстурами, где логов быть не может
                            if not any(x in item_path.lower() for x in ["/addons", "/dta", "/battleye"]):
                                folders_to_scan.append(item_path)
                        
                        # Если это .adm файл — запоминаем его
                        elif item_name.lower().endswith(".adm"):
                            adm_files.append(item)

            if not adm_files:
                logger.error(
                    "Поиск завершен. Проверено папок: %s. Файлы .adm не найдены. "
                    "Проверенные папки сервера: %s", 
                    len(scanned_folders), list(scanned_folders)
                )
                return None

            # Сортируем все найденные файлы .adm по дате изменения (свежие — первые)
            adm_files.sort(key=lambda x: x.get("modified", 0), reverse=True)
            latest_log_path = adm_files[0].get("path")
            logger.info(
                "Логи успешно обнаружены! Найдено файлов .adm: %s. Выбран свежий: %s (Путь: %s)", 
                len(adm_files), adm_files[0].get("name"), latest_log_path
            )

            # 2. Запрос pre-signed ссылки на скачивание файла
            async with session.get(download_url, params={"file": latest_log_path}) as response:
                if response.status != 200:
                    logger.error("Не удалось получить ссылку на скачивание лога. Статус: %s", response.status)
                    return None
                download_data = await response.json()
            
            direct_url = download_data.get("data", {}).get("token", {}).get("url")
            if not direct_url:
                logger.error("Токен скачивания отсутствует в ответе Nitrado.")
                return None

            # 3. Построчное чтение файла лога из стрима
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
            logger.error("Ошибка при поиске логов Nitrado: %s", e, exc_info=True)
            return None

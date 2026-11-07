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
        Ищет ID игрока, выполняя глубокий рекурсивный обход папок сервера Nitrado,
        чтобы гарантированно найти скрытую директорию с .adm логами.
        """
        session = await self._get_session()
        list_url = f"https://nitrado.net{self.server_id}/gameservers/file_server/list"
        download_url = f"https://nitrado.net{self.server_id}/gameservers/file_server/download"
        
        pattern = re.compile(rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)\s+connected', re.IGNORECASE)

        # Очередь для сканирования папок (начнем с корня)
        folders_to_scan = [""]
        scanned_folders = set()
        adm_files = []

        try:
            logger.info("ЗАПУСК ГЛУБОКОГО СКАНИРОВАНИЯ ФАЙЛОВОЙ СИСТЕМЫ СЕРВЕРА...")
            
            # Сканируем не более 40 папок в глубину, чтобы избежать бесконечного цикла
            while folders_to_scan and len(scanned_folders) < 40:
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
                        
                        # Если это папка — добавляем её в очередь на сканирование
                        if item.get("ch_dir") is True or item.get("type") == "dir":
                            # Защита: не лезем слишком глубоко в системные папки
                            if not any(x in item_path.lower() for x in ["/addons", "/dta", "/battleye"]):
                                folders_to_scan.append(item_path)
                        
                        # Если это .adm файл — сохраняем его
                        elif item_name.lower().endswith(".adm"):
                            adm_files.append(item)

            if not adm_files:
                logger.error(
                    "Глубокое сканирование завершено. Проверено папок: %s. Файлы .adm НЕ НАЙДЕНЫ. "
                    "Возможные причины: 1. Сервер выключен и не создал логи. "
                    "2. В панели Nitrado включена опция 'Delete logs on restart' и сервер пустой.", 
                    len(scanned_folders)
                )
                logger.info("Все проверенные папки сервера: %s", list(scanned_folders))
                return None

            # Сортируем найденные .adm файлы по дате изменения
            adm_files.sort(key=lambda x: x.get("modified", 0), reverse=True)
            latest_log_path = adm_files[0].get("path")
            logger.info(
                "Глубокое сканирование УСПЕШНО! Найдено логов: %s. Выбран свежий: %s (Путь: %s)", 
                len(adm_files), adm_files[0].get("name"), latest_log_path
            )

            # 2. Запрос ссылки на скачивание
            async with session.get(download_url, params={"file": latest_log_path}) as response:
                if response.status != 200:
                    return None
                download_data = await response.json()
            
            direct_url = download_data.get("data", {}).get("token", {}).get("url")
            if not direct_url:
                return None

            # 3. Построчное чтение файла
            async with session.get(direct_url) as response:
                if response.status != 200:
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
            logger.error("Ошибка при глубоком сканировании логов: %s", e, exc_info=True)
            return None

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

    async def find_player_id_in_logs(self, psn_nick: str, folder: str = "dayzps/config") -> str | None:
        """
        Автоматически тестирует варианты путей и ищет полный ID игрока по PSN нику в .adm логах.
        """
        session = await self._get_session()
        
        list_url = f"https://api.nitrado.net/services/{self.server_id}/gameservers/file_server/list"
        download_url = f"https://api.nitrado.net/services/{self.server_id}/gameservers/file_server/download"
        
        # Список путей для автоматической проверки структуры Nitrado API
        test_folders = [folder, "", "config", "dayzps", "dayzps/config"]
        # Убираем дубликаты, сохраняя исходный порядок
        test_folders = list(dict.fromkeys(test_folders))
        
        for test_dir in test_folders:
            try:
                logger.info("Проверка директории через API Nitrado: '%s'", test_dir)
                async with session.get(list_url, params={"dir": test_dir}) as response:
                    if response.status != 200:
                        logger.debug("Папка '%s' вернула статус: %s", test_dir, response.status)
                        continue
                        
                    data = await response.json()
                    entries = data.get("data", {}).get("entries", [])
                    
                    # Логируем абсолютно всё, что нашлось в этой папке для отладки
                    all_names = [f.get("name") for f in entries]
                    logger.info("Отладка: в папке '%s' API видит файлы/папки: %s", test_dir, all_names)
                    
                    # Фильтруем файлы с расширением .adm / .ADM
                    adm_files = [f for f in entries if f.get("name", "").lower().endswith(".adm")]
                    
                    if not adm_files:
                        continue
                        
                    logger.info("УРА! Файлы логов успешно найдены в папке: '%s'", test_dir)
                    
                    # Сортируем: самые свежие файлы по дате модификации будут первыми
                    adm_files.sort(key=lambda x: x.get("modified", 0), reverse=True)
                    latest_log_path = adm_files[0].get("path")
                    logger.info("Выбран самый свежий файл лога: %s", latest_log_path)

                    # 2. Получаем ссылку на скачивание файла лога
                    async with session.get(download_url, params={"file": latest_log_path}) as dl_response:
                        if dl_response.status != 200:
                            logger.error("Не удалось получить ссылку на скачивание лога. Статус: %s", dl_response.status)
                            return None
                        download_data = await dl_response.json()
                        
                        direct_url = download_data.get("data", {}).get("token", {}).get("url")
                        if not direct_url:
                            logger.error("Токен скачивания не найден в ответе API Nitrado.")
                            return None

                    # 3. Скачиваем содержимое лога по прямой ссылке
                    async with aiohttp.ClientSession(timeout=self._timeout) as download_session:
                        async with download_session.get(direct_url) as file_response:
                            if file_response.status != 200:
                                logger.error("Не удалось скачать файл лога напрямую. Статус: %s", file_response.status)
                                return None
                            log_content = await file_response.text(encoding="utf-8", errors="ignore")

                    # 4. Ищем регулярным выражением строку подключения игрока
                    pattern = rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)\s+connected'
                    match = re.search(pattern, log_content, re.IGNORECASE)
                    
                    if match:
                        return match.group(1)  # Возвращаем полный ID (UID)
                        
                    logger.info("Игрок %s не найден в скачанном логе %s.", psn_nick, latest_log_path)
                    return None

            except Exception as e:
                logger.error("Исключение при сканировании папки '%s': %s", test_dir, e)
                
        logger.warning("Ни в одной из папок %s файлы логов .adm не обнаружены через API.", test_folders)
        return None

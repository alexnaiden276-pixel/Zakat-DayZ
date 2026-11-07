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

    async def find_player_id_in_logs(self, psn_nick: str, folder: str = "config") -> str | None:
        """
        Умный поиск файлов логов с автоматическим определением структуры папок хостинга Nitrado.
        """
        session = await self._get_session()
        
        list_url = f"https://api.nitrado.net/services/{self.server_id}/gameservers/file_server/list"
        download_url = f"https://api.nitrado.net/services/{self.server_id}/gameservers/file_server/download"
        
        # Шаг 0: Проверим статус сервера, чтобы убедиться, что ID указан верно
        status = await self.get_server_status()
        if status:
            logger.info("Успешное подключение к серверу '%s' (Игра: %s, Статус: %s)", 
                        status.get('query', {}).get('server_name'), 
                        status.get('game'), 
                        status.get('status'))
        else:
            logger.error("Критическая ошибка: Не удалось получить статус сервера. Проверьте server_id или токен!")

        # Перебираем все возможные варианты путей с учетом специфики API консолей
        test_folders = [
            folder,                   # "config"
            f"dayzps/{folder}",       # "dayzps/config"
            f"dayzstandalone/{folder}",# "dayzstandalone/config"
            "",                       # Корень сервера
            "dayzps"                  # Папка игры
        ]
        
        # Удаляем дубликаты
        test_folders = list(dict.fromkeys(test_folders))
        
        for test_dir in test_folders:
            try:
                async with session.get(list_url, params={"dir": test_dir}) as response:
                    if response.status != 200:
                        continue
                        
                    data = await response.json()
                    entries = data.get("data", {}).get("entries", [])
                    
                    # Если папка вернула вложенные элементы — проверяем их
                    if entries:
                        names = [f.get("name") for f in entries]
                        logger.info("API Nitrado успешно прочитал папку '%s'. Найдено объектов: %s", test_dir, len(names))
                        
                        # 1. Ищем .adm файлы в текущей папке
                        adm_files = [f for f in entries if f.get("name", "").lower().endswith(".adm")]
                        
                        # 2. Умный поиск: если мы в корне, проверим, нет ли внутри нужных папок, и зайдем в них
                        if not adm_files:
                            for entry in entries:
                                if entry.get("name") in ["dayzps", "config"] and entry.get("chgd"):
                                    # Это папка, попробуем заглянуть внутрь нее в следующем цикле
                                    new_path = f"{test_dir}/{entry.get('name')}".strip("/")
                                    if new_path not in test_folders:
                                        test_folders.append(new_path)
                            continue

                        logger.info("Файлы логов .ADM обнаружены по пути: '%s'", test_dir)
                        
                        # Сортируем файлы по дате модификации (самый свежий — первый)
                        adm_files.sort(key=lambda x: x.get("modified", 0), reverse=True)
                        latest_log_path = adm_files[0].get("path")
                        logger.info("Скачиваем лог: %s", adm_files[0].get("name"))

                        # Получаем ссылку на скачивание
                        async with session.get(download_url, params={"file": latest_log_path}) as dl_response:
                            if dl_response.status != 200:
                                logger.error("Не удалось получить ссылку на скачивание лога. Код: %s", dl_response.status)
                                return None
                            dl_data = await dl_response.json()
                            direct_url = dl_data.get("data", {}).get("token", {}).get("url")

                        # Скачиваем сам файл
                        async with aiohttp.ClientSession(timeout=self._timeout) as download_session:
                            async with download_session.get(direct_url) as file_response:
                                if file_response.status != 200:
                                    logger.error("Ошибка скачивания файла лога напрямую: %s", file_response.status)
                                    return None
                                log_content = await file_response.text(encoding="utf-8", errors="ignore")

                        # Поиск игрока регулярным выражением
                        pattern = rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)\s+connected'
                        match = re.search(pattern, log_content, re.IGNORECASE)
                        
                        if match:
                            return match.group(1)
                            
                        logger.info("Игрок %s не найден в логе.", psn_nick)
                        return None

            except Exception as e:
                logger.error("Ошибка при сканировании директории '%s': %s", test_dir, e)
                
        logger.warning("Не удалось обнаружить .adm логи. Доступные для проверки пути были: %s", test_folders)
        return None

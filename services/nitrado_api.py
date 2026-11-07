import logging
import re
import aiohttp
import aioftp

logger = logging.getLogger(__name__)

class NitradoAPI:
    def __init__(self, token: str, server_id: int, ftp_host: str = None, ftp_user: str = None, ftp_pass: str = None, ftp_port: int = 21) -> None:
        self.token = token
        self.server_id = server_id
        self.headers = {"Authorization": f"Bearer {token}"}
        # Исправленный полный URL для получения статуса сервера через API
        self.api_url = f"https://nitrado.net{server_id}/gameservers"
        self._timeout = aiohttp.ClientTimeout(total=15)
        self._session: aiohttp.ClientSession | None = None
        
        # FTP настройки для логов
        self.ftp_host = ftp_host
        self.ftp_user = ftp_user
        self.ftp_pass = ftp_pass
        self.ftp_port = ftp_port

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=self._timeout,
            )
        return self._session

    async def get_server_status(self) -> dict | None:
        """Получает актуальную информацию о сервере DayZ через API."""
        session = await self._get_session()
        try:
            async with session.get(self.api_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("data", {}).get("gameserver", {})
                logger.warning("Ошибка Nitrado API (status): %s", response.status)
                return None
        except Exception as e:
            logger.error("Ошибка при запросе статуса сервера: %s", e)
            return None

    async def close(self) -> None:
        """Закрывает сессию при остановке бота."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def find_player_id_in_logs(self, psn_nick: str, folder: str = "/profiles") -> str | None:
        """
        Подключается к серверу Nitrado через FTP, находит самый свежий .adm лог
        и ищет ID игрока. Метод полностью независим от ограничений прав токена.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("FTP credentials не заданы в инициализации класса!")
            return None

        pattern = re.compile(rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)\s+connected', re.IGNORECASE)

        try:
            logger.info("Подключение к FTP Nitrado: %s:%s", self.ftp_host, self.ftp_port)
            
            # 1. Асинхронно подключаемся к FTP-серверу Nitrado
            async with aioftp.Client.context(self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass) as client:
                
                # Переходим в папку с логами (обычно /profiles для DayZ PlayStation) [20.3.1]
                try:
                    await client.change_directory(folder)
                    logger.info("Успешно перешли в FTP директорию: %s", folder)
                except Exception:
                    logger.warning("Директория %s не найдена, пробуем искать из корня сервера", folder)
                
                adm_files = []
                
                # Сканируем файлы в текущей директории
                async for path, stat in client.list():
                    if path.name.lower().endswith(".adm"):
                        # Сохраняем имя файла и дату модификации для сортировки
                        modified_time = stat.get("modify", "0")
                        adm_files.append({"path": path, "modified": modified_time})

                if not adm_files:
                    logger.warning("На FTP сервере в папке %s не обнаружено файлов .adm", folder)
                    return None

                # Сортируем: файлы с самой свежей датой будут первыми
                adm_files.sort(key=lambda x: x["modified"], reverse=True)
                latest_file = adm_files[0]["path"]
                logger.info("Выбран свежий лог через FTP: %s", latest_file.name)

                # 2. Скачиваем лог напрямую в память в виде байт-потока
                log_lines = []
                async with client.collect(latest_file) as stream:
                    async for chunk in stream.iter_chunks():
                        log_lines.append(chunk)

                # Объединяем полученные чанки байт
                full_log_bytes = b"".join(log_lines)
                
                # Декодируем (DayZ использует utf-8, либо cp1252 для спецсимволов)
                log_content = full_log_bytes.decode(encoding="utf-8", errors="replace")

                # 3. Ищем регулярным выражением строку подключения
                # Разделяем на строки для оптимизации поиска
                for line in log_content.splitlines():
                    match = pattern.search(line)
                    if match:
                        player_id = match.group(1)
                        logger.info("Игрок %s успешно найден через FTP лог! ID: %s", psn_nick, player_id)
                        return player_id

                logger.info("Игрок %s не найден в логе через FTP.", psn_nick)
                return None

        except Exception as e:
            logger.error("Критическая ошибка при работе с FTP Nitrado: %s", e, exc_info=True)
            return None

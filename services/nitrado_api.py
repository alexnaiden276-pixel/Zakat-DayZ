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
        self.api_url = f"https://api.nitrado.net/services/{server_id}/gameservers"
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

    async def find_player_id_in_logs(self, psn_nick: str, folder: str = "/dayzps/config") -> str | None:
        """
        Подключается по асинхронному FTP к папке dayzps/config, 
        находит свежий .adm лог и производит поиск ID игрока.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы при инициализации класса!")
            return None

        pattern = re.compile(rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)\s+connected', re.IGNORECASE)

        try:
            logger.info("Асинхронное подключение к FTP Nitrado: %s:%s", self.ftp_host, self.ftp_port)
            
            async with aioftp.Client.context(self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass) as client:
                
                # Переходим в директорию, где лежат логи DayZ
                try:
                    await client.change_directory(folder)
                    logger.info("Успешно перешли в FTP директорию логов: '%s'", folder)
                except Exception as e:
                    logger.error("Не удалось перейти в папку логов '%s' через FTP: %s", folder, e)
                    return None

                adm_files = []
                
                # Сканируем файлы в текущей активной папке dayzps/config
                async for path, stat in client.list():
                    if path.name.lower().endswith(".adm"):
                        modified_time = stat.get("modify", "0")
                        adm_files.append({"path": path, "name": path.name, "modified": modified_time})

                if not adm_files:
                    logger.warning("Файлы логов .adm не обнаружены на FTP в папке %s", folder)
                    return None

                # Сортируем файлы логов по дате изменения (самые свежие — первые)
                adm_files.sort(key=lambda x: x["modified"], reverse=True)
                latest_file_data = adm_files[0]
                logger.info("Свежий .adm лог успешно выбран через FTP: %s", latest_file_data["name"])

                # ИСПРАВЛЕНО: Правильный метод скачивания файлов в библиотеке aioftp
                log_chunks = []
                async with client.download(latest_file_data["path"]) as stream:
                    async for chunk in stream.iter_chunks():
                        log_chunks.append(chunk)

                # Объединяем байты и декодируем строку
                full_log_text = b"".join(log_chunks).decode(encoding="utf-8", errors="replace")

                # Построчно ищем строку подключения игрока
                for line in full_log_text.splitlines():
                    match = pattern.search(line)
                    if match:
                        player_id = match.group(1)
                        logger.info("Игрок %s успешно найден по FTP! ID: %s", psn_nick, player_id)
                        return player_id

                logger.info("Игрок %s не найден в текущем .adm файле логов.", psn_nick)
                return None

        except Exception as e:
            logger.error("Ошибка при поиске игрока через aioftp: %s", e, exc_info=True)
            return None

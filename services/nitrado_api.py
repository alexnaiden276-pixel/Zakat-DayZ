import logging
import re
import os
import aiohttp
import aioftp

logger = logging.getLogger(__name__)

class NitradoAPI:
    def __init__(self, token: str, server_id: int, ftp_host: str = None, ftp_user: str = None, ftp_pass: str = None, ftp_port: int = 21) -> None:
        self.token = token
        self.server_id = server_id
        self.headers = {"Authorization": f"Bearer {token}"}
        # Полный корректный URL для работы API Nitrado
        self.api_url = f"https://api.nitrado.net/services/{server_id}/gameservers"
        self._timeout = aiohttp.ClientTimeout(total=15)
        self._session: aiohttp.ClientSession | None = None
        
        # FTP настройки
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
        """Получает актуальную информацию о сервере DayZ через API Nitrado."""
        session = await self._get_session()
        try:
            async with session.get(self.api_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("data", {}).get("gameserver", {})
                logger.warning("Ошибка Nitrado API (status): статус %s", response.status)
                return None
        except Exception as e:
            logger.error("Ошибка при запросе статуса сервера: %s", e)
            return None

    async def close(self) -> None:
        """Закрывает HTTP-сессию при остановке бота."""
        if self._session and not self._session.closed:
            await self._session.close()

        async def find_player_id_in_logs(self, psn_nick: str, folder: str = "/dayzps/config") -> str | None:
        """
        Подключается по FTP, сортирует логи и проверяет 3 самых свежих файла .adm,
        чтобы гарантированно найти ID игрока, даже если Nitrado задерживает обновление.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы при инициализации класса!")
            return None

        # Расширенный паттерн для поиска любых упоминаний игрока
        pattern = re.compile(rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)', re.IGNORECASE)
        tmp_local_path = "/tmp/dayz_latest_log.adm"

        try:
            logger.info("Асинхронное подключение к FTP Nitrado: %s:%s", self.ftp_host, self.ftp_port)
            
            async with aioftp.Client.context(self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass) as client:
                try:
                    await client.change_directory(folder)
                    logger.info("Успешно перешли в FTP директорию логов: '%s'", folder)
                except Exception as e:
                    logger.error("Не удалось перейти в папку логов '%s' через FTP: %s", folder, e)
                    return None

                adm_files = []
                async for path, stat in client.list():
                    if path.name.lower().endswith(".adm"):
                        modified_time = stat.get("modify", "0")
                        adm_files.append({"path": path, "name": path.name, "modified": modified_time})

                if not adm_files:
                    logger.warning("Файлы логов .adm не обнаружены на FTP в папке %s", folder)
                    return None

                # Сортируем файлы логов по дате изменения (самые свежие — первые)
                adm_files.sort(key=lambda x: x["modified"], reverse=True)
                
                # Берем до 3 самых свежих файлов логов для более глубокого поиска
                files_to_check = adm_files[:3]
                logger.info("Для поиска выбраны последние файлы логов: %s", [f["name"] for f in files_to_check])

                for file_data in files_to_check:
                    logger.info("Проверяем файл лога через FTP: %s", file_data["name"])
                    
                    # Скачиваем текущий файл во временную директорию
                    await client.download(file_data["path"], tmp_local_path, write_into=True)
                    
                    if os.path.exists(tmp_local_path):
                        with open(tmp_local_path, mode="r", encoding="utf-8", errors="replace") as f:
                            full_log_text = f.read()
                        os.remove(tmp_local_path)
                    else:
                        continue

                    # Построчно ищем строку с игроком
                    for line in full_log_text.splitlines():
                        match = pattern.search(line)
                        if match:
                            player_id = match.group(1)
                            logger.info("Игрок %s успешно найден в файле %s! ID: %s", psn_nick, file_data["name"], player_id)
                            return player_id

                logger.info("Игрок %s не найден ни в одном из %s последних логов.", psn_nick, len(files_to_check))
                return None

        except Exception as e:
            if os.path.exists(tmp_local_path):
                os.remove(tmp_local_path)
            logger.error("Ошибка при поиске игрока через aioftp: %s", e, exc_info=True)
            return None


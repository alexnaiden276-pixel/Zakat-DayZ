import logging
import re
import os
import aiohttp
import aioftp

logger = logging.getLogger(__name__)

# Регулярное выражение для поиска убийств в логах DayZ (.adm)
KILL_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2}) \| Player "(?P<killer>[^"]+)"\s*\(id=[^)]+\) killed Player "(?P<victim>[^"]+)"\s*\(id=[^)]+\) with (?P<weapon>[^ ]+) from (?P<distance>[0-9.]+)m',
    re.IGNORECASE
)

class NitradoAPI:
    def __init__(self, token: str, server_id: int, ftp_host: str = None, ftp_user: str = None, ftp_pass: str = None, ftp_port: int = 21) -> None:
        self.token = token
        self.server_id = server_id
        self.headers = {"Authorization": f"Bearer {token}"}
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
        чтобы гарантированно найти ID игрока.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы при инициализации класса!")
            return None

        pattern = re.compile(rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)', re.IGNORECASE)
        downloaded_files = list()

        try:
            logger.info("Асинхронное подключение к FTP Nitrado: %s:%s", self.ftp_host, self.ftp_port)
            
            async with aioftp.Client.context(self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass) as client:
                try:
                    await client.change_directory(folder)
                except Exception as e:
                    logger.error("Не удалось перейти в папку логов '%s' через FTP: %s", folder, e)
                    return None

                adm_files = list()
                async for path, stat in client.list():
                    if path.name.lower().endswith(".adm"):
                        modified_time = stat.get("modify", "0") or "0"
                        adm_files.append({"path": path, "name": path.name, "modified": str(modified_time)})

                if not adm_files:
                    logger.warning("Файлы логов .adm не обнаружены на FTP в папке %s", folder)
                    return None

                adm_files.sort(key=lambda x: x["modified"], reverse=True)
                files_to_check = adm_files[:3]

                for file_data in files_to_check:
                    # ИСПРАВЛЕНО: Скачиваем в /tmp/, указывая путь как директорию назначения в aioftp
                    await client.download(file_data["path"], "/tmp/")
                    
                    # Реальный путь к скачанному файлу на диске
                    real_local_path = os.path.join("/tmp", file_data["name"])
                    downloaded_files.append(real_local_path)
                    
                    if os.path.exists(real_local_path):
                        with open(real_local_path, mode="r", encoding="utf-8", errors="replace") as f:
                            for line in f:
                                match = pattern.search(line)
                                if match:
                                    player_id = match.group(1)
                                    logger.info("Игрок %s успешно найден! ID: %s", psn_nick, player_id)
                                    return player_id
                return None

        except Exception as e:
            logger.error("Ошибка при поиске игрока через aioftp: %s", e, exc_info=True)
            return None
        finally:
            for temp_file in downloaded_files:
                if os.path.exists(temp_file) and os.path.isfile(temp_file):
                    os.remove(temp_file)

    async def get_new_kills(self, last_processed_time: str | None, folder: str = "/dayzps/config") -> tuple[list[dict], str | None]:
        """
        ИСПРАВЛЕНО: Безопасное скачивание лога в папку /tmp/ с корректной зачисткой файла.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы при инициализации класса!")
            return list(), last_processed_time

        new_kills = list()
        current_last_time = last_processed_time
        real_local_path = None

        try:
            async with aioftp.Client.context(self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass) as client:
                try:
                    await client.change_directory(folder)
                except Exception as e:
                    logger.error("Не удалось перейти в папку логов через FTP для поиска киллов: %s", e)
                    return list(), last_processed_time

                adm_files = list()
                async for path, stat in client.list():
                    if path.name.lower().endswith(".adm"):
                        modified_time = stat.get("modify", "0") or "0"
                        adm_files.append({"path": path, "name": path.name, "modified": str(modified_time)})

                if not adm_files:
                    return list(), last_processed_time

                # Нам нужен строго один самый последний файл
                adm_files.sort(key=lambda x: x["modified"], reverse=True)
                latest_file = adm_files[0]

                # ИСПРАВЛЕНО: Скачиваем в папку /tmp/. Файл сохранится как /tmp/имя_файла.adm
                await client.download(latest_file["path"], "/tmp/")
                real_local_path = os.path.join("/tmp", latest_file["name"])

                if os.path.exists(real_local_path) and os.path.isfile(real_local_path):
                    with open(real_local_path, mode="r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            match = KILL_PATTERN.search(line)
                            if match:
                                data = match.groupdict()
                                
                                # Предотвращаем спам старыми убийствами
                                if last_processed_time and data["time"] <= last_processed_time:
                                    continue
                                
                                new_kills.append(data)
                                current_last_time = data["time"]

        except Exception as e:
            logger.error("Ошибка при парсинге логов убийств через aioftp: %s", e, exc_info=True)
        finally:
            # ИСПРАВЛЕНО: Надежная проверка перед удалением, что объект существует и является файлом
            if real_local_path and os.path.exists(real_local_path) and os.path.isfile(real_local_path):
                try:
                    os.remove(real_local_path)
                except Exception as clear_err:
                    logger.warning("Не удалось удалить временный файл логов %s: %s", real_local_path, clear_err)

        return new_kills, current_last_time


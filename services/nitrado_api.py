    async def find_player_id_in_logs(self, psn_nick: str) -> str | None:
        """
        Скачивает свежий .adm лог сервера DayZ и ищет полный ID игрока по PSN нику.
        """
        session = await self._get_session()
        
        # ИСПРАВЛЕНО: убрана "s" из gameservers -> правильно: gameserver
        list_url = f"https://nitrado.net{self.server_id}/gameserver/file_server/list"
        params = {"dir": "profiles"}
        
        try:
            async with session.get(list_url, params=params) as response:
                if response.status != 200:
                    logger.error("Не удалось получить список файлов Nitrado. Статус: %s", response.status)
                    return None
                data = await response.json()
                
                entries = data.get("data", {}).get("entries", [])
                # Ищем файлы, заканчивающиеся на .adm
                adm_files = [f for f in entries if f.get("name", "").endswith(".adm")]
                
                if not adm_files:
                    logger.warning("Файлы логов .adm не найдены в папке profiles.")
                    return None
                    
                # Сортируем файлы: самые свежие будут первыми
                adm_files.sort(key=lambda x: x.get("modified", 0), reverse=True)
                latest_log_path = adm_files[0].get("path")

            # ИСПРАВЛЕНО: убрана "s" из gameservers -> правильно: gameserver
            download_url = f"https://nitrado.net{self.server_id}/gameserver/file_server/download"
            async with session.get(download_url, params={"file": latest_log_path}) as response:
                if response.status != 200:
                    logger.error("Не удалось получить ссылку на скачивание лога. Статус: %s", response.status)
                    return None
                download_data = await response.json()
                direct_url = download_data.get("data", {}).get("url")

            # 3. Скачиваем содержимое лога
            async with session.get(direct_url) as response:
                if response.status != 200:
                    return None
                log_content = await response.text(encoding="utf-8", errors="ignore")

            # 4. Ищем регулярным выражением строку подключения
            pattern = rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)\s+connected'
            match = re.search(pattern, log_content, re.IGNORECASE)
            
            if match:
                return match.group(1)  # Возвращаем полный ID (UID)
                
            logger.info("Игрок %s не найден в скачанном логе.", psn_nick)
            return None

        except Exception as e:
            logger.error("Исключение при поиске ID игрока в логах Nitrado: %s", e)
            return None

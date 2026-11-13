    async def get_new_kills(
        self,
        last_file_name: str | None,
        last_line_hash: str | None,
        folder: str = "/dayzps/config",
    ) -> tuple[list[dict], str | None, str | None]:
        """
        Скачивает свежий .adm лог во временную папку, парсит новые убийства по хэшу строки
        и корректно удаляет временный файл. При смене файла дочитывает остаток предыдущего.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы при инициализации класса!")
            return [], last_file_name, last_line_hash

        downloaded_paths: list[str] = []
        temp_dir = tempfile.gettempdir()
        is_first_run = last_file_name is None and last_line_hash is None

        try:
            async with aioftp.Client.context(
                self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass
            ) as client:
                try:
                    await client.change_directory(folder)
                except Exception as e:
                    logger.error("Не удалось перейти в папку логов через FTP для поиска киллов: %s", e)
                    return [], last_file_name, last_line_hash

                adm_files = []
                async for path, stat in client.list():
                    if path.name.lower().endswith(".adm"):
                        modified_time = stat.get("modify", "0") or "0"
                        adm_files.append({"path": path, "name": path.name, "modified": str(modified_time)})

                if not adm_files:
                    logger.warning("[KILL_LOG] Файлы .adm не найдены в папке %s", folder)
                    return [], last_file_name, last_line_hash

                adm_files.sort(key=lambda x: (x["modified"], x["name"]), reverse=True)
                latest_file = adm_files[0]
                current_file_name = latest_file["name"]

                all_new_kills: list[dict] = []

                # 1. Если произошел рестарт и файл сменился — сначала дочитываем хвост старого файла
                if not is_first_run and last_file_name and current_file_name != last_file_name:
                    prev_file = next((f for f in adm_files if f["name"] == last_file_name), None)
                    if prev_file:
                        prev_local_path = os.path.join(temp_dir, f"kills_prev_{prev_file['name']}")
                        downloaded_paths.append(prev_local_path)
                        await client.download(prev_file["path"], prev_local_path, write_into=True)
                        if os.path.isfile(prev_local_path):
                            prev_kills, _ = await asyncio.to_thread(
                                _parse_adm_file, prev_local_path, last_line_hash, False
                            )
                            all_new_kills.extend(prev_kills)

                # 2. Читаем самый свежий файл
                effective_hash = last_line_hash if current_file_name == last_file_name else None
                real_local_path = os.path.join(temp_dir, f"kills_{current_file_name}")
                downloaded_paths.append(real_local_path)
                await client.download(latest_file["path"], real_local_path, write_into=True)

                if os.path.isfile(real_local_path):
                    new_kills, updated_hash = await asyncio.to_thread(
                        _parse_adm_file, real_local_path, effective_hash, is_first_run
                    )
                    all_new_kills.extend(new_kills)

                    if is_first_run:
                        logger.info(
                            "[KILL_LOG] Точка отсчета установлена: файл=%s, hash=%s",
                            current_file_name,
                            updated_hash,
                        )
                    else:
                        logger.info(
                            "[KILL_LOG] Проверка завершена: файл=%s, новых убийств=%d",
                            current_file_name,
                            len(all_new_kills),
                        )
                    return all_new_kills, current_file_name, updated_hash

        except Exception as e:
            logger.error("Ошибка при парсинге логов убийств через aioftp: %s", e, exc_info=True)
        finally:
            for tmp_path in downloaded_paths:
                if tmp_path and os.path.isfile(tmp_path):
                    with contextlib.suppress(OSError):
                        os.remove(tmp_path)

        return [], last_file_name, last_line_hash

    async def append_spawn_orders(
        self,
        items: list[dict],
        x: float,
        z: float,
        remote_xml_path: str = DEFAULT_SPAWN_XML_PATH,
    ) -> bool:
        """
        Скачивает XML спавна с FTP, дописывает координаты iZurvive для каждого купленного предмета
        и загружает обновленный файл обратно на сервер Nitrado (защищено блокировкой от гонки запросов).
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы!")
            return False

        async with self._spawn_xml_lock:
            local_tmp = os.path.join(tempfile.gettempdir(), f"shop_orders_spawn_{os.getpid()}.xml")
            try:
                async with aioftp.Client.context(
                    self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass
                ) as client:
                    try:
                        await client.download(remote_xml_path, local_tmp, write_into=True)
                        with open(local_tmp, mode="r", encoding="utf-8") as f:
                            xml_content = f.read()
                    except aioftp.StatusCodeError as ftp_err:
                        if "550" in str(ftp_err):
                            xml_content = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<eventposdef>\n</eventposdef>'
                        else:
                            raise

                    new_entries = []
                    for item in items:
                        classname = item["game_classname"]
                        qty = item.get("qty", 1)
                        for _ in range(qty):
                            offset_x = round(x + (len(new_entries) * 0.3), 2)
                            offset_z = round(z, 2)
                            new_entries.append(
                                f'    <event name="Item_{classname}">\n'
                                f'        <pos x="{offset_x}" z="{offset_z}" a="0" />\n'
                                f'    </event>'
                            )

                    entries_block = "\n".join(new_entries) + "\n"

                    if "</eventposdef>" in xml_content:
                        xml_content = xml_content.replace("</eventposdef>", f"{entries_block}</eventposdef>")
                    else:
                        xml_content += f"\n<eventposdef>\n{entries_block}</eventposdef>"

                    with open(local_tmp, mode="w", encoding="utf-8") as f:
                        f.write(xml_content)

                    await client.upload(local_tmp, remote_xml_path, write_into=True)
                    logger.info("Заказ успешно записан в %s на координаты X=%s Z=%s", remote_xml_path, x, z)
                    return True

            except Exception as e:
                logger.error("Ошибка записи заказа на FTP Nitrado: %s", e, exc_info=True)
                return False
            finally:
                if os.path.isfile(local_tmp):
                    with contextlib.suppress(OSError):
                        os.remove(local_tmp)

    async def clear_spawn_orders(
        self,
        remote_xml_path: str = DEFAULT_SPAWN_XML_PATH,
    ) -> bool:
        """Очищает файл заказов на FTP после рестарта сервера."""
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            return False

        async with self._spawn_xml_lock:
            local_tmp = os.path.join(tempfile.gettempdir(), f"shop_orders_clean_{os.getpid()}.xml")
            clean_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<eventposdef>\n</eventposdef>\n'
            try:
                with open(local_tmp, mode="w", encoding="utf-8") as f:
                    f.write(clean_xml)

                async with aioftp.Client.context(
                    self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass
                ) as client:
                    await client.upload(local_tmp, remote_xml_path, write_into=True)
                    logger.info("Файл заказов %s успешно очищен после рестарта сервера.", remote_xml_path)
                    return True
            except Exception as e:
                logger.error("Ошибка очистки файла заказов на FTP: %s", e)
                return False
            finally:
                if os.path.isfile(local_tmp):
                    with contextlib.suppress(OSError):
                        os.remove(local_tmp)

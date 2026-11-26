import array
import gzip
import logging
import os
import struct
import tempfile
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Абсолютный путь к папке data/ в корне проекта (не зависит от рабочей директории запуска)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


class TerrainHeightMap:
    """Быстрый поиск высоты поверхности (Y) по координатам (X, Z) для карты DayZ."""

    def __init__(
        self,
        gz_path: str | Path | None = None,
        zip_path: str | Path | None = None,
    ):
        self.gz_path = Path(gz_path) if gz_path else (DEFAULT_DATA_DIR / "chernarus_height.bin.gz")
        self.zip_path = Path(zip_path) if zip_path else (DEFAULT_DATA_DIR / "ChernarusPlus_Map.png.zip")
        self.width: int = 0
        self.height: int = 0
        self.min_x: float = 0.0
        self.min_z: float = 0.0
        self.step_x: float = 7.5
        self.step_z: float = 7.5
        self.heights_u16: array.array | None = None

    def _resolve_zip_path(self) -> Path | None:
        if self.zip_path.exists():
            return self.zip_path
        data_dir = self.zip_path.parent
        if data_dir.exists():
            zips = sorted(data_dir.glob("*.zip"))
            if zips:
                return zips[0]
        return None

    def _build_gz_from_zip(self) -> bool:
        """Конвертирует исходный .zip с .xyz файлом в компактный .bin.gz (~4.5 МБ)."""
        actual_zip = self._resolve_zip_path()
        if not actual_zip:
            logger.warning(
                "[TERRAIN] Архив с картой высот не найден в папке: %s",
                self.zip_path.parent,
            )
            return False

        logger.info("[TERRAIN] Конвертируем %s в компактный формат...", actual_zip)
        with zipfile.ZipFile(actual_zip, "r") as zf:
            xyz_names = [n for n in zf.namelist() if n.lower().endswith(".xyz")]
            if not xyz_names:
                logger.error("[TERRAIN] Внутри %s не найден .xyz файл", actual_zip)
                return False
            raw = zf.read(xyz_names[0])

        if len(raw) < 20:
            logger.error("[TERRAIN] Некорректный размер .xyz внутри архива: %d байт", len(raw))
            return False

        w, h = struct.unpack_from("<II", raw, 0)
        expected_size = 8 + (w * h * 12)
        if len(raw) != expected_size:
            logger.error(
                "[TERRAIN] Неожиданный размер .xyz: %d байт (ожидалось %d для сетки %dx%d)",
                len(raw),
                expected_size,
                w,
                h,
            )
            return False

        floats = array.array("f")
        floats.frombytes(raw[8:])

        row_off = w * 3
        y_idx = 1 if abs(floats[row_off + 2] - floats[2]) > abs(floats[row_off + 1] - floats[1]) else 2
        z_idx = 3 - y_idx

        x0, x1 = floats[0], floats[(w - 1) * 3]
        z0, z1 = floats[z_idx], floats[(h - 1) * row_off + z_idx]

        heights_u16 = array.array(
            "H",
            (max(0, min(65535, round((y + 500.0) * 20.0))) for y in floats[y_idx::3]),
        )
        header = struct.pack("<IIffff", w, h, x0, x1, z0, z1)

        self.gz_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.gz_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as raw_out:
                with gzip.GzipFile(fileobj=raw_out, mode="wb", compresslevel=6) as gz_out:
                    gz_out.write(header)
                    gz_out.write(heights_u16.tobytes())
            os.replace(tmp_path, self.gz_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

        logger.info(
            "[TERRAIN] Сжатый файл высот создан: %s (%.2f МБ)",
            self.gz_path,
            self.gz_path.stat().st_size / 1024 / 1024,
        )
        return True

    def load(self) -> bool:
        """Загружает сетку высот в оперативную память (~8 МБ RAM)."""
        logger.info("[TERRAIN] Проверяем карту высот по пути: %s", self.gz_path)
        if not self.gz_path.exists() and not self._build_gz_from_zip():
            return False

        with gzip.open(self.gz_path, "rb") as f:
            raw = f.read()

        if len(raw) < 24:
            logger.error("[TERRAIN] Повреждённый файл карты высот: %d байт", len(raw))
            return False

        w, h, x0, x1, z0, z1 = struct.unpack_from("<IIffff", raw, 0)
        expected_size = 24 + (w * h * 2)
        if len(raw) != expected_size:
            logger.error(
                "[TERRAIN] Неверный размер распакованной сетки: %d байт (ожидалось %d)",
                len(raw),
                expected_size,
            )
            return False

        arr = array.array("H")
        arr.frombytes(raw[24:])

        self.width = w
        self.height = h
        self.min_x = float(x0)
        self.min_z = float(z0)
        self.step_x = float(x1 - x0) / (w - 1) if w > 1 else 7.5
        self.step_z = float(z1 - z0) / (h - 1) if h > 1 else 7.5
        self.heights_u16 = arr

        logger.info(
            "[TERRAIN] Карта высот загружена: %dx%d, X=[%.1f..%.1f], Z=[%.1f..%.1f], шаг=(%.2f, %.2f) м",
            self.width,
            self.height,
            self.min_x,
            x1,
            self.min_z,
            z1,
            self.step_x,
            self.step_z,
        )
        return True

    @property
    def is_loaded(self) -> bool:
        return self.heights_u16 is not None and self.width >= 2 and self.height >= 2

    def _raw_y(self, ix: int, iz: int) -> float:
        return (self.heights_u16[iz * self.width + ix] / 20.0) - 500.0  # type: ignore[index]

    def get_height(self, x: float, z: float) -> float | None:
        if not self.is_loaded:
            return None

        fx = (x - self.min_x) / self.step_x
        fz = (z - self.min_z) / self.step_z

        if not (0.0 <= fx <= self.width - 1 and 0.0 <= fz <= self.height - 1):
            return None

        ix = min(int(fx), self.width - 2)
        iz = min(int(fz), self.height - 2)
        tx = fx - ix
        tz = fz - iz

        y00 = self._raw_y(ix, iz)
        y10 = self._raw_y(ix + 1, iz)
        y01 = self._raw_y(ix, iz + 1)
        y11 = self._raw_y(ix + 1, iz + 1)

        y0 = y00 * (1.0 - tx) + y10 * tx
        y1 = y01 * (1.0 - tx) + y11 * tx
        return round(y0 * (1.0 - tz) + y1 * tz, 3)

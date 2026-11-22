import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

_dd = os.getenv("DATA_DIR") or "data"
if not Path(_dd).is_absolute() and os.name != "nt" and Path("/data").is_dir():
    # a relative DATA_DIR inside the container lands on the throw-away filesystem: use the mounted volume
    _dd = "/data"
BASE_DATA_DIR = Path(_dd).resolve()  # the real folder: shared things (keys, models, ffmpeg) and tenant 1's data
BASE_DATA_DIR.mkdir(parents=True, exist_ok=True)

from . import tenant  # noqa: E402

tenant.set_base_dir(BASE_DATA_DIR)
# The data folder of whichever customer the running code is working for (see app/tenant.py). For the original
# installation (tenant 1) this is BASE_DATA_DIR itself, exactly as before.
DATA_DIR = tenant.TenantPath()

DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{(BASE_DATA_DIR / 'app.db').as_posix()}"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

BASE_URL = (os.getenv("BASE_URL") or "http://localhost:8000").rstrip("/")
IS_LOCAL = urlparse(BASE_URL).hostname in {"localhost", "127.0.0.1"} and not os.getenv("RAILWAY_ENVIRONMENT")

APP_USER = os.getenv("APP_USER") or "admin"
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
YT_API_KEY = os.getenv("YT_API_KEY", "")  # optional: faster/cleaner comment fetch

COOKIES_PATH = DATA_DIR / "cookies.txt"  # saved from the dashboard (Settings -> YouTube access)
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "")  # needed if YouTube bot-blocks the server IP
YT_PROXY = os.getenv("YT_PROXY", "")

UPLOAD_LEAD_HOURS = int(os.getenv("UPLOAD_LEAD_HOURS") or "48")
MAX_UPLOADS_PER_DAY = int(os.getenv("MAX_UPLOADS_PER_DAY") or "5")  # API quota: ~6/day by default


FONTS_DIR = Path(os.getenv("FONTS_DIR") or (Path(__file__).resolve().parent.parent / "fonts"))


def _default_font() -> str:
    for p in (
        str(FONTS_DIR / "NotoSans-Bold.ttf"),
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ):
        if Path(p).exists():
            return p
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


FONT_PATH = os.getenv("FONT_PATH", _default_font())


def ffmpeg_bin() -> str:
    env = os.getenv("FFMPEG_BIN")
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if exe.stem == "ffmpeg":
            return str(exe)
        # yt-dlp only recognises a binary literally named ffmpeg
        alias = BASE_DATA_DIR / "bin" / ("ffmpeg" + exe.suffix)
        if not alias.exists():
            alias.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(exe, alias)
        return str(alias)
    except ImportError as e:
        raise RuntimeError("ffmpeg not found: install it or `pip install imageio-ffmpeg`") from e

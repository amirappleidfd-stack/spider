import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
XRAY_DIR = os.path.join(BASE_DIR, "xray")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

DB_PATH = os.path.join(DATA_DIR, "spider.db")
XRAY_VERSION = "v26.3.27"
XRAY_DOWNLOAD_URL = "https://github.com/XTLS/Xray-core/releases/download/v26.3.27/Xray-linux-64.zip"
XRAY_BINARY = os.path.join(XRAY_DIR, "xray")
XRAY_CONFIG_PATH = os.path.join(XRAY_DIR, "config.json")
HOST = "0.0.0.0"
PORT = 8080
SECRET_KEY = os.environ.get("SECRET_KEY", "spider-panel-secret-change-me")
SESSION_COOKIE_NAME = "spider_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7
TOTAL_TRAFFIC_LIMIT_GB = 500
SUB_SOURCE_URL = "https://raw.githubusercontent.com/barry-far/V2ray-config/main/All_Configs_base64_Sub.txt"
THEMES = ["red", "green", "blue"]

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(XRAY_DIR, exist_ok=True)

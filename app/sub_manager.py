import base64, json, re, socket, struct, threading, time, urllib.parse
from typing import Optional
from io import BytesIO
import requests
import qrcode
from qrcode.image.svg import SvgPathImage
from sqlalchemy.orm import Session
from app.config import SUB_SOURCE_URL
from app.models import SubscriptionConfig

COUNTRY_MAP = {
    "US": ("🇺🇸", "United States"), "DE": ("🇩🇪", "Germany"), "FR": ("🇫🇷", "France"),
    "GB": ("🇬🇧", "United Kingdom"), "NL": ("🇳🇱", "Netherlands"), "JP": ("🇯🇵", "Japan"),
    "SG": ("🇸🇬", "Singapore"), "KR": ("🇰🇷", "South Korea"), "CA": ("🇨🇦", "Canada"),
    "AU": ("🇦🇺", "Australia"), "HK": ("🇭🇰", "Hong Kong"), "TW": ("🇹🇼", "Taiwan"),
    "RU": ("🇷🇺", "Russia"), "BR": ("🇧🇷", "Brazil"), "IN": ("🇮🇳", "India"),
    "IT": ("🇮🇹", "Italy"), "ES": ("🇪🇸", "Spain"), "SE": ("🇸🇪", "Sweden"),
    "NO": ("🇳🇴", "Norway"), "FI": ("🇫🇮", "Finland"), "DK": ("🇩🇰", "Denmark"),
    "PL": ("🇵🇱", "Poland"), "CZ": ("🇨🇿", "Czech Republic"), "AT": ("🇦🇹", "Austria"),
    "CH": ("🇨🇭", "Switzerland"), "BE": ("🇧🇪", "Belgium"), "RO": ("🇷🇴", "Romania"),
    "UA": ("🇺🇦", "Ukraine"), "TR": ("🇹🇷", "Turkey"), "IL": ("🇮🇱", "Israel"),
    "AE": ("🇦🇪", "UAE"), "ZA": ("🇿🇦", "South Africa"), "MX": ("🇲🇽", "Mexico"),
    "AR": ("🇦🇷", "Argentina"), "ID": ("🇮🇩", "Indonesia"), "MY": ("🇲🇾", "Malaysia"),
    "PH": ("🇵🇭", "Philippines"), "TH": ("🇹🇭", "Thailand"), "VN": ("🇻🇳", "Vietnam"),
    "EG": ("🇪🇬", "Egypt"), "NG": ("🇳🇬", "Nigeria"), "KE": ("🇰🇪", "Kenya"),
}

SERVER_COUNTRY_HINTS = {
    "germany": "DE", "deutschland": "DE", "frankfurt": "DE",
    "netherlands": "NL", "amsterdam": "NL",
    "united kingdom": "GB", "london": "GB", "uk ": "GB",
    "france": "FR", "paris": "FR",
    "united states": "US", "usa": "US", "new york": "US", "los angeles": "US", "miami": "US", "chicago": "US",
    "japan": "JP", "tokyo": "JP",
    "singapore": "SG", "korea": "KR", "south korea": "KR",
    "canada": "CA", "australia": "AU", "sydney": "AU",
    "hong kong": "HK", "taiwan": "TW",
    "russia": "RU", "moscow": "RU",
    "brazil": "BR", "india": "IN",
    "italy": "IT", "milan": "IT",
    "spain": "ES", "sweden": "SE", "norway": "NO",
    "finland": "FI", "denmark": "DK",
    "poland": "PL", "turkey": "TR", "israel": "IL",
    "uae": "AE", "dubai": "AE",
    "south africa": "ZA",
    "indonesia": "ID", "malaysia": "MY", "vietnam": "VN",
    "thailand": "TH", "philippines": "PH",
}

PRIVATE_RANGES = [
    ("10.0.0.0", "10.255.255.255"), ("172.16.0.0", "172.31.255.255"),
    ("192.168.0.0", "192.168.255.255"), ("127.0.0.0", "127.255.255.255"),
]

_progress = {"total": 0, "current": 0, "status": "idle", "last_update": time.time()}

def get_progress() -> dict:
    return dict(_progress)

def _ip_to_int(ip):
    return struct.unpack("!I", socket.inet_aton(ip))[0]

def _is_private(ip):
    try:
        ip_int = _ip_to_int(ip)
        for start, end in PRIVATE_RANGES:
            if _ip_to_int(start) <= ip_int <= _ip_to_int(end):
                return True
        return False
    except OSError:
        return True

def _resolve_country(host):
    try:
        ip = socket.gethostbyname(host)
        if not _is_private(ip):
            try:
                resp = requests.get(f"http://ip-api.com/json/{ip}?fields=countryCode,country", timeout=5)
                if resp.ok:
                    data = resp.json()
                    cc = data.get("countryCode", "")
                    if cc in COUNTRY_MAP:
                        return COUNTRY_MAP[cc]
                    if data.get("country"):
                        flag = chr(ord(cc[0]) + 0x1F1E6 - ord('A')) + chr(ord(cc[1]) + 0x1F1E6 - ord('A'))
                        return (flag, data.get("country", cc))
            except Exception:
                pass
    except (socket.gaierror, OSError):
        pass

    host_lower = host.lower()
    for keyword, cc in SERVER_COUNTRY_HINTS.items():
        if keyword in host_lower and cc in COUNTRY_MAP:
            return COUNTRY_MAP[cc]

    flag_pattern = re.compile(r'[\U0001F1E6-\U0001F1FF]{2}')
    flags_found = flag_pattern.findall(host)
    if flags_found:
        cc = ""
        for c in flags_found[0]:
            cc += chr(ord(c) - 0x1F1E6 + ord('A'))
        if cc in COUNTRY_MAP:
            return COUNTRY_MAP[cc]

    tld_map = {".de": "DE", ".fr": "FR", ".jp": "JP", ".uk": "GB", ".nl": "NL", ".ru": "RU",
               ".br": "BR", ".sg": "SG", ".hk": "HK", ".tw": "TW", ".kr": "KR", ".in": "IN",
               ".it": "IT", ".es": "ES", ".se": "SE", ".no": "NO", ".fi": "FI", ".dk": "DK",
               ".pl": "PL", ".cz": "CZ", ".at": "AT", ".ch": "CH", ".be": "BE", ".tr": "TR",
               ".za": "ZA", ".mx": "MX", ".ar": "AR", ".id": "ID", ".my": "MY", ".ph": "PH",
               ".th": "TH", ".vn": "VN", ".il": "IL", ".ae": "AE"}
    for tld, cc in tld_map.items():
        if host_lower.endswith(tld) and cc in COUNTRY_MAP:
            return COUNTRY_MAP[cc]

    return ("🌍", "Unknown")

def decode_base64_safe(data):
    try:
        missing = len(data) % 4
        if missing:
            data += "=" * (4 - missing)
        return base64.b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        try:
            return base64.b64decode(data).decode("utf-8", errors="replace")
        except Exception:
            return data

def parse_vmess_link(raw_link):
    try:
        b64_part = raw_link.replace("vmess://", "", 1).strip()
        decoded = decode_base64_safe(b64_part)
        data = json.loads(decoded)
        return {"protocol": "vmess", "server": data.get("add", ""), "port": int(data.get("port", 0)),
                "id": data.get("id"), "aid": data.get("aid", "0"), "net": data.get("net", ""),
                "type": data.get("type", ""), "tls": data.get("tls", ""), "path": data.get("path", ""),
                "host": data.get("host", ""), "sni": data.get("sni", ""), "raw": raw_link}
    except Exception:
        return None

def parse_vless_link(raw_link):
    try:
        parsed = urllib.parse.urlparse(raw_link)
        user_info = parsed.netloc.split("@")
        if len(user_info) < 2:
            return None
        server_part = user_info[1]
        server = server_part.rsplit(":", 1)[0] if ":" in server_part else server_part
        port_str = server_part.rsplit(":", 1)[1] if ":" in server_part else "443"
        port = int(port_str) if port_str.isdigit() else 443
        query = urllib.parse.parse_qs(parsed.query)
        return {"protocol": "vless", "server": server, "port": port, "id": user_info[0],
                "flow": query.get("flow", [""])[0], "encryption": query.get("encryption", ["none"])[0],
                "type": query.get("type", [""])[0], "security": query.get("security", [""])[0],
                "sni": query.get("sni", [""])[0], "path": query.get("path", [""])[0],
                "host": query.get("host", [""])[0], "fp": query.get("fp", [""])[0],
                "pbk": query.get("pbk", [""])[0], "sid": query.get("sid", [""])[0], "raw": raw_link}
    except Exception:
        return None

def parse_trojan_link(raw_link):
    try:
        parsed = urllib.parse.urlparse(raw_link)
        password = parsed.password or (parsed.netloc.split("@")[0] if "@" in parsed.netloc else "")
        host_part = parsed.hostname or (parsed.netloc.split("@")[1] if "@" in parsed.netloc else "")
        port = parsed.port or 443
        query = urllib.parse.parse_qs(parsed.query)
        return {"protocol": "trojan", "server": host_part, "port": port, "password": password,
                "sni": query.get("sni", [""])[0], "type": query.get("type", [""])[0],
                "path": query.get("path", [""])[0], "host": query.get("host", [""])[0],
                "security": query.get("security", ["tls"])[0], "raw": raw_link}
    except Exception:
        return None

def parse_ss_link(raw_link):
    try:
        parsed = urllib.parse.urlparse(raw_link)
        if parsed.netloc and "@" in parsed.netloc:
            user_info = parsed.netloc.split("@")
            b64_part = user_info[0]
            decoded = decode_base64_safe(b64_part)
            method, password = decoded.split(":", 1) if ":" in decoded else ("aes-256-gcm", decoded)
            host_part = user_info[1]
            host = host_part.rsplit(":", 1)[0] if ":" in host_part else host_part
            port_str = host_part.rsplit(":", 1)[1] if ":" in host_part else "443"
            port = int(port_str) if port_str.isdigit() else 443
        else:
            return None
        query = urllib.parse.parse_qs(parsed.query)
        return {"protocol": "shadowsocks", "server": host, "port": port, "method": method,
                "password": password, "plugin": query.get("plugin", [""])[0], "raw": raw_link}
    except Exception:
        return None

def parse_any_link(raw_link):
    link = raw_link.strip()
    if link.startswith("vmess://"):
        return parse_vmess_link(link)
    elif link.startswith("vless://"):
        return parse_vless_link(link)
    elif link.startswith("trojan://"):
        return parse_trojan_link(link)
    elif link.startswith("ss://"):
        return parse_ss_link(link)
    return None

def refresh_configs(db: Session) -> dict:
    global _progress
    _progress = {"total": 0, "current": 0, "status": "downloading", "last_update": time.time()}
    try:
        print("[sub] Fetching subscription data...")
        resp = requests.get(SUB_SOURCE_URL, timeout=60)
        resp.raise_for_status()
        raw_text = resp.text.strip()
        print(f"[sub] Raw data length: {len(raw_text)} chars")

        try:
            decoded = decode_base64_safe(raw_text)
            lines = decoded.splitlines()
        except Exception:
            lines = raw_text.splitlines()

        config_links = []
        for line in lines:
            line = line.strip()
            if any(line.startswith(p) for p in ["vmess://", "vless://", "trojan://", "ss://"]):
                config_links.append(line)

        print(f"[sub] Found {len(config_links)} config links")
        _progress["total"] = len(config_links)
        _progress["status"] = "processing"

        db.query(SubscriptionConfig).delete()
        db.commit()

        new_configs = []
        for i, link in enumerate(config_links):
            _progress["current"] = i + 1
            _progress["last_update"] = time.time()
            parsed = parse_any_link(link)
            if not parsed:
                continue
            flag, country = _resolve_country(parsed["server"])
            if country == "Unknown":
                name = f"🔄 Server - {i + 1:02d}"
            else:
                name = f"{flag} {country} - {i + 1:02d}"
            config = SubscriptionConfig(
                name=name, protocol=parsed["protocol"], config_data=link,
                country=country, country_flag=flag,
                server=parsed["server"], port=parsed["port"], is_active=True,
            )
            new_configs.append(config)
            if len(new_configs) % 50 == 0:
                db.add_all(new_configs)
                db.commit()
                new_configs = []

        if new_configs:
            db.add_all(new_configs)
            db.commit()

        _progress["status"] = "done"
        total = db.query(SubscriptionConfig).count()
        print(f"[sub] Refresh complete: {total} configs stored")
        return {"success": True, "total": total}
    except Exception as e:
        _progress["status"] = f"error: {e}"
        print(f"[sub] Error: {e}")
        return {"success": False, "error": str(e)}

def get_all_configs(db: Session) -> list[dict]:
    configs = db.query(SubscriptionConfig).filter(SubscriptionConfig.is_active == True).all()
    return [{"id": c.id, "name": c.name, "protocol": c.protocol, "server": c.server,
             "port": c.port, "country": c.country, "country_flag": c.country_flag,
             "config_data": c.config_data} for c in configs]

def generate_qr_svg(config_link: str) -> str:
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(config_link)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    buffer = BytesIO()
    img.save(buffer)
    return buffer.getvalue().decode("utf-8")

def generate_subscription_url(db: Session, export_format: str = "v2ray") -> str:
    configs = db.query(SubscriptionConfig).filter(SubscriptionConfig.is_active == True).all()
    links = [c.config_data for c in configs]
    combined = "\n".join(links)
    if export_format == "base64":
        return base64.b64encode(combined.encode()).decode()
    return combined

def generate_clash_config(db: Session) -> str:
    import yaml
    configs = db.query(SubscriptionConfig).filter(SubscriptionConfig.is_active == True).all()
    proxies = []
    for c in configs:
        parsed = parse_any_link(c.config_data)
        if not parsed:
            continue
        proxy = {"name": c.name, "type": "vmess" if parsed["protocol"] == "vmess" else parsed.get("protocol", ""),
                 "server": parsed["server"], "port": parsed["port"]}
        if parsed["protocol"] == "vmess":
            proxy["uuid"] = parsed.get("id", "")
            proxy["alterId"] = int(parsed.get("aid", 0))
            proxy["cipher"] = "auto"
            if parsed.get("tls") == "tls":
                proxy["tls"] = True
        elif parsed["protocol"] == "vless":
            proxy["uuid"] = parsed.get("id", "")
            proxy["tls"] = parsed.get("security") == "tls"
        elif parsed["protocol"] == "trojan":
            proxy["password"] = parsed.get("password", "")
            proxy["tls"] = True
        elif parsed["protocol"] == "shadowsocks":
            proxy["password"] = parsed.get("password", "")
            proxy["cipher"] = parsed.get("method", "aes-256-gcm")
        proxies.append(proxy)
    clash = {"port": 7890, "socks-port": 7891, "redir-port": 7892, "allow-lan": True,
             "mode": "Rule", "log-level": "info", "proxies": proxies,
             "proxy-groups": [{"name": "Proxy", "type": "select", "proxies": [p["name"] for p in proxies]}],
             "rules": ["MATCH,Proxy"]}
    return yaml.dump(clash, default_flow_style=False)

def generate_singbox_config(db: Session) -> str:
    configs = db.query(SubscriptionConfig).filter(SubscriptionConfig.is_active == True).all()
    outbounds = []
    for c in configs:
        parsed = parse_any_link(c.config_data)
        if not parsed:
            continue
        outbound = {"tag": c.name, "type": "vmess" if parsed["protocol"] == "vmess" else parsed.get("protocol", ""),
                    "server": parsed["server"], "server_port": parsed["port"]}
        if parsed["protocol"] == "vmess":
            outbound["uuid"] = parsed.get("id", "")
            outbound["security"] = "auto"
            outbound["alter_id"] = int(parsed.get("aid", 0))
            if parsed.get("tls") == "tls":
                outbound["tls"] = {"enabled": True}
        elif parsed["protocol"] == "vless":
            outbound["uuid"] = parsed.get("id", "")
            outbound["flow"] = parsed.get("flow", "")
            if parsed.get("security") == "tls":
                outbound["tls"] = {"enabled": True}
        elif parsed["protocol"] == "trojan":
            outbound["password"] = parsed.get("password", "")
            outbound["tls"] = {"enabled": True}
        elif parsed["protocol"] == "shadowsocks":
            outbound["method"] = parsed.get("method", "aes-256-gcm")
            outbound["password"] = parsed.get("password", "")
        outbounds.append(outbound)
    config = {"log": {"level": "info"},
              "inbounds": [{"type": "mixed", "listen": "127.0.0.1", "listen_port": 2080}],
              "outbounds": outbounds}
    return json.dumps(config, indent=2, ensure_ascii=False)
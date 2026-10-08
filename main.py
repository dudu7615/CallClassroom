"""CallClassroom 启动入口。

    uv run main.py                 # http://0.0.0.0:8000
    uv run main.py --tls           # 自动生成自签证书，走 https
    uv run main.py --port 9000

浏览器只在**安全上下文**下开放麦克风与 AudioWorklet：localhost 或者 https。
从别的设备用 http://<局域网IP>:8000 访问会被直接拒绝，所以跨设备使用时加 --tls。
"""

from __future__ import annotations

import argparse
import socket
import subprocess
from pathlib import Path

import uvicorn
from loguru import logger

from modules.logsetup import setup_logging

CERT_DIR = Path(__file__).resolve().parent / ".certs"


def local_ips() -> list[str]:
    """列出本机的局域网 IPv4 地址，用来提示可直接访问的 URL。"""
    ips: list[str] = []
    try:
        # 连一个不存在的地址不会真的发包，只是让内核选出默认出口网卡
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            ips.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def ensure_cert(host: str) -> tuple[Path, Path]:
    """生成自签证书（若不存在），返回 (cert, key)。"""
    CERT_DIR.mkdir(exist_ok=True)
    cert, key = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    if cert.exists() and key.exists():
        return cert, key

    alt = ",".join(["DNS:localhost", "IP:127.0.0.1", *[f"IP:{ip}" for ip in local_ips()]])
    cmd = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "825",
        "-keyout",
        str(key),
        "-out",
        str(cert),
        "-subj",
        f"/CN={host or 'callclassroom'}",
        "-addext",
        f"subjectAltName={alt}",
    ]
    logger.info("生成自签证书：{}", cert)
    subprocess.run(cmd, check=True, capture_output=True)
    return cert, key


def announce(port: int, *, tls: bool) -> None:
    scheme = "https" if tls else "http"
    lines = ["CallClassroom 已启动"]
    lines.extend(f"    {scheme}://{target}:{port}" for target in ["localhost", *local_ips()])
    logger.info("\n{}", "\n".join(lines))
    if not tls:
        logger.warning(
            "从其他设备用局域网 IP 访问时浏览器会禁用麦克风（非安全上下文）；"
            "跨设备请改用 --tls，或在 chrome://flags 里把本站加入 "
            "unsafely-treat-insecure-origin-as-secure 白名单"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="CallClassroom 教室对讲服务端")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("--tls", action="store_true", help="生成自签证书并以 https 启动")
    parser.add_argument("--cert", type=Path, help="指定证书文件，自动启用 https")
    parser.add_argument("--key", type=Path, help="指定私钥文件")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    cert, key = args.cert, args.key
    if args.tls and cert is None:
        try:
            cert, key = ensure_cert(args.host)
        except Exception as exc:
            logger.warning("TLS 启动失败，已回退到 http：{}", exc)
            cert, key = None, None

    if cert is not None and key is None:
        parser.error("--cert 需要同时提供 --key")

    announce(args.port, tls=cert is not None)

    uvicorn.run(
        "modules.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="debug" if args.verbose else "info",
        # 关掉 uvicorn 自己的 logging 配置，否则它启动时会用 dictConfig
        # 重置 uvicorn.* 的 logger，把我们挂的 loguru 转发 handler 冲掉
        log_config=None,
        ssl_certfile=str(cert) if cert else None,
        ssl_keyfile=str(key) if key else None,
    )


if __name__ == "__main__":
    main()

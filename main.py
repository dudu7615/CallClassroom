"""CallClassroom 启动入口。

    uv run main.py                 # 默认自动签自签证书，https://0.0.0.0:8000
    uv run main.py --no-tls        # 退回明文 http，只在教室电脑本机调试时够用
    uv run main.py --port 9000

浏览器只在**安全上下文**下开放麦克风与 AudioWorklet：localhost 或者 https。
从别的设备用 http://<局域网IP>:8000 访问会被直接拒绝，所以默认就走 https。
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import socket
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from loguru import logger

from modules.logsetup import setup_logging

CERT_DIR = Path(__file__).resolve().parent / ".certs"

#: 自签证书的有效期。不求真"永久"（有效期离谱的证书浏览器会报错），十年足够
#: 覆盖一台教室电脑的服役期，而且生成一次就不再重签。
CERT_DAYS = 3650


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


def san_names(host: str) -> list[x509.GeneralName]:
    """证书要覆盖的所有名字：回环固定带上，其余按当前网卡实际枚举。

    自签证书是一次生成长期复用的，所以生成时把**当前**地址全写进 SAN；
    以后地址变了由 :func:`ensure_cert` 发现并重签。
    """
    dns = {"localhost"}
    ips = {"127.0.0.1"}
    if host and host not in {"0.0.0.0", "::"}:
        try:
            ips.add(str(ipaddress.ip_address(host)))
        except ValueError:
            dns.add(host)
    ips.update(local_ips())

    names: list[x509.GeneralName] = [x509.DNSName(n) for n in sorted(dns)]
    names.extend(x509.IPAddress(ipaddress.ip_address(n)) for n in sorted(ips))
    return names


def covers(cert: x509.Certificate, wanted: list[x509.GeneralName]) -> bool:
    """证书没过期、且 SAN 覆盖了 ``wanted`` 里的全部名字。"""
    if cert.not_valid_after_utc <= dt.datetime.now(dt.UTC):
        return False
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    want_dns = {n.value for n in wanted if isinstance(n, x509.DNSName)}
    want_ip = {str(n.value) for n in wanted if isinstance(n, x509.IPAddress)}
    have_dns: set[str] = set(san.get_values_for_type(x509.DNSName))
    have_ip = {str(addr) for addr in san.get_values_for_type(x509.IPAddress)}
    return want_dns <= have_dns and want_ip <= have_ip


def ensure_cert(host: str) -> tuple[Path, Path]:
    """返回自签证书的路径，没有或不再适用时才生成一张，之后一直复用。

    生成一次就用到底：重签会让浏览器里点过的"继续访问"失效。只有三种情况才
    重新生成——证书过期、文件损坏、或者当前网卡地址不在它的 SAN 里（换了网络）。

    用 ``cryptography`` 库而不是外部 ``openssl`` 命令：Windows 默认没有
    openssl，调用它会抛错并让 ``--tls`` 静默退化成 http。
    """
    CERT_DIR.mkdir(exist_ok=True)
    cert_path, key_path = CERT_DIR / "cert.pem", CERT_DIR / "key.pem"
    wanted = san_names(host)

    if cert_path.exists() and key_path.exists():
        try:
            existing = x509.load_pem_x509_certificate(cert_path.read_bytes())
        except ValueError as exc:
            logger.warning("已有证书无法解析（{}），重新生成", exc)
        else:
            if covers(existing, wanted):
                return cert_path, key_path
            logger.warning("已有证书已过期或不覆盖当前地址，重新生成")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "callclassroom")])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # 生效时间往前挪几分钟，容忍客户端时钟略慢
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=CERT_DAYS))
        .add_extension(x509.SubjectAlternativeName(wanted), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    logger.info("已生成自签证书（有效期 {} 天）：{}", CERT_DAYS, cert_path)
    return cert_path, key_path


def announce(port: int, *, tls: bool) -> None:
    scheme = "https" if tls else "http"
    lines = ["CallClassroom 已启动"]
    lines.extend(f"    {scheme}://{target}:{port}" for target in ["localhost", *local_ips()])
    logger.info("\n{}", "\n".join(lines))
    if tls:
        logger.info("证书是自签的，浏览器首次打开会提示「不安全」，点「继续前往」即可")
    else:
        logger.warning(
            "从其他设备用局域网 IP 访问时浏览器会禁用麦克风（非安全上下文）；"
            "跨设备请去掉 --no-tls，或在 chrome://flags 里把本站加入 "
            "unsafely-treat-insecure-origin-as-secure 白名单"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="CallClassroom 教室对讲服务端")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument(
        "--tls",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="https + 自签证书（默认开启）。--no-tls 退回明文 http，仅本机调试用",
    )
    parser.add_argument("--cert", type=Path, help="指定证书文件，自动启用 https")
    parser.add_argument("--key", type=Path, help="指定私钥文件")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    cert, key = args.cert, args.key
    if not args.tls:
        if cert is not None:
            parser.error("--no-tls 与 --cert/--key 冲突：证书只在 https 下才有意义")
    elif cert is None:
        # 不降级到 http：生成失败就是真出错，而静默降级会让跨设备访问的麦克风
        # 被浏览器禁用（非安全上下文），正是默认开 https 想避免的事
        cert, key = ensure_cert(args.host)

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

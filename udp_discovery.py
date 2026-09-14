"""
UDP 广播 + 多播发现 — 内网组队发现机制
Hub 启动后在 LAN 广播心跳，同时监听其他 Hub 的广播。
双通道：有限广播(255.255.255.255，跨机器) + 多播组(239.255.43.21，同机可环回验证)
"""
import socket
import json
import threading
import time
from datetime import datetime

BROADCAST_PORT = 30601
BROADCAST_INTERVAL = 5  # 秒
MULTICAST_GROUP = "239.255.43.21"


class UDPDiscovery:
    """UDP 广播/多播发现服务"""

    def __init__(self, hub_id: str, hostname: str, user_name: str, hub_port: int = 3060):
        self.hub_id = hub_id
        self.hostname = hostname
        self.user_name = user_name
        self.hub_port = hub_port
        self._running = False
        self._peers: dict[str, dict] = {}  # hub_id → peer info
        self._lock = threading.Lock()
        self._thread = None

    @property
    def peers(self) -> list[dict]:
        """返回当前发现的 peer 列表（去重，30s 无心跳自动清理；static 模式永不过期）"""
        with self._lock:
            now = time.time()
            if getattr(self, "mode", "multicast") != "static":
                stale = [hid for hid, p in self._peers.items() if now - p["_last_seen"] > 30]
                for hid in stale:
                    del self._peers[hid]
            return [
                {"hub_id": hid, "hostname": p["hostname"], "user_name": p["user_name"],
                 "ip": p["ip"], "port": p["port"]}
                for hid, p in self._peers.items()
            ]

    @classmethod
    def get_peers(cls) -> list:
        """兼容旧调用：无实例时返回空"""
        return []

    def start(self):
        """启动 UDP 广播和监听"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _make_socket(self):
        """创建监听 socket：绑定端口 + 加入多播组（同机多实例可互相发现）"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", BROADCAST_PORT))
            # 加入多播组（同机多实例 + 跨机均可见）
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton("0.0.0.0"))
            except OSError:
                pass
            sock.settimeout(1.0)
            return sock
        except OSError:
            sock.close()
            return None

    def _run(self):
        sock = self._make_socket()
        # 发送 socket（独立，广播/多播发送不需要绑定监听端口）
        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except OSError:
            pass

        last_broadcast = 0
        while self._running:
            now = time.time()

            if now - last_broadcast >= BROADCAST_INTERVAL:
                self._broadcast(send_sock)
                last_broadcast = now

            if sock:
                try:
                    data, addr = sock.recvfrom(4096)
                    self._handle(data, addr)
                except socket.timeout:
                    pass
                except OSError:
                    pass

        if sock:
            sock.close()
        send_sock.close()

    def _broadcast(self, sock):
        msg = json.dumps({
            "type": "xingshu_hello",
            "hub_id": self.hub_id,
            "hostname": self.hostname,
            "user_name": self.user_name,
            "port": self.hub_port,
        }).encode()
        # 有限广播（跨机器）
        try:
            sock.sendto(msg, ("255.255.255.255", BROADCAST_PORT))
        except OSError:
            pass
        # 多播（同机环回 + 跨机器）
        try:
            sock.sendto(msg, (MULTICAST_GROUP, BROADCAST_PORT))
        except OSError:
            pass

    def _handle(self, data: bytes, addr: tuple):
        try:
            msg = json.loads(data.decode())
        except json.JSONDecodeError:
            return
        if msg.get("type") != "xingshu_hello":
            return
        hub_id = msg.get("hub_id", "")
        if hub_id == self.hub_id:
            return  # 忽略自己的广播
        with self._lock:
            self._peers[hub_id] = {
                "hostname": msg.get("hostname", "?"),
                "user_name": msg.get("user_name", "?"),
                "ip": addr[0],
                "port": msg.get("port", 3060),
                "_last_seen": time.time(),
            }

"""单实例与窗口唤出。

Windows 应用的常规行为：第二次启动时不新开窗口，而是把已有窗口提到前台。
这里用两步实现：

* **命名互斥体**（:class:`~offline_oj.win32.single_instance.InstanceLock`）判定"是否已有实例"，
  进程退出时由操作系统自动释放，不会因为崩溃而留下"幽灵锁"；
* **本地套接字**（Windows 下即命名管道）把"请激活窗口"的消息传给第一个实例。

只用两者之一都不够：只有互斥体就不知道窗口句柄，只有套接字则在第一个实例崩溃后
可能残留管道名。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ..win32.single_instance import InstanceLock

log = logging.getLogger(__name__)

#: 本地套接字名（命名管道名）
PIPE_NAME = "OfflineOJ.Activate.v2"
#: 激活消息
MESSAGE_ACTIVATE = b"ACTIVATE"
#: 收发超时（毫秒）
TIMEOUT_MS = 800


class SingleInstanceGuard(QObject):
    """保证同一时间只有一个实例，并支持唤出已有窗口。"""

    #: 收到其它实例的激活请求
    activation_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = InstanceLock()
        self._server: QLocalServer | None = None
        self._is_primary = False

    # ---- 对外 -------------------------------------------------------------

    @property
    def is_primary(self) -> bool:
        """本进程是否是唯一实例。"""
        return self._is_primary

    def acquire(self) -> bool:
        """尝试成为唯一实例。

        :return: ``True`` 表示本进程是主实例，应该继续启动；
                 ``False`` 表示已有实例在运行（调用方应通知它后退出）。
        """
        if not self._lock.acquire():
            return False

        # 拿不到实时锁说明没有别的实例；顺手清理上次崩溃可能残留的管道名
        QLocalServer.removeServer(PIPE_NAME)
        server = QLocalServer(self)
        if not server.listen(PIPE_NAME):
            # 监听失败不影响使用，只是失去了"唤出"能力
            log.warning("无法监听激活管道: %s", server.errorString())
        else:
            server.newConnection.connect(self._on_connection)
            self._server = server
        self._is_primary = True
        return True

    def notify_existing(self) -> bool:
        """请求已有实例把窗口提到前台。"""
        socket = QLocalSocket()
        socket.connectToServer(PIPE_NAME)
        if not socket.waitForConnected(TIMEOUT_MS):
            log.info("未能连接到已有实例: %s", socket.errorString())
            return False
        socket.write(MESSAGE_ACTIVATE)
        socket.flush()
        socket.waitForBytesWritten(TIMEOUT_MS)
        socket.disconnectFromServer()
        if socket.state() != QLocalSocket.UnconnectedState:
            socket.waitForDisconnected(TIMEOUT_MS)
        return True

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(PIPE_NAME)
            self._server = None
        self._lock.release()

    # ---- 内部 -------------------------------------------------------------

    def _on_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            socket.readyRead.connect(lambda s=socket: self._on_ready_read(s))
            socket.disconnected.connect(socket.deleteLater)

    def _on_ready_read(self, socket: QLocalSocket) -> None:
        payload = bytes(socket.readAll().data()).strip()
        if payload == MESSAGE_ACTIVATE:
            log.info("收到另一个实例的激活请求")
            self.activation_requested.emit()
        socket.disconnectFromServer()

"""Cancellable bounded response-header acquisition for a fixed upstream URL."""
from __future__ import annotations

import http.client
import socket
import threading
import time
import urllib.request

from . import pinned

OPEN_SLOTS = threading.BoundedSemaphore(2)


def open_pinned(offset, cancelled=lambda: False, timeout=30):
    from .backend import NoRedirect, WeChatError, check_cancel, require
    check_cancel(cancelled)
    require(OPEN_SLOTS.acquire(blocking=False), "上次连接仍在结束，请稍后重试")
    connections = []
    abandoned = threading.Event()
    completed = threading.Event()
    lock = threading.Lock()
    result = [None, None]

    def tracked(connection_type, host, **kwargs):
        class Connection(connection_type):
            def connect(self):
                super().connect()
                if abandoned.is_set():
                    self.close()
                    raise WeChatError("连接已取消")
        connection = Connection(host, **kwargs)
        connections.append(connection)
        return connection

    class HTTP(urllib.request.HTTPHandler):
        def http_open(self, request):
            return self.do_open(lambda host, **kwargs: tracked(http.client.HTTPConnection, host, **kwargs), request)

    class HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, request):
            options = {"context": self._context}
            if hasattr(self, "_check_hostname"):
                options["check_hostname"] = self._check_hostname
            return self.do_open(lambda host, **kwargs: tracked(http.client.HTTPSConnection, host, **kwargs), request, **options)

    headers = {"User-Agent": "TypixWeChat/0.1", "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(pinned.URL, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), HTTP(), HTTPS(), NoRedirect())

    def work():
        response, error = None, None
        try:
            response = opener.open(request, timeout=10)
        except Exception as exc:
            error = exc
        finally:
            with lock:
                if abandoned.is_set():
                    if response:
                        response.close()
                    if hasattr(error, "close"):
                        error.close()
                else:
                    result[:] = [response, error]
                completed.set()
            OPEN_SLOTS.release()

    threading.Thread(target=work, name="wechat-download-headers", daemon=True).start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            check_cancel(cancelled)
            require(time.monotonic() < deadline, "等待官方下载响应超时，请稍后重试")
            if completed.wait(0.05):
                with lock:
                    response, error = result
                    result[:] = [None, None]
                if error:
                    if hasattr(error, "close"):
                        error.close()
                    raise error
                return response
    except BaseException:
        with lock:
            abandoned.set()
            response, error = result
            result[:] = [None, None]
        # Closing the connection alone does not interrupt a header read through
        # makefile(); shutdown first. Never leave unbounded background workers.
        for connection in tuple(connections):
            if connection.sock:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        if response:
            response.close()
        if hasattr(error, "close"):
            error.close()
        raise

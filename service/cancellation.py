"""单次任务执行的取消作用域；不扫描或终止本进程外的用户 CLI。"""
from contextvars import ContextVar
import threading
import time


class TaskCancelled(BaseException):
    """不能被节点的普通失败重试吞掉；宿主明确处理为 interrupted。"""


current_cancellation = ContextVar("writing_cancellation", default=None)


def check_cancelled():
    token = current_cancellation.get()
    if token is not None:
        token.check()


class CancellationToken:
    def __init__(self):
        self.event = threading.Event()
        self._lock = threading.RLock()
        self._processes = {}
        self._requests = {}

    def check(self):
        if self.event.is_set():
            raise TaskCancelled("任务已取消；保留断点，不再发起模型请求")

    def request_cancel(self):
        self.event.set()

    def track_process(self, process):
        with self._lock:
            self._processes[id(process)] = process

    def untrack_process(self, process):
        with self._lock:
            self._processes.pop(id(process), None)

    def live_process_count(self):
        with self._lock:
            # 根进程退出不代表继承管道的后代已收尾；由调用器完成清理后移除。
            return len(self._processes)

    def request_started(self, settle):
        with self._lock:
            self.check()
            self._requests[threading.get_ident()] = {"started": time.monotonic(), "credited": 0.0, "settle": settle}

    def request_finished(self, elapsed):
        with self._lock:
            record = self._requests.pop(threading.get_ident(), None)
            remaining = max(0.0, elapsed - (record["credited"] if record else 0))
        if record:
            record["settle"](remaining)

    def settle_inflight(self):
        """退出不等API数分钟：先结算到当前时间，晚返回只补未记的差额。"""
        pending = []
        with self._lock:
            now = time.monotonic()
            for record in self._requests.values():
                total = max(0.0, now - record["started"])
                pending.append((record["settle"], max(0.0, total - record["credited"])))
                record["credited"] = total
        for settle, elapsed in pending:
            settle(elapsed)

    def cancel(self):
        self.request_cancel()
        self.settle_inflight()

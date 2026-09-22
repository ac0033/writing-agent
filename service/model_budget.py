"""每次真实模型请求都计入宿主预算，失败和重试不因checkpoint回滚而消失。"""
from contextlib import contextmanager
from contextvars import ContextVar
import time

budget_observer = ContextVar("writing_model_budget_observer", default=None)


class BudgetExceeded(RuntimeError):
    pass


@contextmanager
def model_request(provider):
    observer = budget_observer.get()
    if observer:
        observer("start", provider, 0)
    began = time.monotonic()
    try:
        yield
    finally:
        if observer:
            observer("finish", provider, time.monotonic() - began)


def observer_for(task, persist, guard):
    def observe(event, provider, elapsed):
        import config
        with guard():
            if event == "start":
                calls = task.get("model_calls", 0)
                seconds = task.get("model_seconds", 0)
                if calls >= config.AI_OS_MAX_MODEL_CALLS + task.get("additional_model_calls", 0):
                    raise BudgetExceeded("全篇模型调用预算耗尽，需明确增加预算后续跑")
                if seconds >= config.AI_OS_MAX_SECONDS + task.get("additional_seconds", 0) + task.get("additional_graph_seconds", 0):
                    raise BudgetExceeded("全篇模型执行时间预算耗尽，需明确增加预算后续跑")
                task["model_calls"] = calls + 1
            else:
                task["model_seconds"] = task.get("model_seconds", 0) + elapsed
            persist(task)
    return observe

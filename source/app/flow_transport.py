"""Same-origin format/transport failover, never cross-provider money merging."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
import time
import math


HISTORY_ROUTES = (
    ("eastmoney_https_json", "https://push2his.eastmoney.com", False),
    ("eastmoney_https_jsonp", "https://push2his.eastmoney.com", True),
    ("eastmoney_http_jsonp", "http://push2his.eastmoney.com", True),
)


def retry_after_seconds(value):
    try:
        number = float(value)
        return max(0, number) if math.isfinite(number) else 0
    except (ValueError, TypeError):
        try:
            stamp = parsedate_to_datetime(str(value))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0, (stamp - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class FlowHistoryUnavailable(RuntimeError):
    def __init__(self, message, *, retryable=True, retry_after=0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class HistoryRoutePool:
    def __init__(self):
        self.lock = Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.states = {key: {"failures":0,"successes":0,"until":0,"last_error":""} for key, *_ in HISTORY_ROUTES}
            self.preferred = HISTORY_ROUTES[0][0]
            self.provider_until = 0

    def available(self, attempted):
        with self.lock:
            now = time.monotonic()
            if self.provider_until > now:
                return []
            routes = [route for route in HISTORY_ROUTES if route[0] not in attempted and self.states[route[0]]["until"] <= now]
            return sorted(routes, key=lambda route: route[0] != self.preferred)

    def success(self, key):
        with self.lock:
            state = self.states[key]
            state.update(failures=0, successes=state["successes"]+1, until=0, last_error="")
            self.preferred = key

    def failure(self, key, error, *, rate_wait=0, rate_limited=False):
        with self.lock:
            state = self.states[key]
            state["failures"] += 1
            state["last_error"] = type(error).__name__
            if state["failures"] >= 2:
                state["until"] = time.monotonic() + 60
            # A provider's explicit restriction applies to all transports;
            # switching formats/hosts must not evade Retry-After.
            if rate_limited:
                self.provider_until = max(self.provider_until, time.monotonic()+max(30,rate_wait))

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            remaining = max(0, self.provider_until-now)
            if not remaining and all(s["until"] > now for s in self.states.values()):
                remaining = min(s["until"]-now for s in self.states.values())
            return {"provider":"eastmoney", "preferred_channel":self.preferred,
                    "retry_after_seconds":round(remaining,1),
                    "channels":[{"id":key,"successes":s["successes"],"failures":s["failures"],
                                 "cooldown_seconds":round(max(0,s["until"]-now),1),"last_error":s["last_error"]}
                                for key,s in self.states.items()]}


HISTORY_POOL = HistoryRoutePool()

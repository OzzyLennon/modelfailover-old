from conftest import make_provider
from mf.health import HealthMonitor


def test_probe_success_marks_healthy():
    mon = HealthMonitor([make_provider("p")], probe=lambda p: (True, "", 12))
    st = mon.check_one(mon.providers[0])
    assert st.status == "healthy"
    assert st.latency_ms == 12
    assert st.consecutive_failures == 0


def test_auth_failure_marks_down():
    mon = HealthMonitor([make_provider("p")], probe=lambda p: (False, "鉴权失败 HTTP 401", 10))
    st = mon.check_one(mon.providers[0])
    assert st.status == "down"


def test_rate_limit_marks_degraded():
    mon = HealthMonitor([make_provider("p")], probe=lambda p: (False, "限流 HTTP 429", 10))
    st = mon.check_one(mon.providers[0])
    assert st.status == "degraded"


def test_probe_exception_does_not_raise():
    def boom(p):
        raise RuntimeError("探针炸了")

    mon = HealthMonitor([make_provider("p")], probe=boom)
    st = mon.check_one(mon.providers[0])
    assert st.status == "degraded"
    assert "probe exception" in st.error


def test_recovery_resets_failure_counter():
    calls = {"n": 0}

    def flaky(p):
        calls["n"] += 1
        return (calls["n"] > 1, "" if calls["n"] > 1 else "限流 HTTP 429", 5)

    mon = HealthMonitor([make_provider("p")], probe=flaky)
    mon.check_one(mon.providers[0])
    st = mon.check_one(mon.providers[0])
    assert st.status == "healthy"
    assert st.consecutive_failures == 0

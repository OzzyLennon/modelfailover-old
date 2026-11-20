from conftest import health_map, make_provider
from mf.router import build_chain, explain_chain


def test_strength_match_comes_before_general():
    code_p = make_provider("coder", strengths=["code"], priority=50)
    gen_p = make_provider("generalist", strengths=["general"], priority=10)
    chain = [p.name for p in build_chain(
        "code", [code_p, gen_p], health_map({"coder": "healthy", "generalist": "healthy"}))]
    # coder 优先级(50)虽大于 generalist(10)，但强项匹配分层更靠前
    assert chain[0] == "coder"


def test_disabled_excluded():
    p = make_provider("off", enabled=False)
    assert build_chain("code", [p], health_map({}), max_attempts=3) == []


def test_down_provider_is_last():
    up = make_provider("up", strengths=["code"], priority=10)
    down = make_provider("down", strengths=["code"], priority=1)
    chain = [p.name for p in build_chain(
        "code", [up, down], health_map({"up": "healthy", "down": "down"}))]
    assert chain == ["up", "down"]


def test_max_attempts_cap():
    ps = [make_provider(f"p{i}", priority=i) for i in range(6)]
    chain = build_chain("general", ps, health_map({p.name: "healthy" for p in ps}), max_attempts=3)
    assert len(chain) == 3


def test_pinned_first_even_if_down():
    a = make_provider("a", priority=1)
    b = make_provider("b", priority=2)
    chain = [p.name for p in build_chain(
        "code", [a, b], health_map({"a": "down", "b": "healthy"}), pinned="a")]
    assert chain[0] == "a"


def test_explain_chain_mentions_status_and_strength():
    p = make_provider("coder", strengths=["code"])
    desc = explain_chain("code", [p], health_map({"coder": "healthy"}), [p])
    assert "coder" in desc[0] and "强项匹配" in desc[0] and "healthy" in desc[0]

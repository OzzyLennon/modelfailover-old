from mf.classify import classify


def test_code_prompt():
    task, _ = classify([{"role": "user", "content": "帮我写个 python 脚本，把这个函数重构一下"}])
    assert task == "code"


def test_creative_prompt():
    task, _ = classify([{"role": "user", "content": "给这款产品写一段营销文案，要有故事感"}])
    assert task == "creative"


def test_analysis_prompt():
    task, _ = classify([{"role": "user", "content": "分析一下这份研报，梳理其中的洞察和风险"}])
    assert task == "analysis"


def test_empty_is_general():
    task, _ = classify([{"role": "user", "content": "你好"}])
    assert task == "general"


def test_system_prompt_counts():
    task, _ = classify(
        [
            {"role": "system", "content": "你是一个代码审查助手"},
            {"role": "user", "content": "看看这里"},
        ]
    )
    assert task == "code"


def test_content_list_form():
    task, _ = classify(
        [{"role": "user", "content": [{"type": "text", "text": "重构这个函数"}]}]
    )
    assert task == "code"

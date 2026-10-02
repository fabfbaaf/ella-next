from ella_runtime.modules.models.response_length import choose_length


def test_daily_response_length_weights_and_explicit_override():
    choices = [choose_length("今天怎么样", draw=draw) for draw in range(100)]
    assert choices.count("short") == 40
    assert choices.count("medium") == 30
    assert choices.count("long") == 30
    assert choose_length("展开讲讲", draw=0) == "long"
    assert choose_length("简单点", draw=99) == "short"

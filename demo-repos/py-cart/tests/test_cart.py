from cart import add_item


def test_fresh_cart_each_call():
    assert add_item("apple") == ["apple"]
    assert add_item("pear") == ["pear"]

"""CRLF wire terminator is preserved (#839)."""
def test_newline_wrapper_preserves_crlf():
    import io
    raw = b'{"id":1}\r\n'
    text = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", newline="")
    assert text.readline() == '{"id":1}\r\n'
# ensure console script is tested

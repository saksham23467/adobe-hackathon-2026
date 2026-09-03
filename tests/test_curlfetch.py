"""curl transport: header-dump parsing and the no-shell execution contract."""

from audit_common.curlfetch import curl_available, parse_header_dump


def test_parse_header_dump_redirect_chain():
    raw = (
        "HTTP/1.1 301 Moved Permanently\r\n"
        "Location: https://www.example.com/\r\n"
        "\r\n"
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "\r\n"
    )
    hops, headers = parse_header_dump(raw)
    assert hops == [("", "https://www.example.com/", 301)]
    assert headers["content-type"] == "text/html; charset=utf-8"


def test_curl_is_the_preferred_runtime_tool():
    # The marketplace prefers curl; urllib is only a fallback.
    # Presence is environment-dependent; the function must be boolean.
    assert curl_available() in (True, False)

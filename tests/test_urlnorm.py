import _crawl_paths  # noqa: F401  (sets sys.path)
import urlnorm


def test_normalize_lowercases_host_drops_port_and_fragment():
    assert urlnorm.normalize_url("HTTP://Example.com:80/a/../b?x=1#frag") == "http://example.com/b?x=1"
    assert urlnorm.normalize_url("https://Example.com:443/") == "https://example.com/"


def test_normalize_resolves_relative_against_base():
    assert urlnorm.normalize_url("/page", base="https://x.com/dir/") == "https://x.com/page"
    assert urlnorm.normalize_url("sub", base="https://x.com/dir/") == "https://x.com/dir/sub"


def test_dedup_key_ignores_scheme_www_slash_index_and_tracking():
    keys = {
        urlnorm.dedup_key("https://www.ex.com/a/"),
        urlnorm.dedup_key("http://ex.com/a"),
        urlnorm.dedup_key("https://ex.com/a/index.html"),
        urlnorm.dedup_key("https://ex.com/a?utm_source=news"),
    }
    assert keys == {"ex.com/a"}


def test_dedup_key_keeps_meaningful_query():
    assert urlnorm.dedup_key("https://ex.com/p?id=5") == "ex.com/p?id=5"
    assert urlnorm.dedup_key("https://ex.com/p?id=5") != urlnorm.dedup_key("https://ex.com/p?id=6")


def test_same_site_handles_www_apex():
    assert urlnorm.same_site("https://www.ex.com/x", "ex.com")
    assert urlnorm.same_site("https://ex.com/x", "www.ex.com")
    assert not urlnorm.same_site("https://other.com/x", "ex.com")


def test_variant_kind_flags():
    kinds = urlnorm.variant_kind("https://www.EX.com/a/")
    assert "www" in kinds and "trailing_slash" in kinds and "uppercase_host" in kinds
    assert "index_file" in urlnorm.variant_kind("https://ex.com/a/index.html")


def test_session_and_param_helpers():
    assert urlnorm.has_session_param("https://x.com/a?sid=abc")
    assert not urlnorm.has_session_param("https://x.com/a?id=abc")
    assert urlnorm.param_count("https://x.com/a?b=1&c=2") == 2


def test_repeating_segments_and_depth():
    assert urlnorm.repeating_segments("https://x.com/a/a/a/")
    assert urlnorm.repeating_segments("https://x.com/cal/2020/cal/2020/cal/2020/")
    assert not urlnorm.repeating_segments("https://x.com/a/b/c")
    assert urlnorm.path_depth("https://x.com/a/b/c") == 3


def test_all_variants_are_same_page():
    variants = urlnorm.all_variants("https://ex.com/a")
    assert "https://ex.com/a" in variants
    assert "http://www.ex.com/a/" in variants
    keys = {urlnorm.dedup_key(v) for v in variants}
    assert keys == {"ex.com/a"}

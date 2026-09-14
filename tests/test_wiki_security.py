"""
④e: Wiki 安全验收 — XSS 防御 + 路径逃逸防护
"""
from wiki_engine import md_to_html, validate_path
import pytest


def test_xss_img_onerror_escaped():
    """<img src=x onerror=alert(1)> → 标签被转义，不产生可执行 HTML"""
    result = md_to_html('<img src=x onerror=alert(1)>')
    assert '<img ' not in result, f"<img 不应以未转义形式出现: {result[:100]}"
    assert '&lt;img' in result, f"img 标签应被转义: {result[:100]}"


def test_xss_script_tag_escaped():
    """<script>evil()</script> → 转义为惰性文本"""
    result = md_to_html('<script>evil()</script>')
    assert '<script>' not in result
    assert '&lt;script&gt;' in result


def test_xss_svg_escaped():
    """SVG onload → 标签被转义"""
    result = md_to_html('<svg onload=alert(1)>')
    assert '<svg ' not in result
    assert '&lt;svg' in result


def test_normal_markdown_preserved():
    """正常 markdown 不受影响"""
    result = md_to_html('**bold** and *italic*')
    assert '<strong>bold</strong>' in result
    assert '<em>italic</em>' in result


def test_wikilink_rendered():
    """[[页面名]] → clickable link"""
    result = md_to_html('see [[退换货政策]] for details')
    assert 'href="/wiki/view/退换货政策"' in result
    assert 'wikilink' in result


def test_parent_directory_rejected():
    """../ 路径应被拒"""
    with pytest.raises(ValueError):
        validate_path("../agent_client.py")


def test_absolute_path_rejected():
    """绝对路径应被拒"""
    with pytest.raises(ValueError):
        validate_path("/etc/passwd")


def test_windows_path_rejected():
    """Windows 反斜杠应被拒"""
    with pytest.raises(ValueError):
        validate_path("..\..\secret.txt")


def test_valid_path_accepted():
    """正常子路径应通过"""
    result = validate_path("concepts/test.md")
    assert result.endswith("test.md")

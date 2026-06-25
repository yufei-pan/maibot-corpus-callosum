"""哨兵检测正则测试。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SDK_ROOT = _ROOT.parent / "maibot-plugin-sdk"
for path in (_ROOT, _SDK_ROOT):
    normalized = str(path)
    if path.is_dir() and normalized not in sys.path:
        sys.path.insert(0, normalized)

from plugin import CorpusCallosumPlugin  # noqa: E402


def _extract_reason(response: str, sentinel: str = "reject") -> str | None:
    match = CorpusCallosumPlugin._build_sentinel_re(sentinel).search(response)
    if match is None:
        return None
    return match.group(1).strip()


def test_sentinel_matches_standalone_block():
    assert _extract_reason("<reject>规划器搞错了</reject>") == "规划器搞错了"


def test_sentinel_matches_block_with_surrounding_text():
    assert (
        _extract_reason("我先说两句。<reject>规划器把己方消息认错了</reject>其余忽略。")
        == "规划器把己方消息认错了"
    )


def test_sentinel_matches_block_after_leading_whitespace():
    assert _extract_reason("  \n<reject>理由</reject>") == "理由"


def test_sentinel_does_not_match_without_block():
    assert _extract_reason("正常回复，没有哨兵") is None


def test_sentinel_matches_first_block_when_multiple_present():
    assert _extract_reason("<reject>第一次</reject>以及<reject>第二次</reject>") == "第一次"

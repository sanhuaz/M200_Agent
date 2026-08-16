from app.api.onebot import OneBotManager


def test_group_requires_mention_or_prefix() -> None:
    manager = OneBotManager()
    manager.self_id = "12345"
    assert manager._extract_triggered_text("普通聊天", "group") is None
    assert manager._extract_triggered_text("/ai 你好", "group") == "你好"
    assert manager._extract_triggered_text("[CQ:at,qq=12345] 你好", "group") == "你好"
    assert manager._extract_triggered_text("私聊内容", "private") == "私聊内容"

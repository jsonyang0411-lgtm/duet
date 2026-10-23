"""WeChat's main window must beat a detached chat window whose title only contains the word 微信."""

from unittest.mock import patch

from src.wechat import window_controller as wc


def score(title, cls="Qt51514QWindowIcon", rect=(0, 0, 1000, 800), visible=True):
    with patch.object(wc, "win32gui") as gui, patch.object(wc, "win32process") as process, \
            patch.object(wc, "_get_process_name", return_value="weixin.exe"):
        gui.GetWindowText.return_value = title
        gui.GetClassName.return_value = cls
        gui.IsWindowVisible.return_value = visible
        gui.IsIconic.return_value = False
        gui.GetWindowRect.return_value = rect
        process.GetWindowThreadProcessId.return_value = (1, 5)
        return wc._score_window(100)


def test_the_exact_title_scores_above_a_title_that_only_contains_the_word():
    assert score("微信").score > score("微信团队").score
    assert score("WeChat").score > score("WeChat Team").score


def test_the_main_window_beats_a_detached_chat_of_the_same_size():
    main = score("微信")
    chat = score("微信支付")
    assert main.score > chat.score
    assert "title_wechat_exact" in main.reason and "title_wechat_exact" not in chat.reason


def test_other_titles_still_get_their_old_scores():
    assert "title_wechat" in score("微信团队").reason
    assert "title_wechat_en" in score("WeChat Team").reason
    assert "title_wechat" not in score("张三").reason

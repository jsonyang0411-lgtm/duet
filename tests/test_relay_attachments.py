"""Pictures and files sent with a relay task: held on Windows, downloaded by the Mac worker."""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from test_claude_relay_worker import ROOT, TOKEN, bot, fake_claude, run_one, settings, worker  # noqa: F401  (fixtures)

from src.relay.attachments import HOLD_SEC, Held, attachment_kind, locate_received_file, resolve
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore
from src.wechat.wcdb_backend import WcdbBackend, _extract_file_appmsg

ME = "wxid_me"

FILE_XML = ("<msg><appmsg appid=\"\" sdkver=\"0\"><title>报告.pdf</title><type>6</type>"
            "<appattach><totallen>{size}</totallen><fileext>pdf</fileext></appattach></appmsg></msg>")


def picture(tmp_path, name="shot.png"):
    path = tmp_path / name
    Image.new("RGB", (40, 30), (200, 10, 10)).save(path)
    return path


def image_msg(path="", **kw):
    msg = {"chat_id": ME, "sender_id": ME, "content": "[图片]", "msg_type": 3, "is_group": False,
           "media_path": str(path), "timestamp": time.time()}
    msg.update(kw)
    return msg


def file_msg(name="报告.pdf", size=0, **kw):
    msg = {"chat_id": ME, "sender_id": ME, "content": "[文件]", "msg_type": 49, "is_group": False,
           "file_name": name, "file_size": size, "timestamp": time.time()}
    msg.update(kw)
    return msg


# ── what counts, and where it is on Windows ───────────────────────────────

def test_what_is_an_attachment():
    assert attachment_kind(image_msg()) == "image"
    assert attachment_kind(file_msg()) == "file"
    assert attachment_kind(file_msg(name="")) == ""                    # a link, a mini program ...
    assert attachment_kind({"msg_type": 47}) == "" and attachment_kind({"msg_type": 1}) == ""
    assert attachment_kind({"msg_type": "x"}) == ""


def test_a_file_message_gives_its_name_and_size():
    assert _extract_file_appmsg(FILE_XML.format(size=1234)) == {"name": "报告.pdf", "size": 1234}
    assert _extract_file_appmsg("wxid_x:\n" + FILE_XML.format(size="")) == {"name": "报告.pdf", "size": 0}
    link = FILE_XML.replace("<type>6</type>", "<type>5</type>")
    assert _extract_file_appmsg(link) is None and _extract_file_appmsg("[文件]") is None
    assert _extract_file_appmsg("<msg><appmsg><type>6</type></appmsg>") is None


def test_the_wcdb_backend_puts_the_file_on_the_message():
    from test_wcdb_backend import FakeClient
    backend = WcdbBackend(bot_display_name="商务助手", groups=["测试群"])
    backend._client = FakeClient()
    msg = backend._standardize({"sender_username": "wxid_friend", "message_content": FILE_XML.format(size=9),
                                "local_type": 49, "create_time": int(time.time()), "local_id": "f1"},
                               "私聊好友", "wxid_friend", is_group=False)
    assert msg["file_name"] == "报告.pdf" and msg["file_size"] == 9 and msg["content"] == "[文件]"
    text = backend._standardize({"sender_username": "wxid_friend", "message_content": "你好", "local_type": 1,
                                 "create_time": int(time.time()), "local_id": "t1"},
                                "私聊好友", "wxid_friend", is_group=False)
    assert text["file_name"] == "" and text["file_size"] == 0


def test_a_received_file_is_found_in_the_download_folders(tmp_path):
    month = time.strftime("%Y-%m")
    new = tmp_path / "xwechat_files/wxid_abc_1234/msg/file" / month
    old = tmp_path / "WeChat Files/wxid_abc/FileStorage/File" / month
    new.mkdir(parents=True)
    old.mkdir(parents=True)
    (old / "报告.pdf").write_bytes(b"x" * 10)
    (new / "报告(1).pdf").write_bytes(b"y" * 20)
    (new / "报告(1).pdf.bak").write_bytes(b"z" * 20)
    bases = [tmp_path / "xwechat_files", tmp_path / "WeChat Files"]
    os.utime(old / "报告.pdf", (time.time() - 100,) * 2)
    assert locate_received_file(bases, "报告.pdf") == new / "报告(1).pdf"            # the newest
    assert locate_received_file(bases, "报告.pdf", size=10) == old / "报告.pdf"      # the one of that size
    assert locate_received_file(bases, "报告.pdf", size=99) is None                  # still downloading
    assert locate_received_file(bases, "别的.pdf") is None
    assert locate_received_file(bases, "../报告.pdf") is None
    assert locate_received_file([tmp_path / "xwechat_files/wxid_abc_1234"], "报告.pdf") == new / "报告(1).pdf"


def test_a_picture_is_found_or_photographed(tmp_path):
    config = SimpleNamespace(wechat_data_dir="")
    path, name, problem = resolve(config, Held("image", image_msg(picture(tmp_path)), 0), 1)
    assert Path(path) == tmp_path / "shot.png" and name == "图片-1.png" and problem == ""
    shot = picture(tmp_path, "chat.jpg")
    path, name, _ = resolve(config, Held("image", image_msg(_capture_media_screenshot=lambda: str(shot)), 0), 2)
    assert path == str(shot) and name == "聊天截图-2.jpg"
    path, _, problem = resolve(config, Held("image", image_msg(), 0), 3)
    assert path == "" and "没找到" in problem
    _, _, problem = resolve(config, Held("file", file_msg(size=3 * 1024 ** 3), 0), 4)
    assert "超过 2 GB" in problem


# ── the bot holds them for the next task ──────────────────────────────────

def make_relay(tmp_path, **overrides):
    values = dict(
        claude_relay="on", claude_relay_token="t" * 32, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:",
        claude_relay_max_chars=1500, admin_wxid=ME, claude_relay_only="off", claude_relay_progress_min=0,
        wechat_data_dir=str(tmp_path / "wechat"),
    )
    values.update(overrides)
    clock = SimpleNamespace(now=time.time())
    relay = ClaudeRelay(SimpleNamespace(**values), store=RelayStore(":memory:"), files_dir=tmp_path / "files",
                        clock=lambda: clock.now)
    return relay, clock


def text(content, **kw):
    msg = {"chat_id": ME, "sender_id": ME, "content": content, "msg_type": 1, "is_group": False,
           "timestamp": time.time()}
    msg.update(kw)
    return msg


def test_relay_only_holds_a_picture_and_the_next_task_takes_it(tmp_path):
    relay, clock = make_relay(tmp_path, claude_relay_only="on")
    shot = picture(tmp_path)
    assert relay.handle(image_msg(shot)) == (True, "收到图片，下一个任务会带上它（现在有 1 个附件，10 分钟内有效）。")
    month = time.strftime("%Y-%m")
    folder = tmp_path / "wechat/wxid_me_1/msg/file" / month
    folder.mkdir(parents=True)
    (folder / "报告.pdf").write_bytes(b"%PDF-1.4 ...")
    assert "现在有 2 个附件" in relay.handle(file_msg(size=12))[1]
    matched, reply = relay.handle(text("看看这张图和这个报告"))
    assert reply.startswith("收到，已交给 Mac（#1，带 2 个附件）。")
    job = relay.next_job(0)
    assert [f["name"] for f in job["files"]] == ["图片-1.png", "报告.pdf"]
    copy = relay.file_path(1, job["files"][0]["id"])
    assert Path(copy).read_bytes() == shot.read_bytes() and Path(copy).parent.parent == tmp_path / "files/attachments"
    assert relay.file_path(2, job["files"][0]["id"]) is None                       # not that job's file
    assert relay.file_path(1, job["files"][1]["id"]) == str(folder / "报告.pdf")   # a file is not copied
    relay.handle(text("新任务 下一个任务"))
    assert relay.next_job(0)["files"] == []                                         # taken once


def test_with_the_prefix_a_picture_is_held_quietly(tmp_path):
    relay, clock = make_relay(tmp_path)
    assert relay.handle(image_msg(picture(tmp_path))) == (False, None)              # the normal reply goes on
    assert "带 1 个附件" in relay.handle(text("cc 这张图里是什么"))[1]


def test_held_attachments_expire_and_others_are_not_held(tmp_path):
    relay, clock = make_relay(tmp_path, claude_relay_only="on")
    relay.handle(image_msg(picture(tmp_path)))
    clock.now += HOLD_SEC + 1
    assert "附件" not in relay.handle(text("任务", timestamp=clock.now))[1]
    now = clock.now
    assert relay.handle(image_msg(picture(tmp_path), sender_id="wxid_other", chat_id="wxid_other",
                                  timestamp=now)) == (False, None)
    assert relay.handle(image_msg(picture(tmp_path), is_group=True, timestamp=now)) == (False, None)
    assert relay._held == {}
    old = image_msg(picture(tmp_path), timestamp=now - 3600)
    assert relay.handle(old) == (False, None)


def test_what_could_not_be_found_is_said_in_the_receipt(tmp_path):
    relay, clock = make_relay(tmp_path, claude_relay_only="on")
    relay.handle(image_msg())                                                       # no file anywhere
    relay.handle(file_msg(name="没下载.zip"))
    reply = relay.handle(text("看看"))[1]
    assert "（#1）" in reply and "一张图片在电脑上没找到" in reply and "文件「没下载.zip」在电脑上没找到" in reply


def test_a_retry_takes_the_same_files(tmp_path):
    relay, clock = make_relay(tmp_path, claude_relay_only="on")
    relay.handle(image_msg(picture(tmp_path)))
    relay.handle(text("看图"))
    job = relay.next_job(0)
    relay.finish(job["id"], {"ok": False, "text": "失败了"})
    assert "带 1 个附件" in relay.handle(text("重试 1"))[1]
    assert [f["name"] for f in relay.next_job(0)["files"]] == ["图片-1.png"]


def test_two_files_of_the_same_name_keep_apart(tmp_path):
    relay, clock = make_relay(tmp_path, claude_relay_only="on")
    folder = tmp_path / "wechat/wxid_me_1/msg/file" / time.strftime("%Y-%m")
    folder.mkdir(parents=True)
    (folder / "报告.pdf").write_bytes(b"%PDF")
    relay.handle(file_msg())
    relay.handle(file_msg())
    relay.handle(text("比较这两个"))
    assert [f["name"] for f in relay.next_job(0)["files"]] == ["报告.pdf", "报告(2).pdf"]


# ── over HTTP, to the worker, into Claude's reach ─────────────────────────

def test_the_worker_downloads_them_and_claude_is_told(worker, bot, fake_claude, tmp_path):
    s = settings(bot, fake_claude, tmp_path)
    shot = picture(tmp_path)
    bot.relay._files_dir = tmp_path / "files"
    bot.relay._hold(ME, Held("image", image_msg(shot), time.time()))
    bot.ask("cc 这张图是什么")
    run_one(worker, bot, s)
    call = fake_claude.calls()[0]
    inbox = tmp_path / "home/inbox/job-1"
    assert call["args"][call["args"].index("--add-dir") + 1] == str(inbox)
    assert call["prompt"].startswith("这张图是什么\n\n（微信里和这条任务一起发来的附件，已存到 ")
    assert "图片-1.png" in call["prompt"]
    assert (inbox / "图片-1.png").read_bytes() == shot.read_bytes()
    assert bot.sent[0].startswith("#1 完成\n完成：这张图是什么")


def test_the_file_endpoint_needs_the_token(bot, tmp_path):
    bot.relay._files_dir = tmp_path / "files"
    bot.relay._hold(ME, Held("image", image_msg(picture(tmp_path)), time.time()))
    bot.ask("cc 看图")
    file_id = bot.relay.next_job(0)["files"][0]["id"]
    url = f"{bot.url}/relay/jobs/1/files/{file_id}"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url, timeout=5)
    assert e.value.code == 401
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    assert opener.open(req, timeout=5).read().startswith(b"\x89PNG")
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(urllib.request.Request(f"{bot.url}/relay/jobs/1/files/999",
                                           headers={"Authorization": f"Bearer {TOKEN}"}), timeout=5)
    assert e.value.code == 404


def test_a_job_without_files_runs_as_before(worker, bot, fake_claude, tmp_path):
    bot.ask("cc 普通任务")
    run_one(worker, bot, settings(bot, fake_claude, tmp_path))
    call = fake_claude.calls()[0]
    assert "--add-dir" not in call["args"] and call["prompt"] == "普通任务"


def test_a_failed_download_is_told_to_claude(worker, tmp_path):
    class Client:
        def download(self, job_id, file_id):
            raise OSError("connection reset")
    folder, note = worker.fetch_files(Client(), {"id": 3, "files": [{"id": 1, "name": "a.png"}]})
    assert folder is None and "还有附件没能从 Windows 取到：a.png" in note


def test_names_stay_inside_the_inbox(worker):
    assert worker.safe_name("../../etc/passwd", "f") == "_.._etc_passwd"
    assert worker.safe_name("a:b*c?.txt", "f") == "a_b_c_.txt"
    assert worker.safe_name("...", "file-1") == "file-1"


# ── big files: no copy, streamed both ways ────────────────────────────────

def test_a_big_file_goes_through_whole(worker, bot, fake_claude, tmp_path):
    import os
    month = time.strftime("%Y-%m")
    folder = tmp_path / "wechat/wxid_me_1/msg/file" / month
    folder.mkdir(parents=True)
    big = folder / "数据.bin"
    big.write_bytes(os.urandom(3 * 1024 * 1024 + 17))                 # more than one 1 MB chunk
    bot.relay._config.wechat_data_dir = str(tmp_path / "wechat")
    bot.relay._files_dir = tmp_path / "files"
    bot.relay._hold(ME, Held("file", file_msg(name="数据.bin", size=big.stat().st_size), time.time()))
    bot.ask("cc 看看这个数据")
    run_one(worker, bot, settings(bot, fake_claude, tmp_path))
    got = tmp_path / "home/inbox/job-1/数据.bin"
    assert got.read_bytes() == big.read_bytes()
    assert not list((tmp_path / "home/inbox/job-1").glob("*.part"))
    assert not (tmp_path / "files/attachments").exists()                 # the file was not copied


def test_a_download_that_breaks_off_leaves_nothing(worker, tmp_path):
    import http.server
    import threading as th

    class Short(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"x" * 40)                               # then the connection closes

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Short)
    th.Thread(target=server.handle_request, daemon=True).start()
    client = worker.RelayClient(f"http://127.0.0.1:{server.server_address[1]}", "t")
    dest = tmp_path / "a.bin"
    with pytest.raises(Exception):
        client.download(1, 1, dest)
    server.server_close()
    assert not dest.exists() and not (tmp_path / "a.bin.part").exists()

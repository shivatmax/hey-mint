"""A request never ends in silence (10 Oct): the user asked for a "build" folder, find_files found nothing, the
model ended its turn without a word and Mint fell asleep 6 s later - "it just died". Talking over the reply put Mint
to sleep in the same second; the model once *said* ",name:find_files}" instead of calling it; and Spotlight timing
out under load came back as "Nothing found"."""
import asyncio
import time

try:
    from mint.app import session
    from mint.voice import listening
    from mint.tools import harness as ht
except ImportError:
    from mint import session, listening
    from mint import harness_tools as ht


class _Live:
    def __init__(self):
        self.sent = []

    async def send_realtime_input(self, text=None, **_):
        self.sent.append(text)


class _Audio:
    playing = False


class _Queue:
    def empty(self):
        return True


class _UI:
    def __init__(self):
        self.said = []

    def assistant_said(self, text):
        self.said.append(text)


def _mint(**kw):
    m = session.Mint.__new__(session.Mint)
    m.session = _Live()
    m._stop_epoch = 1
    m._heard, m._said = "", ""
    m._busy = False
    m._tool_task = None
    m._model_active_at = 0.0
    m.audio, m.audio_in, m.ui = _Audio(), _Queue(), _UI()
    m._window = listening.Window()
    m.printed = []
    m._print = m.printed.append
    m._tools_unspoken = 1
    m._flush_playback = lambda: None
    m.__dict__.update(kw)
    return m


def _quiet(monkeypatch):
    try:
        from mint.app import control, live
    except ImportError:
        from mint import control, live
    monkeypatch.setattr(control, "stopped", lambda: False)
    monkeypatch.setattr(live, "request", lambda: "find the build folder in the chrome extension")


def test_quiet_after_tools_asks_the_model_to_answer(monkeypatch):
    _quiet(monkeypatch)
    m = _mint()
    asyncio.run(m._silence_watch(1, time.monotonic(), 0.3))
    assert len(m.session.sent) == 1 and "said nothing" in m.session.sent[0]
    assert "build folder" in m.session.sent[0]
    assert m._window.until == float("inf")                  # still handling: no follow-up clock, no sleep


def test_no_rescue_when_the_model_carries_on(monkeypatch):
    _quiet(monkeypatch)
    m = _mint()
    since = time.monotonic()

    async def run():
        task = asyncio.create_task(m._silence_watch(1, since, 0.6))
        await asyncio.sleep(0.2)
        m._model_active_at = time.monotonic()               # the model's answer arrives
        await task
    asyncio.run(run())
    assert m.session.sent == []


def test_no_rescue_when_it_already_spoke_or_the_user_stopped(monkeypatch):
    _quiet(monkeypatch)
    spoke = _mint(_tools_unspoken=0)
    asyncio.run(spoke._silence_watch(1, time.monotonic(), 0.2))
    stopped = _mint(_stop_epoch=2)
    asyncio.run(stopped._silence_watch(1, time.monotonic(), 0.2))
    assert spoke.session.sent == [] and stopped.session.sent == []


def test_after_two_rescues_the_user_is_told_instead_of_silence(monkeypatch):
    _quiet(monkeypatch)
    m = _mint(_rescues=session.Mint.MAX_RESCUES)
    asyncio.run(m._silence_watch(1, time.monotonic(), 0.2))
    assert m.session.sent == []
    assert m.ui.said and "ask me again" in m.ui.said[0]
    assert m._window.until != float("inf")                  # now the follow-up window may end


def test_tool_call_text_is_cut_and_the_real_call_requested(monkeypatch):
    _quiet(monkeypatch)
    m = _mint(_tools_unspoken=0)
    assert not m._leak_check("I found the main folders")
    assert m._leak_check(",name:find_files}")
    assert m._leak_check(" and more words after it")         # the rest of that turn is cut too
    asyncio.run(m._silence_watch(1, time.monotonic(), 0.2, leaked=True))
    assert len(m.session.sent) == 1 and "Make the real tool call" in m.session.sent[0]
    for text in ('{"name": "open_app"', "use_tool(name='x')", "default_api.find_files(", "tool_code"):
        assert session.Mint._LEAK.search(text), text
    for text in ("Your name is Boss.", "I named the file build.zip", "Here are the arguments for it"):
        assert not session.Mint._LEAK.search(text), text


def test_talking_over_the_reply_keeps_mint_listening():
    w = listening.Window(follow_up=6.0)
    w.finished(100.0)                                       # the window began before the tool ran
    w.hold(110.0)                                           # the user talks over the reply at 110 s
    assert not w.should_sleep(110.5)
    assert w.should_sleep(116.5)


def test_spotlight_timeout_keeps_what_came_back(monkeypatch):
    import subprocess

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"), output="/tmp/x/a/build\n/tmp/x/b/build\n")
    monkeypatch.setattr(subprocess, "run", slow)
    assert ht._mdfind(["-name", "build"]) == ["/tmp/x/a/build", "/tmp/x/b/build"]


def test_a_name_with_a_place_finds_the_folder_in_that_place(tmp_path, monkeypatch):
    for place in ("Projects/Work/chrome-plugin/build", "Projects/other/build", "Projects/Task_builder",
                  "Projects/Work/chrome-plugin/src"):
        (tmp_path / place).mkdir(parents=True)
    monkeypatch.setattr(ht, "_mdfind", lambda argv, **kw: [])          # Spotlight has nothing: the walk finds it
    monkeypatch.setattr(ht, "_blocked", lambda path, write=False: "")  # (tmp_path is outside the home folder)
    found = ht._search_paths("chrome build", tmp_path, "folder", 20)
    assert found and found[0] == tmp_path / "Projects/Work/chrome-plugin/build"
    plain = ht._search_paths("build", tmp_path, "folder", 20)
    assert {p.name for p in plain[:2]} == {"build"}                    # exact names before Task_builder


def test_folders_macos_refuses_turn_into_a_permission_ask(tmp_path, monkeypatch):
    try:
        from mint.core import permit
    except ImportError:
        from mint import permit
    locked = tmp_path / "Downloads"
    locked.mkdir()
    real = ht.os.scandir

    def scandir(path):
        if str(path) == str(locked):
            raise PermissionError(1, "Operation not permitted")
        return real(path)
    monkeypatch.setattr(ht.os, "scandir", scandir)
    monkeypatch.setattr(ht, "_mdfind", lambda argv, **kw: [])
    monkeypatch.setattr(ht, "_blocked", lambda path, write=False: "")
    monkeypatch.setattr(ht, "_resolve", lambda raw, must_exist=True: (locked, ""))
    result = ht.find_files({"query": "taxes", "folder": str(locked)})
    assert "lacks Files access" in result
    assert permit.from_result(result) == "files"


def test_the_users_words_rank_the_right_folder_first(tmp_path, monkeypatch):
    try:
        from mint.app import live
    except ImportError:
        from mint import live
    for place in ("a/Task_builder/work/x/build", "b/other/build", "Projects/Work/chrome-plugin/build"):
        (tmp_path / place).mkdir(parents=True)
    paths = [str(tmp_path / p) for p in ("a/Task_builder/work/x/build", "b/other/build",
                                          "Projects/Work/chrome-plugin/build")]
    monkeypatch.setattr(ht, "_mdfind", lambda argv, **kw: paths if "-name" in argv else [])
    monkeypatch.setattr(ht, "_blocked", lambda path, write=False: "")
    monkeypatch.setattr(live, "request", lambda: "find the build folder somewhere in my Chrome extension project")
    found = ht._search_paths("build", None, "folder", 20)
    assert found[0] == tmp_path / "Projects/Work/chrome-plugin/build"


def test_a_key_switch_after_the_answer_does_not_redo_the_job():
    m = _mint(_tools_at=time.monotonic() - 5, _answered_at=time.monotonic() - 1, task=None)
    assert m._working_on_request() and not m._job_open()                # answered after its last tool
    m._answered_at = m._tools_at - 1
    assert m._job_open()                                                # tools since the last words: mid-job


def test_asking_find_tools_again_gives_the_arguments_again(monkeypatch):
    try:
        from mint.tools import diet
    except ImportError:
        from mint import tool_diet as diet
    family = next(f for f, info in diet.FAMILIES.items() if "find_files" in info["tools"])
    diet._shown.clear()
    diet._served.clear()
    first = diet.pack([family], "find files")
    again = diet.pack([family], "find files", again=True)
    assert "find_files" in first and "args:" in first
    assert "- find_files args:" in again and "call use_tool now" in again

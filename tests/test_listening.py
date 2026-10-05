"""Listening: the follow-up window (how long Mint stays open) and the barrier (was that for Mint?).

Offline and deterministic: times are plain numbers, Jev's answers are canned. The words are real
ones from the user's log that Mint answered or acted on although they were not meant for it.
"""
import asyncio
import math

import pytest

try:                                    # the published layout
    from mint.app import session as session_mod
    from mint.voice import hearing, listening
except ImportError:                     # the private working copy
    from mint import hearing, listening
    from mint import session as session_mod


@pytest.fixture(autouse=True)
def _plain_hearing(monkeypatch):
    """No learned mishearings or custom wake phrases from this Mac's settings."""
    monkeypatch.setattr(hearing, "fixes", lambda: [])
    monkeypatch.setattr(hearing, "_custom_phrases", lambda: set())


# --- the window ------------------------------------------------------------------------------------

def _talk(window, start, seconds, step=0.1):
    t = start
    while t < start + seconds:
        window.sound(t, True)
        t += step
    return t


def test_after_the_wake_word_mint_waits_a_few_seconds_then_sleeps():
    w = listening.Window(follow_up=6, wake_grace=8)
    w.woke(100.0)
    assert not w.should_sleep(107.5)
    assert w.should_sleep(108.5)
    assert "wake word" in w.why()


def test_after_a_reply_only_the_follow_up_window():
    w = listening.Window(follow_up=6, wake_grace=8)
    w.woke(0.0)
    _talk(w, 1.0, 2.0)
    w.judged(3.5, for_mint=True)              # "open Slack": for Mint
    assert w.until == math.inf
    assert not w.should_sleep(30.0, busy=True)    # working: never asleep mid-task
    assert not w.should_sleep(30.0, playing=True)
    w.finished(40.0)                           # Mint said "Opened Slack." and is done
    assert not w.should_sleep(45.5)
    assert w.should_sleep(46.5)
    assert w.why() == "no follow-up in 6s"


def test_background_talk_does_not_keep_it_open():
    """1 Oct 20:56-20:59: Hindi chat, "Arey ...", music - Mint stayed open for minutes."""
    w = listening.Window(follow_up=6, wake_grace=8)
    w.finished(0.0)
    end = _talk(w, 4.0, 5.0)                   # someone talks, starting inside the window
    assert not w.should_sleep(end)             # still talking: it may yet be the user
    w.judged(end + 0.5, for_mint=False)        # judged: not for Mint
    assert w.should_sleep(end + 0.6)           # nothing extended: asleep at once
    # More talk that only began after the window had closed holds nothing open.
    w2 = listening.Window(follow_up=6)
    w2.finished(0.0)
    _talk(w2, 7.0, 3.0)
    assert w2.should_sleep(10.0)


def test_a_follow_up_that_starts_in_time_may_finish():
    w = listening.Window(follow_up=6)
    w.finished(0.0)
    end = _talk(w, 5.5, 4.0)                   # began at 5.5 s, talks past the 6 s mark
    assert not w.should_sleep(end + 1.0)       # waiting for the transcript and the verdict
    w.judged(end + 1.5, for_mint=True)
    assert not w.should_sleep(end + 30.0)      # being answered
    # ...but an utterance that never gets a verdict does not hold the window for ever.
    w2 = listening.Window(follow_up=6)
    w2.finished(0.0)
    end2 = _talk(w2, 5.0, 2.0)
    assert w2.should_sleep(end2 + listening.JUDGE_WAIT + 0.1)


def test_another_voice_holds_nothing_open():
    w = listening.Window(follow_up=6)
    w.finished(0.0)
    _talk(w, 5.0, 2.0)
    w.other_voice()                            # the voice lock: someone else
    assert w.should_sleep(7.5)


def test_a_reply_that_never_finishes_is_over_after_a_while():
    w = listening.Window(follow_up=6)
    w.handling()
    assert not w.should_sleep(100.0, idle_for=10)
    assert w.should_sleep(100.0, idle_for=listening.STALE + 1)


def test_follow_up_zero_means_the_wake_word_every_time():
    w = listening.Window(follow_up=0, wake_grace=8)
    w.finished(10.0)
    assert w.should_sleep(10.1)


def test_follow_up_pref_is_clamped(monkeypatch):
    try:
        from mint.core import prefs
    except ImportError:
        from mint import prefs
    for raw, want in ((None, 6.0), ("x", 6.0), (3, 3.0), (-2, 0.0), (500, 30.0)):
        monkeypatch.setattr(prefs, "get", lambda key, raw=raw: raw if key == "follow_up_seconds" else None)
        assert listening.follow_up_seconds() == want


# --- the barrier: rules -----------------------------------------------------------------------------

@pytest.mark.parametrize("text, mint_said", [
    ("I want you to go out of this notch mode.", "Yes, Boss? How can I help you?"),
    ("Just go back to North mode.", "Dropping out of the notch now, back in the orb."),
    ("Arraignment go back to the notch mode.", ""),              # "Hey Mint" misheard, then a command
    ("Payment now again go go outside of this notch mode.", ""),
    ("Now I want you to show me my calendar.", "Flying into the notch now!"),
    ("Also clear from the trash.", "I've emptied the recycle bin."),
    ("Can you play the Spotify?", ""),
    ("Mint, what's on my calendar", ""),
    ("the second one", "Which one should I open: Projects or Downloads?"),
    ("yes", "Should I send it?"),
    ("Call Up Now Spotify.", ""),
])
def test_follow_ups_that_are_requests_act_at_once(text, mint_said):
    assert listening.quick(text, "follow", mint_said) == "act"


@pytest.mark.parametrize("text, mint_said", [
    ("enter Vadodara", "I have successfully created the file."),            # 30 Sep 01:24
    ("hammering", "I've triggered the music playback on Spotify for you, Boss."),
    ("enfermedad", "I've saved the staging server name to your server file, Boss."),
    ("option hoodie jacket", "Samajh gaya Boss."),                          # became a web search
    ("Barf nahi hai na?", "Please let me know how"),                        # answered "Haan, barf nahi"
    ("mat kar.", "Haan, barf nahi"),
    ("Siri, uncle Siri.", "I have created the image of the giraffe eating ice cream for you."),
    ("Arey behan chod.", "Circling around the full screen now, Boss."),
    ("Aniket Aniket Savidal", ""),
    ("आज यह वार करेगा। ठीक है।", "We are already using notch mode, Boss."),
    ("pagare language", ""),
    ("detect detect", ""),
])
def test_follow_ups_that_are_not_requests_never_act_on_the_rules_alone(text, mint_said):
    assert listening.quick(text, "follow", mint_said) is None       # Jev decides


def test_wake_turns_are_trusted_unless_a_short_scrap():
    assert listening.quick("I have a little fever today.", "wake") == "act"
    assert listening.quick("What's the time right now?", "wake") == "act"
    assert listening.quick("Stop the music.", "wake") == "act"
    for scrap in ("Generally parents", "Alex Lake"):                    # 30 Sep / 1 Oct, just after a wake
        assert listening.quick(scrap, "wake") is None
    assert listening.quick("Friday", "wake") == "wait"                 # one stray word: nothing said yet


def test_special_words_and_deliberate_turns_always_get_through():
    assert listening.quick("hmm whatever", "follow", special=True) == "act"    # stop / guard yes / goodbye
    assert listening.quick("Alex Lake", "asked") == "act"                     # shortcut or menu: on purpose
    assert listening.quick("", "follow") is None


def test_jev_verdicts():
    # Follow-ups: only a sure "for Mint" acts.
    assert listening.judge("follow", "mint", 0.77) == "act"            # "No, so there is no music played."
    assert listening.judge("follow", "mint", 0.52) == "ignore"         # unsure: not acted on
    assert listening.judge("follow", "scrap", 0.73) == "ignore"        # "enter Vadodara"
    assert listening.judge("follow", "other", 0.86) == "ignore"        # "Siri, uncle Siri."
    assert listening.judge("follow", None, 0.9) == "ignore"
    assert listening.judge("follow", "mint", 0.45, "Shall I open it?") == "act"   # Mint asked: looser
    # After the wake word: only a sure scrap / other is ignored.
    assert listening.judge("wake", "scrap", 0.77) == "ignore"          # "Generally parents"
    assert listening.judge("wake", "other", 0.76) == "ignore"          # "Alex Lake"
    assert listening.judge("wake", "other", 0.55) == "act"
    assert listening.judge("wake", "mint", 0.3) == "act"
    # A reply about what Mint just did picks up its words: looser (Jev said "other 0.67" for this one).
    assert listening.judge("follow", "other", 0.67, "I've opened the GIF.", "It's not opened in Safari or yet.") == "act"
    assert listening.judge("follow", "other", 0.9, "I've opened the GIF.", "the GIF opened, look") == "ignore"
    assert listening.judge("follow", "mint", 0.45, "Samajh gaya Boss.", "option hoodie jacket") == "ignore"


def test_without_jev_follow_ups_must_look_like_a_question_or_request():
    assert listening.fallback("wake", "Friday") == "act"
    assert listening.fallback("follow", "hammering") == "ignore"
    assert listening.fallback("follow", "option hoodie jacket") == "ignore"
    assert listening.fallback("follow", "आप मतलब अपनी एक हुडी रख लो।") == "ignore"
    assert listening.fallback("follow", "where is the data labeling portal?") == "act"
    assert listening.fallback("follow", "is it done yet for me") == "act"
    assert listening.fallback("follow", "the blue one", "Which one should I use?") == "act"
    assert listening.fallback("follow", "Barf nahi hai na?", "Please let me know how") == "ignore"
    assert listening.fallback("follow", "No, so there is no music played.", "I'm playing music on Spotify.") == "act"


# --- the barrier in the session: held reply, released or dropped -----------------------------------

class _Audio:
    playing = False
    muted = False

    def flush(self):
        pass


def _mint(monkeypatch, kind="follow", heard="", last_said="", jev=None):
    Mint = session_mod.Mint
    m = Mint.__new__(Mint)
    m.ui = session_mod._NoUI()
    m.audio = _Audio()
    m.audio_in = asyncio.Queue()
    m.voice_on, m.half_duplex, m.text_mode, m._silent_reply = True, False, False, False
    m.asleep, m.paused, m.hands_free, m._busy, m._hush = False, False, True, False, False
    m._goodbye_done, m._farewell_bytes = True, -1
    m._suppress_turn, m._said, m._last_said, m._turn_levels = False, "", last_said, []
    m._window = listening.Window(follow_up=6)
    m._window.finished(0.0)
    m._last_voice = 0.0
    m._next_kind = "follow"
    m._printed = []
    monkeypatch.setattr(Mint, "_state", lambda self, *a, **k: None)
    monkeypatch.setattr(Mint, "_print", lambda self, text: self._printed.append(text))
    monkeypatch.setattr(Mint, "_special", lambda self, text: False)
    monkeypatch.setattr(listening, "ask_jev", lambda *a, **k: jev)
    m._reset_verdict()
    m._voice_turn = True
    m._kind = kind
    m._heard = heard
    return m


def test_a_follow_up_not_for_mint_is_never_said_or_done(monkeypatch):
    async def run():
        m = _mint(monkeypatch, heard="enter Vadodara", last_said="I have created the file.", jev=("scrap", 0.73, 0.59))
        m._start_addressee_check()                     # the model began answering
        assert m._verdict_pending()
        m._held.append(b"\x01\x00" * 100)              # "Did you need help with anything else, Boss?"
        await m._await_verdict(2.0)
        assert m._verdict == "ignore" and m._suppress_turn
        assert m.audio_in.empty() and not m._held      # never played
        assert any("not for me" in line for line in m._printed)
        assert m._window.should_sleep(7.0)             # and it did not keep Mint listening
    asyncio.run(run())


def test_a_real_follow_up_plays_once_judged(monkeypatch):
    async def run():
        m = _mint(monkeypatch, heard="No, so there is no music played.", jev=("mint", 0.77, 0.65))
        m._start_addressee_check()
        m._held.append(b"\x01\x00" * 100)
        await m._await_verdict(2.0)
        assert m._verdict == "act" and not m._suppress_turn
        assert m.audio_in.qsize() == 1
        assert not m._window.should_sleep(60.0)       # being answered
    asyncio.run(run())


def test_rules_decide_without_waiting_for_jev(monkeypatch):
    def no_call(*a, **k):
        raise AssertionError("Jev must not be asked")
    m = _mint(monkeypatch, heard="Now I want you to show me my calendar.")
    monkeypatch.setattr(listening, "ask_jev", no_call)
    m._start_addressee_check()
    assert m._verdict == "act" and m._check_task is None


def test_jev_unreachable_falls_back_to_the_rules(monkeypatch):
    async def run():
        m = _mint(monkeypatch, heard="Barf nahi hai na", jev=None)
        m._start_addressee_check()
        await m._await_verdict(2.0)
        assert m._verdict == "ignore"
        w = _mint(monkeypatch, kind="wake", heard="Alex Lake", jev=None)
        w._start_addressee_check()
        await w._await_verdict(2.0)
        assert w._verdict == "act"                      # after the wake word: trusted
    asyncio.run(run())


def test_new_speech_while_mint_works_only_silences_its_reaction(monkeypatch):
    async def run():
        m = _mint(monkeypatch, heard="Make a zip of this folder. ", jev=("other", 0.84, 0.76))
        m._decide("act", "rule")                         # the request itself
        m._busy = True
        m._heard += "Haz de Mickey aparecer."
        m._new_segment()
        m._seg = len("Make a zip of this folder. ")
        m._start_addressee_check()
        await m._await_verdict(2.0)
        assert m._verdict == "ignore"
        assert not m._suppress_turn                      # the zip still gets made
        assert m._mute and m._heard == "Make a zip of this folder. "
    asyncio.run(run())


@pytest.mark.parametrize("text", ["I want to do", "um", "so basically the", "can you", "Hey I want you to",
                                  "okay so", "and then"])
def test_unfinished_words_wait_silently(text):
    """5 Oct: "I want to do" got "it seems your request got cut off"; "ma'am" got "could you repeat"."""
    assert listening.quick(text, "wake") == "wait"
    assert listening.quick(text, "follow") == "wait"


@pytest.mark.parametrize("text", ["what is this", "open that", "tell me", "can you hear me", "pause",
                                  "I wanted to basically install the colorful CSV extension in VS code."])
def test_finished_requests_are_not_held(text):
    assert not listening.unfinished(text)


def test_answers_and_other_scripts_are_not_unfinished():
    assert listening.quick("the second one", "follow", "Which one should I open?") == "act"
    assert not listening.unfinished("आज यह वार करेगा। ठीक है।")

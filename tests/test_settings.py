"""The Settings and first-run screens: the key store (a file, or Windows Credential Manager through a
fake), checking a pasted key without spending, the settings file, the monthly limit, a Civ VI folder
chosen by hand, and the server's settings endpoints. Claude is never called: a fake stands in."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import anthropic
import httpx2 as httpx
import pytest

from civspeak import keys, paths, server, settings, spend, system
from civspeak.answer import MODEL, Answerer, KeyMissing

GOOD_KEY = "sk-ant-api03-" + "x" * 40  # invented: the shape of a key, not a key


class FakeCredentials:
    def __init__(self):
        self.saved: dict[str, str] = {}

    def api(self):
        return (self.saved.get, lambda target, secret, user: self.saved.__setitem__(target, secret), self._delete)

    def _delete(self, target):
        self.saved.pop(target, None)


# -- where the key is kept ------------------------------------------------------------------------


def test_the_key_file_store_keeps_it_private(tmp_path):
    store = keys.FileKeys(tmp_path / "config" / "anthropic-key")
    assert not store.present()
    with pytest.raises(KeyMissing):
        store.read()
    store.save(GOOD_KEY)
    assert store.present() and store.read() == GOOD_KEY
    assert (store.path.stat().st_mode & 0o777) == 0o600
    store.delete()
    assert not store.present()


def test_credential_manager_store_and_moving_an_old_key_file_into_it(tmp_path):
    fake = FakeCredentials()
    legacy = tmp_path / "anthropic-key"
    legacy.write_text(GOOD_KEY + "\n")
    store = keys.CredentialKeys(legacy=legacy, api=fake.api())
    assert store.present()
    assert store.read() == GOOD_KEY
    assert fake.saved == {keys.CREDENTIAL: GOOD_KEY} and not legacy.exists()  # moved, and the file is gone
    store.save("sk-ant-another-" + "y" * 30)
    assert store.read() == "sk-ant-another-" + "y" * 30
    store.delete()
    assert not store.present()
    with pytest.raises(KeyMissing, match="add one in Civspeak's Settings"):
        store.read()


def test_credential_manager_failures_read_as_no_key(tmp_path):
    def broken(target):
        raise OSError(5, "Access is denied")

    store = keys.CredentialKeys(api=(broken, None, None))
    with pytest.raises(KeyMissing, match="could not be read from Windows Credential Manager"):
        store.read()
    assert not store.present()


def test_which_store_this_civspeak_uses(tmp_path, monkeypatch):
    assert isinstance(keys.store(), keys.FileKeys) and keys.store().path == paths.key_file()
    assert keys.store(tmp_path / "k").path == tmp_path / "k"
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    store = keys.store()
    assert isinstance(store, keys.CredentialKeys) and store.legacy == paths.key_file()


# -- checking a pasted key ------------------------------------------------------------------------


def test_a_pasted_key_is_tidied_and_its_shape_checked():
    assert keys.tidy(f'  "{GOOD_KEY[:20]}\n{GOOD_KEY[20:]}" ') == GOOD_KEY
    assert keys.shape_problem("") == "Paste your Claude API key first."
    assert keys.shape_problem("hello").startswith("That isn't a Claude API key: those start with sk-ant-.")
    assert keys.shape_problem("sk-ant-short").startswith("That key looks cut short")
    assert keys.shape_problem(GOOD_KEY + "!").startswith("That key looks cut short")
    assert keys.shape_problem(GOOD_KEY) == ""


def _status_error(cls, code):
    response = httpx.Response(code, request=httpx.Request("GET", "https://api.anthropic.com/v1/models/x"))
    return cls("no", response=response, body=None)


class FakeModels:
    def __init__(self, error=None):
        self.error = error
        self.asked: list[str] = []

    def retrieve(self, model):
        self.asked.append(model)
        if self.error:
            raise self.error


@pytest.mark.parametrize(
    ("error", "says"),
    [
        (None, ""),
        (_status_error(anthropic.AuthenticationError, 401), "Claude did not accept that key."),
        (_status_error(anthropic.PermissionDeniedError, 403), "That key is not allowed to use Claude's models."),
        (_status_error(anthropic.NotFoundError, 404), "That key works, but it cannot use"),
        (anthropic.APIConnectionError(request=httpx.Request("GET", "https://api.anthropic.com")), "Could not reach Claude"),
        (_status_error(anthropic.InternalServerError, 500), "Claude could not check the key just now (error 500)."),
    ],
)
def test_checking_a_key_asks_the_free_models_api(error, says):
    fake = FakeModels(error)
    used = []
    client_for = lambda key: used.append(key) or type("C", (), {"models": fake})()  # noqa: E731
    problem = keys.check(GOOD_KEY, MODEL, client_for)
    assert problem.startswith(says) if says else problem == ""
    assert fake.asked == [MODEL] and used == [GOOD_KEY]


def test_a_badly_shaped_key_is_never_sent_to_claude():
    assert keys.check("not a key", MODEL, lambda key: pytest.fail("must not call Claude")).startswith("That isn't")


# -- the settings file, the monthly limit and the Civ VI folder ------------------------------------


def test_settings_survive_a_round_trip_and_bad_values_fall_back():
    assert settings.load() == settings.Settings()
    settings.update(cap_usd=7.5, setup_done=True)
    assert settings.load() == settings.Settings(cap_usd=7.5, game_dir="", setup_done=True)
    settings.path().write_text(json.dumps({"cap_usd": "lots", "game_dir": 3, "setup_done": "yes", "extra": 1}))
    assert settings.load() == settings.Settings()
    settings.path().write_text("not json")
    assert settings.load() == settings.Settings()
    with pytest.raises(KeyError):
        settings.update(colour="gold")


def test_the_monthly_limit_comes_from_the_environment_then_settings_then_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CIVSPEAK_MONTHLY_CAP_USD", raising=False)
    assert spend.configured_cap() == spend.default_cap() == 10.0  # WSL, as it always was
    monkeypatch.setattr(system, "IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert spend.default_cap() == 5.0  # the Windows app's players
    settings.update(cap_usd=3.0)
    assert spend.configured_cap() == 3.0 and spend.Ledger(tmp_path / "s.jsonl").cap == 3.0
    monkeypatch.setenv("CIVSPEAK_MONTHLY_CAP_USD", "2.5")
    assert spend.configured_cap() == 2.5
    monkeypatch.setenv("CIVSPEAK_MONTHLY_CAP_USD", "nonsense")
    assert spend.configured_cap() == 3.0


def _install(where):
    (where / "Base" / "Assets").mkdir(parents=True, exist_ok=True)
    return where


def test_a_civ_folder_chosen_by_hand(tmp_path):
    game = _install(tmp_path / "steamapps" / "common" / paths.GAME_FOLDER)
    for picked in (game, tmp_path / "steamapps" / "common", tmp_path / "steamapps", tmp_path):
        assert paths.game_dir_from(str(picked)) == (game, "")
    assert paths.game_dir_from("  ") == (None, "Choose the folder Civ VI is installed in.")
    assert paths.game_dir_from(str(tmp_path / "nope"))[1].endswith("is not a folder on this PC.")
    (tmp_path / "empty").mkdir()
    assert paths.game_dir_from(str(tmp_path / "empty"))[1].startswith(f"Civ VI is not in {tmp_path / 'empty'}.")


def test_discovery_uses_the_folder_chosen_in_settings(tmp_path, monkeypatch, fake_game):
    monkeypatch.setenv("CIVSPEAK_USER_DIR", str(fake_game.user_dir))
    monkeypatch.delenv("CIVSPEAK_GAME_DIR", raising=False)
    view = paths.WindowsView(to_local=lambda p: None)
    with pytest.raises(paths.SetupError, match="Choose its folder in Settings"):
        paths.discover(view)
    _install(fake_game.game_dir)
    settings.update(game_dir=str(fake_game.game_dir))
    assert paths.discover(view).game_dir == fake_game.game_dir


# -- the server's settings endpoints --------------------------------------------------------------


class FakeDesktop:
    def __init__(self, folder=""):
        self.folder = folder
        self.login = False
        self.can_choose_folder = True

    def choose_folder(self):
        return self.folder

    def starts_at_login(self):
        return self.login

    def set_start_at_login(self, on):
        self.login = on


@pytest.fixture
def settings_server(tmp_path, monkeypatch):
    monkeypatch.delenv("CIVSPEAK_MONTHLY_CAP_USD", raising=False)
    real_discover = paths.discover
    # Never this PC's own game: an empty Windows, where only a folder chosen in Settings is found.
    monkeypatch.setattr(paths, "discover", lambda view=None: real_discover(paths.WindowsView(to_local=lambda p: None)))
    monkeypatch.setenv("CIVSPEAK_USER_DIR", str(tmp_path))
    store = keys.FileKeys(tmp_path / "config" / "anthropic-key")
    state = server.State(tmp_path / "index", Answerer(spend.Ledger(tmp_path / "spend.jsonl"), key_store=store))
    state.check = lambda: None
    checked = []

    def checker(key, model):
        checked.append((key, model))
        return "" if key == GOOD_KEY else "Claude did not accept that key."

    started = []

    def start(desktop=None):
        handler = type("H", (server.Handler,), {"state": state, "talk": server.Talk(), "desktop": desktop})
        handler.key_checker = staticmethod(checker)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start, state, store, checked
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def get(url):
    return json.loads(urllib.request.urlopen(url, timeout=5).read())


def post(url, body, content_type="application/json"):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": content_type}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


def test_settings_page_data_without_the_windows_app(settings_server):
    start, state, store, _ = settings_server
    url = start()
    v = get(f"{url}/api/settings")
    assert v["native"] is False and v["setup_needed"] is False
    assert v["key"] == {"present": False, "where": str(store.path)}
    assert v["cap"] == {"usd": 10.0, "default": 10.0, "chosen": None, "from_environment": False, "spent": 0.0}
    assert v["models"] is None and v["start_at_login"] is None and v["can_choose_folder"] is False
    assert v["game"]["found"] == "" and v["game"]["problem"] == server.SETUP_PROBLEMS["install"]
    assert post(f"{url}/api/settings", {"start_at_login": True})["problem"].startswith("Start when I log in is a setting")


def test_a_key_is_checked_then_kept_and_never_sent_back(settings_server):
    start, state, store, checked = settings_server
    url = start()
    out = post(f"{url}/api/settings/key", {"key": "sk-ant-wrong-" + "z" * 30})
    assert out["problem"] == "Claude did not accept that key." and not store.present()
    out = post(f"{url}/api/settings/key", {"key": f" {GOOD_KEY}\n"})
    assert out["problem"] == "" and out["settings"]["key"]["present"] is True
    assert store.read() == GOOD_KEY and checked[-1] == (GOOD_KEY, MODEL)
    for body in (json.dumps(out), json.dumps(get(f"{url}/api/settings")), json.dumps(get(f"{url}/api/status"))):
        assert GOOD_KEY not in body and "sk-ant" not in body
    out = post(f"{url}/api/settings/key", {"remove": True})
    assert out["problem"] == "" and not store.present()


def test_settings_changes_are_checked_and_kept(settings_server, tmp_path):
    start, state, store, _ = settings_server
    url = start()
    assert post(f"{url}/api/settings", {"cap_usd": 7.25})["settings"]["cap"]["usd"] == 7.25
    assert state.answerer.ledger.cap == 7.25 and settings.load().cap_usd == 7.25
    assert post(f"{url}/api/settings", {"cap_usd": -1})["problem"].startswith("Choose a monthly limit between")
    assert post(f"{url}/api/settings", {"cap_usd": "five"})["problem"] == "The monthly limit must be a number of dollars."
    assert post(f"{url}/api/settings", {"cap_usd": None})["settings"]["cap"]["usd"] == 10.0
    refused = post(f"{url}/api/settings", {"game_dir": str(tmp_path)})
    assert refused["problem"].startswith(f"Civ VI is not in {tmp_path}.") and settings.load().game_dir == ""
    game = _install(tmp_path / "Games" / paths.GAME_FOLDER)
    assert post(f"{url}/api/settings", {"game_dir": str(tmp_path / "Games")})["problem"] == ""
    assert settings.load().game_dir == str(game)
    assert get(f"{url}/api/settings")["game"] == {
        "found": str(game),
        "user_dir": str(tmp_path),
        "chosen": str(game),
        "problem": "",
        "missing": "",
        "language": "",
    }
    # Other websites cannot change settings (they cannot send JSON to localhost without asking first).
    with pytest.raises(urllib.error.HTTPError) as refused_post:
        post(f"{url}/api/settings", {"cap_usd": 1000}, content_type="text/plain")
    assert refused_post.value.code == 415


def test_the_windows_app_first_run_folder_picker_and_start_at_login(settings_server, tmp_path):
    start, state, store, _ = settings_server
    game = _install(tmp_path / "Epic" / "CivVI")
    desktop = FakeDesktop(folder=str(game))
    url = start(desktop)
    v = get(f"{url}/api/settings")
    assert v["native"] is True and v["setup_needed"] is True and v["can_choose_folder"] is True
    assert v["start_at_login"] is False
    assert post(f"{url}/api/settings", {"start_at_login": True})["settings"]["start_at_login"] is True and desktop.login
    out = post(f"{url}/api/settings/folder", {})
    assert out["problem"] == "" and settings.load().game_dir == str(game)
    desktop.folder = ""  # the player cancelled the picker: nothing changes
    assert post(f"{url}/api/settings/folder", {})["problem"] == "" and settings.load().game_dir == str(game)
    assert post(f"{url}/api/settings", {"setup_done": True})["settings"]["setup_needed"] is False


def test_the_folder_picker_is_only_the_windows_apps(settings_server):
    start, *_ = settings_server
    url = start()
    with pytest.raises(urllib.error.HTTPError) as refused:
        post(f"{url}/api/settings/folder", {})
    assert refused.value.code == 404

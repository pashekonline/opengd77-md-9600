import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT / "webui"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

import pytest
from django.test import Client
from editor.state import state
from opengd77.codeplug import Channel, Codeplug

import gd77_patch  # noqa: F401
from cp_utils import set_channel_enable_bit
from gd77_zones import build_channel_zone_map, create_zone_raw


def _make_cp() -> Codeplug:
    cp = Codeplug(bytes(Codeplug.SIZE))

    for idx, name in ((0, "CALL"), (1, "RPT")):
        ch = Channel()
        ch.data[:] = b"\x00" * len(ch.data)
        ch.name = name
        ch.rx_freq = 145_500_000
        ch.tx_freq = 145_000_000
        ch.mode = 0
        cp.channels[idx] = ch
        set_channel_enable_bit(cp, idx)

    return Codeplug(create_zone_raw(cp, "HOME", [0]))


@pytest.fixture()
def loaded():
    cp = _make_cp()
    state.__init__()
    state.data = bytes(cp)
    state.saved_bytes = state.data
    state.zone_map = build_channel_zone_map(cp)
    return state


@pytest.fixture()
def client(loaded):
    return Client()


def test_index_renders_channels(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "CALL" in body
    assert "RPT" in body
    assert "HOME" in body


def test_index_empty_state():
    state.__init__()
    r = Client().get("/")
    assert r.status_code == 200
    assert "ещё не прочитан" in r.content.decode()


def test_search_filter(client):
    r = client.get("/", {"q": "145.5"})
    assert "CALL" in r.content.decode()

    r = client.get("/", {"q": "нет-такого"})
    assert "CALL" not in r.content.decode()


def test_edit_channel_get(client):
    r = client.get("/channel/0/edit/")
    assert r.status_code == 200
    assert 'value="CALL"' in r.content.decode()


def test_edit_channel_post(client):
    r = client.post(
        "/channel/0/edit/",
        {
            "name": "CALL2",
            "rx": "145.750",
            "tx": "145.250",
            "mode": "FM",
            "rx_tone": "88.5",
            "tx_tone": "",
            "cc": "0",
            "contact": "0",
            "zones": ["0"],
        },
        follow=True,
    )
    assert r.status_code == 200
    assert state.dirty
    cp = state.codeplug()
    assert cp.channels[0].name == "CALL2"
    assert cp.channels[0].rx_freq == 145_750_000


def test_edit_channel_validation_error(client):
    r = client.post(
        "/channel/0/edit/",
        {
            "name": "ПАРТИЗА",
            "rx": "abc",
            "tx": "",
            "mode": "FM",
            "rx_tone": "",
            "tx_tone": "",
            "cc": "0",
            "contact": "0",
        },
    )
    body = r.content.decode()
    assert "ASCII" in body
    assert "не число" in body
    assert not state.dirty


def test_new_channel_finds_free_slot(client):
    client.post(
        "/channel/new/",
        {
            "name": "NEW",
            "rx": "433.500",
            "tx": "433.500",
            "mode": "DMR",
            "rx_tone": "",
            "tx_tone": "",
            "cc": "3",
            "contact": "7",
        },
        follow=True,
    )
    cp = state.codeplug()
    names = {ch.name for ch in cp.channels if ch.name}
    assert "NEW" in names


def test_delete_channel(client):
    r = client.post("/channel/0/delete/", follow=True)
    assert r.status_code == 200
    cp = state.codeplug()
    assert cp.channels[0].name == ""


def test_duplicate_channel(client):
    client.post("/channel/0/duplicate/", follow=True)
    cp = state.codeplug()
    names = [ch.name for ch in cp.channels if ch.name]
    assert "CALL copy" in names


def test_undo_redo(client):
    client.post(
        "/channel/0/edit/",
        {
            "name": "ZZZ",
            "rx": "145.000",
            "tx": "145.000",
            "mode": "FM",
            "rx_tone": "",
            "tx_tone": "",
            "cc": "0",
            "contact": "0",
        },
        follow=True,
    )
    client.post("/undo/", follow=True)
    assert state.codeplug().channels[0].name == "CALL"

    client.post("/redo/", follow=True)
    assert state.codeplug().channels[0].name == "ZZZ"


def test_zones_page(client):
    body = client.get("/zones/").content.decode()
    assert "HOME" in body


def test_zone_create_rename_delete(client):
    client.post("/zones/", {"action": "create", "name": "MOBILE"}, follow=True)
    labels = [n for _, n in state.zone_labels()]
    assert "MOBILE" in labels

    zid = next(z for z, n in state.zone_labels() if n == "MOBILE")
    client.post(
        "/zones/",
        {"action": "rename", "zid": zid, f"name_{zid}": "MOB2"},
        follow=True,
    )
    labels = [n for _, n in state.zone_labels()]
    assert "MOB2" in labels

    client.post("/zones/", {"action": "delete", "zid": zid}, follow=True)
    labels = [n for _, n in state.zone_labels()]
    assert "MOB2" not in labels


def test_csv_export(client):
    r = client.get("/csv/export/")
    assert r.status_code == 200
    assert "attachment" in r["Content-Disposition"]
    text = r.content.decode("utf-8-sig")
    assert "name;rx_freq" in text
    assert "CALL" in text


def test_csv_import_preview_and_apply(client):
    csv_text = (
        "index;name;rx_freq;tx_freq;mode;rx_tone;tx_tone;"
        "tx_color;contact_num;zones\n"
        "0;SAT1;145.850;145.850;FM;;;0;0;\n"
        "1;BAD;not_a_freq;1;;;\n"
    )
    r = client.post(
        "/csv/import/",
        {"file": io.BytesIO(csv_text.encode("utf-8"))},
    )
    body = r.content.decode()
    assert "SAT1" in body
    assert "Строка 3" in body

    r = client.post("/csv/import/", {"action": "apply"}, follow=True)
    assert r.status_code == 200
    names = [ch.name for ch in state.codeplug().channels if ch.name]
    assert "SAT1" in names
    assert "BAD" not in names


def test_backup(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = client.post("/backup/", follow=True)
    assert r.status_code == 200
    assert list(tmp_path.glob("codeplug_backup_*.g77"))


def test_write_confirm_requires_write_word(client):
    body = client.get("/radio/write/").content.decode()
    assert "WRITE" in body

    r = client.post("/radio/write/", {"confirm_text": "write"})
    assert "заглавными" in r.content.decode()


def test_api_job_idle(client):
    import json

    r = client.get("/api/job/")
    assert json.loads(r.content.decode()) == {"active": False}


def test_read_requires_port_or_fails_gracefully(client, monkeypatch):
    monkeypatch.setattr("editor.radio.detect_port", lambda *a, **k: None)
    monkeypatch.setattr("editor.radio.port_candidates", list)
    r = client.post("/radio/read/", follow=True)
    assert r.status_code == 200

    from time import sleep

    for _ in range(50):
        if any("Рация не найдена" in e for e in state.events()):
            break
        sleep(0.05)

    assert any("Рация не найдена" in e for e in state.events())




# ---------------------------------------------------------------------------
# Спутники
# ---------------------------------------------------------------------------

SAT_JSON = [
    {
        "name": "ISS",
        "norad_id": 25544,
        "rx_freq_mhz": 437.800,
        "tx_freq_mhz": 145.990,
        "ctcss_tx_hz": 67.0,
        "arm_ctcss_hz": None,
        "secondary_rx_freq_mhz": 145.825,
        "secondary_tx_freq_mhz": 145.825,
        "tertiary_rx_freq_mhz": 145.800,
        "tertiary_tx_freq_mhz": None,
    },
    {
        "name": "SO-50",
        "norad_id": 27607,
        "rx_freq_mhz": 436.795,
        "tx_freq_mhz": 145.850,
        "ctcss_tx_hz": 67.0,
        "arm_ctcss_hz": 74.4,
    },
]

TLE1 = "1 25544U 98067A   26234.51780937  .00016717  00000-0  10270-3 0  9002"
TLE2 = "2 25544  51.6416 208.9163 0004088 300.1200  60.0000 15.50440000000000"


@pytest.fixture()
def sat_config(monkeypatch, tmp_path):
    import json

    from editor import sats

    path = tmp_path / "satellites.json"
    path.write_text(json.dumps(SAT_JSON), encoding="utf-8")
    monkeypatch.setattr(sats, "CONFIG_PATH", path)
    state.__init__()
    return sats, path


def _wait_job(timeout=5.0):
    from time import sleep

    from editor.radio import radio

    for _ in range(int(timeout / 0.05)):
        job = radio.job
        if job is None or job["status"] != "running":
            return job
        sleep(0.05)

    raise AssertionError("задача не завершилась")


def test_sats_page_lists_config(client, sat_config):
    body = client.get("/satellites/").content.decode()
    assert "ISS" in body
    assert "SO-50" in body
    assert "25544" in body


def test_fetch_tle_job(client, sat_config, monkeypatch):
    from editor import sats as sats_mod

    monkeypatch.setattr(
        sats_mod,
        "fetch_tle",
        lambda nid, name="", **k: (TLE1, TLE2),
    )

    r = client.post("/satellites/", {"action": "fetch_tle"})
    assert r.status_code == 302
    job = _wait_job()
    assert job["status"] == "done"
    assert set(state.sat_tle) == {"ISS", "SO-50"}

    body = client.get("/satellites/").content.decode()
    assert "26234.51780937" in body


def test_build_block_from_config(sat_config):
    import struct

    sats_mod, _ = sat_config
    cache = {"ISS": (TLE1, TLE2), "SO-50": (TLE1, TLE2)}

    block = sats_mod.build_block(cache)
    assert len(block) == sats_mod.ls.SAT_BLOCK_LEN
    assert block[:3] == b"ISS"

    # частоты в записи — герцы (не децигерцы: иначе 437.798 на экране
    # рации превращается в 43.779)
    rx1, tx1 = struct.unpack("<II", block[48:56])
    assert rx1 == 437_800_000
    assert tx1 == 145_990_000


def test_sat_add_validation(client, sat_config):
    r = client.post("/satellites/new/", {
        "name": "ОСП-1",
        "norad_id": "abc",
        "rx_freq_mhz": "",
        "tx_freq_mhz": "",
    })
    body = r.content.decode()
    assert "ASCII" in body
    assert "NORAD ID" in body
    assert "RX/TX" in body


def test_sat_add_duplicate_name(client, sat_config):
    r = client.post("/satellites/new/", {
        "name": "ISS",
        "norad_id": "25544",
        "rx_freq_mhz": "437.8",
        "tx_freq_mhz": "145.99",
    })
    assert "уже есть в списке" in r.content.decode()


def test_sat_add_edit_delete_saves_json(client, sat_config):
    import json

    _sats, path = sat_config

    r = client.post("/satellites/new/", {
        "name": "AO-91",
        "norad_id": "43017",
        "rx_freq_mhz": "435.250",
        "tx_freq_mhz": "435.250",
        "ctcss_tx_hz": "",
        "arm_ctcss_hz": "",
    }, follow=True)
    assert r.status_code == 200

    rows = json.loads(path.read_text(encoding="utf-8"))
    assert [row["name"] for row in rows] == ["ISS", "SO-50", "AO-91"]
    assert rows[2]["norad_id"] == 43017
    assert "tertiary_rx_freq_mhz" in rows[2]

    # редактирование AO-91 (slot 2)
    client.post("/satellites/2/edit/", {
        "name": "AO-91",
        "norad_id": "43017",
        "rx_freq_mhz": "145.950",
        "tx_freq_mhz": "435.250",
    }, follow=True)
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert rows[2]["rx_freq_mhz"] == 145.95

    # удаление SO-50 (slot 1)
    client.post("/satellites/", {"action": "delete", "slot": "1"},
                follow=True)
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert [row["name"] for row in rows] == ["ISS", "AO-91"]


def test_sat_edit_form_prefills(client, sat_config):
    body = client.get("/satellites/0/edit/").content.decode()
    assert 'value="ISS"' in body
    assert "25544" in body


CATALOG_SAMPLE = (
    "ISS (ZARYA)\n"
    "1 25544U 98067A   26234.51780937  .00016717  00000-0  10270-3 0  9002\n"
    "2 25544  51.6416 208.9163 0004088 300.1200  60.0000 15.50440000000000\n"
    "SO-50\n"
    "1 27607U 02058A   26233.45678900  .00002345  00000-0  12345-4 0  9999\n"
    "2 27607 101.4567 123.4567 0009876  87.6543 270.0000  6.45000000 99991\n"
)

TX_SAMPLE = [
    {
        "description": "Mode V/U FM Voice CTCSS 67.0 Hz",
        "alive": True,
        "uplink_low": 145850000,
        "downlink_low": 436795000,
        "mode": "FM",
        "uplink_mode": "FM",
        "invert": False,
        "norad_cat_id": 27607,
        "status": "active",
    },
    {
        "description": "Mode V TLM",
        "alive": False,
        "uplink_low": None,
        "downlink_low": 149025000,
        "mode": "FM",
        "uplink_mode": None,
        "norad_cat_id": 27607,
        "status": "inactive",
    },
]


def _fake_http_get(url, timeout=30.0):
    if url.endswith("satonline.txt"):
        return CATALOG_SAMPLE.encode()

    import io
    import zipfile

    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("transmitters.json", json.dumps(TX_SAMPLE))

    return buf.getvalue()


@pytest.fixture()
def sat_sources(monkeypatch):
    from editor import sats as sats_mod

    monkeypatch.setattr(sats_mod, "_http_get", _fake_http_get)
    monkeypatch.setattr(sats_mod, "_tle_catalog", {"fetched": 0.0, "data": {}})
    monkeypatch.setattr(
        sats_mod, "_transmitters", {"fetched": 0.0, "data": {}}
    )
    return sats_mod


def test_catalog_parse_and_fetch_tle(sat_sources):
    sats_mod = sat_sources
    cat = sats_mod.load_tle_catalog()
    assert set(cat) == {25544, 27607}

    l1, _l2 = sats_mod.fetch_tle(27607, "SO-50")
    assert l1.startswith("1 27607U")
    assert sats_mod.epoch_of(l1) == "26233.45678900"

    with pytest.raises(ValueError):
        sats_mod.fetch_tle(999999)


def test_find_norad_from_catalog(sat_sources):
    res = sat_sources.find_norad("so-5")
    assert res == [{"norad_id": 27607, "name": "SO-50"}]


def test_transmitters_parsing(sat_sources):
    rows = sat_sources.find_transmitters(27607)
    assert len(rows) == 2
    active = rows[0]
    assert active["rx"] == 436.795
    assert active["tx"] == 145.85
    assert active["tone"] == 67.0
    assert active["active"] is True
    assert rows[1]["active"] is False
    assert rows[1]["tx"] is None

    assert sat_sources.find_transmitters(1) == []


def test_norad_search_endpoint(client, sat_config, sat_sources):
    r = client.get("/satellites/norad-search", {"q": "ISS"})
    data = json.loads(r.content.decode())
    assert data["results"] == [{"norad_id": 25544, "name": "ISS (ZARYA)"}]

    r = client.get("/satellites/norad-search", {"q": "I"})
    assert json.loads(r.content.decode())["results"] == []


def test_transmitters_endpoint(client, sat_config, sat_sources):
    r = client.get("/satellites/transmitters", {"norad": "27607"})
    data = json.loads(r.content.decode())
    assert data["error"] is None
    assert data["rows"][0]["rx"] == 436.795
    assert data["rows"][0]["tone"] == 67.0

    r = client.get("/satellites/transmitters", {"norad": "abc"})
    assert json.loads(r.content.decode())["rows"] == []


def test_sat_write_job(client, sat_config, monkeypatch):
    import struct

    from editor import sats as sats_mod

    state.sat_tle = {"ISS": (TLE1, TLE2), "SO-50": (TLE1, TLE2)}
    expected = sats_mod.build_block(state.sat_tle)
    written = {}

    class FakeSession:
        def __init__(self, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def _read_area(self, addr, length):
            if addr == sats_mod.ls.CUSTOM_DATA_ADDR:
                return b"OpenGD77"
            if addr == sats_mod.ls.CUSTOM_DATA_ADDR + 12:
                return struct.pack("<II", 3, sats_mod.ls.SAT_BLOCK_LEN)
            return b"\x00" * length

        def write_region(self, addr, data, old=None):
            written["addr"] = addr
            written["data"] = data
            return 1

    monkeypatch.setattr(sats_mod.ls, "RadioSession", FakeSession)
    monkeypatch.setattr("editor.radio.detect_port", lambda *a, **k: "COM_TEST")

    client.post("/satellites/", {"action": "write"})
    job = _wait_job()
    assert job["status"] == "done", job["result"]
    assert written["data"] == expected
    assert job["done"] == 5 and job["total"] == 5


def test_sat_write_same_block_skips(client, sat_config, monkeypatch):
    import struct

    from editor import sats as sats_mod

    state.sat_tle = {"ISS": (TLE1, TLE2), "SO-50": (TLE1, TLE2)}
    expected = sats_mod.build_block(state.sat_tle)

    class FakeSession:
        def __init__(self, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def _read_area(self, addr, length):
            if addr == sats_mod.ls.CUSTOM_DATA_ADDR:
                return b"OpenGD77"
            if addr == sats_mod.ls.CUSTOM_DATA_ADDR + 12:
                return struct.pack("<II", 3, sats_mod.ls.SAT_BLOCK_LEN)
            return expected

        def write_region(self, addr, data, old=None):
            raise AssertionError("писать не нужно")

    monkeypatch.setattr(sats_mod.ls, "RadioSession", FakeSession)
    monkeypatch.setattr("editor.radio.detect_port", lambda *a, **k: "COM_TEST")

    client.post("/satellites/", {"action": "write"})
    job = _wait_job()
    assert job["status"] == "done"
    assert "идентичен" in job["result"]


def test_sat_dash_values_treated_as_empty(client, sat_config):
    _sats, path = sat_config

    r = client.post("/satellites/new/", {
        "name": "BEACON",
        "norad_id": "1234",
        "rx_freq_mhz": "435.0",
        "tx_freq_mhz": "—",
        "ctcss_tx_hz": "—",
    }, follow=True)
    assert r.status_code == 200

    rows = json.loads(path.read_text(encoding="utf-8"))
    added = next(x for x in rows if x["name"] == "BEACON")
    assert added["tx_freq_mhz"] is None
    assert added["ctcss_tx_hz"] is None


def test_sat_form_no_placeholders_or_hint(client, sat_config):
    body = client.get("/satellites/new/").content.decode()
    assert "placeholder" not in body
    assert "как в satellites.json" not in body


def test_sat_full_name_saved_and_radio_truncated(client, sat_config):
    sats_mod, path = sat_config

    client.post("/satellites/new/", {
        "name": "UMKA-1 (RS40S)",
        "norad_id": "4444",
        "rx_freq_mhz": "435.0",
        "tx_freq_mhz": "435.0",
    }, follow=True)

    rows = json.loads(path.read_text(encoding="utf-8"))
    assert any(r["name"] == "UMKA-1 (RS40S)" for r in rows)

    state.sat_tle = {"UMKA-1 (RS40S)": (TLE1, TLE2)}
    block = sats_mod.build_block(state.sat_tle)
    assert block[2 * 100:2 * 100 + 6] == b"UMKA-1"
    assert sats_mod.radio_name("SAUDISAT-1C (SO-50)") == "SAUDISAT"
    assert sats_mod.radio_name("SO-50") == "SO-50"

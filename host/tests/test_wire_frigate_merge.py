"""wire_frigate() must MERGE the site MQTT settings into Frigate's config.

It used to overwrite /etc/frigate/config.yml with just mqtt + "cameras: {}",
erasing the frigate-docker builder's baseline (VAAPI hwaccel args, go2rtc
WebRTC candidates) and turning existing cameras into "name: ..." placeholders.

Runs the REAL function out of ctrlable-firstboot.sh in bash against a temp
config. Pass a different script path as argv[1] to run it against another
revision (the old one must fail).
"""
import json, os, subprocess, sys, tempfile, textwrap
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "firstboot", "ctrlable-firstboot.sh")

BASELINE = {
    "mqtt": {"enabled": False},
    "semantic_search": {"enabled": False},
    "go2rtc": {"log": {"level": "info"},
               "webrtc": {"candidates": ["10.0.0.5:8555", "stun:8555"]}},
    "ffmpeg": {"hwaccel_args": "-hwaccel vaapi -hwaccel_device /dev/dri/renderD128"},
    "cameras": {"front": {"ffmpeg": {"inputs": [{"path": "rtsp://cam/1", "roles": ["detect"]}]}}},
}


def extract(fn: str) -> str:
    src = open(SCRIPT).read()
    start = src.index(f"{fn}() {{")
    end = src.index("\n}\n", start) + 3
    # Older revisions hardcode the directory; point them at the temp dir too,
    # so every revision is judged on the same file.
    return src[start:end].replace("local cfg_dir=/etc/frigate", 'local cfg_dir="$FRIGATE_CONFIG_DIR"')


def run_wire(cfg_dir: str, wire_to: dict) -> subprocess.CompletedProcess:
    prog = "\n".join([
        'log() { echo "$*"; }', 'die() { echo "FATAL $*"; exit 1; }',
        extract("jget"), extract("wire_frigate"),
        "systemctl() { :; }",
        f"FRIGATE_CONFIG_DIR={cfg_dir} wire_frigate '{json.dumps(wire_to)}'",
    ])
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True)


def setup(cfg=BASELINE):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "config.yml"), "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False)
    return d


def load(d):
    return yaml.safe_load(open(os.path.join(d, "config.yml")))


def test_mqtt_is_set():
    d = setup()
    r = run_wire(d, {"mqtt_host": "10.0.0.9", "mqtt_port": 1883, "mqtt_user": "u", "mqtt_pass": "p"})
    assert r.returncode == 0, r.stdout + r.stderr
    m = load(d)["mqtt"]
    assert m["enabled"] is True and m["host"] == "10.0.0.9" and m["port"] == 1883
    assert m["user"] == "u" and m["password"] == "p"


def test_baseline_survives():
    d = setup()
    run_wire(d, {"mqtt_host": "10.0.0.9"})
    cfg = load(d)
    assert cfg.get("go2rtc") == BASELINE["go2rtc"], cfg.get("go2rtc")
    assert cfg.get("ffmpeg") == BASELINE["ffmpeg"], cfg.get("ffmpeg")
    assert cfg.get("semantic_search") == {"enabled": False}


def test_cameras_survive():
    d = setup()
    run_wire(d, {"mqtt_host": "10.0.0.9"})
    assert load(d)["cameras"] == BASELINE["cameras"], load(d)["cameras"]


def test_no_credentials_omits_user():
    d = setup()
    run_wire(d, {"mqtt_host": "10.0.0.9"})
    assert "user" not in load(d)["mqtt"]


def test_no_broker_leaves_config_untouched():
    # The deploy used to pass mqtt_host "" when no broker was given; Frigate
    # then logged "Invalid host" forever. No broker = keep MQTT disabled.
    for wire in ({"mqtt_host": ""}, {}):
        d = setup()
        before = open(os.path.join(d, "config.yml")).read()
        r = run_wire(d, wire)
        assert r.returncode == 0, r.stdout + r.stderr
        assert open(os.path.join(d, "config.yml")).read() == before, load(d).get("mqtt")


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"ok   {name}")
            except Exception as exc:
                failed += 1; print(f"FAIL {name}: {str(exc)[:200]}")
    print("all passed" if not failed else f"{failed} failed")
    sys.exit(1 if failed else 0)

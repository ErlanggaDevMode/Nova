"""Checks for the Nova 3D avatar asset and its static serving."""
import json
import struct
from pathlib import Path

from fastapi.testclient import TestClient

from nova_core.main import app

GLB_PATH = Path(__file__).resolve().parents[2] / "nova_core" / "web" / "avatar" / "nova_avatar.glb"
REQUIRED_MORPHS = {"blink", "mouth_open", "smile", "confused"}

client = TestClient(app)


def _gltf_json(data: bytes) -> dict:
    chunk_len, chunk_type = struct.unpack_from("<II", data, 12)
    assert chunk_type == 0x4E4F534A, "first GLB chunk must be JSON"
    return json.loads(data[20:20 + chunk_len])


def test_glb_header_and_size():
    data = GLB_PATH.read_bytes()
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    assert magic == b"glTF"
    assert version == 2
    assert length == len(data)
    assert len(data) < 2_500_000


def test_glb_has_skin_and_morphs():
    gltf = _gltf_json(GLB_PATH.read_bytes())
    assert len(gltf.get("skins", [])) >= 1
    names = set()
    for mesh in gltf.get("meshes", []):
        names.update(mesh.get("extras", {}).get("targetNames", []))
    assert REQUIRED_MORPHS <= names


def test_avatar_module_served():
    res = client.get("/static/avatar.js")
    assert res.status_code == 200
    assert "nova:state" in res.text


def test_three_vendored_locally():
    res = client.get("/static/vendor/three/three.module.min.js")
    assert res.status_code == 200
    res = client.get("/static/vendor/three/addons/loaders/GLTFLoader.js")
    assert res.status_code == 200


def test_dashboard_has_avatar_card_and_importmap():
    html = (Path(__file__).resolve().parents[2] / "nova_core" / "web" / "index.html").read_text()
    assert 'id="nova-avatar"' in html
    assert '"three": "/static/vendor/three/three.module.min.js"' in html
    assert 'src="/static/avatar.js"' in html
    assert "https://" not in html.split("importmap")[1].split("</script>")[0]


def test_glb_served_statically():
    res = client.get("/static/avatar/nova_avatar.glb")
    assert res.status_code == 200
    ctype = res.headers.get("content-type", "")
    assert "gltf" in ctype or "octet-stream" in ctype

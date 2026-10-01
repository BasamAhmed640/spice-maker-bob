"""Offline acquisition controls; synthetic bytes are not manufacturer device evidence."""

from __future__ import annotations

import email.message
import hashlib
import json
import stat
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from boardmodeler.authoring import official_spice as module
from boardmodeler.authoring.official_spice import (
    OfficialSpiceError,
    acquire_official_spice,
    discover_model_links,
)
from boardmodeler.security.network import NetworkRefused

URL = "https://www.ti.com/lit/zip/example"
MODEL = b"* synthetic fixture\r\n.SUBCKT TEST123 IN OUT GND PARAMS: GAIN=1\r\nR1 IN OUT 1k\r\n.ENDS TEST123\r\n"


def _zip(members: dict[str, bytes]) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return stream.getvalue()


def _acquire(monkeypatch, tmp_path: Path, payload: bytes):
    monkeypatch.setattr(
        module, "_download", lambda *args, **kwargs: (payload, "application/octet-stream", URL)
    )
    return acquire_official_spice(URL, tmp_path, allowed_hosts=("ti.com",))


def test_exact_original_bytes_native_ports_and_package_hash(monkeypatch, tmp_path: Path) -> None:
    payload = _zip({"TEST123.lib": MODEL, "README.md": b"Synthetic fixture only"})
    bundle = _acquire(monkeypatch, tmp_path, payload)
    assert bundle.source_sha256 == hashlib.sha256(payload).hexdigest()
    assert (bundle.bundle_dir / "TEST123.lib").read_bytes() == MODEL
    assert (bundle.bundle_dir.parent / "source.zip").read_bytes() == payload
    assert bundle.entries[0].name == "TEST123"
    assert bundle.entries[0].ports == ("IN", "OUT", "GND")
    manifest = json.loads((bundle.bundle_dir.parent / "acquisition.json").read_text())
    assert manifest["adapted"] is False
    assert manifest["ltspice_compatibility"] == "NOT_RUN"
    assert manifest["electrical_validation"] == "UNKNOWN"


def test_continuation_and_spaced_parameters_are_not_ports(monkeypatch, tmp_path: Path) -> None:
    model = b".SUBCKT TEST123 IN OUT\n+ GND GAIN = 1\nR1 IN OUT 1k\n.ENDS\n"
    bundle = _acquire(monkeypatch, tmp_path, model)
    assert bundle.entries[0].ports == ("IN", "OUT", "GND")


def test_model_primitive_entry_preserved(monkeypatch, tmp_path: Path) -> None:
    bundle = _acquire(monkeypatch, tmp_path, b".model TEST123 D(Is=1n)\n")
    assert bundle.entries[0].kind == "model"
    assert bundle.entries[0].model_type == "D"


def test_native_model_links_first_then_plain_pspice_before_tina() -> None:
    html = b"""<a href="/lit/zip/a">TINA-TI SPICE model</a>
    <a href="/lit/zip/b">Unencrypted PSpice model</a>
    <a href="/lit/zip/c">LTspice model</a>
    <a href="https://evil.test/model.lib">LTspice model</a>
    <a href="/bad.exe">LTspice installer</a>
    <a href="/guide.pdf">LTspice application guide</a>
    <a href="/ltspice.html">LTspice information</a>
    <a href="/lit/zip/d">Encrypted PSpice model</a>"""
    links = discover_model_links(
        "https://www.ti.com/product/TEST123", html, allowed_hosts=("ti.com",)
    )
    assert [link.url.rsplit("/", 1)[-1] for link in links] == ["c", "b", "a"]
    assert links[0].native_ltspice is True
    assert links[1].native_ltspice is False


@pytest.mark.parametrize(
    "name",
    ["../evil.lib", "C:/evil.lib", "//host/share/evil.lib", "CON.lib", "a.exe", "a.dll", "a.cmd"],
)
def test_unsafe_archive_rejected_before_any_model_is_saved(
    monkeypatch, tmp_path: Path, name: str
) -> None:
    with pytest.raises(OfficialSpiceError):
        _acquire(monkeypatch, tmp_path, _zip({"TEST123.lib": MODEL, name: b"bad"}))
    assert not list(tmp_path.rglob("*.lib"))


def test_zip_symlink_and_case_collision_rejected(monkeypatch, tmp_path: Path) -> None:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        info = zipfile.ZipInfo("linked.lib")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "outside.lib")
    with pytest.raises(OfficialSpiceError, match="archive_path_refused"):
        _acquire(monkeypatch, tmp_path, stream.getvalue())
    with pytest.raises(OfficialSpiceError, match="archive_duplicate_path"):
        _acquire(monkeypatch, tmp_path, _zip({"a.lib": MODEL, "A.LIB": MODEL}))


@pytest.mark.parametrize(
    "target",
    [
        "../../outside.lib",
        "\\\\host\\share\\outside.lib",
        "C:\\outside.lib",
        "https://evil.test/model.lib",
        "%APPDATA%/model.lib",
        "${HOME}/model.lib",
    ],
)
def test_unsafe_dependency_rejected_before_resolve(
    monkeypatch, tmp_path: Path, target: str
) -> None:
    model = f'.include\n+ "{target}"\n'.encode() + MODEL
    with pytest.raises(OfficialSpiceError, match="dependency_outside_bundle"):
        _acquire(monkeypatch, tmp_path, model)
    assert not list(tmp_path.rglob("*.lib"))


def test_nested_nonstandard_dependency_is_inspected(monkeypatch, tmp_path: Path) -> None:
    main = b'.include "helper.inc"\n' + MODEL
    with pytest.raises(OfficialSpiceError, match="dependency_outside_bundle"):
        _acquire(
            monkeypatch,
            tmp_path,
            _zip({"TEST123.lib": main, "helper.inc": b'.include "../outside.lib"\n'}),
        )
    good = _acquire(
        monkeypatch, tmp_path, _zip({"TEST123.lib": main, "helper.inc": b".param EXTRA=1\n"})
    )
    assert (good.bundle_dir / "helper.inc").read_bytes() == b".param EXTRA=1\n"


def test_unresolved_extensionless_lib_is_not_assumed_section(monkeypatch, tmp_path: Path) -> None:
    with pytest.raises(OfficialSpiceError, match="dependency_missing"):
        _acquire(monkeypatch, tmp_path, b".lib outside\n" + MODEL)
    bundle = _acquire(monkeypatch, tmp_path, b".lib typical\n" + MODEL + b".endl typical\n")
    assert bundle.entries[0].name == "TEST123"


def test_matching_endl_cannot_disguise_unsafe_library_path(monkeypatch, tmp_path: Path) -> None:
    model = b'.lib "\\\\host\\share\\outside"\n' + MODEL + b".endl \\host\\share\\outside\n"
    with pytest.raises(OfficialSpiceError, match="dependency_outside_bundle"):
        _acquire(monkeypatch, tmp_path, model)


@pytest.mark.parametrize(
    "directive",
    [
        ".wave C:/outside.wav V(OUT)",
        ".savebias C:/outside",
        ".loadbias C:/outside",
        ".savestate C:/outside",
        ".loadstate C:/outside",
        ".postrun calc.exe",
        "! calc.exe",
        ".control",
        ".shell calc.exe",
        '.include "missing.lib"',
    ],
)
def test_external_execution_output_and_missing_dependencies_rejected(
    monkeypatch, tmp_path: Path, directive: str
) -> None:
    with pytest.raises(OfficialSpiceError):
        _acquire(monkeypatch, tmp_path, directive.encode() + b"\n" + MODEL)


@pytest.mark.parametrize(
    "payload",
    [b".protect\n" + MODEL, b"$CDNENCSTART\n" + MODEL, b"\0binary", b"<html>login required</html>"],
)
def test_encrypted_binary_and_html_not_accepted_as_model(
    monkeypatch, tmp_path: Path, payload: bytes
) -> None:
    with pytest.raises(OfficialSpiceError):
        _acquire(monkeypatch, tmp_path, payload)


def test_mutated_saved_original_is_not_overwritten(monkeypatch, tmp_path: Path) -> None:
    bundle = _acquire(monkeypatch, tmp_path, MODEL)
    bundle.entries[0].file.write_bytes(b"changed")
    with pytest.raises(OfficialSpiceError, match="original_changed"):
        _acquire(monkeypatch, tmp_path, MODEL)
    assert bundle.entries[0].file.read_bytes() == b"changed"


def test_network_off_refuses_before_opener(monkeypatch) -> None:
    def refused(stage: str):
        raise NetworkRefused(stage)

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled network must not construct opener")

    monkeypatch.setattr(module, "require_network", refused)
    monkeypatch.setattr(module, "build_opener", forbidden)
    with pytest.raises(OfficialSpiceError, match="internet_access_off"):
        module.fetch_official_bytes(URL, allowed_hosts=("ti.com",))


def test_document_fetch_checks_authority_response_and_size(monkeypatch) -> None:
    class Response:
        headers = email.message.Message()
        headers["Content-Type"] = "text/html"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def geturl(self):
            return "https://www.ti.com/page"

        def read(self, cap):
            if getattr(self, "delivered", False):
                return b""
            self.delivered = True
            return b"page"

    class Opener:
        def open(self, request, timeout):
            assert 0 < timeout <= 7
            return Response()

    monkeypatch.setattr(module, "require_network", lambda stage: None)
    monkeypatch.setattr(module, "_require_public_host", lambda url: None)
    monkeypatch.setattr(module, "build_opener", lambda handler: Opener())
    assert module.fetch_official_bytes(
        "https://www.ti.com/page", allowed_hosts=("ti.com",), timeout_s=7
    ) == (b"page", "text/html")
    with pytest.raises(OfficialSpiceError, match="vendor_refused"):
        module.fetch_official_bytes("https://evil.test/page", allowed_hosts=("ti.com",))
    with pytest.raises(OfficialSpiceError, match="manufacturer_authority_invalid"):
        module.fetch_official_bytes("https://evil.test/page", allowed_hosts=("evil.test",))


def test_private_manufacturer_dns_is_controlled_refusal(monkeypatch) -> None:
    monkeypatch.setattr(module, "require_network", lambda stage: None)

    def refused(url):
        raise module.FetchRefused("host_refused: manufacturer resolved to private address")

    monkeypatch.setattr(module, "_require_public_host", refused)
    with pytest.raises(OfficialSpiceError, match="host_refused"):
        module.fetch_official_bytes("https://www.ti.com/page", allowed_hosts=("ti.com",))


@pytest.mark.parametrize("status", [403, 404, 429, 503])
def test_http_status_is_retained_without_server_details(monkeypatch, status) -> None:
    from urllib.error import HTTPError

    response = BytesIO(b"private server body")

    class Opener:
        def open(self, request, timeout):
            raise HTTPError(
                "https://www.ti.com/page?token=private-token",
                status,
                "private server message",
                {"Set-Cookie": "private-cookie"},
                response,
            )

    monkeypatch.setattr(module, "require_network", lambda stage: None)
    monkeypatch.setattr(module, "_require_public_host", lambda url: None)
    monkeypatch.setattr(module, "build_opener", lambda handler: Opener())
    with pytest.raises(OfficialSpiceError) as error:
        module.fetch_official_bytes("https://www.ti.com/page", allowed_hosts=("ti.com",))
    assert str(error.value) == f"official_download_failed: HTTP {status}"
    assert response.closed


def test_archive_caps_are_checked_before_saving(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(module, "_MAX_FILES", 1)
    with pytest.raises(OfficialSpiceError, match="archive_limit"):
        _acquire(monkeypatch, tmp_path, _zip({"TEST123.lib": MODEL, "readme.md": b"synthetic"}))
    assert not list(tmp_path.rglob("*.lib"))


def test_parent_junction_refused_before_resolving_its_child(monkeypatch, tmp_path: Path) -> None:
    payload = _zip(
        {
            "nested/TEST123.lib": b'.include "helper.inc"\n' + MODEL,
            "nested/helper.inc": b".param EXTRA=1\n",
        }
    )
    root = tmp_path / "official" / hashlib.sha256(payload).hexdigest() / "original"
    junction = root / "nested"
    original_resolve = Path.resolve

    def guarded_resolve(path, *args, **kwargs):
        if path == junction or junction in path.parents:
            raise AssertionError("a junction child must not be resolved")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(Path, "is_junction", lambda path: path == junction)
    with pytest.raises(OfficialSpiceError, match="original_path_link"):
        _acquire(monkeypatch, tmp_path, payload)


@pytest.mark.parametrize(
    "out_dir",
    [r"\\host\share\models", "//host/share/models", "https://host/models", r"\\?\C:\models"],
)
def test_network_output_path_refused_before_download_or_filesystem(
    monkeypatch, out_dir: str
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("unsafe output path must not download or touch filesystem")

    monkeypatch.setattr(module, "_download", forbidden)
    monkeypatch.setattr(Path, "absolute", forbidden)
    with pytest.raises(OfficialSpiceError, match="original_path_refused"):
        acquire_official_spice(URL, out_dir, allowed_hosts=("ti.com",))

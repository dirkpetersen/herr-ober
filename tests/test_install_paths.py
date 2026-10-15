#!/usr/bin/env python3
"""Tests for install path resolution and systemd ProtectHome handling."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from ober import config as ober_config
from ober.commands import bootstrap as bs
from ober.commands.bootstrap import bootstrap
from ober.config import OberConfig, _get_default_install_path


class TestResolutionOrder:
    def test_env_var_wins(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pointer = temp_dir / "ptr"
        pointer.write_text(f"{temp_dir}\n")
        monkeypatch.setattr(ober_config, "INSTALL_POINTER_PATH", pointer)
        monkeypatch.setenv("OBER_INSTALL_PATH", "/from/env")
        assert _get_default_install_path() == Path("/from/env")

    def test_pointer_file(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pointer = temp_dir / "ptr"
        target = temp_dir / "opt-ober"
        target.mkdir()
        pointer.write_text(f"{target}\n")
        monkeypatch.setattr(ober_config, "INSTALL_POINTER_PATH", pointer)
        assert _get_default_install_path() == target

    @pytest.mark.parametrize("content", [None, "", "  \n"])
    def test_pointer_missing_or_empty_ignored(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch, content: str | None
    ) -> None:
        pointer = temp_dir / "ptr"
        if content is not None:
            pointer.write_text(content)
        monkeypatch.setattr(ober_config, "INSTALL_POINTER_PATH", pointer)
        with patch("sys.prefix", "/some/venv"), patch("sys.base_prefix", "/usr"):
            assert _get_default_install_path() == Path("/some/venv")

    def test_unreadable_pointer_ignored(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A directory cannot be read as a file -> OSError
        monkeypatch.setattr(ober_config, "INSTALL_POINTER_PATH", temp_dir)
        with patch("sys.prefix", "/some/venv"), patch("sys.base_prefix", "/usr"):
            assert _get_default_install_path() == Path("/some/venv")

    def test_tempdir_fallback(self) -> None:
        import tempfile

        with patch("sys.prefix", "/usr"), patch("sys.base_prefix", "/usr"):
            assert _get_default_install_path() == Path(tempfile.gettempdir()) / "ober"

    def test_load_uses_pointer(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        install = temp_dir / "custom"
        OberConfig(install_path=install).save()
        pointer = temp_dir / "ptr"
        pointer.write_text(str(install))
        monkeypatch.setattr(ober_config, "INSTALL_POINTER_PATH", pointer)
        assert OberConfig.load().install_path == install


class TestBootstrapPointer:
    def test_bootstrap_writes_pointer(self, temp_dir: Path) -> None:
        install = temp_dir / "ober"
        system = MagicMock(is_supported=True, is_root=True)
        obj = MagicMock(system=system)
        with (
            patch.object(bs, "_apply_kernel_tuning"),
            patch.object(bs, "_install_packages"),
            patch.object(bs, "_setup_venv"),
            patch.object(bs, "_install_exabgp"),
            patch.object(bs, "_create_systemd_services"),
            patch.object(bs, "_create_config_templates"),
            patch.object(bs, "_configure_watchdog"),
            patch.object(bs, "_open_firewall_ports"),
            patch.object(bs, "_get_current_venv_path", return_value=None),
        ):
            result = CliRunner().invoke(bootstrap, [str(install), "-y"], obj=obj)
        assert result.exit_code == 0, result.output
        assert ober_config.INSTALL_POINTER_PATH.read_text().strip() == str(install)
        assert not str(ober_config.INSTALL_POINTER_PATH).startswith("/etc/ober")


class TestProtectHome:
    def _units(self, install: Path, venv: Path, tmp: Path) -> dict[str, str]:
        config = OberConfig(install_path=install)
        sysd = tmp / "systemd"
        sysd.mkdir()
        real_path = Path

        def fake_path(*a: str) -> Path:
            if a == ("/etc/systemd/system",):
                return sysd
            return real_path(*a)

        with (
            patch.object(bs, "Path", side_effect=fake_path),
            patch.object(bs, "run_command"),
        ):
            bs._create_systemd_services(config, venv)
        return {f.name: f.read_text() for f in sysd.iterdir()}

    def test_home_venv_read_only(self, temp_dir: Path) -> None:
        units = self._units(
            Path("/root/.local/pipx/venvs/herr-ober"),
            Path("/root/.local/pipx/venvs/herr-ober"),
            temp_dir,
        )
        assert len(units) == 3
        for text in units.values():
            assert "ProtectHome=read-only" in text
            assert "ProtectHome=true" not in text

    def test_opt_is_true(self, temp_dir: Path) -> None:
        units = self._units(Path("/opt/ober"), Path("/opt/pipx/venvs/herr-ober"), temp_dir)
        for text in units.values():
            assert "ProtectHome=true" in text

    def test_venv_under_home_install_elsewhere(self, temp_dir: Path) -> None:
        units = self._units(Path("/opt/ober"), Path("/home/u/venv"), temp_dir)
        for text in units.values():
            assert "ProtectHome=read-only" in text

    def test_is_under_home(self) -> None:
        assert bs._is_under_home(Path("/root"))
        assert bs._is_under_home(Path("/home/x/y"))
        assert not bs._is_under_home(Path("/homestead"))
        assert not bs._is_under_home(Path("/opt/ober"))

    def test_is_under_home_symlink(self, temp_dir: Path) -> None:
        link = temp_dir / "link"
        link.symlink_to("/home")
        assert bs._is_under_home(link / "x")
        assert not bs._is_under_home(Path("/homestead"))


class TestPointerHardening:
    def _point(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        pointer = temp_dir / "ptr"
        pointer.write_text(text)
        monkeypatch.setattr(ober_config, "INSTALL_POINTER_PATH", pointer)

    def test_trailing_newline_stripped(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = temp_dir / "inst"
        target.mkdir()
        self._point(temp_dir, monkeypatch, f"{target}\r\n")
        assert ober_config._read_install_pointer() == target

    def test_relative_pointer_ignored(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._point(temp_dir, monkeypatch, "relative/dir\n")
        assert ober_config._read_install_pointer() is None

    def test_stale_pointer_falls_through(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._point(temp_dir, monkeypatch, str(temp_dir / "gone"))
        with patch("sys.prefix", "/some/venv"), patch("sys.base_prefix", "/usr"):
            assert _get_default_install_path() == Path("/some/venv")


def _run_bootstrap(args: list[str], venv: Path | None, **extra: MagicMock) -> object:
    system = MagicMock(is_supported=True, is_root=True)
    obj = MagicMock(system=system)
    with (
        patch.object(bs, "_apply_kernel_tuning"),
        patch.object(bs, "_install_packages"),
        patch.object(bs, "_setup_venv"),
        patch.object(bs, "_install_exabgp"),
        patch.object(bs, "_create_systemd_services"),
        patch.object(bs, "_configure_watchdog"),
        patch.object(bs, "_open_firewall_ports"),
        patch.object(bs, "_create_config_templates", **extra),
        patch.object(bs, "_get_current_venv_path", return_value=venv),
    ):
        return CliRunner().invoke(bootstrap, args, obj=obj)


class TestBootstrapPaths:
    def test_relative_path_stored_absolute(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(temp_dir)
        result = _run_bootstrap(["rel/ober", "-y"], None)
        assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
        recorded = ober_config.INSTALL_POINTER_PATH.read_text().strip()
        assert recorded == str((temp_dir / "rel" / "ober").resolve())

    def test_tilde_path_stored_absolute(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(temp_dir))
        result = _run_bootstrap(["~/ober", "-y"], None)
        assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
        recorded = ober_config.INSTALL_POINTER_PATH.read_text().strip()
        assert recorded == str((temp_dir / "ober").resolve())

    def test_venv_mode_reuses_pointer(self, temp_dir: Path) -> None:
        existing = temp_dir / "existing"
        existing.mkdir()
        ober_config.INSTALL_POINTER_PATH.parent.mkdir(parents=True)
        ober_config.INSTALL_POINTER_PATH.write_text(f"{existing}\n")
        venv = temp_dir / "venv"
        result = _run_bootstrap(["-y"], venv)
        assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
        assert ober_config.INSTALL_POINTER_PATH.read_text().strip() == str(existing)
        assert "Reusing existing install path" in result.output  # type: ignore[attr-defined]

    def test_venv_mode_without_pointer_uses_venv(self, temp_dir: Path) -> None:
        venv = temp_dir / "venv"
        result = _run_bootstrap(["-y"], venv)
        assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
        assert ober_config.INSTALL_POINTER_PATH.read_text().strip() == str(venv)


class TestBootstrapRerun:
    def _templates(self, config: OberConfig, venv: Path) -> None:
        system = MagicMock()
        system.get_local_ip.return_value = "10.1.1.1"
        bs._create_config_templates(config, system, venv)

    def test_existing_config_kept(self, temp_dir: Path) -> None:
        install = temp_dir / "inst"
        venv = temp_dir / "v"
        first = OberConfig(install_path=install)
        first.ensure_directories()
        self._templates(first, venv)

        custom = OberConfig.load(first.config_path)
        custom.stats_port = 9999
        custom.ha_mode = "keepalived"
        custom.save()
        first.haproxy_config_path.write_text("# custom haproxy\n")
        first.bgp_config_path.write_text("# custom bgp\n")
        first.keepalived_config_path.write_text("# custom ka\n")

        self._templates(OberConfig(install_path=install), venv)

        reloaded = OberConfig.load(first.config_path)
        assert reloaded.stats_port == 9999
        assert reloaded.ha_mode == "keepalived"
        assert reloaded.install_path == install
        assert reloaded.venv_path == venv
        assert first.haproxy_config_path.read_text() == "# custom haproxy\n"
        assert first.bgp_config_path.read_text() == "# custom bgp\n"
        assert first.keepalived_config_path.read_text() == "# custom ka\n"

    def test_kernel_tuning_nonlocal_bind(self) -> None:
        assert "net.ipv4.ip_nonlocal_bind = 1" in bs.KERNEL_TUNING


class TestUninstall:
    def _run(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch, pipx_venv: Path | None):
        from ober.commands import uninstall as un

        install = temp_dir / "inst"
        install.mkdir()
        home = temp_dir / "home"
        home.mkdir()
        monkeypatch.setenv("OBER_INSTALL_PATH", str(install))
        ober_config.INSTALL_POINTER_PATH.parent.mkdir(parents=True)
        ober_config.INSTALL_POINTER_PATH.write_text(f"{install}\n")
        sysd = temp_dir / "etc" / "systemd" / "system"
        sysd.mkdir(parents=True)
        for name in ("http", "bgp", "ha"):
            (sysd / f"ober-{name}.service").write_text("x")
        real_path = Path

        def fake_path(*a: str) -> Path:
            if a and isinstance(a[0], str) and a[0].startswith(("/etc/systemd", "/etc/sysctl")):
                return real_path(str(temp_dir) + a[0])
            if a == ("/usr/local/bin/ober",):
                return temp_dir / "nonexistent-ober"
            return real_path(*a)

        fake_path.home = lambda: home  # type: ignore[attr-defined]
        service = MagicMock(is_active=True, is_enabled=True)
        calls: list[list[str]] = []
        sub = MagicMock()
        system = MagicMock(is_root=True)
        with (
            patch.object(un, "Path", new=fake_path),
            patch.object(un, "ServiceInfo") as si,
            patch.object(un, "run_command", side_effect=lambda c, **_: calls.append(c)),
            patch.object(un, "_remove_vip_interface"),
            patch.object(un, "_is_pipx_install", return_value=pipx_venv),
            patch.object(un, "check_command_exists", return_value=pipx_venv is not None),
            patch("subprocess.run", sub),
        ):
            si.from_service_name.return_value = service
            result = CliRunner().invoke(un.uninstall, ["-y"], obj=MagicMock(system=system))
        return result, calls, sysd, sub

    def test_removes_pointer_and_ha(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        result, calls, sysd, _ = self._run(temp_dir, monkeypatch, None)
        assert result.exit_code == 0, result.output
        assert not ober_config.INSTALL_POINTER_PATH.exists()
        assert not (sysd / "ober-ha.service").exists()
        assert ["systemctl", "stop", "ober-ha"] in calls
        assert ["systemctl", "disable", "ober-ha"] in calls

    def test_pipx_env_global(self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        venv = Path("/opt/pipx/venvs/herr-ober")
        result, _, _, sub = self._run(temp_dir, monkeypatch, venv)
        assert result.exit_code == 0, result.output
        env = sub.call_args.kwargs["env"]
        assert env["PIPX_HOME"] == "/opt/pipx"
        assert env["PIPX_BIN_DIR"] == "/usr/local/bin"

    def test_pipx_env_legacy_root_uses_defaults(
        self, temp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PIPX_HOME", raising=False)
        monkeypatch.delenv("PIPX_BIN_DIR", raising=False)
        venv = Path("/root/.local/pipx/venvs/herr-ober")
        result, _, _, sub = self._run(temp_dir, monkeypatch, venv)
        assert result.exit_code == 0, result.output
        env = sub.call_args.kwargs["env"]
        assert "PIPX_HOME" not in env
        assert "PIPX_BIN_DIR" not in env

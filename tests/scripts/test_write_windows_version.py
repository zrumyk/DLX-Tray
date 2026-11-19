"""Check the fixed and display versions in the Windows executable resource."""

import pytest

from scripts import write_windows_version


def test_uses_release_version_for_file_and_product() -> None:
    resource = write_windows_version.render_version_info("1.22.65535")
    assert "filevers=(1, 22, 65535, 0)" in resource
    assert "prodvers=(1, 22, 65535, 0)" in resource
    assert "StringStruct('FileVersion', '1.22.65535')" in resource
    assert "StringStruct('ProductVersion', '1.22.65535')" in resource


@pytest.mark.parametrize("version", ["1.2", "1.2.3rc1", "1.2.65536"])
def test_rejects_versions_without_a_windows_release_mapping(version: str) -> None:
    with pytest.raises(ValueError, match="version"):
        write_windows_version.render_version_info(version)

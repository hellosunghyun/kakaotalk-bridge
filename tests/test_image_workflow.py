import os
import re
import subprocess
from pathlib import Path

from device import iris

ROOT = Path(__file__).resolve().parents[1]
CHANGES = ROOT / ".github/scripts/image-changes.sh"
TAGS = ROOT / ".github/scripts/image-tags.sh"
FLATTEN = ROOT / ".github/scripts/flatten-apks.sh"


def run(script, cwd, env, args=()):
    output = cwd / "github-output.txt"
    output.write_text("")
    result = subprocess.run(
        ["bash", str(script), *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "GITHUB_OUTPUT": str(output), **env},
    )
    return result, output.read_text()


def parse(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    )


def init_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("docs\n")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-q", "-m", "base")
    return subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()


def commit_file(repo, relative, content):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git(repo, "add", relative)
    git(repo, "commit", "-q", "-m", relative)


def changed(tmp_path, relative, content, **env):
    repo = tmp_path / relative.replace("/", "_")
    repo.mkdir()
    base = init_repo(repo)
    commit_file(repo, relative, content)
    result, text = run(
        CHANGES,
        repo,
        {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main", "BASE_SHA": base, **env},
    )
    assert result.returncode == 0, result.stderr
    return parse(text)


def test_dockerfile_keeps_amd64_android_tools_and_collector_build():
    text = (ROOT / "docker/device.Dockerfile").read_text()
    assert re.search(r"^FROM --platform=linux/amd64 \S+ AS android-build$", text, re.MULTILINE)
    assert "FROM scratch AS apk-artifacts" in text
    assert f"ARG COLLECTOR_BUILD={iris.BUILD}" in text
    assert "COPY --from=apk-artifacts /iris.apk /opt/iris.apk" in text
    workflow = (ROOT / ".github/workflows/images.yml").read_text()
    assert "ubuntu-24.04-arm" in workflow
    assert "setup-qemu-action" not in workflow
    assert "platforms: linux/arm64" in workflow
    assert "platforms: linux/amd64" in workflow
    assert "cancel-in-progress: true" in workflow
    compose = (ROOT / "compose.yaml").read_text()
    assert "${SERVER_IMAGE:-${COLLECTOR_IMAGE:-kakaotalk-bridge/server:local}}" in compose
    assert "${SERVER_IMAGE:-${DOT_IMAGE:-${COLLECTOR_IMAGE:-kakaotalk-bridge/server:local}}}" in compose
    assert "${DEVICE_IMAGE:-kakaotalk-bridge/device:local}" in compose
    assert "${GATEWAY_IMAGE:-kakaotalk-bridge/gateway:local}" in compose


def test_path_filters_follow_image_inputs(tmp_path):
    docs = changed(tmp_path, "docs/operations.md", "notes\n")
    assert docs == {"push": "true", "device": "false", "server": "false", "gateway": "false"}

    android = changed(tmp_path, "android/bridge.kt", "class Bridge\n")
    assert android["device"] == "true"
    assert android["server"] == "false"
    assert android["gateway"] == "false"

    server = changed(tmp_path, "server/app.py", "print('api')\n")
    assert server["device"] == "true" and server["server"] == "true" and server["gateway"] == "false"

    ops = changed(tmp_path, "ops/cli.py", "print('ops')\n")
    assert ops["server"] == "true" and ops["device"] == "false" and ops["gateway"] == "false"

    gateway = changed(tmp_path, "docker/gateway.Dockerfile", "FROM scratch\n")
    assert gateway["gateway"] == "true" and gateway["device"] == "false" and gateway["server"] == "false"

    lock = changed(tmp_path, "requirements.lock", "hash\n")
    assert lock["device"] == "true" and lock["server"] == "true" and lock["gateway"] == "false"


def test_dispatch_tags_and_pull_requests_control_push(tmp_path):
    result, text = run(
        CHANGES,
        tmp_path,
        {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "WORKFLOW_IMAGES": "server",
        },
    )
    assert result.returncode == 0, result.stderr
    assert parse(text) == {"push": "true", "device": "false", "server": "true", "gateway": "false"}

    result, text = run(
        CHANGES,
        tmp_path,
        {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/tags/v1.2.3", "BASE_SHA": "0" * 40},
    )
    assert parse(text)["push"] == "true"
    assert parse(text)["device"] == "true" and parse(text)["gateway"] == "true"

    base = init_repo(tmp_path)
    commit_file(tmp_path, "android/bridge.kt", "class Bridge\n")
    result, text = run(
        CHANGES,
        tmp_path,
        {
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_REF": "refs/pull/7/merge",
            "BASE_SHA": base,
        },
    )
    parsed = parse(text)
    assert result.returncode == 0, result.stderr
    assert parsed["push"] == "false" and parsed["device"] == "true" and parsed["server"] == "false"


def test_image_tags_use_sha_branch_and_collector_build(tmp_path):
    sha = "abcdef1234567890abcdef1234567890abcdef12"
    result, text = run(
        TAGS,
        tmp_path,
        {
            "GITHUB_REPOSITORY": "HelloSunghyun/KakaoTalk-Bridge-Private",
            "GITHUB_SHA": sha,
            "GITHUB_REF_NAME": "feat/webhook-forwarder",
        },
        ["device", iris.BUILD],
    )
    assert result.returncode == 0, result.stderr
    assert "image=ghcr.io/hellosunghyun/kakaotalk-bridge-private/device" in text
    assert "ghcr.io/hellosunghyun/kakaotalk-bridge-private/device:abcdef1" in text
    assert "ghcr.io/hellosunghyun/kakaotalk-bridge-private/device:feat-webhook-forwarder" in text
    assert iris.BUILD in text
    assert text.strip().endswith("EOF")


def test_image_tags_reject_invalid_extra_tag(tmp_path):
    result = subprocess.run(
        ["bash", str(TAGS), "device", "not a tag"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(tmp_path / "out.txt"),
            "GITHUB_REPOSITORY": "hellosunghyun/kakaotalk-bridge-private",
            "GITHUB_SHA": "b" * 40,
            "GITHUB_REF_NAME": "main",
        },
    )
    assert result.returncode != 0
    assert "invalid image tag" in result.stderr


def test_flatten_apks_lifts_nested_platform_directory(tmp_path):
    nested = tmp_path / "linux_amd64"
    nested.mkdir()
    names = (
        "apksigner.jar",
        "bridge.apk",
        "iris.apk",
        "iris-source.tar.gz",
        "iris-dependencies.txt",
    )
    for name in names:
        (nested / name).write_bytes(b"x")
    subprocess.run(["bash", str(FLATTEN), str(tmp_path)], check=True)
    assert (tmp_path / "iris.apk").read_bytes() == b"x"
    missing = subprocess.run(
        ["bash", str(FLATTEN), str(tmp_path / "empty")],
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing.returncode != 0


def test_release_workflow_publishes_native_multiarch_images_and_release():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert 'tags: ["v*"]' in workflow
    assert "ubuntu-24.04-arm" in workflow
    assert "setup-qemu-action" not in workflow
    assert "push-by-digest=true" in workflow
    assert "imagetools create" in workflow
    assert 'image="ghcr.io/${repo}/${kind}"' in workflow
    assert "gh release create" in workflow
    assert "scripts/build-release.py" in workflow
    images = (ROOT / ".github/workflows/images.yml").read_text()
    assert 'tags: ["**"]' not in images

#!/usr/bin/env python3
"""Build, install, inspect or remove the quietbridge authorization plug-in.

No command stores a password. install/uninstall require an administrator shell.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
BUNDLE_NAME = "WorkBuddyCUAAuthorizationPlugin.bundle"
SOURCE_BUNDLE = ROOT / "build" / BUNDLE_NAME
INSTALLED_BUNDLE = Path("/Library/Security/SecurityAgentPlugins") / BUNDLE_NAME
STATE_DIRECTORY = Path("/Library/Application Support/WorkBuddyCUAAuthorizationPlugin")
STATE_FILE = STATE_DIRECTORY / "installation.plist"
ORIGINAL_RIGHT_FILE = STATE_DIRECTORY / "system.login.screensaver.original.plist"
PLUGIN_IDENTIFIER = "com.quietbridge.AuthorizationPlugin"
LEGACY_PLUGIN_IDENTIFIER = "com.workbuddy.cua.AuthorizationPlugin"
TEAM_IDENTIFIER = "62RNP54C47"
PLUGIN_MECHANISM = "WorkBuddyCUAAuthorizationPlugin:allow"
# Historical value from the privileged-host experiment. Kept only so the state
# ledger and rollback tooling can recognise an installation made with it.
LEGACY_PRIVILEGED_MECHANISM = "WorkBuddyCUAAuthorizationPlugin:allow,privileged"
RIGHT = "system.login.screensaver"
# User confirmed native full-screen UI and password unlock after logout/login
# on 2026-11-05 with the direct-login-window-ui deny-only installation.
DIRECT_UI_VALIDATED_AFTER_FRESH_LOGIN = True


def checked_path(path):
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        if node.is_symlink():
            raise ValueError("拒绝符号链接路径: %s" % node)
    return path


def authorization_read(right=RIGHT):
    process = subprocess.run(
        ["/usr/bin/security", "authorizationdb", "read", right],
        check=False, capture_output=True)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.decode("utf-8", "replace").strip() or
                           "无法读取 authorization right")
    return process.stdout


def authorization_write(data, right=RIGHT):
    process = subprocess.run(
        ["/usr/bin/security", "authorizationdb", "write", right],
        input=data, check=False, capture_output=True)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.decode("utf-8", "replace").strip() or
                           "无法写入 authorization right")


def require_supported_lock_ui(deny_only=False):
    if deny_only or DIRECT_UI_VALIDATED_AFTER_FRESH_LOGIN:
        return
    raise RuntimeError(
        "自动解锁安装已禁用：新的顶层 use-login-window-ui 授权结构"
        "仍需注销重新登录后的界面验证。仅允许 --experimental --deny-only 安装验证版。")


def transformed_right(source, include_plugin=True):
    raise RuntimeError("安装已禁用：必须显式选择 --experimental；旧授权结构不再支持。")


REMOTE_RIGHT = "com.workbuddy.cua.AuthorizationPlugin.remote"
STOCK_RIGHT = "com.workbuddy.cua.AuthorizationPlugin.stock"


def candidate_policy(source):
    """Generate reviewable files only. This does not install or prove UI fallback."""
    original = plistlib.loads(source)
    if (not isinstance(original, dict) or original.get("class") != "rule"
            or original.get("rule") not in (["use-login-window-ui"], "use-login-window-ui")
            or "mechanisms" in original):
        raise ValueError("只支持未定制的 use-login-window-ui 原始规则；拒绝猜测迁移")
    # loginwindow selects its native UI from the top-level rule array. Wrapping
    # this marker in a stock subrule changed the UI after a fresh login.
    primary = dict(original)
    # authorizationdb does not persist tries for class=rule; it belongs to the
    # evaluate-mechanisms branch. Compare the persisted policy without weakening
    # read-back verification for other fields.
    primary.pop("tries", None)
    primary.update({"class": "rule", "k-of-n": 1,
                    "rule": [REMOTE_RIGHT, "use-login-window-ui"]})
    return {
        REMOTE_RIGHT: {
            "class": "evaluate-mechanisms",
            "mechanisms": [PLUGIN_MECHANISM],
            "shared": False,
            "tries": 1,
        },
        RIGHT: primary,
    }


def export_candidate(destination):
    # Use an exclusive new directory so existing plans/backups are never replaced.
    plan = candidate_policy(authorization_read())
    destination = checked_path(destination)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, value in plan.items():
        (destination / (name + ".plist")).write_bytes(plistlib.dumps(value))
    return {"candidateDirectory": str(destination), "installed": False,
            "validatedForUnlock": False, "rights": list(plan)}


def verify_bundle(bundle):
    bundle = checked_path(bundle)
    signature = subprocess.run(
        ["/usr/bin/codesign", "-dvvv", str(bundle)],
        check=False, capture_output=True, text=True)
    details = signature.stdout + signature.stderr
    verify = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--strict", "-R",
         '=anchor apple generic and identifier "' + PLUGIN_IDENTIFIER +
         '" and certificate leaf[subject.OU] = "' + TEAM_IDENTIFIER + '"', str(bundle)],
        check=False, capture_output=True)
    if verify.returncode != 0:
        raise ValueError("Authorization Plug-in 签名校验失败")
    if ("Identifier=%s" % PLUGIN_IDENTIFIER) not in details:
        raise ValueError("Authorization Plug-in identifier 不匹配")
    if ("TeamIdentifier=%s" % TEAM_IDENTIFIER) not in details:
        raise ValueError("Authorization Plug-in Team ID 不匹配")
    return details


def status():
    right = plistlib.loads(authorization_read())
    mechanisms = right.get("mechanisms", right.get("rule", []))
    installed = INSTALLED_BUNDLE.is_dir()
    registered = PLUGIN_MECHANISM in mechanisms or REMOTE_RIGHT in mechanisms
    state = None
    if STATE_FILE.is_file():
        state = plistlib.loads(STATE_FILE.read_bytes())
    return {
        "installed": installed,
        "registered": registered,
        "mechanisms": mechanisms,
        "state": state,
    }


def require_root(action):
    if os.geteuid() != 0:
        raise PermissionError("%s 需要管理员权限；请先检查 install.py status，再用 sudo 执行" % action)


def build_bundle():
    process = subprocess.run(["make", "clean", "all"], cwd=ROOT, check=False)
    if process.returncode != 0:
        raise RuntimeError("Authorization Plug-in 构建或签名失败")
    return SOURCE_BUNDLE


def atomic_plist(path, values, mode=0o600):
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(plistlib.dumps(values))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def policy_matches(actual, expected):
    """Ignore database timestamps, but compare all policy-bearing keys."""
    def normalized(value):
        value = dict(value)
        for key in ("created", "modified", "version", "comment"):
            value.pop(key, None)
        for key in ("rule", "mechanisms"):
            if isinstance(value.get(key), str):
                value[key] = [value[key]]
        return value
    actual, expected = normalized(actual), normalized(expected)
    # security authorizationdb adds its creator identity on newly created rules.
    # Accept only the exact observed Apple pair, never arbitrary requirements.
    if "identifier" not in expected and "requirement" not in expected:
        if (actual.get("identifier") == "com.apple.security" and
                actual.get("requirement") == 'identifier "com.apple.security" and anchor apple'):
            actual.pop("identifier")
            actual.pop("requirement")
    return actual == expected


def write_verified(right, value):
    authorization_write(plistlib.dumps(value), right)
    actual = plistlib.loads(authorization_read(right))
    if not policy_matches(actual, value):
        raise RuntimeError("授权规则读回与写入内容不一致: " + right +
                           "；实际内容=" + json.dumps(actual, ensure_ascii=False, sort_keys=True))
    return actual


def right_exists(right):
    process = subprocess.run(
        ["/usr/bin/security", "authorizationdb", "read", right],
        capture_output=True, check=False)
    if process.returncode == 0:
        return True
    # Fail closed on unexpected errors; security reports -60005 for no rule.
    if b"-60005" not in process.stderr:
        raise RuntimeError("无法确认授权规则是否存在: " + right)
    return False


def remove_branch(right):
    if right not in (REMOTE_RIGHT, STOCK_RIGHT):
        raise ValueError("拒绝移除非本插件分支")
    if not right_exists(right):
        return
    subprocess.run(["/usr/bin/security", "authorizationdb", "remove", right], check=True)
    if right_exists(right):
        raise RuntimeError("无法移除插件分支: " + right)


def install(bundle=None, experimental=False, deny_only=False):
    if not experimental:
        transformed_right(b"")  # legacy installation stays disabled
    # Validate the new UI topology after a fresh login before enabling grants.
    require_supported_lock_ui(deny_only=deny_only)
    require_root("install")
    source = checked_path(bundle or SOURCE_BUNDLE)
    signature = verify_bundle(source)
    bundle_info = plistlib.loads((source / "Contents" / "Info.plist").read_bytes())
    if bundle_info.get("CUADenyOnly", False) is not deny_only:
        raise RuntimeError("插件 CUADenyOnly 与请求的安装模式不匹配")
    checked_path(STATE_DIRECTORY)
    checked_path(INSTALLED_BUNDLE)
    if STATE_DIRECTORY.exists() or INSTALLED_BUNDLE.exists():
        raise RuntimeError("存在旧安装或未完成事务；请先查看 status 并恢复，不覆盖备份")
    current_raw = authorization_read()
    plan = candidate_policy(current_raw)
    for right in (REMOTE_RIGHT, STOCK_RIGHT):
        if right_exists(right):
            raise RuntimeError("候选分支已存在；拒绝覆盖: " + right)

    STATE_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(STATE_DIRECTORY, 0o755)
    # Exclusive creation: never overwrite the recovery copy.
    with ORIGINAL_RIGHT_FILE.open("xb") as stream:
        os.chmod(ORIGINAL_RIGHT_FILE, 0o600)
        stream.write(current_raw)
        stream.flush()
        os.fsync(stream.fileno())
    recovery = STATE_DIRECTORY / "recovery.py"
    with recovery.open("xb") as stream:
        os.chmod(recovery, 0o600)
        stream.write(Path(__file__).read_bytes())
    metadata = {
        "recoveryScript": str(recovery),
        "version": 2, "protocolVersion": 3,
        "enabled": False, "experimentalOptIn": True,
        "validatedForUnlock": False, "phase": "prepared",
        "policyTopology": "direct-login-window-ui",
        "validatedAfterFreshLogin": DIRECT_UI_VALIDATED_AFTER_FRESH_LOGIN,
        "denyOnly": deny_only,
        "installedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "bundleIdentifier": PLUGIN_IDENTIFIER, "teamIdentifier": TEAM_IDENTIFIER,
        "mechanism": PLUGIN_MECHANISM, "right": RIGHT,
        "bundlePath": str(INSTALLED_BUNDLE), "expectedPolicies": plan,
        "signature": [line for line in signature.splitlines()
                      if line.startswith(("Authority=", "TeamIdentifier="))],
    }
    atomic_plist(STATE_FILE, metadata, 0o644)
    staging = INSTALLED_BUNDLE.with_name("." + BUNDLE_NAME + ".install")
    checked_path(staging)
    if staging.exists():
        raise RuntimeError("存在旧暂存目录；备份已保留，停止安装")
    primary_touched = False
    try:
        INSTALLED_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, staging, symlinks=False)
        subprocess.run(["/usr/sbin/chown", "-R", "root:wheel", str(staging)], check=True)
        subprocess.run(["/bin/chmod", "-R", "go-w", str(staging)], check=True)
        verify_bundle(staging)
        staging.rename(INSTALLED_BUNDLE)
        # Dependencies first, primary entry last. A mechanism never replaces the
        # login-window rule. Keep each actual read-back for uninstall conflict checks.
        actual = {}
        for right in (REMOTE_RIGHT, RIGHT):
            if right == RIGHT:
                # Refuse intervening policy edits before touching the entry point.
                if plistlib.loads(authorization_read()) != plistlib.loads(current_raw):
                    raise RuntimeError("系统授权规则已被其他进程修改；停止安装")
                primary_touched = True
            actual[right] = write_verified(right, plan[right])
            metadata["phase"] = "wrote:" + right
            metadata["actualPolicies"] = actual
            atomic_plist(STATE_FILE, metadata, 0o644)
        metadata.update(enabled=not deny_only, phase="committed")
        atomic_plist(STATE_FILE, metadata, 0o644)
    except Exception as error:
        # Disable grants before attempting restoration. Never remove the loaded
        # plug-in while a system entry might still reference it.
        metadata.update(enabled=False, phase="rollback-needed", error=str(error))
        atomic_plist(STATE_FILE, metadata, 0o644)
        if primary_touched:
            write_verified(RIGHT, plistlib.loads(current_raw))
        metadata["phase"] = "entry-restored"
        atomic_plist(STATE_FILE, metadata, 0o644)
        # Preserve the journal and original backup for inspection/recovery.
        raise
    return status()


def uninstall():
    require_root("uninstall")
    checked_path(STATE_DIRECTORY)
    checked_path(INSTALLED_BUNDLE)
    if not ORIGINAL_RIGHT_FILE.is_file() or not STATE_FILE.is_file():
        raise RuntimeError("原始策略备份或安装记录缺失；停止卸载并要求人工恢复")
    checked_path(ORIGINAL_RIGHT_FILE)
    checked_path(STATE_FILE)
    metadata = plistlib.loads(STATE_FILE.read_bytes())
    original = plistlib.loads(ORIGINAL_RIGHT_FILE.read_bytes())
    current = plistlib.loads(authorization_read())
    if metadata.get("version") == 2:
        expected = metadata.get("actualPolicies", {}).get(RIGHT)
        intended = metadata.get("expectedPolicies", {}).get(RIGHT)
        if (not policy_matches(current, original)
                and not (expected and policy_matches(current, expected))
                and not (intended and policy_matches(current, intended))):
            raise RuntimeError("系统入口已有其他改动；保留备份，拒绝自动覆盖")
        # Check dependency conflicts before making any changes.
        for right in (REMOTE_RIGHT, STOCK_RIGHT):
            if right_exists(right):
                expected = metadata.get("actualPolicies", {}).get(right)
                intended = metadata.get("expectedPolicies", {}).get(right)
                actual = plistlib.loads(authorization_read(right))
                if not ((expected and policy_matches(actual, expected))
                        or (intended and policy_matches(actual, intended))):
                    raise RuntimeError("插件分支已有其他改动；拒绝自动移除: " + right)
    elif metadata.get("bundleIdentifier") not in (PLUGIN_IDENTIFIER, LEGACY_PLUGIN_IDENTIFIER):
        raise RuntimeError("安装记录身份不匹配")
    metadata["enabled"] = False
    atomic_plist(STATE_FILE, metadata, 0o644)
    write_verified(RIGHT, original)
    if metadata.get("version") == 2:
        for right in (REMOTE_RIGHT, STOCK_RIGHT):
            remove_branch(right)
    if INSTALLED_BUNDLE.exists():
        shutil.rmtree(INSTALLED_BUNDLE)
    # Delete recovery material only after original policy read-back succeeded.
    shutil.rmtree(STATE_DIRECTORY)
    return status()


def require_root_owned(path):
    checked_path(path)
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("插件恢复文件归属或权限不安全: " + str(path))


def maintenance_record():
    """Only maintain a committed, recognised policy; never replace its backup."""
    require_root("维护插件")
    for path in (STATE_DIRECTORY, STATE_FILE, ORIGINAL_RIGHT_FILE, INSTALLED_BUNDLE):
        require_root_owned(path)
    metadata = plistlib.loads(STATE_FILE.read_bytes())
    if (metadata.get("version") != 2 or metadata.get("phase") != "committed"
            or metadata.get("bundleIdentifier") not in (PLUGIN_IDENTIFIER, LEGACY_PLUGIN_IDENTIFIER)
            or metadata.get("teamIdentifier") != TEAM_IDENTIFIER
            or metadata.get("protocolVersion") != 3
            or metadata.get("policyTopology") != "direct-login-window-ui"):
        raise RuntimeError("安装记录需要恢复；请查看状态并使用恢复/卸载，不覆盖原始备份")
    original = plistlib.loads(ORIGINAL_RIGHT_FILE.read_bytes())
    expected = metadata.get("actualPolicies", {})
    intended = candidate_policy(plistlib.dumps(original))
    for right in (REMOTE_RIGHT, RIGHT):
        if (not expected.get(right) or not policy_matches(expected[right], intended[right])
                or not policy_matches(plistlib.loads(authorization_read(right)), expected[right])):
            raise RuntimeError("授权规则已有其他修改；拒绝维护: " + right)
    if right_exists(STOCK_RIGHT):
        raise RuntimeError("发现历史授权结构；请先恢复原规则")
    return metadata


def upgrade(bundle=None):
    metadata = maintenance_record()
    source = checked_path(bundle or SOURCE_BUNDLE)
    signature = verify_bundle(source)
    info = plistlib.loads((source / "Contents/Info.plist").read_bytes())
    if info.get("CUADenyOnly", False) is not metadata.get("denyOnly", False):
        raise RuntimeError("升级不能更改安装模式；请先恢复/卸载后再安装")
    staging = checked_path(STATE_DIRECTORY / "plugin-upgrade-next.bundle")
    previous = checked_path(STATE_DIRECTORY / "plugin-upgrade-previous.bundle")
    if staging.exists() or previous.exists():
        raise RuntimeError("存在未完成升级；请使用恢复/卸载")
    shutil.copytree(source, staging, symlinks=False)
    try:
        subprocess.run(["/usr/sbin/chown", "-R", "root:wheel", str(staging)], check=True)
        subprocess.run(["/bin/chmod", "-R", "a+rX,go-w", str(staging)], check=True)
        verify_bundle(staging)
        pending = dict(metadata, enabled=False, phase="plugin-upgrading")
        atomic_plist(STATE_FILE, pending, 0o644)
        INSTALLED_BUNDLE.rename(previous)
        staging.rename(INSTALLED_BUNDLE)
        verify_bundle(INSTALLED_BUNDLE)
        updated = dict(metadata, bundleIdentifier=PLUGIN_IDENTIFIER,
                       pluginVersion=info.get("CFBundleShortVersionString", ""),
                       pluginBuild=info.get("CFBundleVersion", ""),
                       upgradedAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       signature=[line for line in signature.splitlines()
                                  if line.startswith(("Authority=", "TeamIdentifier="))])
        atomic_plist(STATE_FILE, updated, 0o644)
    except Exception:
        if previous.exists():
            if INSTALLED_BUNDLE.exists():
                shutil.rmtree(INSTALLED_BUNDLE)
            previous.rename(INSTALLED_BUNDLE)
        atomic_plist(STATE_FILE, metadata, 0o644)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if previous.exists():
        shutil.rmtree(previous)
    return status()


def enable():
    metadata = maintenance_record()
    verify_bundle(INSTALLED_BUNDLE)
    if metadata.get("denyOnly") or metadata.get("experimentalOptIn") is not True:
        raise RuntimeError("该安装不支持启用自动锁屏授权")
    metadata["enabled"] = True
    atomic_plist(STATE_FILE, metadata, 0o644)
    return status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "build", "plan", "install", "upgrade", "enable", "uninstall"])
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--deny-only", action="store_true",
                        help="仅安装签名的拒绝自动解锁版本，用于验证原生锁屏")
    parser.add_argument("--experimental", action="store_true",
                        help="显式启用第二版实验安装；不代表已通过锁屏验证")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "status":
            result = status()
        elif args.action == "plan":
            if args.output is None:
                raise ValueError("plan 需要 --output 指定新的输出目录")
            result = export_candidate(args.output)
        elif args.action == "build":
            bundle = build_bundle()
            result = {"bundle": str(bundle), "signature": verify_bundle(bundle)}
        elif args.action == "install":
            result = install(args.bundle, experimental=args.experimental, deny_only=args.deny_only)
        elif args.action == "upgrade":
            result = upgrade(args.bundle)
        elif args.action == "enable":
            result = enable()
        else:
            result = uninstall()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as error:
        print("Authorization Plug-in 操作失败: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

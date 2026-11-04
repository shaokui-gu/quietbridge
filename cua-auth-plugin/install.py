#!/usr/bin/env python3
"""Build, install, inspect or remove the workbuddy-cua authorization plug-in.

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
PLUGIN_IDENTIFIER = "com.workbuddy.cua.AuthorizationPlugin"
TEAM_IDENTIFIER = "62RNP54C47"
PLUGIN_MECHANISM = "WorkBuddyCUAAuthorizationPlugin:allow,privileged"
RIGHT = "system.login.screensaver"


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


def transformed_right(source, include_plugin=True):
    raise RuntimeError(
        "安装已禁用：旧版将 use-login-window-ui 规则误当作 mechanism，"
        "可能移除系统密码认证路径。新授权分支尚未完成验证；"
        "默认入口不写系统；第二版实验安装需显式 --experimental。")


REMOTE_RIGHT = "com.workbuddy.cua.AuthorizationPlugin.remote"
STOCK_RIGHT = "com.workbuddy.cua.AuthorizationPlugin.stock"


def candidate_policy(source):
    """Generate reviewable files only. This does not install or prove UI fallback."""
    original = plistlib.loads(source)
    if (not isinstance(original, dict) or original.get("class") != "rule"
            or original.get("rule") not in (["use-login-window-ui"], "use-login-window-ui")
            or "mechanisms" in original):
        raise ValueError("只支持未定制的 use-login-window-ui 原始规则；拒绝猜测迁移")
    return {
        STOCK_RIGHT: original,
        REMOTE_RIGHT: {
            "class": "evaluate-mechanisms",
            "mechanisms": [PLUGIN_MECHANISM],
            "shared": False,
            "tries": 1,
        },
        RIGHT: {
            "class": "rule",
            "k-of-n": 1,
            "rule": [REMOTE_RIGHT, STOCK_RIGHT],
        },
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


def install(bundle=None, experimental=False):
    if not experimental:
        transformed_right(b"")  # legacy installation stays disabled
    require_root("install")
    source = checked_path(bundle or SOURCE_BUNDLE)
    signature = verify_bundle(source)
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
        for right in (STOCK_RIGHT, REMOTE_RIGHT, RIGHT):
            if right == RIGHT:
                # Refuse intervening policy edits before touching the entry point.
                if plistlib.loads(authorization_read()) != plistlib.loads(current_raw):
                    raise RuntimeError("系统授权规则已被其他进程修改；停止安装")
                primary_touched = True
            actual[right] = write_verified(right, plan[right])
            metadata["phase"] = "wrote:" + right
            metadata["actualPolicies"] = actual
            atomic_plist(STATE_FILE, metadata, 0o644)
        metadata.update(enabled=True, phase="committed")
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
    elif metadata.get("bundleIdentifier") != PLUGIN_IDENTIFIER:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "build", "plan", "install", "uninstall"])
    parser.add_argument("--bundle", type=Path)
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
            result = install(args.bundle, experimental=args.experimental)
        else:
            result = uninstall()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as error:
        print("Authorization Plug-in 操作失败: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

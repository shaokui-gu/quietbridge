#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Swap the installed plug-in for the locally built one and move the remote
branch back to SecurityAgent's user-level host.

The entry rule (system.login.screensaver) is never rewritten here. It keeps
pointing at the same two branches for the whole upgrade, so the password path
stays intact even if this script dies halfway. Only two things move:

  1. /Library/Security/SecurityAgentPlugins/<bundle>  (atomic directory swap)
  2. mechanisms of the remote branch                  (allow,privileged -> allow)

Any failure restores both the previous bundle and the previous mechanism.
Backups are kept, never deleted.
"""
import argparse
import copy
import datetime
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import uuid

import install

BUNDLE_NAME = install.BUNDLE_NAME


def bundle_version(bundle):
    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    return [info.get("CFBundleShortVersionString"), info.get("CFBundleVersion")]


def binary_digest(bundle):
    info = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
    executable = info.get('CFBundleExecutable')
    # Existing installed bundles still carry the pre-rename executable basename.
    if executable not in ('QuietBridgeAuthorizationPlugin', 'WorkBuddyCUAAuthorizationPlugin'):
        raise RuntimeError('插件可执行文件标识无法识别')
    path = bundle / "Contents" / "MacOS" / executable
    if not path.is_file():
        raise RuntimeError("插件可执行文件缺失: " + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_state():
    if not install.STATE_FILE.is_file():
        raise RuntimeError("未找到安装记录；这台机器上没有本插件的安装")
    return plistlib.loads(install.STATE_FILE.read_bytes())


def read_policies(state):
    actual = {}
    for right in (install.RIGHT, install.STOCK_RIGHT, install.REMOTE_RIGHT):
        policy = plistlib.loads(install.authorization_read(right))
        recorded = state.get("actualPolicies", {}).get(right)
        if not recorded or not install.policy_matches(policy, recorded):
            raise RuntimeError("授权规则与安装记录不一致，拒绝升级: " + right)
        actual[right] = policy
    return actual


def verify_preconditions(state, actual):
    if state.get("phase") != "committed" or state.get("protocolVersion") != 3:
        raise RuntimeError("安装记录不是已提交状态: %r" % state.get("phase"))
    if not install.INSTALLED_BUNDLE.is_dir():
        raise RuntimeError("系统内没有已安装的插件")
    mechanisms = actual[install.REMOTE_RIGHT].get("mechanisms")
    if mechanisms not in ([install.LEGACY_PRIVILEGED_MECHANISM], [install.PLUGIN_MECHANISM]):
        raise RuntimeError("remote 分支机制不是已知值: %r" % (mechanisms,))
    if not install.SOURCE_BUNDLE.is_dir():
        raise RuntimeError("还没有构建产物；先执行 make clean all")
    install.verify_bundle(install.SOURCE_BUNDLE)


def report(state, actual):
    return {
        "entryRule": actual[install.RIGHT].get("rule"),
        "remoteMechanisms": actual[install.REMOTE_RIGHT].get("mechanisms"),
        "installedBundleVersion": bundle_version(install.INSTALLED_BUNDLE)
        if install.INSTALLED_BUNDLE.is_dir() else None,
        "builtBundleVersion": bundle_version(install.SOURCE_BUNDLE)
        if install.SOURCE_BUNDLE.is_dir() else None,
    }


def upgrade():
    install.require_root("upgrade")
    install.checked_path(install.STATE_DIRECTORY)
    install.checked_path(install.INSTALLED_BUNDLE)
    state = load_state()
    actual = read_policies(state)
    verify_preconditions(state, actual)

    same_binary = binary_digest(install.SOURCE_BUNDLE) == binary_digest(install.INSTALLED_BUNDLE)
    already_migrated = actual[install.REMOTE_RIGHT].get("mechanisms") == [install.PLUGIN_MECHANISM]
    if same_binary and already_migrated:
        return {"changed": False, "reason": "已是最新产物与非特权机制", **report(state, actual)}

    backup_material = install.STATE_DIRECTORY / ("before-upgrade-" + uuid.uuid4().hex + ".plist")
    install.atomic_plist(backup_material, {"state": state, "policies": actual}, 0o600)

    staging = install.INSTALLED_BUNDLE.with_name("." + BUNDLE_NAME + ".upgrade")
    previous = install.INSTALLED_BUNDLE.with_name(BUNDLE_NAME + ".previous-" + uuid.uuid4().hex)
    if staging.exists():
        raise RuntimeError("存在未完成的暂存目录；先人工检查: " + str(staging))

    shutil.copytree(install.SOURCE_BUNDLE, staging, symlinks=False)
    subprocess.run(["/usr/sbin/chown", "-R", "root:wheel", str(staging)], check=True)
    subprocess.run(["/bin/chmod", "-R", "go-w", str(staging)], check=True)
    install.verify_bundle(staging)

    swapped = False
    policy_rewritten = False
    old_policy = actual[install.REMOTE_RIGHT]
    try:
        install.INSTALLED_BUNDLE.rename(previous)
        staging.rename(install.INSTALLED_BUNDLE)
        swapped = True
        install.verify_bundle(install.INSTALLED_BUNDLE)

        desired = dict(state["expectedPolicies"][install.REMOTE_RIGHT])
        desired["mechanisms"] = [install.PLUGIN_MECHANISM]
        new_policy = install.write_verified(install.REMOTE_RIGHT, desired)
        policy_rewritten = True

        for right in (install.RIGHT, install.STOCK_RIGHT):
            current = plistlib.loads(install.authorization_read(right))
            if not install.policy_matches(current, actual[right]):
                raise RuntimeError("升级过程中入口规则被外部修改: " + right)
    except Exception:
        if policy_rewritten:
            install.write_verified(install.REMOTE_RIGHT, old_policy)
        if swapped:
            failed = install.INSTALLED_BUNDLE.with_name(
                BUNDLE_NAME + ".failed-" + uuid.uuid4().hex)
            install.INSTALLED_BUNDLE.rename(failed)
            previous.rename(install.INSTALLED_BUNDLE)
            print("已回滚插件；失败产物保留在: " + str(failed), file=sys.stderr)
        elif staging.exists():
            print("暂存目录保留在: " + str(staging), file=sys.stderr)
        raise

    updated = copy.deepcopy(state)
    updated["bundleIdentifier"] = install.PLUGIN_IDENTIFIER
    updated["actualPolicies"][install.REMOTE_RIGHT] = new_policy
    updated["expectedPolicies"][install.REMOTE_RIGHT]["mechanisms"] = [install.PLUGIN_MECHANISM]
    updated["mechanism"] = install.PLUGIN_MECHANISM
    updated["bundleVersion"] = bundle_version(install.INSTALLED_BUNDLE)
    updated["upgradedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    updated["upgradeBackup"] = str(backup_material)
    updated["previousBundle"] = str(previous)
    install.atomic_plist(install.STATE_FILE, updated, 0o644)

    return {
        "changed": True,
        "entryRuleUnchanged": actual[install.RIGHT].get("rule"),
        "mechanisms": new_policy.get("mechanisms"),
        "installedBundleVersion": bundle_version(install.INSTALLED_BUNDLE),
        "previousBundle": str(previous),
        "ledgerBackup": str(backup_material),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="只读核对前置条件，不改动任何内容（无需 root）")
    args = parser.parse_args()
    try:
        state = load_state()
        actual = read_policies(state)
        verify_preconditions(state, actual)
        if args.check:
            result = {"ready": True, **report(state, actual),
                      "builtBinaryMatchesInstalled":
                          binary_digest(install.SOURCE_BUNDLE) ==
                          binary_digest(install.INSTALLED_BUNDLE)}
        else:
            result = upgrade()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as error:
        print("插件升级失败: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

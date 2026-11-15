#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Package an already-built release executable; never install it on this Mac."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'services/mcp')]
from release_layout import copy_bridge
from compatibility import MENU_IDENTIFIER, HELPER_IDENTIFIER, PLUGIN_IDENTIFIER
from signing_profiles import load_profile
PRODUCT = json.loads((ROOT / 'project.json').read_text())
IDENTIFIER = MENU_IDENTIFIER
LAUNCH_IDENTIFIER = IDENTIFIER + ".launch"
APPLICATION_PATH = "Applications/QuietBridge.app"


def run(*args):
    subprocess.run(list(map(str, args)), check=True)


def build_app_icon(resources):
    source = Path(__file__).with_name("assets") / "AppIcon.icns"
    shutil.copy2(source, resources / "AppIcon.icns")


def normalize_tree_modes(root):
    """Make packaged files readable by the desktop user.

    Files created under a restrictive umask must not stay 0600 inside the
    installed app and support payload: PackageKit preserves source modes, the
    postinstall deploy runs as the desktop user, and codesign verification as
    that user fails on unreadable bridge files. Directory and file modes are
    normalized while executable files keep their execute bits.
    """
    root = Path(root)
    for path in sorted(root.rglob('*'), reverse=True):
        if path.is_symlink():
            raise SystemExit("Package payload contains a symlink: " + str(path))
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            executable = bool(path.stat().st_mode & 0o111)
            path.chmod(0o755 if executable else 0o644)
        else:
            raise SystemExit("Package payload contains a non-regular entry: " + str(path))


def write_distribution(destination, version):
    document = ET.Element("installer-gui-script", minSpecVersion="2")
    ET.SubElement(document, "title").text = "QuietBridge"
    ET.SubElement(document, "options", customize="always", **{
        "require-scripts": "false", "hostArchitectures": "arm64",
    })
    ET.SubElement(document, "domains", enable_anywhere="false",
                  enable_currentUserHome="false", enable_localSystem="true")
    ET.SubElement(ET.SubElement(document, "allowed-os-versions"), "os-version", min="14.0")
    outline = ET.SubElement(document, "choices-outline")
    ET.SubElement(outline, "line", choice="application")
    ET.SubElement(outline, "line", choice="launch")
    application = ET.SubElement(document, "choice", id="application", title="QuietBridge", **{
        "description": "安装应用、原生宿主与浏览器扩展文件。浏览器扩展可稍后在应用面板内加载。",
        "start_selected": "true", "start_enabled": "false",
    })
    ET.SubElement(application, "pkg-ref", id=IDENTIFIER)
    launch = ET.SubElement(document, "choice", id="launch", title="安装完成后立即运行", **{
        "description": "安装完成后打开 QuietBridge 并显示菜单栏图标。未勾选时，可稍后从应用程序文件夹打开。",
        "start_selected": "false",
    })
    ET.SubElement(launch, "pkg-ref", id=LAUNCH_IDENTIFIER)
    # Package order follows the choices: deploy the application before running
    # the optional scripts-only launch component.
    for identifier, name in ((IDENTIFIER, "QuietBridge.pkg"), (LAUNCH_IDENTIFIER, "LaunchQuietBridge.pkg")):
        ET.SubElement(document, "pkg-ref", id=identifier, version=version, onConclusion="none").text = name
    ET.ElementTree(document).write(destination, encoding="utf-8", xml_declaration=True)


def remove_appledouble(package, work, destination):
    expanded = work / 'expanded-package'
    run('/usr/sbin/pkgutil', '--expand', package, expanded)
    payload = expanded / 'Payload'
    raw = gzip.decompress(payload.read_bytes())
    position = 0
    clean = bytearray()
    count = 0
    while position < len(raw):
        header = raw[position:position + 76]
        if len(header) != 76 or header[:6] != b'070707':
            raise ValueError('Unsupported package payload format')
        name_size = int(header[59:65], 8)
        file_size = int(header[65:76], 8)
        end = position + 76 + name_size + file_size
        if name_size < 1 or end > len(raw):
            raise ValueError('Truncated package payload')
        name = raw[position + 76:position + 76 + name_size - 1].decode('utf-8')
        if not any(part.startswith('._') or part == '__pycache__' for part in name.split('/')):
            clean.extend(raw[position:end])
            if name not in ('.', 'TRAILER!!!'):
                count += 1
        position = end
        if name == 'TRAILER!!!':
            break
    else:
        raise ValueError('Package payload trailer missing')
    payload.write_bytes(gzip.compress(clean, mtime=0))
    bom = expanded / 'Bom'
    listing = subprocess.run(['/usr/bin/lsbom', str(bom)], check=True, capture_output=True, text=True).stdout
    lines = [line for line in listing.splitlines() if not any(
        part.startswith('._') or part == '__pycache__' for part in line.split('\t', 1)[0].split('/'))]
    filelist = work / 'clean-bom.txt'
    filelist.write_text('\n'.join(lines) + '\n')
    clean_bom = work / 'clean.bom'
    run('/usr/bin/mkbom', '-i', filelist, clean_bom)
    os.replace(clean_bom, bom)
    info = expanded / 'PackageInfo'
    tree = ET.parse(info)
    tree.getroot().find('payload').set('numberOfFiles', str(count))
    tree.write(info, encoding='utf-8', xml_declaration=True)
    for metadata in (expanded / 'Scripts').glob('._*'):
        metadata.unlink()
    run('/usr/sbin/pkgutil', '--flatten', expanded, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--profiles-dir", type=Path,
                        help="Directory containing matching QuietBridge profiles and signing certificate")
    args = parser.parse_args()
    profiles = {}
    if args.profiles_dir is not None:
        certificate = (args.profiles_dir / 'developerID_application.cer').read_bytes()
        for name, identifier in (('QuietBridgeApp', MENU_IDENTIFIER), ('QuietBridgeHelper', HELPER_IDENTIFIER),
                                 ('QuietBridgeAuthp', PLUGIN_IDENTIFIER)):
            profiles[identifier] = load_profile(args.profiles_dir / (name + '.provisionprofile'),
                                                identifier, '62RNP54C47', certificate)
    release = ROOT / "apps/macos/.build/release/quietbridge-native"
    if not release.is_file():
        raise SystemExit("Build the Swift release executable first")
    work = ROOT / "dist" / ("package-" + args.version)
    work.mkdir(parents=True, exist_ok=False)
    payload = work / "root"
    app = payload / APPLICATION_PATH
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    shutil.copy2(release, contents / "MacOS/quietbridge-native")
    info = {
        "CFBundleDisplayName": PRODUCT["displayName"], "CFBundleName": PRODUCT["displayName"],
        "CFBundleIconFile": "AppIcon",
        "CFBundleIdentifier": IDENTIFIER, "CFBundleExecutable": "quietbridge-native",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": args.version,
        "CFBundleVersion": args.build, "LSMinimumSystemVersion": "14.0",
        "LSUIElement": True, "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "quietbridge local macOS helper",
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    if MENU_IDENTIFIER in profiles:
        (contents / 'embedded.provisionprofile').write_bytes(profiles[MENU_IDENTIFIER])
    resources = contents / "Resources"
    resources.mkdir()
    build_app_icon(resources)
    localized = resources / "en.lproj"
    localized.mkdir()
    (localized / "InfoPlist.strings").write_bytes(plistlib.dumps({
        "CFBundleName": "QuietBridge", "CFBundleDisplayName": "QuietBridge",
    }))
    bridge = resources / "bridge"
    bridge.mkdir()
    copy_bridge(ROOT, bridge)
    support = payload / "Library/Application Support/QuietBridge"
    support.mkdir(parents=True)
    shutil.copy2(ROOT / 'services/mcp/compatibility.py', support / 'compatibility.py')
    shutil.copytree(ROOT / 'adapters', support / 'adapters', ignore=shutil.ignore_patterns('__pycache__', 'tests', 'test', '*.pyc'))
    # Same executable, separate signing identities. The authorization plug-in
    # trusts only the helper; the menu app must never become its server.
    helper_stage = work / "helper/QuietBridge.app"
    (helper_stage / "Contents/MacOS").mkdir(parents=True)
    shutil.copy2(release, helper_stage / "Contents/MacOS/quietbridge-native")
    helper_info = dict(info, CFBundleIdentifier=HELPER_IDENTIFIER,
                       CFBundleName=PRODUCT["displayName"], CFBundleDisplayName=PRODUCT["displayName"])
    helper_info.pop("CFBundleIconFile")
    (helper_stage / "Contents/Info.plist").write_bytes(plistlib.dumps(helper_info))
    if HELPER_IDENTIFIER in profiles:
        (helper_stage / 'Contents/embedded.provisionprofile').write_bytes(profiles[HELPER_IDENTIFIER])
    run("/usr/bin/codesign", "--force", "--options", "runtime", "--timestamp=none",
        "--sign", args.identity, helper_stage)
    run("/usr/bin/codesign", "--verify", "--strict", "-R",
        '=anchor apple generic and identifier "' + HELPER_IDENTIFIER + '" and certificate leaf[subject.OU] = "62RNP54C47"', helper_stage)
    helper_digest = hashlib.sha256((helper_stage / "Contents/MacOS/quietbridge-native").read_bytes()).hexdigest()[:16]
    helper_relative = Path("Library/Application Support/WorkBuddyCUAHelper/releases") / helper_digest / "QuietBridge.app"
    shutil.copytree(helper_stage, payload / helper_relative)
    (support / "installation.plist").write_bytes(plistlib.dumps({
        "version": args.version, "build": args.build,
        "helper": str(Path("/") / helper_relative / "Contents/MacOS/quietbridge-native"),
    }))
    for name in ("deploy.py", "runtime.sh", "恢复安装.command"):
        shutil.copy2(Path(__file__).with_name(name), support / name)
    plugin_source = ROOT / "system/authorization-plugin/build/WorkBuddyCUAAuthorizationPlugin.bundle"
    if not plugin_source.is_dir():
        raise SystemExit("Build and sign the authorization plug-in first")
    if PLUGIN_IDENTIFIER in profiles:
        embedded = plugin_source / 'Contents/embedded.provisionprofile'
        if not embedded.is_file() or embedded.read_bytes() != profiles[PLUGIN_IDENTIFIER]:
            raise SystemExit('Rebuild the authorization plug-in with the matching supplied provisioning profile')
    plugin_info = plistlib.loads((plugin_source / "Contents/Info.plist").read_bytes())
    if (plugin_info.get("CFBundleExecutable") != "QuietBridgeAuthorizationPlugin" or
            plugin_info.get("CUADenyOnly") is not False or
            plugin_info.get("CUAServerCodeIdentifier") != HELPER_IDENTIFIER or
            plugin_info.get("CFBundleIdentifier") != PLUGIN_IDENTIFIER or
            plugin_info.get("CUAServerTeamIdentifier") != "62RNP54C47"):
        raise SystemExit("The plug-in must use the production helper identity and grant mode")
    run("/usr/bin/codesign", "--verify", "--strict", "-R",
        '=anchor apple generic and identifier "' + PLUGIN_IDENTIFIER + '" and certificate leaf[subject.OU] = "62RNP54C47"', plugin_source)
    plugin = support / "plugin"
    (plugin / "build").mkdir(parents=True)
    shutil.copytree(plugin_source, plugin / "build" / plugin_source.name)
    shutil.copy2(ROOT / "system/authorization-plugin/install.py", plugin / "install.py")
    shutil.copy2(Path(__file__).with_name("manage-plugin.command"), plugin / "manage-plugin.command")
    extension = support / "extension"
    extension.mkdir(parents=True)
    extension_stage = extension / "extension-stage"
    shutil.copytree(ROOT / "extensions/chromium", extension_stage,
                    ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "._*", "*.test.mjs",
                                                  "native-host-config.json", "native-host-launcher.sh"))
    for name in ("native_host.py", "install_native_host.py"):
        shutil.copy2(extension_stage / name, extension / name)
    shutil.copy2(ROOT / "services/mcp/browser_events.py", extension / "browser_events.py")
    shutil.copy2(ROOT / "services/mcp/browser_events.py", extension_stage / "browser_events.py")
    for destination in (extension, extension_stage):
        shutil.copy2(ROOT / 'services/mcp/compatibility.py', destination / 'compatibility.py')
        for module in ('session_registry.py', 'lifecycle_store.py', 'browser_preview.py'):
            shutil.copy2(ROOT / 'services/mcp' / module, destination / module)
    extension_version = json.loads((extension_stage / "manifest.json").read_text())["version"]
    for name in ("popup.html", "popup.css", "popup.js", "service-worker.js", "content.js", "installer/index.html"):
        if not (extension_stage / name).is_file():
            raise SystemExit("Missing extension component: " + name)
    with zipfile.ZipFile(extension / ("QuietBridgeBrowser-" + extension_version + ".zip"), "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(extension_stage.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(extension_stage))
    scripts = work / "scripts"
    scripts.mkdir()
    for name in ("preinstall", "postinstall", "deploy.py"):
        shutil.copy2(Path(__file__).with_name(name), scripts / name)
    (scripts / 'upgrade.plist').write_bytes(plistlib.dumps({'version': args.version, 'build': args.build}))
    shutil.copy2(ROOT / 'services/mcp/compatibility.py', scripts / 'compatibility.py')
    shutil.copytree(ROOT / 'adapters', scripts / 'adapters', ignore=shutil.ignore_patterns('__pycache__', 'tests', 'test', '*.pyc'))
    for name in ("preinstall", "postinstall", "launch-postinstall", "runtime.sh", "manage-plugin.command", "恢复安装.command"):
        run("/bin/bash", "-n", Path(__file__).with_name(name))
    # Payload modes come from the working tree; normalize before sealing so the
    # installed bridge stays readable by the desktop user.
    normalize_tree_modes(payload)
    normalize_tree_modes(scripts)
    run("/usr/bin/codesign", "--force", "--options", "runtime", "--timestamp=none",
        "--sign", args.identity, app)
    run("/usr/bin/codesign", "--verify", "--strict", app)
    components = [{
        "RootRelativeBundlePath": APPLICATION_PATH,
        "BundleIsRelocatable": False, "BundleIsVersionChecked": True,
        # Preinstall verifies either recognised signed identity at this exact
        # non-relocatable path. Allow PackageKit to cross the old/new ID boundary.
        "BundleHasStrictIdentifier": False, "BundleOverwriteAction": "upgrade",
    }, {
        "RootRelativeBundlePath": str(helper_relative),
        "BundleIsRelocatable": False, "BundleIsVersionChecked": False,
        "BundleHasStrictIdentifier": True, "BundleOverwriteAction": "upgrade",
    }]
    component_file = work / "components.plist"
    component_file.write_bytes(plistlib.dumps(components))
    package = ROOT / "dist" / ("quietbridge-" + args.version + ".pkg")
    if package.exists():
        raise SystemExit("Refusing to overwrite " + str(package))
    # Suppress AppleDouble metadata, which Chrome rejects in extension folders.
    os.environ["COPYFILE_DISABLE"] = "1"
    # pkgbuild also materialises extended attributes as AppleDouble BOM entries.
    # COPYFILE_DISABLE alone does not remove com.apple.provenance copied from
    # workspace files. Clear staging metadata after signing, before archiving.
    run("/usr/bin/xattr", "-cr", payload, scripts)
    raw_package = work / 'raw.pkg'
    run("/usr/bin/pkgbuild", "--root", payload, "--scripts", scripts, "--ownership", "recommended",
        "--component-plist", component_file, "--identifier", IDENTIFIER,
        "--version", args.version, "--install-location", "/", raw_package)
    application_package = work / "QuietBridge.pkg"
    remove_appledouble(raw_package, work, application_package)
    listed = subprocess.run(["/usr/sbin/pkgutil", "--payload-files", str(application_package)],
                            check=True, capture_output=True, text=True).stdout.splitlines()
    if any(Path(path).name.startswith("._") or "__pycache__" in Path(path).parts for path in listed):
        raise SystemExit("Package contains unsupported metadata files; do not distribute")
    launch_scripts = work / "launch-scripts"
    launch_scripts.mkdir()
    shutil.copy2(Path(__file__).with_name("launch-postinstall"), launch_scripts / "postinstall")
    (launch_scripts / "postinstall").chmod(0o755)
    run("/usr/bin/xattr", "-cr", launch_scripts)
    run("/usr/bin/pkgbuild", "--nopayload", "--scripts", launch_scripts,
        "--identifier", LAUNCH_IDENTIFIER, "--version", args.version,
        "--install-location", "/", work / "LaunchQuietBridge.pkg")
    distribution = work / "Distribution.xml"
    write_distribution(distribution, args.version)
    run("/usr/bin/productbuild", "--distribution", distribution, "--package-path", work,
        "--identifier", IDENTIFIER, "--version", args.version, package)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    (package.with_suffix(".pkg.sha256")).write_text(digest + "  " + package.name + "\n")
    print(json.dumps({"package": str(package), "app": str(app), "version": args.version,
                      "build": args.build, "sha256": digest}, indent=2))


if __name__ == "__main__":
    main()

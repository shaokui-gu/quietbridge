#!/usr/bin/env python3
"""Package an already-built release executable; never install it on this Mac."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
IDENTIFIER = "com.workbuddy.cua.menubar"
LAUNCH_IDENTIFIER = IDENTIFIER + ".launch"
APPLICATION_PATH = "Applications/Workbuddy CUA.app"


def run(*args):
    subprocess.run(list(map(str, args)), check=True)


def build_app_icon(resources):
    source = Path(__file__).with_name("assets") / "AppIcon.icns"
    shutil.copy2(source, resources / "AppIcon.icns")


def write_distribution(destination, version):
    document = ET.Element("installer-gui-script", minSpecVersion="2")
    ET.SubElement(document, "title").text = "Workbuddy CUA"
    ET.SubElement(document, "options", customize="always", **{
        "require-scripts": "false", "hostArchitectures": "arm64",
    })
    ET.SubElement(document, "domains", enable_anywhere="false",
                  enable_currentUserHome="false", enable_localSystem="true")
    ET.SubElement(ET.SubElement(document, "allowed-os-versions"), "os-version", min="14.0")
    outline = ET.SubElement(document, "choices-outline")
    ET.SubElement(outline, "line", choice="application")
    ET.SubElement(outline, "line", choice="launch")
    application = ET.SubElement(document, "choice", id="application", title="Workbuddy CUA", **{
        "description": "安装应用、原生宿主与浏览器扩展文件。浏览器扩展可稍后在应用面板内加载。",
        "start_selected": "true", "start_enabled": "false",
    })
    ET.SubElement(application, "pkg-ref", id=IDENTIFIER)
    launch = ET.SubElement(document, "choice", id="launch", title="安装完成后立即运行", **{
        "description": "安装完成后打开 Workbuddy CUA 并显示菜单栏图标。未勾选时，可稍后从应用程序文件夹打开。",
        "start_selected": "false",
    })
    ET.SubElement(launch, "pkg-ref", id=LAUNCH_IDENTIFIER)
    # Package order follows the choices: deploy the application before running
    # the optional scripts-only launch component.
    for identifier, name in ((IDENTIFIER, "WorkbuddyCUA.pkg"), (LAUNCH_IDENTIFIER, "LaunchWorkbuddyCUA.pkg")):
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
    args = parser.parse_args()
    release = ROOT / "cua-helper-build/.build/release/workbuddy-cua-native"
    if not release.is_file():
        raise SystemExit("Build the Swift release executable first")
    work = ROOT / "dist" / ("package-" + args.version)
    work.mkdir(parents=True, exist_ok=False)
    payload = work / "root"
    app = payload / APPLICATION_PATH
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    shutil.copy2(release, contents / "MacOS/workbuddy-cua-native")
    info = {
        "CFBundleDisplayName": "Workbuddy CUA", "CFBundleName": "Workbuddy CUA",
        "CFBundleIconFile": "AppIcon",
        "CFBundleIdentifier": IDENTIFIER, "CFBundleExecutable": "workbuddy-cua-native",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": args.version,
        "CFBundleVersion": args.build, "LSMinimumSystemVersion": "14.0",
        "LSUIElement": True, "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "workbuddy-cua local macOS helper",
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    resources = contents / "Resources"
    resources.mkdir()
    build_app_icon(resources)
    localized = resources / "en.lproj"
    localized.mkdir()
    (localized / "InfoPlist.strings").write_bytes(plistlib.dumps({
        "CFBundleName": "Workbuddy CUA", "CFBundleDisplayName": "Workbuddy CUA",
    }))
    bridge = resources / "bridge"
    bridge.mkdir()
    for name in ("server.py", "browser_events.py", "recovery.py", "connector.py", "task_lifecycle.py", "stdio_transport.py", "host_lifecycle.py"):
        shutil.copy2(ROOT / "cua-mcp-bridge" / name, bridge / name)
    shutil.copytree(ROOT / 'cua-mcp-bridge/assets', bridge / 'assets')
    support = payload / "Library/Application Support/workbuddy-cua"
    support.mkdir(parents=True)
    # Same executable, separate signing identities. The authorization plug-in
    # trusts only the helper; the menu app must never become its server.
    helper_stage = work / "helper/workbuddy-cua.app"
    (helper_stage / "Contents/MacOS").mkdir(parents=True)
    shutil.copy2(release, helper_stage / "Contents/MacOS/workbuddy-cua-native")
    helper_info = dict(info, CFBundleIdentifier="com.workbuddy.cua.helper",
                       CFBundleName="workbuddy-cua", CFBundleDisplayName="workbuddy-cua")
    helper_info.pop("CFBundleIconFile")
    (helper_stage / "Contents/Info.plist").write_bytes(plistlib.dumps(helper_info))
    run("/usr/bin/codesign", "--force", "--options", "runtime", "--timestamp=none",
        "--sign", args.identity, helper_stage)
    run("/usr/bin/codesign", "--verify", "--strict", "-R",
        '=anchor apple generic and identifier "com.workbuddy.cua.helper" and certificate leaf[subject.OU] = "62RNP54C47"', helper_stage)
    helper_digest = hashlib.sha256((helper_stage / "Contents/MacOS/workbuddy-cua-native").read_bytes()).hexdigest()[:16]
    helper_relative = Path("Library/Application Support/WorkBuddyCUAHelper/releases") / helper_digest / "workbuddy-cua.app"
    shutil.copytree(helper_stage, payload / helper_relative)
    (support / "installation.plist").write_bytes(plistlib.dumps({
        "version": args.version, "build": args.build,
        "helper": str(Path("/") / helper_relative / "Contents/MacOS/workbuddy-cua-native"),
    }))
    for name in ("deploy.py", "runtime.sh", "恢复安装.command"):
        shutil.copy2(Path(__file__).with_name(name), support / name)
    plugin_source = ROOT / "cua-auth-plugin/build/WorkBuddyCUAAuthorizationPlugin.bundle"
    if not plugin_source.is_dir():
        raise SystemExit("Build and sign the authorization plug-in first")
    plugin_info = plistlib.loads((plugin_source / "Contents/Info.plist").read_bytes())
    if (plugin_info.get("CUADenyOnly") is not False or
            plugin_info.get("CUAServerCodeIdentifier") != "com.workbuddy.cua.helper" or
            plugin_info.get("CUAServerTeamIdentifier") != "62RNP54C47"):
        raise SystemExit("The plug-in must use the production helper identity and grant mode")
    run("/usr/bin/codesign", "--verify", "--strict", "-R",
        '=anchor apple generic and identifier "com.workbuddy.cua.AuthorizationPlugin" and certificate leaf[subject.OU] = "62RNP54C47"', plugin_source)
    plugin = support / "plugin"
    (plugin / "build").mkdir(parents=True)
    shutil.copytree(plugin_source, plugin / "build" / plugin_source.name)
    shutil.copy2(ROOT / "cua-auth-plugin/install.py", plugin / "install.py")
    shutil.copy2(Path(__file__).with_name("manage-plugin.command"), plugin / "manage-plugin.command")
    extension = support / "extension"
    extension.mkdir(parents=True)
    extension_stage = extension / "extension-stage"
    shutil.copytree(ROOT / "browser-observer-extension", extension_stage,
                    ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "._*", "*.test.mjs",
                                                  "native-host-config.json", "native-host-launcher.sh"))
    for name in ("native_host.py", "install_native_host.py"):
        shutil.copy2(extension_stage / name, extension / name)
    shutil.copy2(ROOT / "cua-mcp-bridge/browser_events.py", extension / "browser_events.py")
    shutil.copy2(ROOT / "cua-mcp-bridge/browser_events.py", extension_stage / "browser_events.py")
    extension_version = json.loads((extension_stage / "manifest.json").read_text())["version"]
    for name in ("popup.html", "popup.css", "popup.js", "service-worker.js", "content.js", "installer/index.html"):
        if not (extension_stage / name).is_file():
            raise SystemExit("Missing extension component: " + name)
    with zipfile.ZipFile(extension / ("WorkbuddyExtension-" + extension_version + ".zip"), "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(extension_stage.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(extension_stage))
    scripts = work / "scripts"
    scripts.mkdir()
    for name in ("preinstall", "postinstall", "deploy.py"):
        shutil.copy2(Path(__file__).with_name(name), scripts / name)
    for name in ("preinstall", "postinstall", "launch-postinstall", "runtime.sh", "manage-plugin.command", "恢复安装.command"):
        run("/bin/bash", "-n", Path(__file__).with_name(name))
    run("/usr/bin/codesign", "--force", "--options", "runtime", "--timestamp=none",
        "--sign", args.identity, app)
    run("/usr/bin/codesign", "--verify", "--strict", app)
    components = [{
        "RootRelativeBundlePath": APPLICATION_PATH,
        "BundleIsRelocatable": False, "BundleIsVersionChecked": True,
        "BundleHasStrictIdentifier": True, "BundleOverwriteAction": "upgrade",
    }, {
        "RootRelativeBundlePath": str(helper_relative),
        "BundleIsRelocatable": False, "BundleIsVersionChecked": False,
        "BundleHasStrictIdentifier": True, "BundleOverwriteAction": "upgrade",
    }]
    component_file = work / "components.plist"
    component_file.write_bytes(plistlib.dumps(components))
    package = ROOT / "dist" / ("workbuddy-cua-" + args.version + ".pkg")
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
    application_package = work / "WorkbuddyCUA.pkg"
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
        "--install-location", "/", work / "LaunchWorkbuddyCUA.pkg")
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
